"""Causal validity gate for the fixed exhaustion-reclaim branch."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import pandas as pd

from research_long_minute_consensus_v1 import read_minutes
from research_path_efficiency_router_v1 import attach_delay_outcomes, clean, metrics
from research_path_exhaustion_reclaim_v2 import build_candidates


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "exhaustion_online_validity_v4.json"
OUT_CSV = ROOT / "tmp" / "exhaustion_online_validity_v4_trades.csv"
DELAYS = (0, 5, 6, 10)
LOOKBACK = pd.Timedelta(days=7)
MIN_SETTLED_PER_SIDE = 15
BREAKEVEN = 5.0 / 9.0


def apply_validity_gate(candidates: pd.DataFrame) -> pd.DataFrame:
    histories: dict[str, deque[tuple[pd.Timestamp, bool]]] = defaultdict(deque)
    pending: deque[dict[str, Any]] = deque()
    accepted: list[dict[str, Any]] = []
    for row in candidates.sort_values("time").to_dict("records"):
        now = pd.Timestamp(row["time"])
        while pending and pd.Timestamp(pending[0]["settle_time"]) <= now:
            resolved = pending.popleft()
            direction = 1.0 if resolved["signal"] == "UP" else -1.0
            won = float(resolved["move_bps"]) * direction > 0.0
            histories[str(resolved["signal"])].append((pd.Timestamp(resolved["settle_time"]), won))

        signal = str(row["signal"])
        history = histories[signal]
        cutoff = now - LOOKBACK
        while history and history[0][0] < cutoff:
            history.popleft()
        pending.append(row)
        if len(history) < MIN_SETTLED_PER_SIDE:
            continue
        wins = sum(won for _, won in history)
        posterior_rate = (wins + 1.0) / (len(history) + 2.0)
        if posterior_rate < BREAKEVEN:
            continue
        item = dict(row)
        item["validity_samples"] = len(history)
        item["validity_wins"] = wins
        item["validity_posterior"] = posterior_rate
        accepted.append(item)
    return pd.DataFrame(accepted)


def run() -> dict[str, Any]:
    candidates = build_candidates(read_minutes())
    trades = apply_validity_gate(candidates)
    trades["month"] = trades.time.dt.strftime("%Y-%m")
    trades["beijing_day"] = trades.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    latest = attach_delay_outcomes(trades[trades.time.ge(pd.Timestamp("2026-07-14", tz="UTC"))].copy())
    for delay in DELAYS:
        trades.loc[latest.index, f"move_bps_d{delay}"] = latest[f"move_bps_d{delay}"]
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    total_hours = (candidates.time.max() - candidates.time.min()).total_seconds() / 3600.0
    report = {
        "method": {
            "parameterSearch": False, "causal": True,
            "baseSignal": "fixed one-sided exhaustion reclaim V2",
            "validityGate": "per-side prior 7-day settled shadow posterior >= 55.56%, minimum 15",
        },
        "candidateCount": len(candidates),
        "overall": metrics(trades, "move_bps", total_hours),
        "byMonth": {
            month: metrics(group, "move_bps", max((group.time.max() - group.time.min()).total_seconds() / 3600.0, 24.0))
            for month, group in trades.groupby("month")
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
