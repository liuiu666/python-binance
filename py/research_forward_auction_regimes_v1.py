"""Fixed auction-mechanics hypotheses on the latest non-overlapping events.

No threshold is selected from this file's outcomes.  The branches encode three
pre-declared auction states: accepted migration, failed auction, and balanced
value reversion.  Results are diagnostic and cannot be called untouched after
this run.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "tmp" / "frozen_position_forward" / "events_10m.csv"
OUT_JSON = ROOT / "tmp" / "forward_auction_regimes_v1.json"
OUT_CSV = ROOT / "tmp" / "forward_auction_regimes_v1_trades.csv"
DELAYS = (0, 5, 10)


def sign(value: float) -> int:
    return 1 if value > 0.0 else -1 if value < 0.0 else 0


def classify(row: pd.Series) -> tuple[str, int, str]:
    z = float(row["z_600"])
    edge = sign(z)
    if edge == 0:
        return "skip", 0, "price_at_center"

    ret300 = edge * float(row["ret_300"])
    ret60 = edge * float(row["ret_60"])
    flow60 = edge * float(row["flow_60"])
    flow10 = edge * float(row["flow_10"])
    book60 = edge * float(row["imbalance_60"])
    micro10 = edge * float(row["micro_10"])
    outside = abs(z) >= 1.2 and ret300 >= 8.0

    if outside and ret60 >= 3.0 and flow60 >= 0.08 and book60 >= 0.05:
        return "accepted_migration", edge, "outside_old_value_with_aligned_price_flow_and_book"

    if outside and ret60 <= -2.0 and flow10 <= -0.05 and micro10 <= 0.0:
        return "failed_auction", -edge, "outside_old_value_then_price_and_aggression_reject"

    balanced = (
        0.55 <= float(row["inside1_600"]) <= 0.80
        and abs(float(row["slope_sigma_600"])) < 0.75
        and 0.67 <= float(row["sigma_expand_600"]) <= 1.50
    )
    if balanced and 1.0 <= abs(z) <= 1.8 and abs(float(row["ret_300"])) <= 8.0:
        return "balanced_reversion", -edge, "stable_value_area_edge_without_directional_displacement"

    return "skip", 0, "auction_state_unconfirmed"


def wilson_interval(wins: int, trades: int, z_value: float = 1.96) -> tuple[float | None, float | None]:
    if trades <= 0:
        return None, None
    probability = wins / trades
    denominator = 1.0 + z_value**2 / trades
    center = (probability + z_value**2 / (2.0 * trades)) / denominator
    margin = z_value * math.sqrt(
        probability * (1.0 - probability) / trades + z_value**2 / (4.0 * trades**2)
    ) / denominator
    return round((center - margin) * 100.0, 2), round((center + margin) * 100.0, 2)


def metrics(frame: pd.DataFrame, won_column: str) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0, "winRate": None, "wilson95": [None, None], "pnl5U": 0.0}
    wins = frame[won_column].astype(bool)
    pnl = np.where(wins, 4.0, -5.0)
    equity = np.cumsum(pnl)
    prior_peak = np.maximum.accumulate(np.r_[0.0, equity])[:-1]
    loss_streak = maximum = 0
    for won in wins:
        loss_streak = 0 if won else loss_streak + 1
        maximum = max(maximum, loss_streak)
    lower, upper = wilson_interval(int(wins.sum()), len(frame))
    return {
        "trades": int(len(frame)),
        "wins": int(wins.sum()),
        "winRate": round(float(wins.mean()) * 100.0, 2),
        "wilson95": [lower, upper],
        "pnl5U": round(float(pnl.sum()), 2),
        "maxDrawdown5U": round(float(np.max(np.maximum(0.0, prior_peak - equity))), 2),
        "maxLossStreak": int(maximum),
    }


def main() -> None:
    events = pd.read_csv(EVENTS, parse_dates=["time", "entry_time", "settle_time"])
    decisions = events.apply(classify, axis=1, result_type="expand")
    decisions.columns = ["branch", "direction_sign", "reason"]
    data = pd.concat([events, decisions], axis=1)
    trades = data[data["direction_sign"] != 0].copy()
    trades["signal"] = np.where(trades["direction_sign"] > 0, "UP", "DOWN")
    trades["local_day"] = trades["time"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    for delay in DELAYS:
        actual_up = trades[f"up_d{delay}"].astype(bool)
        trades[f"won_d{delay}"] = actual_up == (trades["direction_sign"] > 0)
        trades[f"signed_bps_d{delay}"] = trades[f"raw_move_bps_d{delay}"] * trades["direction_sign"]

    report: dict[str, Any] = {
        "method": {
            "sample": "Fixed UTC ten-minute grid with non-overlapping ten-minute labels.",
            "parameterSearch": False,
            "branches": {
                "accepted_migration": "Follow only when price is outside old value and 60s price, taker flow and book align.",
                "failed_auction": "Fade only after outside price reverses with opposing 10s aggression and microprice.",
                "balanced_reversion": "Fade a 1.0-1.8 sigma edge only in a stable, nearly flat value area.",
            },
            "breakEvenWinRate": 55.56,
            "warning": "July 14-15 is now inspected explanatory data, not an untouched holdout.",
        },
        "sample": {
            "events": int(len(events)),
            "trades": int(len(trades)),
            "start": events["time"].min(),
            "end": events["time"].max(),
        },
        "delaySensitivity": {
            str(delay): metrics(trades, f"won_d{delay}") for delay in DELAYS
        },
        "byBranchAt5Sec": {
            str(branch): metrics(group, "won_d5") for branch, group in trades.groupby("branch")
        },
        "byDayAt5Sec": {
            str(day): metrics(group, "won_d5") for day, group in trades.groupby("local_day")
        },
        "byDayAndBranchAt5Sec": {
            f"{day}|{branch}": metrics(group, "won_d5")
            for (day, branch), group in trades.groupby(["local_day", "branch"])
        },
    }
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
