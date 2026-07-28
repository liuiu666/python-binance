"""Route confirmed one-sided pullbacks by 10/30/60-minute agreement."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from research_long_minute_consensus_v1 import read_minutes
from research_path_efficiency_router_v1 import attach_delay_outcomes, clean, metrics
from research_path_exhaustion_reclaim_v2 import build_candidates


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "multiscale_auction_router_v3.json"
OUT_CSV = ROOT / "tmp" / "multiscale_auction_router_v3_trades.csv"
DELAYS = (0, 5, 6, 10)


def route(minutes: pd.DataFrame) -> pd.DataFrame:
    trades = build_candidates(minutes)
    close = minutes.close.astype(float)
    returns = pd.DataFrame(
        {
            "ret_context_30": (close / close.shift(30) - 1.0) * 10000.0,
            "ret_context_60": (close / close.shift(60) - 1.0) * 10000.0,
        },
        index=minutes.index,
    )
    trades = trades.merge(returns.reset_index(names="time"), on="time", how="left")
    aligned = (
        (trades.ret_10 * trades.ret_context_30 > 0.0)
        & (trades.ret_10 * trades.ret_context_60 > 0.0)
    )
    trades.loc[aligned & trades.ret_10.gt(0.0), "signal"] = "UP"
    trades.loc[aligned & trades.ret_10.lt(0.0), "signal"] = "DOWN"
    trades.loc[aligned, "branch"] = "multiscale_pullback_follow"
    trades.loc[~aligned, "branch"] = "local_exhaustion_reclaim"
    return trades


def run() -> dict[str, Any]:
    trades = route(read_minutes())
    trades["month"] = trades.time.dt.strftime("%Y-%m")
    trades["beijing_day"] = trades.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    latest = attach_delay_outcomes(trades[trades.time.ge(pd.Timestamp("2026-07-14", tz="UTC"))].copy())
    for delay in DELAYS:
        trades.loc[latest.index, f"move_bps_d{delay}"] = latest[f"move_bps_d{delay}"]
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    total_hours = (trades.time.max() - trades.time.min()).total_seconds() / 3600.0
    report = {
        "method": {
            "parameterSearch": False, "causal": True,
            "rule": "10/30/60 aligned pullbacks follow; non-aligned local exhaustion reclaims fade",
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
            f"delay{delay}s": metrics(latest, f"move_bps_d{delay}", 35.16)
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
