from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss


def wilson_interval(wins: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = wins / total
    denom = 1.0 + z * z / total
    center = (p + z * z / (2.0 * total)) / denom
    half = z * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total)) / denom
    return center - half, center + half


def expected_calibration_error(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float | None:
    if len(y) == 0:
        return None
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = len(y)
    value = 0.0
    for i in range(bins):
        mask = (p >= edges[i]) & (p < edges[i + 1] if i < bins - 1 else p <= edges[i + 1])
        if mask.any():
            value += mask.mean() * abs(float(y[mask].mean()) - float(p[mask].mean()))
    return float(value)


def _max_loss_streak(won: np.ndarray) -> int:
    best = current = 0
    for item in won:
        current = 0 if bool(item) else current + 1
        best = max(best, current)
    return best


def evaluate_predictions(frame: pd.DataFrame, payout_rate: float, stake: float) -> dict[str, Any]:
    ordered = frame.sort_values("time").copy()
    scored = ordered[ordered["signal"].isin(["UP", "DOWN"]) & ~ordered["tie"].astype(bool)].copy()
    probability_rows = ordered[~ordered["tie"].astype(bool) & ordered["p_up"].notna()]
    if scored.empty:
        base = {"trades": 0, "wins": 0, "winRate": None, "pnl": 0.0, "maxDrawdown": 0.0,
                "maxLossStreak": 0, "tradesPerDay": 0.0, "winRate95": [None, None]}
    else:
        won = ((scored["signal"] == "UP") == scored["up"].astype(bool)).to_numpy(bool)
        pnl = np.where(won, stake * payout_rate, -stake)
        equity = np.cumsum(pnl)
        peak = np.maximum.accumulate(np.r_[0.0, equity])[:-1]
        elapsed_days = max((ordered["time"].max() - ordered["time"].min()).total_seconds() / 86400.0, 1.0)
        low, high = wilson_interval(int(won.sum()), len(won))
        base = {
            "trades": int(len(won)), "wins": int(won.sum()), "winRate": round(float(won.mean()) * 100.0, 4),
            "pnl": round(float(pnl.sum()), 4), "maxDrawdown": round(float((peak - equity).max()), 4),
            "maxLossStreak": _max_loss_streak(won), "tradesPerDay": round(len(won) / elapsed_days, 4),
            "winRate95": [round(low * 100.0, 4), round(high * 100.0, 4)],
        }
    if probability_rows.empty:
        base.update({"brier": None, "logLoss": None, "ece": None})
    else:
        y = probability_rows["up"].astype(int).to_numpy()
        p = np.clip(probability_rows["p_up"].astype(float).to_numpy(), 1e-6, 1.0 - 1e-6)
        base.update({"brier": round(float(brier_score_loss(y, p)), 8),
                     "logLoss": round(float(log_loss(y, p, labels=[0, 1])), 8),
                     "ece": round(float(expected_calibration_error(y, p) or 0.0), 8)})
    monthly: dict[str, Any] = {}
    all_months = sorted(ordered["time"].dt.strftime("%Y-%m").unique()) if not ordered.empty else []
    scored_month = scored["time"].dt.strftime("%Y-%m") if not scored.empty else pd.Series(dtype=str)
    for month in all_months:
        part = scored.loc[scored_month == month]
        if part.empty:
            monthly[str(month)] = {"trades": 0, "winRate": None, "pnl": 0.0}
            continue
        won = ((part["signal"] == "UP") == part["up"].astype(bool)).to_numpy(bool)
        monthly[str(month)] = {"trades": int(len(won)), "winRate": round(float(won.mean()) * 100.0, 4),
                               "pnl": round(float(np.where(won, stake * payout_rate, -stake).sum()), 4)}
    base["byMonth"] = monthly
    base["positiveMonths"] = sum(1 for row in monthly.values() if row["pnl"] > 0)
    base["months"] = len(monthly)
    return base


def success_assessment(world: dict[str, Any], logistic: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "winRate": world.get("winRate") is not None and world["winRate"] >= cfg["min_win_rate_pct"],
        "trades": world["trades"] >= cfg["min_trades"],
        "pnl": world["pnl"] > cfg["min_pnl"],
        "majorityPositiveMonths": world["months"] > 0 and world["positiveMonths"] > world["months"] / 2,
        "beatsLogistic": (
            world.get("winRate") is not None and logistic.get("winRate") is not None
            and world["winRate"] > logistic["winRate"] and world["pnl"] > logistic["pnl"]
        ),
    }
    if not cfg.get("require_majority_positive_months", True):
        checks["majorityPositiveMonths"] = True
    if not cfg.get("require_world_model_beats_logistic", True):
        checks["beatsLogistic"] = True
    return {"passed": all(checks.values()), "checks": checks}
