"""Augment the currently running low-frequency V2 without adding a strategy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research_path_efficiency_router_v1 import clean


ROOT = Path(__file__).resolve().parents[1]
ONLINE_TRADES = ROOT / "tmp" / "online_strategies_latest_backtest_trades.csv"
SUPPLEMENT_TRADES = ROOT / "tmp" / "exhaustion_post_confirmation_cooldown_v7_trades.csv"
OUT_JSON = ROOT / "tmp" / "current_v2_augmented_v8.json"
OUT_CSV = ROOT / "tmp" / "current_v2_augmented_v8_trades.csv"
STRATEGY_ID = "BTC_10min_NORMAL_LIQ_OB_V2_QUALITY"
DELAYS = (0, 5, 6, 10)


def load_current() -> pd.DataFrame:
    source = pd.read_csv(ONLINE_TRADES)
    source = source[source.strategy_id.eq(STRATEGY_ID)].copy()
    source["time"] = pd.to_datetime(source.time, utc=True, errors="coerce")
    source["branch"] = "current_v2_original"
    source["priority"] = 0
    columns = ["time", "signal", "branch", "priority", "reason"]
    for delay in DELAYS:
        columns.append(f"signed_bps_d{delay}")
    return source[columns]


def load_supplement() -> pd.DataFrame:
    source = pd.read_csv(SUPPLEMENT_TRADES)
    source = source[source.period.eq("forward_july14_15")].copy()
    # Minute timestamps identify the candle open; the causal decision exists
    # only after that minute has completed.
    source["time"] = pd.to_datetime(source.time, utc=True, errors="coerce") + pd.Timedelta(seconds=59)
    source["branch"] = "exhaustion_orderbook_supplement"
    source["priority"] = 1
    source["reason"] = "one_sided_exhaustion_reclaim_orderbook_2of3"
    direction = np.where(source.signal.eq("UP"), 1.0, -1.0)
    columns = ["time", "signal", "branch", "priority", "reason"]
    for delay in DELAYS:
        source[f"signed_bps_d{delay}"] = source[f"move_bps_d{delay}"] * direction
        columns.append(f"signed_bps_d{delay}")
    return source[columns]


def shared_cooldown(frame: pd.DataFrame) -> pd.DataFrame:
    accepted: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    for row in frame.sort_values(["time", "priority"]).to_dict("records"):
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < 600:
            continue
        accepted.append(row)
        last_time = timestamp
    return pd.DataFrame(accepted)


def maximum_loss_streak(wins: np.ndarray) -> int:
    current = maximum = 0
    for won in wins:
        current = 0 if won else current + 1
        maximum = max(maximum, current)
    return maximum


def metrics(frame: pd.DataFrame, delay: int, hours: float) -> dict[str, Any]:
    signed = frame[f"signed_bps_d{delay}"].to_numpy(float)
    wins = signed > 0.0
    pnl = np.where(wins, 4.0, -5.0)
    equity = np.r_[0.0, np.cumsum(pnl)]
    drawdown = np.maximum.accumulate(equity) - equity
    return {
        "trades": int(len(frame)), "wins": int(wins.sum()),
        "winRate": round(float(wins.mean()) * 100.0, 2) if len(frame) else 0.0,
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float(drawdown.max()), 2),
        "maxLossStreak": maximum_loss_streak(wins),
        "tradesPerDay": round(len(frame) / (hours / 24.0), 2),
        "medianSignedBps": round(float(np.median(signed)), 3) if len(frame) else None,
    }


def run() -> dict[str, Any]:
    current = load_current()
    supplement = load_supplement()
    trades = shared_cooldown(pd.concat([current, supplement], ignore_index=True))
    trades["beijing_day"] = trades.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    report = {
        "method": {
            "parameterSearch": False, "causal": True,
            "strategyId": STRATEGY_ID,
            "architecture": "existing V2 original branch plus exhaustion/order-book supplement under one shared cooldown",
            "currentBranchPriority": True,
            "cooldownSec": 600,
            "amountU": 5,
        },
        "inputs": {"currentSignals": len(current), "supplementSignals": len(supplement)},
        "combinedDelays": {f"delay{delay}s": metrics(trades, delay, 35.16) for delay in DELAYS},
        "byBranchDelay6s": {
            branch: metrics(group, 6, 35.16) for branch, group in trades.groupby("branch")
        },
        "byDayDelay6s": {
            day: metrics(group, 6, 24.0) for day, group in trades.groupby("beijing_day")
        },
        "acceptance": {"minTradesPerDay": 10.0, "minWinRate": 55.56, "maxDrawdownU": 20.0, "maxLossStreak": 3, "allDelaysMustBeProfitable": True},
        "warning": "Latest period was already inspected; this is architecture validation, not untouched proof.",
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
