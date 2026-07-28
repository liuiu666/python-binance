"""Causal branch-health gate for the current V2 dynamic confirmation branch."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

import pandas as pd

from research_current_v2_augmented_multiperiod_v9 import DELAYS, metrics
from research_path_efficiency_router_v1 import clean


ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "tmp" / "current_v2_dynamic_confirmation_v10_candidates.csv"
OUT_JSON = ROOT / "tmp" / "current_v2_causal_health_v11.json"
OUT_CSV = ROOT / "tmp" / "current_v2_causal_health_v11_trades.csv"
DYNAMIC_BRANCH = "current_v2_dynamic_book"
HEALTH_SAMPLES = 12
HEALTH_MIN_WINS = 7
OUTCOME_DELAY_SEC = 610
COOLDOWN_SEC = 600


def select(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    history: deque[bool] = deque(maxlen=HEALTH_SAMPLES)
    pending: deque[tuple[pd.Timestamp, bool]] = deque()
    accepted: list[dict[str, Any]] = []
    last_emit: pd.Timestamp | None = None
    counts = {"dynamicWarmup": 0, "dynamicUnhealthy": 0, "dynamicHealthy": 0, "supplement": 0, "cooldown": 0}

    for row in frame.sort_values(["time", "priority"]).to_dict("records"):
        now = pd.Timestamp(row["time"])
        while pending and pending[0][0] <= now:
            _, won = pending.popleft()
            history.append(won)

        is_dynamic = str(row["branch"]) == DYNAMIC_BRANCH
        eligible = not is_dynamic
        if is_dynamic:
            won = float(row["signed_bps_d6"]) > 0.0
            pending.append((now + pd.Timedelta(seconds=OUTCOME_DELAY_SEC), won))
            if len(history) < HEALTH_SAMPLES:
                counts["dynamicWarmup"] += 1
            else:
                healthy = sum(history) >= HEALTH_MIN_WINS and not all(not item for item in list(history)[-3:])
                if healthy:
                    counts["dynamicHealthy"] += 1
                    eligible = True
                else:
                    counts["dynamicUnhealthy"] += 1
        else:
            counts["supplement"] += 1

        if not eligible:
            continue
        if last_emit is not None and (now - last_emit).total_seconds() < COOLDOWN_SEC:
            counts["cooldown"] += 1
            continue
        item = dict(row)
        item["health_samples"] = len(history)
        item["health_wins"] = sum(history)
        accepted.append(item)
        last_emit = now
    return pd.DataFrame(accepted), counts


def run() -> dict[str, Any]:
    candidates = pd.read_csv(CANDIDATES)
    candidates["time"] = pd.to_datetime(candidates.time, utc=True, errors="coerce")
    trades, counts = select(candidates)
    trades["beijing_day"] = trades.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    period_hours = {"development_july5_10": 144.0, "validation_july11_13": 72.0, "forward_july14_15": 35.16}
    report = {
        "method": {
            "parameterSearch": False, "causal": True,
            "strategyId": "BTC_10min_NORMAL_LIQ_OB_V2_QUALITY",
            "base": "V10 current-core dynamic confirmation plus exhaustion supplement",
            "health": "dynamic branch enabled when at least 7 of the latest 12 settled shadow candidates won and latest 3 are not all losses",
            "outcomeDelaySec": OUTCOME_DELAY_SEC,
            "cooldownSec": COOLDOWN_SEC,
            "supplementAlwaysEligible": True,
        },
        "candidateCount": len(candidates),
        "selectionCounts": counts,
        "periods": {
            period: {f"delay{delay}s": metrics(group, delay, period_hours[period]) for delay in DELAYS}
            for period, group in trades.groupby("period")
        },
        "byDayDelay6s": {
            day: metrics(group, 6, 24.0) for day, group in trades.groupby("beijing_day")
        },
        "byBranchDelay6s": {
            branch: metrics(group, 6, sum(period_hours.values())) for branch, group in trades.groupby("branch")
        },
        "acceptance": {"minTradesPerDay": 10.0, "minWinRate": 55.56, "maxDrawdownU": 20.0, "maxLossStreak": 3, "allDelaysMustBeProfitable": True},
        "warning": "Reused evidence only; no deployment.",
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
