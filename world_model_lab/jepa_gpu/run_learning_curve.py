from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.backtest import development_gate, run_learning_curves
from src.dataset import AssetArrays, build_cache
from src.embeddings import build_decision_frame, extract_embeddings, handcrafted_features
from src.probe import METHOD_DESCRIPTIONS
from src.runtime import experiment_manifest


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run leakage-safe few-shot JEPA probe learning curves")
    parser.add_argument("--checkpoint", type=Path, default=ROOT / "checkpoints" / "dev.pt")
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "learning_curve.json")
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--force-cache", action="store_true")
    parser.add_argument("--min-train-rows", type=int, default=100)
    return parser.parse_args()


def _load_config() -> dict[str, Any]:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def _resolve(items: list[str]) -> list[Path]:
    paths = [(ROOT / item).resolve() for item in items]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(f"missing BTC source files: {missing}")
    return paths


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _seed_everything(seed: int) -> None:
    import torch

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _array_sha256(array: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(array)
    return hashlib.sha256(memoryview(contiguous)).hexdigest()


def _embedding_cache_key(
    *,
    kind: str,
    config: dict[str, Any],
    cache_metadata: dict[str, Any],
    checkpoint_metadata: dict[str, Any],
    sample_indices: np.ndarray,
) -> str:
    identity = {
        "version": 1,
        "kind": kind,
        "configSha256": _canonical_sha256(config),
        "sourceSha256": cache_metadata["sourceSha256"],
        "checkpointSha256": (
            checkpoint_metadata["sha256"] if kind == "pretrained" else None
        ),
        "randomSeed": int(config["training"]["seed"]),
        "sampleIndicesSha256": _array_sha256(sample_indices),
        "sampleRows": int(len(sample_indices)),
    }
    return _canonical_sha256(identity)


def _load_or_extract_embeddings(
    *,
    kind: str,
    cache_key: str,
    asset: AssetArrays,
    sample_indices: np.ndarray,
    model: Any,
    embedding_dimension: int,
    context_minutes: int,
    batch_size: int,
    device: str,
) -> np.ndarray:
    cache_path = ROOT / "cache" / f"learning_curve_{kind}_{cache_key}.npy"
    if cache_path.exists():
        embeddings = np.load(cache_path, mmap_mode="r")
        expected_shape = (len(sample_indices), embedding_dimension)
        if embeddings.shape == expected_shape and embeddings.dtype == np.float32:
            print(f"loaded {kind} embeddings from {cache_path}", flush=True)
            return embeddings

    embeddings = extract_embeddings(
        asset,
        sample_indices,
        model,
        context_minutes=context_minutes,
        batch_size=batch_size,
        device=device,
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_name(f"{cache_path.name}.tmp")
    try:
        with temporary.open("wb") as stream:
            np.save(stream, np.asarray(embeddings, dtype=np.float32), allow_pickle=False)
        os.replace(temporary, cache_path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"saved {kind} embeddings to {cache_path}", flush=True)
    return embeddings


def _create_models(config: dict[str, Any], checkpoint_path: Path, device: str) -> tuple[Any, Any, dict[str, Any]]:
    import torch

    from src.model import TemporalJEPA

    if str(device).startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable")
    if not checkpoint_path.exists():
        raise FileNotFoundError(f"pretrained checkpoint does not exist: {checkpoint_path}")

    seed = int(config["training"]["seed"])
    _seed_everything(seed)
    random_model = TemporalJEPA(**config["model"])
    _seed_everything(seed)
    pretrained_model = TemporalJEPA(**config["model"])
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    expected_manifest = experiment_manifest(ROOT, config)
    if checkpoint.get("manifest") != expected_manifest:
        raise ValueError(
            "checkpoint manifest does not match current config, data, or preprocessing"
        )
    pretrained_model.load_state_dict(checkpoint["model"])
    metadata = {
        "path": str(checkpoint_path.resolve()),
        "sha256": _sha256(checkpoint_path),
        "step": int(checkpoint.get("step", -1)),
    }
    return random_model, pretrained_model, metadata


def main() -> None:
    args = _arguments()
    config = _load_config()
    data = config["data"]
    model_config = config["model"]
    probe_config = config["probe"]
    label_history_start = pd.Timestamp(data["probe_label_history_start"])
    development_start = pd.Timestamp(data["dev_probe_start"])
    development_end = pd.Timestamp(data["development_end_exclusive"])
    frozen_start = pd.Timestamp(data["frozen_start"])
    if development_end != frozen_start:
        raise ValueError("development_end_exclusive must equal frozen_start")

    btc_paths = _resolve(list(data["btc_files"]))
    cache_prefix = ROOT / "cache" / "btc"
    cache_metadata = build_cache(btc_paths, cache_prefix, force=bool(args.force_cache))
    btc = AssetArrays(cache_prefix, 0)
    samples = build_decision_frame(
        btc,
        label_history_start,
        development_end,
        step_minutes=int(probe_config["sample_step_minutes"]),
        context_minutes=int(model_config["context_minutes"]),
        horizon_minutes=int(probe_config["horizon_minutes"]),
        max_settle_time=development_end,
    )
    if samples.empty:
        raise ValueError("development decision sample is empty")
    if (samples["time"] >= frozen_start).any() or (samples["settle_time"] > frozen_start).any():
        raise RuntimeError("frozen-label boundary violation")

    sample_indices = samples["sample_index"].to_numpy()
    handcrafted, handcrafted_names = handcrafted_features(btc, sample_indices)
    random_model, pretrained_model, checkpoint_metadata = _create_models(config, args.checkpoint, args.device)
    random_cache_key = _embedding_cache_key(
        kind="random",
        config=config,
        cache_metadata=cache_metadata,
        checkpoint_metadata=checkpoint_metadata,
        sample_indices=sample_indices,
    )
    pretrained_cache_key = _embedding_cache_key(
        kind="pretrained",
        config=config,
        cache_metadata=cache_metadata,
        checkpoint_metadata=checkpoint_metadata,
        sample_indices=sample_indices,
    )
    random_embeddings = _load_or_extract_embeddings(
        kind="random",
        cache_key=random_cache_key,
        asset=btc,
        sample_indices=sample_indices,
        model=random_model,
        embedding_dimension=int(model_config["d_model"]),
        context_minutes=int(model_config["context_minutes"]),
        batch_size=int(args.batch_size),
        device=args.device,
    )
    del random_model
    try:
        import torch

        if str(args.device).startswith("cuda"):
            torch.cuda.empty_cache()
    except ImportError:
        pass
    pretrained_embeddings = _load_or_extract_embeddings(
        kind="pretrained",
        cache_key=pretrained_cache_key,
        asset=btc,
        sample_indices=sample_indices,
        model=pretrained_model,
        embedding_dimension=int(model_config["d_model"]),
        context_minutes=int(model_config["context_minutes"]),
        batch_size=int(args.batch_size),
        device=args.device,
    )

    features = {
        "handcrafted_logistic": handcrafted,
        "random_frozen_linear": random_embeddings,
        "pretrained_linear": pretrained_embeddings,
        "pretrained_mlp2": pretrained_embeddings,
    }
    learning_curves = run_learning_curves(
        samples,
        features,
        development_start=development_start,
        development_end_exclusive=development_end,
        label_months_values=list(probe_config["label_months"]),
        label_history_start=label_history_start,
        threshold_selection_end_exclusive=probe_config[
            "threshold_selection_end_exclusive"
        ],
        min_selection_trades=int(probe_config["min_selection_trades"]),
        payout_rate=float(probe_config["payout_rate"]),
        stake=float(probe_config["stake"]),
        min_ev_grid=list(probe_config["min_ev_grid"]),
        seed=int(config["training"]["seed"]),
        min_train_rows=int(args.min_train_rows),
    )

    report = {
        "version": 1,
        "boundaries": {
            "labelHistoryStart": label_history_start.isoformat(),
            "developmentStart": development_start.isoformat(),
            "developmentEndExclusive": development_end.isoformat(),
            "frozenStart": frozen_start.isoformat(),
            "maximumLabelSettleTimeRead": samples["settle_time"].max().isoformat(),
            "frozenLabelsReadOrEvaluated": False,
            "trainLabelRule": "train_start <= decision_time < test_start and settle_time <= test_start",
            "testRule": "test_start <= decision_time < test_end and settle_time <= test_end <= frozen_start",
            "decisionTimeSemantic": "decision_time = final context K-line open_time + 1 minute; entry = its close",
            "labelSemantic": "10-minute direction compares entry close[i] with settle close[i+10] at decision_time+10m",
        },
        "methods": [
            {"name": name, "description": description}
            for name, description in METHOD_DESCRIPTIONS.items()
        ],
        "features": {
            "handcraftedColumns": handcrafted_names,
            "handcraftedDimension": int(handcrafted.shape[1]),
            "embeddingDimension": int(pretrained_embeddings.shape[1]),
            "contextMinutes": int(model_config["context_minutes"]),
            "sampleStepMinutes": int(probe_config["sample_step_minutes"]),
            "horizonMinutes": int(probe_config["horizon_minutes"]),
        },
        "data": {
            "asset": "BTCUSDT futures 1m",
            "rows": int(len(samples)),
            "firstDecisionTime": samples["time"].min().isoformat(),
            "lastDecisionTime": samples["time"].max().isoformat(),
            "cacheSourceSha256": cache_metadata["sourceSha256"],
        },
        "checkpoint": checkpoint_metadata,
        "probe": {
            "payoutRate": float(probe_config["payout_rate"]),
            "stake": float(probe_config["stake"]),
            "minEvGrid": [float(item) for item in probe_config["min_ev_grid"]],
            "labelMonths": [int(item) for item in probe_config["label_months"]],
            "thresholdSelectionEndExclusive": probe_config[
                "threshold_selection_end_exclusive"
            ],
            "minimumSelectionTrades": int(
                probe_config["min_selection_trades"]
            ),
            "minTrainRows": int(args.min_train_rows),
        },
        "learningCurves": learning_curves,
        "developmentGate": development_gate(
            learning_curves,
            few_shot_months=(1, 3),
            minimum_confirmation_trades=300,
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
