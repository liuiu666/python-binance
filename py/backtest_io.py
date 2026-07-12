"""Shared causal backtest input adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def load_scan_times(path: Path) -> set[pd.Timestamp]:
    """Load and normalize timestamp values from a signal-audit JSON list."""

    raw: Any = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, list):
        raise ValueError("scan-times file must contain a JSON list")
    values: set[pd.Timestamp] = set()
    for item in raw:
        value = item.get("time") if isinstance(item, dict) else item
        timestamp = pd.to_datetime(value, utc=True, errors="coerce")
        if not pd.isna(timestamp):
            values.add(pd.Timestamp(timestamp))
    return values


def read_orderbook(
    path: Path,
    target_index: pd.DatetimeIndex,
    max_age_sec: int = 3,
) -> pd.DataFrame:
    """Align 1-second order-book snapshots to the trade-bar index."""

    usecols = {
        "timestamp",
        "mid",
        "spread_bps",
        "bid_qty_20",
        "ask_qty_20",
        "imbalance_5",
        "imbalance_20",
        "microprice_edge_bps",
        "bid_wall_qty",
        "ask_wall_qty",
    }
    raw = pd.read_csv(path, usecols=lambda col: col in usecols)
    timestamps = pd.to_datetime(raw["timestamp"], utc=True, errors="coerce").dt.floor("s")
    valid = timestamps.notna()
    raw = raw.loc[valid].reset_index(drop=True)
    timestamps = timestamps.loc[valid].reset_index(drop=True)
    columns = [
        "mid",
        "spread_bps",
        "bid_qty_20",
        "ask_qty_20",
        "imbalance_5",
        "imbalance_20",
        "microprice_edge_bps",
        "bid_wall_qty",
        "ask_wall_qty",
    ]
    orderbook = pd.DataFrame(index=timestamps.to_numpy())
    for column in columns:
        if column in raw.columns:
            orderbook[column] = pd.to_numeric(raw[column], errors="coerce").to_numpy(float)
        else:
            orderbook[column] = np.nan
    orderbook["ob_ts_ms"] = (timestamps.astype("int64") // 1_000_000).to_numpy()
    orderbook = orderbook[~orderbook.index.duplicated(keep="last")].sort_index()
    aligned = orderbook.reindex(target_index, method="ffill", limit=max(1, int(max_age_sec)))
    target_ms = pd.Series(target_index.astype("int64") // 1_000_000, index=target_index)
    aligned["ob_age_sec"] = (target_ms - aligned["ob_ts_ms"]) / 1000.0
    aligned["ob_available"] = (
        aligned["mid"].notna()
        & aligned["ob_age_sec"].notna()
        & (aligned["ob_age_sec"] <= float(max_age_sec))
    )
    return aligned
