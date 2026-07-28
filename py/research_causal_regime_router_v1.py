"""Fixed, causal regime router evaluated under several execution delays.

No threshold search is performed.  Regimes use only distribution shape,
multi-scale slope and current auction alignment.  Transition states abstain.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "tmp" / "unified_auction_events_10m.csv"
OUT_JSON = ROOT / "tmp" / "causal_regime_router_v1_latest.json"
OUT_TRADES = ROOT / "tmp" / "causal_regime_router_v1_trades.csv"
DELAYS = (0, 5, 6, 10)
AMOUNT_U = 5.0
PAYOUT_RATE = 0.8


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def classify(row: pd.Series) -> str:
    required = [
        "inside1_600", "skew_600", "kurtosis_600", "slope_sigma_120",
        "slope_sigma_300", "slope_sigma_600", "sigma_expand_600",
    ]
    if not all(finite(row.get(name)) for name in required):
        return "unknown"
    normal = (
        0.55 <= row.inside1_600 <= 0.80
        and abs(row.skew_600) <= 0.75
        and abs(row.kurtosis_600) <= 1.50
        and abs(row.slope_sigma_600) < 0.75
        and 0.67 <= row.sigma_expand_600 <= 1.50
    )
    if normal:
        return "normal"
    slope_signs = [int(np.sign(row[f"slope_sigma_{width}"])) for width in (120, 300, 600)]
    meaningful = [abs(row[f"slope_sigma_{width}"]) >= 0.50 for width in (120, 300, 600)]
    if row.sigma_expand_600 > 1.50 or (all(meaningful) and len(set(slope_signs)) > 1):
        return "transition"
    if row.slope_sigma_300 >= 0.75 and row.slope_sigma_600 >= 0.75:
        return "trend_up"
    if row.slope_sigma_300 <= -0.75 and row.slope_sigma_600 <= -0.75:
        return "trend_down"
    return "distorted"


def decide(row: pd.Series) -> tuple[str | None, str]:
    regime = str(row.regime)
    if regime == "normal":
        if row.z_600 >= 1.0 and row.z_120 >= 0.5:
            return "DOWN", "normal_upper_reversion"
        if row.z_600 <= -1.0 and row.z_120 <= -0.5:
            return "UP", "normal_lower_reversion"
        return None, "normal_waiting_tail"
    if regime in {"trend_up", "trend_down"}:
        direction = 1 if regime == "trend_up" else -1
        aligned_votes = sum([
            direction * row.flow_60 > 0.0,
            direction * row.imbalance_60 > 0.0,
            direction * row.micro_60 > 0.0,
        ])
        ret_aligned = direction * row.ret_60 > 0.0 and direction * row.ret_300 > 0.0
        if ret_aligned and aligned_votes >= 2 and row.volume_ratio_60 >= 0.8:
            return ("UP" if direction > 0 else "DOWN"), "mature_trend_auction_follow"
        return None, "trend_waiting_auction_alignment"
    if regime == "transition":
        return None, "transition_abstain"
    return None, "distorted_abstain"


def metrics(frame: pd.DataFrame, delay: int) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": 0.0, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0}
    direction = np.where(frame.signal == "UP", 1.0, -1.0)
    signed = frame[f"raw_move_bps_d{delay}"].to_numpy(float) * direction
    wins = signed > 0.0
    pnl = np.where(wins, AMOUNT_U * PAYOUT_RATE, -AMOUNT_U)
    equity = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0.0, equity])[:-1]
    streak = maximum = 0
    for won in wins:
        streak = 0 if won else streak + 1
        maximum = max(maximum, streak)
    return {
        "trades": int(len(frame)),
        "wins": int(wins.sum()),
        "winRate": round(float(wins.mean()) * 100.0, 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float((peak - equity).max()), 2),
        "maxLossStreak": maximum,
        "medianSignedBps": round(float(np.median(signed)), 4),
        "thinMarginPctLe3bp": round(float(np.mean(np.abs(signed) <= 3.0)) * 100.0, 2),
    }


def state_runs(frame: pd.DataFrame) -> dict[str, Any]:
    runs: list[tuple[str, int]] = []
    state = None
    count = 0
    previous_time: pd.Timestamp | None = None
    previous_source = None
    for row in frame.sort_values("time").itertuples():
        timestamp = pd.Timestamp(row.time)
        reset = previous_time is None or row.source != previous_source or (timestamp - previous_time).total_seconds() > 900
        if reset or row.regime != state:
            if state is not None:
                runs.append((state, count))
            state, count = row.regime, 1
        else:
            count += 1
        previous_time, previous_source = timestamp, row.source
    if state is not None:
        runs.append((state, count))
    result: dict[str, Any] = {}
    for regime in sorted(set(name for name, _ in runs)):
        lengths = np.array([length for name, length in runs if name == regime], dtype=float) * 10.0
        result[regime] = {
            "runs": int(len(lengths)),
            "medianMinutes": round(float(np.median(lengths)), 2),
            "p90Minutes": round(float(np.quantile(lengths, 0.9)), 2),
            "maxMinutes": round(float(lengths.max()), 2),
        }
    return result


def main() -> None:
    data = pd.read_csv(EVENTS, parse_dates=["time", "entry_time", "settle_time"])
    data["regime"] = data.apply(classify, axis=1)
    decisions = data.apply(decide, axis=1, result_type="expand")
    decisions.columns = ["signal", "reason"]
    data = pd.concat([data, decisions], axis=1)
    trades = data[data.signal.isin(["UP", "DOWN"])].copy()
    report = {
        "method": {
            "parameterSearch": False,
            "causal": True,
            "rule": "Normal tails revert; aligned mature trends follow; transitions and distorted shapes abstain.",
            "delaysSec": DELAYS,
            "validationWarning": "Non-history roles were previously inspected and are reused validation, not untouched forward evidence.",
        },
        "states": {
            "counts": data.groupby(["role", "regime"]).size().unstack(fill_value=0).to_dict(orient="index"),
            "persistence": state_runs(data),
        },
        "roles": {
            role: {f"delay{delay}s": metrics(group, delay) for delay in DELAYS}
            for role, group in trades.groupby("role")
        },
        "overall": {f"delay{delay}s": metrics(trades, delay) for delay in DELAYS},
        "byReasonDelay6s": {
            reason: metrics(group, 6) for reason, group in trades.groupby("reason")
        },
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
