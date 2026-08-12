from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def action_values(p_up: np.ndarray, payout_rate: float) -> tuple[np.ndarray, np.ndarray]:
    probability = np.asarray(p_up, dtype=np.float64)
    if not np.isfinite(probability).all() or ((probability < 0.0) | (probability > 1.0)).any():
        raise ValueError("p_up must contain finite probabilities in [0, 1]")
    payout = float(payout_rate)
    return probability * payout - (1.0 - probability), (1.0 - probability) * payout - probability


def plan_actions(p_up: np.ndarray, *, payout_rate: float, min_ev: float) -> pd.DataFrame:
    probability = np.asarray(p_up, dtype=np.float64)
    up_ev, down_ev = action_values(probability, payout_rate)
    best = np.maximum(up_ev, down_ev)
    signal = np.where(best < float(min_ev), "SKIP", np.where(up_ev >= down_ev, "UP", "DOWN"))
    return pd.DataFrame(
        {
            "p_up": probability,
            "confidence": np.maximum(probability, 1.0 - probability),
            "ev_up": up_ev,
            "ev_down": down_ev,
            "best_ev": best,
            "signal": signal,
        }
    )


def wilson_interval(wins: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    probability = wins / total
    denominator = 1.0 + z * z / total
    center = (probability + z * z / (2.0 * total)) / denominator
    half = z * math.sqrt(probability * (1.0 - probability) / total + z * z / (4.0 * total * total)) / denominator
    return center - half, center + half


def expected_calibration_error(labels: np.ndarray, probabilities: np.ndarray, bins: int = 10) -> float | None:
    y = np.asarray(labels, dtype=np.float64)
    p = np.asarray(probabilities, dtype=np.float64)
    if not len(y):
        return None
    edges = np.linspace(0.0, 1.0, int(bins) + 1)
    value = 0.0
    for index in range(len(edges) - 1):
        mask = (p >= edges[index]) & (p < edges[index + 1] if index < len(edges) - 2 else p <= edges[index + 1])
        if mask.any():
            value += float(mask.mean()) * abs(float(y[mask].mean()) - float(p[mask].mean()))
    return value


def _max_loss_streak(won: np.ndarray) -> int:
    best = current = 0
    for item in won:
        current = 0 if bool(item) else current + 1
        best = max(best, current)
    return best


def _probability_metrics(labels: np.ndarray, probabilities: np.ndarray) -> dict[str, float | None]:
    if not len(labels):
        return {"brier": None, "logLoss": None, "ece": None}
    y = np.asarray(labels, dtype=np.float64)
    p = np.clip(np.asarray(probabilities, dtype=np.float64), 1e-6, 1.0 - 1e-6)
    brier = np.mean((p - y) ** 2)
    log_loss = -np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))
    return {
        "brier": round(float(brier), 8),
        "logLoss": round(float(log_loss), 8),
        "ece": round(float(expected_calibration_error(y, p) or 0.0), 8),
    }


def evaluate_predictions(frame: pd.DataFrame, *, payout_rate: float, stake: float) -> dict[str, Any]:
    required = {"time", "signal", "tie", "up", "p_up"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"prediction frame missing columns: {sorted(missing)}")
    ordered = frame.sort_values("time").copy()
    if not ordered.empty:
        ordered["time"] = pd.to_datetime(ordered["time"], utc=True)
    non_ties = ordered[~ordered["tie"].astype(bool) & ordered["p_up"].notna()]
    scored = non_ties[non_ties["signal"].isin(["UP", "DOWN"])].copy()

    if scored.empty:
        result: dict[str, Any] = {
            "trades": 0,
            "wins": 0,
            "winRate": None,
            "coverage": 0.0,
            "pnl": 0.0,
            "returnOnStake": 0.0,
            "maxDrawdown": 0.0,
            "maxLossStreak": 0,
            "tradesPerDay": 0.0,
            "winRate95": [None, None],
            "averageSelectedEv": None,
        }
    else:
        won = ((scored["signal"] == "UP") == scored["up"].astype(bool)).to_numpy(bool)
        pnl = np.where(won, float(stake) * float(payout_rate), -float(stake))
        equity = np.cumsum(pnl)
        running_peak = np.maximum.accumulate(np.r_[0.0, equity])[1:]
        if len(ordered) > 1:
            elapsed_days = max((ordered["time"].max() - ordered["time"].min()).total_seconds() / 86400.0, 1.0)
        else:
            elapsed_days = 1.0
        low, high = wilson_interval(int(won.sum()), len(won))
        selected_ev = np.where(scored["signal"].to_numpy() == "UP", scored["ev_up"], scored["ev_down"])
        result = {
            "trades": int(len(won)),
            "wins": int(won.sum()),
            "winRate": round(float(won.mean()) * 100.0, 4),
            "coverage": round(float(len(scored) / max(len(non_ties), 1)) * 100.0, 4),
            "pnl": round(float(pnl.sum()), 4),
            "returnOnStake": round(float(pnl.sum() / (len(pnl) * float(stake))) * 100.0, 4),
            "maxDrawdown": round(float((running_peak - equity).max()), 4),
            "maxLossStreak": _max_loss_streak(won),
            "tradesPerDay": round(float(len(won) / elapsed_days), 4),
            "winRate95": [round(float(low) * 100.0, 4), round(float(high) * 100.0, 4)],
            "averageSelectedEv": round(float(np.mean(selected_ev)), 8),
        }
    result.update(_probability_metrics(non_ties["up"].astype(int).to_numpy(), non_ties["p_up"].to_numpy()))

    monthly: dict[str, Any] = {}
    all_months = sorted(ordered["time"].dt.strftime("%Y-%m").unique()) if not ordered.empty else []
    for month in all_months:
        month_rows = scored[scored["time"].dt.strftime("%Y-%m") == month]
        if month_rows.empty:
            monthly[str(month)] = {"trades": 0, "wins": 0, "winRate": None, "pnl": 0.0}
            continue
        won = ((month_rows["signal"] == "UP") == month_rows["up"].astype(bool)).to_numpy(bool)
        pnl = np.where(won, float(stake) * float(payout_rate), -float(stake))
        monthly[str(month)] = {
            "trades": int(len(won)),
            "wins": int(won.sum()),
            "winRate": round(float(won.mean()) * 100.0, 4),
            "pnl": round(float(pnl.sum()), 4),
        }
    result["byMonth"] = monthly
    result["positiveMonths"] = sum(row["pnl"] > 0 for row in monthly.values())
    result["months"] = len(monthly)
    return result
