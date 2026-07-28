"""Fixed auction-model consensus with chronological and forward validation.

This file performs no parameter search.  Model structure and the 60% admission
confidence are fixed before the July 14-15 forward segment is scored.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "tmp" / "unified_auction_events_10m.csv"
FORWARD = ROOT / "tmp" / "frozen_position_forward" / "events_10m.csv"
OUT_JSON = ROOT / "tmp" / "auction_consensus_holdout_v1.json"
OUT_CSV = ROOT / "tmp" / "auction_consensus_holdout_v1_trades.csv"
DELAYS = (0, 5, 6, 10)
CONFIDENCE = 0.60

FEATURES = (
    "ret_10", "ret_30", "ret_60", "ret_120", "ret_300", "ret_600",
    "z_60", "z_120", "z_300", "z_600",
    "inside1_120", "inside1_300", "inside1_600",
    "skew_120", "skew_300", "skew_600",
    "kurtosis_120", "kurtosis_300", "kurtosis_600",
    "slope_sigma_120", "slope_sigma_300", "slope_sigma_600",
    "sigma_expand_120", "sigma_expand_300", "sigma_expand_600",
    "flow_10", "flow_60", "flow_300",
    "imbalance_10", "imbalance_60", "imbalance_300",
    "micro_10", "micro_60", "spread_60",
    "depth_imbalance_60", "bid_depth_change_60", "ask_depth_change_60",
    "volume_ratio_60", "trend_strength_600", "speed_ratio_60_600",
    "flow_price_alignment_60", "book_flow_alignment_60",
)


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


def load(path: Path, segment: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame["segment"] = segment
    for column in ("time", "entry_time", "settle_time"):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    numeric = [*FEATURES, *[f"raw_move_bps_d{delay}" for delay in DELAYS]]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=["time", *numeric])
    frame["label"] = (frame["raw_move_bps_d0"] > 0.0).astype(int)
    return frame.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)


def models() -> tuple[Any, Any]:
    linear = make_pipeline(
        StandardScaler(),
        LogisticRegression(C=0.25, max_iter=1000, class_weight="balanced", random_state=23),
    )
    nonlinear = HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_iter=100,
        max_leaf_nodes=7,
        min_samples_leaf=30,
        l2_regularization=2.0,
        random_state=23,
    )
    return linear, nonlinear


def predict(train: pd.DataFrame, test: pd.DataFrame, fold: str) -> pd.DataFrame:
    if len(train) < 300 or test.empty:
        return pd.DataFrame()
    linear, nonlinear = models()
    x_train = train.loc[:, FEATURES].to_numpy(float)
    y_train = train["label"].to_numpy(int)
    x_test = test.loc[:, FEATURES].to_numpy(float)
    linear.fit(x_train, y_train)
    nonlinear.fit(x_train, y_train)
    linear_up = linear.predict_proba(x_test)[:, 1]
    nonlinear_up = nonlinear.predict_proba(x_test)[:, 1]
    linear_side = linear_up >= 0.5
    nonlinear_side = nonlinear_up >= 0.5
    up_probability = (linear_up + nonlinear_up) / 2.0
    confidence = np.maximum(up_probability, 1.0 - up_probability)
    admitted = (linear_side == nonlinear_side) & (confidence >= CONFIDENCE)
    result = test.loc[admitted].copy()
    result["signal"] = np.where(up_probability[admitted] >= 0.5, "UP", "DOWN")
    result["confidence"] = confidence[admitted]
    result["linear_up_probability"] = linear_up[admitted]
    result["nonlinear_up_probability"] = nonlinear_up[admitted]
    result["fold"] = fold
    result["train_end"] = train["time"].max()
    result["train_rows"] = len(train)
    return result


def maximum_loss_streak(wins: np.ndarray) -> int:
    current = maximum = 0
    for won in wins:
        current = 0 if won else current + 1
        maximum = max(maximum, current)
    return maximum


def metrics(frame: pd.DataFrame, delay: int, hours: float | None = None) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": 0.0, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0, "tradesPerDay": 0.0}
    direction = np.where(frame.signal.eq("UP"), 1.0, -1.0)
    signed = frame[f"raw_move_bps_d{delay}"].to_numpy(float) * direction
    wins = signed > 0.0
    pnl = np.where(wins, 4.0, -5.0)
    equity = np.cumsum(pnl)
    equity_path = np.r_[0.0, equity]
    drawdown = np.maximum.accumulate(equity_path) - equity_path
    if hours is None:
        hours = max((frame.time.max() - frame.time.min()).total_seconds() / 3600.0, 1 / 6)
    return {
        "trades": int(len(frame)),
        "wins": int(wins.sum()),
        "winRate": round(float(wins.mean()) * 100.0, 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float(drawdown.max()), 2),
        "maxLossStreak": maximum_loss_streak(wins),
        "tradesPerDay": round(len(frame) / max(hours / 24.0, 1e-9), 2),
        "medianSignedBps": round(float(np.median(signed)), 3),
    }


def run() -> dict[str, Any]:
    history = load(HISTORY, "history")
    forward = load(FORWARD, "forward")
    folds: list[pd.DataFrame] = []
    boundaries = (0.55, 0.70, 0.85, 1.0)
    previous = 0.40
    for number, boundary in enumerate(boundaries, start=1):
        train_end = int(len(history) * previous)
        test_end = int(len(history) * boundary)
        fold = predict(history.iloc[:train_end], history.iloc[train_end:test_end], f"history_{number}")
        if not fold.empty:
            folds.append(fold)
        previous = boundary
    forward_trades = predict(history, forward, "forward_frozen")
    all_trades = pd.concat([*folds, forward_trades], ignore_index=True) if folds or not forward_trades.empty else pd.DataFrame()
    if not all_trades.empty:
        all_trades["beijing_day"] = all_trades.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
        all_trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    history_hours = (history.time.max() - history.time.min()).total_seconds() / 3600.0
    forward_hours = (forward.time.max() - forward.time.min()).total_seconds() / 3600.0
    report = {
        "method": {
            "parameterSearch": False,
            "causalFeatures": True,
            "models": "regularized logistic + shallow histogram gradient boosting",
            "admission": f"models agree and mean confidence >= {CONFIDENCE}",
            "features": list(FEATURES),
            "forwardModelFrozen": True,
        },
        "data": {
            "historyStart": history.time.min(), "historyEnd": history.time.max(), "historyRows": len(history),
            "forwardStart": forward.time.min(), "forwardEnd": forward.time.max(), "forwardRows": len(forward),
        },
        "historyWalkForward": {
            f"delay{delay}s": metrics(pd.concat(folds, ignore_index=True) if folds else pd.DataFrame(), delay, history_hours * 0.60)
            for delay in DELAYS
        },
        "forward": {f"delay{delay}s": metrics(forward_trades, delay, forward_hours) for delay in DELAYS},
        "forwardByDayDelay6s": {
            day: metrics(group, 6) for day, group in forward_trades.assign(
                beijing_day=forward_trades.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
            ).groupby("beijing_day")
        } if not forward_trades.empty else {},
        "acceptance": {
            "minTradesPerDay": 10.0,
            "minForwardWinRate": 55.56,
            "maxDrawdownU": 20.0,
            "maxLossStreak": 3,
            "allDelaysMustBeProfitable": True,
        },
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
