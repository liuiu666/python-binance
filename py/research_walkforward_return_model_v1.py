"""Causal walk-forward regression of the next ten-minute BTC return."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline


ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "tmp" / "unified_auction_events_10m.csv"
OUT_JSON = ROOT / "tmp" / "walkforward_return_model_v1_latest.json"
OUT_CSV = ROOT / "tmp" / "walkforward_return_model_v1_predictions.csv"
DELAYS = (0, 5, 6, 10)
TRAIN_MIN = 300
REFIT_EVERY = 36
PURGE_SAMPLES = 2
MIN_EXPECTED_MOVE_BPS = 5.0
TARGET_CLIP_BPS = 50.0
AMOUNT_U = 5.0
PAYOUT_RATE = 0.8


def feature_columns(data: pd.DataFrame) -> list[str]:
    excluded_prefixes = ("entry", "settle", "up_d", "raw_move_bps_d", "future_")
    excluded = {
        "source", "role", "time", "validation_status", "up", "raw_move_bps",
        "observed_pct_600", "orderbook_pct_600",
    }
    return [
        name for name in data.columns
        if name not in excluded
        and not name.startswith(excluded_prefixes)
        and pd.api.types.is_numeric_dtype(data[name])
    ]


def make_model() -> Pipeline:
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("model", HistGradientBoostingRegressor(
            loss="absolute_error",
            learning_rate=0.04,
            max_iter=120,
            max_leaf_nodes=7,
            min_samples_leaf=30,
            l2_regularization=3.0,
            random_state=23,
        )),
    ])


def metrics(frame: pd.DataFrame, delay: int) -> dict[str, Any]:
    trades = frame[frame.signal.isin(["UP", "DOWN"])].copy()
    if trades.empty:
        return {"trades": 0, "wins": 0, "winRate": 0.0, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0}
    direction = np.where(trades.signal == "UP", 1.0, -1.0)
    signed = trades[f"raw_move_bps_d{delay}"].to_numpy(float) * direction
    won = signed > 0.0
    pnl = np.where(won, AMOUNT_U * PAYOUT_RATE, -AMOUNT_U)
    equity = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0.0, equity])[:-1]
    streak = maximum = 0
    for item in won:
        streak = 0 if item else streak + 1
        maximum = max(maximum, streak)
    return {
        "trades": int(len(trades)),
        "wins": int(won.sum()),
        "winRate": round(float(won.mean()) * 100.0, 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float((peak - equity).max()), 2),
        "maxLossStreak": maximum,
        "medianSignedBps": round(float(np.median(signed)), 4),
        "meanPredictedAbsBps": round(float(trades.predicted_move_bps.abs().mean()), 4),
        "thinMarginPctLe3bp": round(float(np.mean(np.abs(signed) <= 3.0)) * 100.0, 2),
    }


def main() -> None:
    data = pd.read_csv(EVENTS, parse_dates=["time", "entry_time", "settle_time"]).sort_values("time").reset_index(drop=True)
    features = feature_columns(data)
    data["target_move_bps"] = data["raw_move_bps_d5"].clip(-TARGET_CLIP_BPS, TARGET_CLIP_BPS)
    predictions: list[pd.DataFrame] = []
    for batch_start in range(TRAIN_MIN, len(data), REFIT_EVERY):
        train_end = batch_start - PURGE_SAMPLES
        batch_end = min(len(data), batch_start + REFIT_EVERY)
        train = data.iloc[:train_end]
        test = data.iloc[batch_start:batch_end].copy()
        if test.empty:
            continue
        model = make_model()
        model.fit(train[features], train["target_move_bps"])
        predicted = model.predict(test[features])
        test["predicted_move_bps"] = predicted
        test["signal"] = np.where(
            predicted >= MIN_EXPECTED_MOVE_BPS,
            "UP",
            np.where(predicted <= -MIN_EXPECTED_MOVE_BPS, "DOWN", None),
        )
        test["train_rows"] = len(train)
        test["train_end_time"] = train["time"].iloc[-1]
        predictions.append(test)
    result = pd.concat(predictions, ignore_index=True) if predictions else pd.DataFrame()
    result["day"] = result["time"].dt.strftime("%Y-%m-%d")
    report = {
        "method": {
            "parameterSearch": False,
            "target": "Clipped next-ten-minute signed return in basis points.",
            "targetClipBps": TARGET_CLIP_BPS,
            "minExpectedMoveBps": MIN_EXPECTED_MOVE_BPS,
            "trainMinSamples": TRAIN_MIN,
            "refitEverySamples": REFIT_EVERY,
            "purgeSamples": PURGE_SAMPLES,
            "features": features,
            "validationWarning": "All dates are already inspected; a new frozen future period is still required.",
        },
        "coverage": {
            "predictions": len(result),
            "trades": int(result.signal.isin(["UP", "DOWN"]).sum()),
            "start": result.time.min().isoformat() if not result.empty else None,
            "end": result.time.max().isoformat() if not result.empty else None,
        },
        "overall": {f"delay{delay}s": metrics(result, delay) for delay in DELAYS},
        "rolesDelay6s": {role: metrics(group, 6) for role, group in result.groupby("role")},
        "daysDelay6s": {day: metrics(group, 6) for day, group in result.groupby("day")},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    result.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
