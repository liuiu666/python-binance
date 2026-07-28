"""Fixed path-efficiency router for ten-minute BTC event contracts.

High-efficiency paths represent one-sided auctions and follow their direction.
Low-efficiency paths at a distribution extreme require a short reclaim before
trading back toward the center.  Rules are fixed and evaluated in one pass.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research_long_minute_consensus_v1 import read_minutes


ROOT = Path(__file__).resolve().parents[1]
SECONDS = ROOT / "tmp" / "frozen_position_forward" / "btcusdt_1s_trades.csv"
OUT_JSON = ROOT / "tmp" / "path_efficiency_router_v1.json"
OUT_CSV = ROOT / "tmp" / "path_efficiency_router_v1_trades.csv"
DELAYS = (0, 5, 6, 10)


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def build_candidates(minutes: pd.DataFrame) -> pd.DataFrame:
    close = minutes.close.astype(float)
    ret1 = (close / close.shift(1) - 1.0) * 10000.0
    frame = pd.DataFrame(index=minutes.index)
    for width in (1, 2, 3, 5, 10, 30):
        frame[f"ret_{width}"] = (close / close.shift(width) - 1.0) * 10000.0
    frame["path_10"] = ret1.abs().rolling(10, min_periods=10).sum()
    frame["efficiency_10"] = frame.ret_10.abs() / frame.path_10.replace(0.0, np.nan)
    frame["noise_30"] = ret1.rolling(30, min_periods=30).std(ddof=0)
    frame["trend_strength"] = frame.ret_10.abs() / (frame.noise_30 * math.sqrt(10.0)).replace(0.0, np.nan)
    center30 = close.rolling(30, min_periods=30).mean()
    sigma30 = close.rolling(30, min_periods=30).std(ddof=0)
    frame["z_30"] = (close - center30) / sigma30.replace(0.0, np.nan)
    frame["volume_ratio"] = (
        minutes.volume.rolling(5, min_periods=5).mean()
        / minutes.volume.rolling(30, min_periods=30).mean().replace(0.0, np.nan)
    )
    frame["observed"] = close.notna().astype(float).rolling(120, min_periods=120).mean()
    frame["entry_time"] = frame.index + pd.Timedelta(minutes=1)
    frame["settle_time"] = frame.index + pd.Timedelta(minutes=11)
    frame["entry"] = minutes.open.shift(-1)
    frame["settle"] = close.shift(-10)
    frame["move_bps"] = (frame.settle / frame.entry - 1.0) * 10000.0

    trend = (
        frame.efficiency_10.ge(0.60)
        & frame.trend_strength.ge(1.25)
        & (frame.ret_3 * frame.ret_10).gt(0.0)
        & frame.ret_3.abs().ge(3.0)
        & frame.volume_ratio.ge(0.80)
        & frame.ret_1.abs().le(np.maximum(6.0, frame.ret_5.abs() * 0.60))
    )
    reversion = (
        frame.efficiency_10.le(0.35)
        & frame.z_30.abs().ge(1.50)
        & (frame.ret_5 * frame.z_30).gt(0.0)
        & (frame.ret_2 * frame.z_30).lt(0.0)
        & frame.ret_2.abs().ge(2.0)
        & frame.volume_ratio.le(1.50)
    )
    frame["signal"] = None
    frame["branch"] = None
    frame.loc[trend & frame.ret_10.gt(0.0), ["signal", "branch"]] = ["UP", "one_sided_follow"]
    frame.loc[trend & frame.ret_10.lt(0.0), ["signal", "branch"]] = ["DOWN", "one_sided_follow"]
    frame.loc[reversion & frame.z_30.gt(0.0), ["signal", "branch"]] = ["DOWN", "low_efficiency_reclaim"]
    frame.loc[reversion & frame.z_30.lt(0.0), ["signal", "branch"]] = ["UP", "low_efficiency_reclaim"]
    frame = frame[
        frame.signal.notna() & frame.observed.ge(0.98) & frame.move_bps.notna()
    ].replace([np.inf, -np.inf], np.nan).dropna(
        subset=["entry", "settle", "efficiency_10", "trend_strength", "z_30"]
    )

    accepted = []
    last_time: pd.Timestamp | None = None
    for row in frame.reset_index(names="time").to_dict("records"):
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < 600:
            continue
        accepted.append(row)
        last_time = timestamp
    return pd.DataFrame(accepted)


def attach_delay_outcomes(trades: pd.DataFrame) -> pd.DataFrame:
    seconds = pd.read_csv(SECONDS, usecols=["timestamp", "close"])
    seconds["timestamp"] = pd.to_datetime(seconds.timestamp, utc=True, errors="coerce")
    seconds = seconds.dropna().drop_duplicates("timestamp").sort_values("timestamp").set_index("timestamp")
    values = seconds.close.astype(float)
    index = values.index

    def price_at(timestamp: pd.Timestamp) -> float:
        position = int(index.searchsorted(timestamp, side="left"))
        if position >= len(index) or abs((index[position] - timestamp).total_seconds()) > 2:
            return float("nan")
        return float(values.iloc[position])

    result = trades.copy()
    for delay in DELAYS:
        entries = [price_at(pd.Timestamp(value) + pd.Timedelta(seconds=delay)) for value in result.entry_time]
        settles = [price_at(pd.Timestamp(value) + pd.Timedelta(seconds=delay)) for value in result.settle_time]
        result[f"move_bps_d{delay}"] = (np.asarray(settles) / np.asarray(entries) - 1.0) * 10000.0
    return result


def maximum_loss_streak(wins: np.ndarray) -> int:
    current = maximum = 0
    for won in wins:
        current = 0 if won else current + 1
        maximum = max(maximum, current)
    return maximum


def metrics(frame: pd.DataFrame, move_column: str, hours: float) -> dict[str, Any]:
    frame = frame.dropna(subset=[move_column])
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": 0.0, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0, "tradesPerDay": 0.0}
    direction = np.where(frame.signal.eq("UP"), 1.0, -1.0)
    signed = frame[move_column].to_numpy(float) * direction
    wins = signed > 0.0
    pnl = np.where(wins, 4.0, -5.0)
    equity = np.r_[0.0, np.cumsum(pnl)]
    drawdown = np.maximum.accumulate(equity) - equity
    return {
        "trades": int(len(frame)), "wins": int(wins.sum()),
        "winRate": round(float(wins.mean()) * 100.0, 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float(drawdown.max()), 2),
        "maxLossStreak": maximum_loss_streak(wins),
        "tradesPerDay": round(len(frame) / max(hours / 24.0, 1e-9), 2),
        "medianSignedBps": round(float(np.median(signed)), 3),
    }


def run() -> dict[str, Any]:
    trades = build_candidates(read_minutes())
    trades["month"] = trades.time.dt.strftime("%Y-%m")
    trades["beijing_day"] = trades.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    latest = trades[trades.time.ge(pd.Timestamp("2026-07-14", tz="UTC"))].copy()
    latest = attach_delay_outcomes(latest)
    for delay in DELAYS:
        trades.loc[latest.index, f"move_bps_d{delay}"] = latest[f"move_bps_d{delay}"]
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    total_hours = (trades.time.max() - trades.time.min()).total_seconds() / 3600.0
    latest_hours = 35.16
    report = {
        "method": {
            "parameterSearch": False, "causal": True,
            "rule": "high-efficiency one-sided auctions follow; low-efficiency extreme reclaims revert",
            "cooldownSec": 600,
        },
        "overall": metrics(trades, "move_bps", total_hours),
        "byMonth": {
            month: metrics(group, "move_bps", max((group.time.max() - group.time.min()).total_seconds() / 3600.0, 24.0))
            for month, group in trades.groupby("month")
        },
        "byBranch": {
            branch: metrics(group, "move_bps", total_hours)
            for branch, group in trades.groupby("branch")
        },
        "latestDelays": {
            f"delay{delay}s": metrics(latest, f"move_bps_d{delay}", latest_hours)
            for delay in DELAYS
        },
        "latestByDayDelay6s": {
            day: metrics(group, "move_bps_d6", 24.0)
            for day, group in latest.groupby("beijing_day")
        },
        "acceptance": {"minTradesPerDay": 10.0, "minWinRate": 55.56, "maxDrawdownU": 20.0, "maxLossStreak": 3, "allDelaysMustBeProfitable": True},
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
