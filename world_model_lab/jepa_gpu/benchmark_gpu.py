from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.dataset import make_context_mask
from src.losses import jepa_loss
from src.runtime import create_model, load_config, torch_environment


def run_batch(batch_size: int, iterations: int = 10) -> dict[str, float | int]:
    config = load_config(ROOT)
    model = create_model(config).cuda().train()
    model_config = config["model"]
    context = torch.randn(batch_size, model_config["context_minutes"], model_config["input_channels"], device="cuda")
    targets = torch.randn(batch_size, len(model_config["target_end_offsets_minutes"]), model_config["patch_minutes"],
                          model_config["input_channels"], device="cuda")
    asset = torch.randint(0, 2, (batch_size,), device="cuda")
    optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=3e-4)
    scaler = torch.amp.GradScaler("cuda")
    torch.cuda.reset_peak_memory_stats()
    torch.cuda.synchronize()
    started = time.perf_counter()
    for _ in range(iterations):
        optimizer.zero_grad(set_to_none=True)
        mask = make_context_mask(batch_size, model_config["context_minutes"] // model_config["patch_minutes"], "cuda")
        with torch.autocast(device_type="cuda", dtype=torch.float16):
            output = model(context, targets, asset, mask)
            loss, _ = jepa_loss(output.predicted, output.target)
        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
        model.update_target(0.99)
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    return {"batchSize": batch_size, "iterations": iterations, "samplesPerSec": batch_size * iterations / elapsed,
            "peakMemoryMB": torch.cuda.max_memory_allocated() / 1024 ** 2, "loss": float(loss.detach())}


def main() -> None:
    environment = torch_environment()
    if not environment["cudaAvailable"]:
        raise SystemExit(f"CUDA unavailable: {environment}")
    results = []
    for batch_size in (32, 64, 96, 128):
        try:
            results.append(run_batch(batch_size))
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            results.append({"batchSize": batch_size, "oom": True})
            break
    report = {"environment": environment, "results": results}
    (ROOT / "reports" / "gpu_benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
