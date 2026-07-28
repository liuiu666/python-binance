"""Causal, non-overlapping event samples for ten-minute auction research."""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import pandas as pd


ENTRY_DELAY_SEC = 5
HORIZON_SEC = 600
SPACING_SEC = 600
ROBUSTNESS_DELAYS = (0, 5, 6, 10)
SHAPE_WINDOWS = (60, 120, 300, 600)

FEATURE_COLUMNS = [
    "ret_10", "ret_30", "ret_60", "ret_120", "ret_300", "ret_600",
    "vol_60", "vol_120", "vol_300", "vol_600",
    "z_60", "z_120", "z_300", "z_600",
    "inside1_60", "inside1_120", "inside1_300", "inside1_600",
    "skew_60", "skew_120", "skew_300", "skew_600",
    "kurtosis_60", "kurtosis_120", "kurtosis_300", "kurtosis_600",
    "slope_sigma_60", "slope_sigma_120", "slope_sigma_300", "slope_sigma_600",
    "sigma_expand_60", "sigma_expand_120", "sigma_expand_300", "sigma_expand_600",
    "flow_10", "flow_60", "flow_300", "volume_ratio_60",
    "imbalance_10", "imbalance_60", "imbalance_300",
    "micro_10", "micro_60", "spread_60",
    "bid_depth_60", "ask_depth_60", "depth_imbalance_60",
    "bid_depth_change_60", "ask_depth_change_60",
    "trend_strength_600", "speed_ratio_60_600",
    "flow_price_alignment_60", "book_flow_alignment_60",
]


def _window(data: pd.DataFrame, pos: int, width: int) -> pd.DataFrame:
    return data.iloc[pos - width + 1:pos + 1]


def _mean(frame: pd.DataFrame, name: str) -> float:
    if name not in frame:
        return float("nan")
    values = frame[name].astype(float).replace([np.inf, -np.inf], np.nan)
    return float(values.mean())


def _flow_ratio(frame: pd.DataFrame) -> float:
    buy = float(frame["buy_qty"].fillna(0.0).sum())
    sell = float(frame["sell_qty"].fillna(0.0).sum())
    total = buy + sell
    return (buy - sell) / total if total > 0.0 else 0.0


def _shape(close: pd.Series) -> dict[str, float]:
    values = close.astype(float).to_numpy()
    center = float(np.mean(values))
    sigma = float(np.std(values, ddof=0))
    if center <= 0.0 or sigma <= 0.0:
        return {
            "vol": 0.0, "z": 0.0, "inside1": 1.0, "skew": 0.0,
            "kurtosis": 0.0, "slope_sigma": 0.0, "sigma_expand": 1.0,
        }
    normalized = (values - center) / sigma
    returns = pd.Series(values).pct_change().dropna().to_numpy() * 10000.0
    quarter = max(10, len(values) // 4)
    first_center = float(np.mean(values[:quarter]))
    last_center = float(np.mean(values[-quarter:]))
    sigma_bps = sigma / center * 10000.0
    slope_bps = (last_center / first_center - 1.0) * 10000.0 if first_center > 0.0 else 0.0
    half = len(values) // 2
    first_sigma = float(np.std(values[:half], ddof=0))
    last_sigma = float(np.std(values[half:], ddof=0))
    return {
        "vol": float(np.std(returns, ddof=0)) * math.sqrt(len(values)) if len(returns) else 0.0,
        "z": float(normalized[-1]),
        "inside1": float(np.mean(np.abs(normalized) <= 1.0)),
        "skew": float(np.mean(normalized**3)),
        "kurtosis": float(np.mean(normalized**4) - 3.0),
        "slope_sigma": slope_bps / sigma_bps if sigma_bps > 0.0 else 0.0,
        "sigma_expand": last_sigma / first_sigma if first_sigma > 0.0 else 1.0,
    }


def _depth_change(frame: pd.DataFrame, name: str) -> float:
    if name not in frame:
        return float("nan")
    values = frame[name].astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    if len(values) < 2:
        return float("nan")
    baseline = float(values.iloc[:max(1, len(values) // 5)].mean())
    current = float(values.iloc[-max(1, len(values) // 5):].mean())
    return current / baseline - 1.0 if baseline > 0.0 else 0.0


def build_event_samples(
    source: Any,
    *,
    entry_delay_sec: int = ENTRY_DELAY_SEC,
    horizon_sec: int = HORIZON_SEC,
    spacing_sec: int = SPACING_SEC,
    min_orderbook_pct: float = 80.0,
) -> pd.DataFrame:
    data = source.data
    index = data.index
    warmup = max(SHAPE_WINDOWS)
    first = max(source.test_start, index.min() + pd.Timedelta(seconds=warmup))
    max_delay = max(ROBUSTNESS_DELAYS)
    last = min(source.test_end, index.max() - pd.Timedelta(seconds=horizon_sec + max_delay))
    start = first.ceil(f"{spacing_sec}s")
    rows: list[dict[str, Any]] = []
    for timestamp in pd.date_range(start, last, freq=f"{spacing_sec}s", tz="UTC"):
        pos = int(index.searchsorted(timestamp))
        if pos >= len(data) or pos < warmup or abs((index[pos] - timestamp).total_seconds()) > 1:
            continue
        history = _window(data, pos, warmup)
        observed = history.get("observed", pd.Series(True, index=history.index)).fillna(False)
        available = history.get("ob_available", pd.Series(False, index=history.index)).fillna(False)
        observed_pct = float(observed.mean() * 100.0)
        orderbook_pct = float(available.mean() * 100.0)
        if observed_pct < 90.0 or orderbook_pct < min_orderbook_pct:
            continue
        entry_target = timestamp + pd.Timedelta(seconds=entry_delay_sec)
        settle_target = entry_target + pd.Timedelta(seconds=horizon_sec)
        entry_pos = int(index.searchsorted(entry_target))
        settle_pos = int(index.searchsorted(settle_target))
        if entry_pos >= len(data) or settle_pos >= len(data):
            continue
        if (index[entry_pos] - entry_target).total_seconds() > 1 or (index[settle_pos] - settle_target).total_seconds() > 1:
            continue
        close = data["close"].astype(float)
        entry = float(close.iloc[entry_pos])
        settle = float(close.iloc[settle_pos])
        future_path = close.iloc[entry_pos:settle_pos + 1]
        if entry <= 0.0 or settle <= 0.0 or future_path.empty:
            continue
        row: dict[str, Any] = {
            "source": source.spec.name,
            "role": source.spec.role,
            "time": timestamp,
            "entry_time": index[entry_pos],
            "settle_time": index[settle_pos],
            "entry_delay_sec": entry_delay_sec,
            "entry": entry,
            "settle": settle,
            "up": int(settle > entry),
            "raw_move_bps": (settle / entry - 1.0) * 10000.0,
            "future_high_bps": (float(future_path.max()) / entry - 1.0) * 10000.0,
            "future_low_bps": (float(future_path.min()) / entry - 1.0) * 10000.0,
            "observed_pct_600": observed_pct,
            "orderbook_pct_600": orderbook_pct,
        }
        for delay in ROBUSTNESS_DELAYS:
            delayed_entry_target = timestamp + pd.Timedelta(seconds=delay)
            delayed_settle_target = delayed_entry_target + pd.Timedelta(seconds=horizon_sec)
            delayed_entry_pos = int(index.searchsorted(delayed_entry_target))
            delayed_settle_pos = int(index.searchsorted(delayed_settle_target))
            delayed_entry = float(close.iloc[delayed_entry_pos])
            delayed_settle = float(close.iloc[delayed_settle_pos])
            row[f"entry_d{delay}"] = delayed_entry
            row[f"settle_d{delay}"] = delayed_settle
            row[f"up_d{delay}"] = int(delayed_settle > delayed_entry)
            row[f"raw_move_bps_d{delay}"] = (delayed_settle / delayed_entry - 1.0) * 10000.0
        for width in (10, 30, 60, 120, 300, 600):
            values = _window(data, pos, width)["close"].astype(float)
            row[f"ret_{width}"] = (float(values.iloc[-1]) / float(values.iloc[0]) - 1.0) * 10000.0
        for width in SHAPE_WINDOWS:
            shape = _shape(_window(data, pos, width)["close"])
            for name, value in shape.items():
                row[f"{name}_{width}"] = value
        for width in (10, 60, 300):
            frame = _window(data, pos, width)
            row[f"flow_{width}"] = _flow_ratio(frame)
            row[f"imbalance_{width}"] = _mean(frame, "imbalance_20")
        minute = _window(data, pos, 60)
        row["micro_10"] = _mean(_window(data, pos, 10), "microprice_edge_bps")
        row["micro_60"] = _mean(minute, "microprice_edge_bps")
        row["spread_60"] = _mean(minute, "spread_bps")
        row["bid_depth_60"] = _mean(minute, "bid_qty_20")
        row["ask_depth_60"] = _mean(minute, "ask_qty_20")
        depth_total = row["bid_depth_60"] + row["ask_depth_60"]
        row["depth_imbalance_60"] = (
            (row["bid_depth_60"] - row["ask_depth_60"]) / depth_total if depth_total > 0.0 else 0.0
        )
        row["bid_depth_change_60"] = _depth_change(minute, "bid_qty_20")
        row["ask_depth_change_60"] = _depth_change(minute, "ask_qty_20")
        volume60 = float(minute["volume"].fillna(0.0).sum())
        volume600 = float(history["volume"].fillna(0.0).sum()) / 10.0
        row["volume_ratio_60"] = volume60 / volume600 if volume600 > 0.0 else 0.0
        row["trend_strength_600"] = abs(row["ret_600"]) / max(row["vol_600"], 0.5)
        row["speed_ratio_60_600"] = abs(row["ret_60"]) / max(abs(row["ret_600"]), 1.0)
        row["flow_price_alignment_60"] = float(np.sign(row["ret_60"] * row["flow_60"]))
        row["book_flow_alignment_60"] = float(np.sign(row["flow_60"] * row["imbalance_60"]))
        rows.append(row)
    return pd.DataFrame(rows)


def combine_event_samples(sources: Iterable[Any]) -> pd.DataFrame:
    frames = [build_event_samples(source) for source in sources]
    if not frames:
        return pd.DataFrame()
    return (
        pd.concat(frames, ignore_index=True)
        .sort_values("time")
        .drop_duplicates("time", keep="last")
        .reset_index(drop=True)
    )
