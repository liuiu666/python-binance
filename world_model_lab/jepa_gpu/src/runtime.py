from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import torch

from .dataset import AssetArrays, RandomWindowDataset, build_cache
from .model import TemporalJEPA


def load_config(root: Path) -> dict[str, Any]:
    return json.loads((root / "config.json").read_text(encoding="utf-8"))


def resolve_files(root: Path, items: list[str]) -> list[Path]:
    return [(root / item).resolve() for item in items]


def prepare_assets(root: Path, config: dict[str, Any], force: bool = False) -> list[AssetArrays]:
    btc_files = resolve_files(root, config["data"]["btc_files"])
    eth_files = resolve_files(root, config["data"]["eth_files"])
    build_cache(btc_files, root / "cache" / "btc", force=force)
    build_cache(eth_files, root / "cache" / "eth", force=force)
    return [AssetArrays(root / "cache" / "btc", 0), AssetArrays(root / "cache" / "eth", 1)]


def experiment_manifest(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    caches = {}
    for name in ("btc", "eth"):
        metadata = json.loads((root / "cache" / f"{name}.json").read_text(encoding="utf-8"))
        caches[name] = {
            key: metadata[key] for key in (
                "cacheSchemaVersion", "preprocessingVersion", "sourceSha256", "rows", "start", "end", "channels"
            )
        }
    return {"configSha256": hashlib.sha256(canonical).hexdigest(), "caches": caches,
            "pretrainEndExclusive": config["data"]["dev_pretrain_end_exclusive"],
            "targetOffsetSemantic": "end-exclusive patch offsets from context anchor; version 1"}


def create_model(config: dict[str, Any]) -> TemporalJEPA:
    return TemporalJEPA(**config["model"])


def dataset_pair(assets: list[AssetArrays], config: dict[str, Any], end_exclusive: str) -> tuple[RandomWindowDataset, RandomWindowDataset]:
    model = config["model"]
    seed = int(config["training"]["seed"])
    validation_start = config["data"]["dev_validation_start"]
    offsets = list(model["target_end_offsets_minutes"])
    total_samples = (int(config["training"]["steps"]) * int(config["training"]["batch_size"])
                     * int(config["training"]["gradient_accumulation"]) + int(config["training"]["batch_size"]))
    train = RandomWindowDataset(assets, validation_start, int(model["context_minutes"]), offsets,
                                int(model["patch_minutes"]), seed=seed, virtual_length=total_samples)
    validation = RandomWindowDataset(assets, end_exclusive, int(model["context_minutes"]), offsets,
                                     int(model["patch_minutes"]), seed=seed + 1_000_000_007,
                                     virtual_length=100_000, start_inclusive=validation_start)
    return train, validation


def torch_environment() -> dict[str, Any]:
    return {"torch": torch.__version__, "cudaBuild": torch.version.cuda,
            "cudaAvailable": torch.cuda.is_available(),
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
            "capability": torch.cuda.get_device_capability(0) if torch.cuda.is_available() else None}
