from __future__ import annotations

import json
import math
import os
import random
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch.utils.data import DataLoader

from .dataset import RandomWindowDataset, make_context_mask
from .losses import jepa_loss, representation_diagnostics
from .model import TemporalJEPA, model_parameter_count


def seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def ema_momentum(step: int, total: int, start: float, end: float) -> float:
    progress = min(max(step / max(total - 1, 1), 0.0), 1.0)
    return end - (end - start) * (math.cos(math.pi * progress) + 1.0) / 2.0


def learning_rate(step: int, total: int, warmup: int, peak: float) -> float:
    if step < warmup:
        return peak * (step + 1) / max(warmup, 1)
    progress = (step - warmup) / max(total - warmup, 1)
    return peak * 0.5 * (1.0 + math.cos(math.pi * min(progress, 1.0)))


def save_checkpoint(path: Path, model: TemporalJEPA, optimizer: torch.optim.Optimizer,
                    scaler: torch.amp.GradScaler, step: int, sample_cursor: int,
                    config: dict[str, Any], manifest: dict[str, Any], history: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    payload = {
        "model": model.state_dict(),
        "optimizer": optimizer.state_dict(),
        "scaler": scaler.state_dict(),
        "step": step,
        "sampleCursor": sample_cursor,
        "config": config,
        "manifest": manifest,
        "history": history,
        "pythonRng": random.getstate(),
        "numpyRng": np.random.get_state(),
        "torchRng": torch.get_rng_state(),
        "cudaRng": torch.cuda.get_rng_state_all(),
    }
    try:
        torch.save(payload, temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_checkpoint(path: Path, model: TemporalJEPA, optimizer: torch.optim.Optimizer | None = None,
                    scaler: torch.amp.GradScaler | None = None,
                    expected_manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    checkpoint = torch.load(path, map_location="cpu", weights_only=False)
    if expected_manifest is not None and checkpoint.get("manifest") != expected_manifest:
        raise ValueError("checkpoint manifest does not match current config, data, or preprocessing")
    model.load_state_dict(checkpoint["model"])
    if optimizer is not None:
        optimizer.load_state_dict(checkpoint["optimizer"])
    if scaler is not None:
        scaler.load_state_dict(checkpoint["scaler"])
    return checkpoint


def restore_rng(checkpoint: dict[str, Any]) -> None:
    random.setstate(checkpoint["pythonRng"])
    np.random.set_state(checkpoint["numpyRng"])
    torch.set_rng_state(checkpoint["torchRng"])
    torch.cuda.set_rng_state_all(checkpoint["cudaRng"])


def _loss_kwargs(training: dict[str, Any] | None) -> dict[str, float]:
    if training is None:
        return {}
    return {
        "variance_weight": float(training["variance_weight"]),
        "context_variance_weight": float(training["context_variance_weight"]),
        "context_covariance_weight": float(training["context_covariance_weight"]),
        "context_std_target": float(training["context_std_target"]),
    }


def validate(model: TemporalJEPA, loader: DataLoader, device: torch.device, batches: int,
             training: dict[str, Any] | None = None) -> dict[str, float]:
    model.eval()
    losses = []
    embeddings = []
    with torch.no_grad():
        for index, (context, targets, asset) in enumerate(loader):
            if index >= batches:
                break
            context, targets, asset = context.to(device), targets.to(device), asset.to(device)
            with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=device.type == "cuda"):
                output = model(context, targets, asset)
                loss, _ = jepa_loss(
                    output.predicted,
                    output.target,
                    output.context_embedding,
                    **_loss_kwargs(training),
                )
            losses.append(float(loss))
            embeddings.append(output.context_embedding.float().cpu())
    diagnostics = representation_diagnostics(torch.cat(embeddings)) if embeddings else {}
    return {"loss": float(np.mean(losses)) if losses else float("nan"), **diagnostics}


def train(model: TemporalJEPA, train_data: RandomWindowDataset, validation_data: RandomWindowDataset,
          config: dict[str, Any], checkpoint_path: Path, manifest: dict[str, Any],
          resume: bool = True) -> dict[str, Any]:
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for the GPU JEPA experiment")
    training = config["training"]
    seed_everything(int(training["seed"]))
    device = torch.device("cuda")
    model.to(device)
    optimizer = torch.optim.AdamW(
        [parameter for parameter in model.parameters() if parameter.requires_grad],
        lr=float(training["learning_rate"]), weight_decay=float(training["weight_decay"]), betas=(0.9, 0.95),
    )
    scaler = torch.amp.GradScaler("cuda")
    start_step = 0
    sample_cursor = 0
    history: list[dict[str, Any]] = []
    checkpoint: dict[str, Any] | None = None
    if resume and checkpoint_path.exists():
        checkpoint = load_checkpoint(checkpoint_path, model, optimizer, scaler, expected_manifest=manifest)
        start_step = int(checkpoint["step"]) + 1
        sample_cursor = int(checkpoint["sampleCursor"])
        train_data.index_offset = sample_cursor
        history = list(checkpoint.get("history", []))
    loader_generator = torch.Generator().manual_seed(int(training["seed"]) + 17)
    loader = DataLoader(train_data, batch_size=int(training["batch_size"]), shuffle=False,
                        generator=loader_generator,
                        num_workers=int(training["workers"]), pin_memory=True, drop_last=True)
    validation_loader = DataLoader(validation_data, batch_size=int(training["batch_size"]), shuffle=False,
                                   num_workers=0, pin_memory=True, drop_last=True)
    iterator = iter(loader)
    if checkpoint is not None:
        restore_rng(checkpoint)
    accumulation = int(training["gradient_accumulation"])
    total_steps = int(training["steps"])
    optimizer.zero_grad(set_to_none=True)
    started = time.perf_counter()
    for step in range(start_step, total_steps):
        lr = learning_rate(step, total_steps, int(training["warmup_steps"]), float(training["learning_rate"]))
        for group in optimizer.param_groups:
            group["lr"] = lr
        loss_total = 0.0
        diagnostics: dict[str, float] = {}
        for micro in range(accumulation):
            try:
                context, targets, asset = next(iterator)
            except StopIteration:
                iterator = iter(loader)
                context, targets, asset = next(iterator)
            context = context.to(device, non_blocking=True)
            targets = targets.to(device, non_blocking=True)
            asset = asset.to(device, non_blocking=True)
            mask = make_context_mask(len(context), context.shape[1] // model.context_encoder.patch_minutes, device)
            with torch.autocast(device_type="cuda", dtype=torch.float16):
                output = model(context, targets, asset, mask)
                loss, diagnostics = jepa_loss(
                    output.predicted,
                    output.target,
                    output.context_embedding,
                    **_loss_kwargs(training),
                )
                loss = loss / accumulation
            scaler.scale(loss).backward()
            loss_total += float(loss.detach())
            sample_cursor += len(context)
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), float(training["gradient_clip"]))
        scaler.step(optimizer)
        scaler.update()
        optimizer.zero_grad(set_to_none=True)
        momentum = ema_momentum(step, total_steps, float(training["ema_start"]), float(training["ema_end"]))
        model.update_target(momentum)

        if (step + 1) % int(training["log_every"]) == 0 or step == start_step:
            elapsed = max(time.perf_counter() - started, 1e-9)
            row = {"step": step + 1, "loss": loss_total, "lr": lr, "ema": momentum,
                   "samplesPerSec": (step - start_step + 1) * int(training["batch_size"]) * accumulation / elapsed,
                   "peakMemoryMB": torch.cuda.max_memory_allocated() / 1024 ** 2, **diagnostics}
            history.append(row)
            print(json.dumps(row), flush=True)
        if (step + 1) % int(training["save_every"]) == 0:
            save_checkpoint(checkpoint_path, model, optimizer, scaler, step, sample_cursor,
                            config, manifest, history)
    validation = validate(
        model,
        validation_loader,
        device,
        int(training["validation_batches"]),
        training,
    )
    save_checkpoint(checkpoint_path, model, optimizer, scaler, total_steps - 1, sample_cursor,
                    config, manifest, history)
    return {"parameters": model_parameter_count(model), "history": history, "validation": validation,
            "torch": torch.__version__, "cuda": torch.version.cuda,
            "gpu": torch.cuda.get_device_name(0), "peakMemoryMB": torch.cuda.max_memory_allocated() / 1024 ** 2}
