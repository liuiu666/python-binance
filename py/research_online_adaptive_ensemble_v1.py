"""Causal online ensemble for ten-minute BTC event contracts.

The experiment is deliberately single-pass: fixed features, fixed learners and
fixed admission rules.  A row can update the learners only after its ten-minute
outcome has settled, so no future label enters the current prediction.
"""

from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import SGDClassifier
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "tmp" / "unified_long_price_events_10m.csv"
FORWARD = ROOT / "tmp" / "frozen_position_forward" / "events_10m.csv"
OUT_JSON = ROOT / "tmp" / "online_adaptive_ensemble_v1.json"
OUT_CSV = ROOT / "tmp" / "online_adaptive_ensemble_v1_trades.csv"

DELAYS = (0, 5, 6, 10)
AMOUNT_U = 5.0
PAYOUT_U = 4.0
MIN_SETTLED = 72
MIN_CONFIDENCE = 0.54

FEATURES = (
    "ret_10", "ret_30", "ret_60", "ret_120", "ret_300", "ret_600",
    "z_60", "z_120", "z_300", "z_600",
    "inside1_60", "inside1_120", "inside1_300", "inside1_600",
    "skew_120", "skew_300", "skew_600",
    "kurtosis_120", "kurtosis_300", "kurtosis_600",
    "slope_sigma_60", "slope_sigma_120", "slope_sigma_300", "slope_sigma_600",
    "sigma_expand_60", "sigma_expand_120", "sigma_expand_300", "sigma_expand_600",
    "vol_60", "vol_120", "vol_300", "vol_600",
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


def load_events() -> pd.DataFrame:
    frames = []
    for path, segment in ((HISTORY, "history"), (FORWARD, "forward")):
        frame = pd.read_csv(path)
        frame["segment"] = segment
        frames.append(frame)
    data = pd.concat(frames, ignore_index=True)
    for column in ("time", "entry_time", "settle_time"):
        data[column] = pd.to_datetime(data[column], utc=True, errors="coerce")
    data = data.sort_values(["time", "segment"]).drop_duplicates("time", keep="last")
    required = [*FEATURES, *[f"raw_move_bps_d{delay}" for delay in DELAYS]]
    data[required] = data[required].apply(pd.to_numeric, errors="coerce")
    data = data.replace([np.inf, -np.inf], np.nan).dropna(subset=["time", "settle_time", *required])
    return data.reset_index(drop=True)


class OnlineLearner:
    def __init__(self, eta: float) -> None:
        self.scaler = StandardScaler()
        self.model = SGDClassifier(
            loss="log_loss",
            penalty="l2",
            alpha=0.001,
            learning_rate="constant",
            eta0=eta,
            random_state=17,
        )
        self.ready = False

    def update(self, features: np.ndarray, label: int) -> None:
        row = features.reshape(1, -1)
        self.scaler.partial_fit(row)
        scaled = self.scaler.transform(row)
        if self.ready:
            self.model.partial_fit(scaled, np.array([label]))
        else:
            self.model.partial_fit(scaled, np.array([label]), classes=np.array([0, 1]))
            self.ready = True

    def predict(self, features: np.ndarray) -> tuple[int, float]:
        scaled = self.scaler.transform(features.reshape(1, -1))
        probability = float(self.model.predict_proba(scaled)[0, 1])
        return (1 if probability >= 0.5 else 0), max(probability, 1.0 - probability)


def maximum_loss_streak(wins: np.ndarray) -> int:
    current = maximum = 0
    for won in wins:
        current = 0 if won else current + 1
        maximum = max(maximum, current)
    return maximum


def metrics(frame: pd.DataFrame, delay: int) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": 0.0, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0}
    direction = np.where(frame["signal"].eq("UP"), 1.0, -1.0)
    signed = frame[f"raw_move_bps_d{delay}"].to_numpy(float) * direction
    wins = signed > 0.0
    pnl = np.where(wins, PAYOUT_U, -AMOUNT_U)
    equity = np.cumsum(pnl)
    equity_path = np.r_[0.0, equity]
    drawdown = np.maximum.accumulate(equity_path) - equity_path
    return {
        "trades": int(len(frame)),
        "wins": int(wins.sum()),
        "winRate": round(float(wins.mean() * 100.0), 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float(drawdown.max()), 2),
        "maxLossStreak": maximum_loss_streak(wins),
        "medianSignedBps": round(float(np.median(signed)), 3),
        "thinMarginPctLe3bp": round(float(np.mean(np.abs(signed) <= 3.0) * 100.0), 2),
    }


def run() -> dict[str, Any]:
    data = load_events()
    fast = OnlineLearner(eta=0.02)
    slow = OnlineLearner(eta=0.005)
    pending: deque[tuple[pd.Timestamp, np.ndarray, int]] = deque()
    settled = 0
    rows: list[dict[str, Any]] = []

    for row in data.to_dict("records"):
        now = pd.Timestamp(row["time"])
        while pending and pending[0][0] <= now:
            _, old_features, old_label = pending.popleft()
            fast.update(old_features, old_label)
            slow.update(old_features, old_label)
            settled += 1

        features = np.asarray([float(row[name]) for name in FEATURES], dtype=float)
        label = int(float(row["raw_move_bps_d0"]) > 0.0)
        pending.append((pd.Timestamp(row["settle_time"]), features, label))
        if settled < MIN_SETTLED or not fast.ready or not slow.ready:
            continue

        fast_side, fast_confidence = fast.predict(features)
        slow_side, slow_confidence = slow.predict(features)
        confidence = (fast_confidence + slow_confidence) / 2.0
        if fast_side != slow_side or confidence < MIN_CONFIDENCE:
            continue
        item = dict(row)
        item.update(
            signal="UP" if fast_side else "DOWN",
            confidence=round(confidence, 6),
            fast_confidence=round(fast_confidence, 6),
            slow_confidence=round(slow_confidence, 6),
            settled_samples=settled,
        )
        rows.append(item)

    trades = pd.DataFrame(rows)
    if not trades.empty:
        trades["beijing_day"] = trades["time"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
        trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    hours = (data["time"].max() - data["time"].min()).total_seconds() / 3600.0
    report = {
        "method": {
            "causal": True,
            "parameterSearch": False,
            "model": "fixed fast/slow online logistic ensemble",
            "features": list(FEATURES),
            "warmupSettledEvents": MIN_SETTLED,
            "minimumConfidence": MIN_CONFIDENCE,
            "admission": "fast and slow models must agree",
            "amountU": AMOUNT_U,
        },
        "data": {
            "start": data["time"].min(),
            "end": data["time"].max(),
            "hours": round(hours, 2),
            "events": len(data),
        },
        "overall": {f"delay{delay}s": metrics(trades, delay) for delay in DELAYS},
        "bySegment": {
            segment: {f"delay{delay}s": metrics(group, delay) for delay in DELAYS}
            for segment, group in trades.groupby("segment")
        } if not trades.empty else {},
        "byDayDelay6s": {
            day: metrics(group, 6) for day, group in trades.groupby("beijing_day")
        } if not trades.empty else {},
        "frequency": {
            "tradesPerDayOverall": round(len(trades) / max(hours / 24.0, 1e-9), 2),
            "forwardTradesPerDay": round(
                len(trades[trades.segment.eq("forward")])
                / max((data[data.segment.eq("forward")]["time"].max() - data[data.segment.eq("forward")]["time"].min()).total_seconds() / 86400.0, 1e-9),
                2,
            ),
        },
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
