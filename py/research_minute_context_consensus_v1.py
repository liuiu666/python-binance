"""Causal calibrated consensus across three different model families."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "tmp" / "minute_context_events_10m.csv"
OUT_JSON = ROOT / "tmp" / "minute_context_consensus_v1_latest.json"
OUT_CSV = ROOT / "tmp" / "minute_context_consensus_v1_predictions.csv"
TRAIN_MIN = 2016
TRAIN_MAX = 4320
REFIT_EVERY = 144
PURGE_SAMPLES = 2
CALIBRATION_SAMPLES = 1008  # Seven days.
BREAKEVEN_PROBABILITY = 5.0 / 9.0
AMOUNT_U = 5.0
PAYOUT_RATE = 0.8


def models() -> dict[str, Pipeline]:
    return {
        "linear": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("model", LogisticRegression(C=0.1, max_iter=2000, random_state=41)),
        ]),
        "boosted": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", HistGradientBoostingClassifier(
                learning_rate=0.04, max_iter=120, max_leaf_nodes=7,
                min_samples_leaf=60, l2_regularization=3.0, random_state=43,
            )),
        ]),
        "forest": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", RandomForestClassifier(
                n_estimators=160, max_depth=5, min_samples_leaf=40,
                max_features="sqrt", n_jobs=-1, random_state=47,
            )),
        ]),
    }


def platt(raw: np.ndarray, labels: pd.Series, test_raw: np.ndarray) -> np.ndarray:
    eps = 1e-6
    logits = np.log(np.clip(raw, eps, 1.0 - eps) / np.clip(1.0 - raw, eps, 1.0 - eps)).reshape(-1, 1)
    test_logits = np.log(np.clip(test_raw, eps, 1.0 - eps) / np.clip(1.0 - test_raw, eps, 1.0 - eps)).reshape(-1, 1)
    calibrator = LogisticRegression(C=1.0, max_iter=1000, random_state=53)
    calibrator.fit(logits, labels)
    return calibrator.predict_proba(test_logits)[:, 1]


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
        "trades": int(len(trades)), "wins": int(won.sum()),
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
        calibration_start = max(train_start + 720, train_end - CALIBRATION_SAMPLES)
        fit = data.iloc[train_start:calibration_start]
        calibration = data.iloc[calibration_start:train_end]
        test = data.iloc[start:min(len(data), start + REFIT_EVERY)].copy()
        if len(fit) < 720 or len(calibration) < 288 or test.empty:
            continue
        probabilities: dict[str, np.ndarray] = {}
        for name, model in models().items():
            model.fit(fit[features], fit.up)
            calibration_raw = model.predict_proba(calibration[features])[:, 1]
            test_raw = model.predict_proba(test[features])[:, 1]
            probabilities[name] = platt(calibration_raw, calibration.up, test_raw)
            test[f"prob_{name}"] = probabilities[name]
        matrix = np.column_stack(list(probabilities.values()))
        all_up = np.all(matrix >= BREAKEVEN_PROBABILITY, axis=1)
        all_down = np.all(matrix <= 1.0 - BREAKEVEN_PROBABILITY, axis=1)
        test["signal"] = np.where(all_up, "UP", np.where(all_down, "DOWN", None))
        test["min_model_edge"] = np.where(
            all_up,
            matrix.min(axis=1) - BREAKEVEN_PROBABILITY,
            np.where(all_down, 1.0 - BREAKEVEN_PROBABILITY - matrix.max(axis=1), 0.0),
        )
        batches.append(test)
    result = pd.concat(batches, ignore_index=True) if batches else pd.DataFrame()
    result["month"] = result.time.dt.strftime("%Y-%m")
    result["day"] = result.time.dt.strftime("%Y-%m-%d")
    report = {
        "method": {
            "parameterSearch": False,
            "models": ["regularized logistic", "shallow histogram boosting", "shallow random forest"],
            "calibration": "Seven-day trailing Platt calibration inside each past-only training window.",
            "tradeRule": "All three calibrated probabilities must clear the 55.56% payout break-even probability in the same direction.",
            "trainMin": TRAIN_MIN, "trainMax": TRAIN_MAX,
            "refitEvery": REFIT_EVERY, "purgeSamples": PURGE_SAMPLES,
            "breakEvenProbability": BREAKEVEN_PROBABILITY,
            "warning": "Minute closes test slow-context information; second-level execution validation is still required.",
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
