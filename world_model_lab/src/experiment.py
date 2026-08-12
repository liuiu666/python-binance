from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .data import audit_minutes, describe_boundaries, load_config, load_minutes, resolve_data_files, sample_decisions, sha256_file
from .features import build_features


def prepare(root: Path) -> tuple[dict[str, Any], pd.DataFrame, dict[str, Any]]:
    config_path = root / "config.json"
    config = load_config(config_path)
    paths = resolve_data_files(config, root)
    minutes = load_minutes(paths)
    audit = audit_minutes(minutes)
    features = build_features(minutes)
    samples = sample_decisions(
        features,
        step_minutes=int(config["sample_step_minutes"]),
        horizon_minutes=int(config["horizon_minutes"]),
    )
    provenance = {
        "configSha256": sha256_file(config_path),
        "dataSha256": {str(path): sha256_file(path) for path in paths},
        "dataAudit": audit,
        "boundaries": describe_boundaries(samples, config),
    }
    return config, samples, provenance


def clean_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    if isinstance(value, tuple):
        return [clean_json(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(clean_json(value), ensure_ascii=False, indent=2), encoding="utf-8")
