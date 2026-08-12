from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

USE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "quote_volume",
    "trades", "taker_buy_volume",
]
NUMERIC_COLUMNS = [name for name in USE_COLUMNS if name != "open_time"]


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_data_files(config: dict[str, Any], root: Path) -> list[Path]:
    return [(root / item).resolve() for item in config["data_files"]]


def load_minutes(paths: list[Path]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
        frame = pd.read_csv(path, usecols=USE_COLUMNS)
        frame["time"] = pd.to_datetime(frame.pop("open_time"), utc=True, errors="raise")
        for name in NUMERIC_COLUMNS:
            frame[name] = pd.to_numeric(frame[name], errors="coerce")
        if frame[NUMERIC_COLUMNS].isna().any().any():
            raise ValueError(f"{path} contains invalid numeric values")
        frames.append(frame)
    data = pd.concat(frames, ignore_index=True).sort_values("time")
    if data["time"].duplicated().any():
        duplicates = int(data["time"].duplicated().sum())
        raise ValueError(f"minute data contains {duplicates} duplicate timestamps")
    data = data.set_index("time")
    return data


def audit_minutes(data: pd.DataFrame) -> dict[str, Any]:
    if data.empty:
        raise ValueError("empty minute data")
    delta = data.index.to_series().diff().dropna().dt.total_seconds().div(60.0)
    missing = int(np.maximum(delta.to_numpy(dtype=float) - 1.0, 0.0).sum())
    audit = {
        "rows": int(len(data)),
        "start": data.index.min().isoformat(),
        "end": data.index.max().isoformat(),
        "duplicates": int(data.index.duplicated().sum()),
        "missingMinutes": missing,
        "maxStepMinutes": float(delta.max()) if len(delta) else 0.0,
        "monotonic": bool(data.index.is_monotonic_increasing),
    }
    if audit["duplicates"] or audit["missingMinutes"] or not audit["monotonic"]:
        raise ValueError(f"minute data failed audit: {audit}")
    return audit


def sample_decisions(features: pd.DataFrame, *, step_minutes: int, horizon_minutes: int) -> pd.DataFrame:
    """Create non-overlapping decisions and future feature targets.

    A decision observes the completed minute at t and settles at t+horizon.
    Feature columns prefixed ``future__`` are targets only and must never enter
    a current-state model.
    """
    if horizon_minutes % step_minutes != 0:
        raise ValueError("horizon must be a multiple of sample step")
    # Binance timestamps identify bar opening time. A bar's close is only known
    # one minute later, so decisions are timestamped at bar_end, never bar_open.
    decision_index = features.index + pd.Timedelta(minutes=1)
    mask = (decision_index.minute % step_minutes == 0) & (decision_index.second == 0)
    sampled = features.loc[mask].copy()
    sampled.index = decision_index[mask]
    steps = horizon_minutes // step_minutes
    sampled["settle_time"] = sampled.index.to_series().shift(-steps)
    sampled["settle"] = sampled["close"].shift(-steps)
    sampled["future_realized_vol"] = sampled["realized_vol_10"].shift(-steps)
    for name in [column for column in sampled.columns if column.startswith("x_")]:
        sampled[f"future__{name}"] = sampled[name].shift(-steps)
    expected_settle = sampled.index.to_series() + pd.Timedelta(minutes=horizon_minutes)
    continuous = sampled["settle_time"].eq(expected_settle)
    sampled = sampled.loc[continuous & sampled["settle"].notna()].copy()
    sampled["entry"] = sampled["close"]
    sampled["raw_move"] = sampled["settle"] / sampled["entry"] - 1.0
    sampled["tie"] = sampled["settle"].eq(sampled["entry"])
    sampled["up"] = sampled["settle"].gt(sampled["entry"]).astype("int8")
    sampled.index.name = "time"
    return sampled.reset_index()


def describe_boundaries(samples: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    development_end = pd.Timestamp(config["development_end_exclusive"])
    frozen_start = pd.Timestamp(config["frozen_start"])
    if development_end != frozen_start:
        raise ValueError("development_end_exclusive must equal frozen_start")
    dev = samples[(samples["time"] >= pd.Timestamp(config["development_start"])) & (samples["time"] < development_end)]
    frozen = samples[samples["time"] >= frozen_start]
    return {
        "allCandidates": int(len(samples)),
        "developmentCandidates": int(len(dev)),
        "frozenCandidates": int(len(frozen)),
        "developmentStart": dev["time"].min().isoformat() if len(dev) else None,
        "developmentEnd": dev["time"].max().isoformat() if len(dev) else None,
        "frozenStart": frozen["time"].min().isoformat() if len(frozen) else None,
        "frozenEnd": frozen["time"].max().isoformat() if len(frozen) else None,
    }
