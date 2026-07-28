"""Walk-forward direction test using slow positioning and minute context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline


ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "tmp" / "minute_context_events_10m.csv"
OUT_JSON = ROOT / "tmp" / "minute_context_walkforward_v1_latest.json"
OUT_CSV = ROOT / "tmp" / "minute_context_walkforward_v1_predictions.csv"
TRAIN_MIN = 2016  # 14 days of non-overlapping ten-minute samples.
TRAIN_MAX = 4320  # Rolling 30-day adaptation window.
REFIT_EVERY = 144
PURGE_SAMPLES = 2
GATE = 0.62
AMOUNT_U = 5.0
PAYOUT_RATE = 0.8


def make_model() -> Pipeline:
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("model", HistGradientBoostingClassifier(
            learning_rate=0.04,
            max_iter=120,
            max_leaf_nodes=7,
            min_samples_leaf=60,
            l2_regularization=3.0,
            random_state=31,
        )),
    ])


def metrics(frame: pd.DataFrame) -> dict[str, Any]:
    trades = frame[frame.signal.isin(["UP", "DOWN"])].copy()
    if trades.empty:
        return {"trades": 0, "wins": 0, "winRate": 0.0, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0}
    direction = np.where(trades.signal == "UP", 1.0, -1.0)
    signed = trades.raw_move_bps.to_numpy(float) * direction
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
    }


def main() -> None:
    data = pd.read_csv(EVENTS, parse_dates=["time"]).sort_values("time").reset_index(drop=True)
    excluded = {"time", "entry", "settle", "raw_move_bps", "up"}
    features = [name for name in data.columns if name not in excluded and pd.api.types.is_numeric_dtype(data[name])]
    batches: list[pd.DataFrame] = []
    for start in range(TRAIN_MIN, len(data), REFIT_EVERY):
        train_end = start - PURGE_SAMPLES
        train_start = max(0, train_end - TRAIN_MAX)
        test_end = min(len(data), start + REFIT_EVERY)
        train = data.iloc[train_start:train_end]
        test = data.iloc[start:test_end].copy()
        if test.empty or train.up.nunique() < 2:
            continue
        model = make_model()
        model.fit(train[features], train.up)
        probability = model.predict_proba(test[features])[:, 1]
        test["prob_up"] = probability
        test["confidence"] = np.maximum(probability, 1.0 - probability)
        test["signal"] = np.where(probability >= GATE, "UP", np.where(probability <= 1.0 - GATE, "DOWN", None))
        test["train_start_time"] = train.time.iloc[0]
        test["train_end_time"] = train.time.iloc[-1]
        batches.append(test)
    result = pd.concat(batches, ignore_index=True) if batches else pd.DataFrame()
    result["month"] = result.time.dt.strftime("%Y-%m")
    result["day"] = result.time.dt.strftime("%Y-%m-%d")
    report = {
        "method": {
            "parameterSearch": False,
            "trainMin": TRAIN_MIN,
            "trainMax": TRAIN_MAX,
            "refitEvery": REFIT_EVERY,
            "purgeSamples": PURGE_SAMPLES,
            "gate": GATE,
            "features": features,
            "warning": "This tests slow-context information with minute closes, not real execution PnL.",
        },
        "coverage": {
            "predictions": len(result),
            "trades": int(result.signal.isin(["UP", "DOWN"]).sum()),
            "start": result.time.min().isoformat() if not result.empty else None,
            "end": result.time.max().isoformat() if not result.empty else None,
        },
        "overall": metrics(result),
        "months": {month: metrics(group) for month, group in result.groupby("month")},
        "days": {day: metrics(group) for day, group in result.groupby("day")},
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    result.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps({k: v for k, v in report.items() if k != "days"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
