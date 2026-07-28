"""Route auction pullbacks using causal 6h/24h return autocorrelation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from research_long_minute_consensus_v1 import read_minutes
from research_path_efficiency_router_v1 import attach_delay_outcomes, clean, metrics
from research_path_exhaustion_reclaim_v2 import build_candidates


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "autocorrelation_regime_router_v5.json"
OUT_CSV = ROOT / "tmp" / "autocorrelation_regime_router_v5_trades.csv"
DELAYS = (0, 5, 6, 10)


def route(minutes: pd.DataFrame) -> pd.DataFrame:
    candidates = build_candidates(minutes)
    ret1 = minutes.close.astype(float).pct_change()
    context = pd.DataFrame(index=minutes.index)
    context["autocorr_6h"] = ret1.rolling(360, min_periods=330).corr(ret1.shift(1))
    context["autocorr_24h"] = ret1.rolling(1440, min_periods=1320).corr(ret1.shift(1))
    trades = candidates.merge(context.reset_index(names="time"), on="time", how="left").dropna(
        subset=["autocorr_6h", "autocorr_24h"]
    )
    reverting = trades.autocorr_6h.le(0.0) & trades.autocorr_24h.le(0.0)
    trending = trades.autocorr_6h.gt(0.0) & trades.autocorr_24h.gt(0.0)
    trades = trades[reverting | trending].copy()
    trades.loc[reverting[reverting | trending], "branch"] = "negative_autocorr_reclaim"
    trades.loc[trending[reverting | trending], "branch"] = "positive_autocorr_follow"
    follow = trades.branch.eq("positive_autocorr_follow")
    trades.loc[follow & trades.ret_10.gt(0.0), "signal"] = "UP"
    trades.loc[follow & trades.ret_10.lt(0.0), "signal"] = "DOWN"
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
            "rule": "negative 6h+24h autocorrelation fades; positive 6h+24h autocorrelation follows; mixed abstains",
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
