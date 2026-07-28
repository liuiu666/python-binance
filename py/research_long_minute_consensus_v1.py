"""Long-history, fixed-consensus audit for ten-minute event direction."""

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
MINUTES = ROOT / "data" / "server_latest" / "btcusdt_1m.csv"
FORWARD_SECONDS = ROOT / "tmp" / "frozen_position_forward" / "btcusdt_1s_trades.csv"
OUT_JSON = ROOT / "tmp" / "long_minute_consensus_v1.json"
OUT_CSV = ROOT / "tmp" / "long_minute_consensus_v1_trades.csv"
CONFIDENCE = 0.60
DELAYS = (0, 5, 6, 10)
WINDOWS = (1, 2, 3, 5, 10, 15, 30, 60, 120)


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


def read_minutes() -> pd.DataFrame:
    history = pd.read_csv(MINUTES)
    history["time"] = pd.to_datetime(history["open_time"], utc=True, errors="coerce")
    history = history.set_index("time")[["open", "high", "low", "close", "volume"]]

    seconds = pd.read_csv(FORWARD_SECONDS)
    seconds["time"] = pd.to_datetime(seconds["timestamp"], utc=True, errors="coerce")
    seconds = seconds.set_index("time")
    forward = seconds.resample("1min").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
    )
    data = pd.concat([history, forward]).sort_index()
    data = data[~data.index.duplicated(keep="last")]
    return data.apply(pd.to_numeric, errors="coerce")


def build_events(minutes: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    close = minutes.close
    log_return = np.log(close / close.shift(1))
    features: dict[str, pd.Series] = {}
    for width in WINDOWS:
        features[f"ret_{width}"] = (close / close.shift(width) - 1.0) * 10000.0
    for width in (5, 10, 15, 30, 60, 120):
        mean = close.rolling(width, min_periods=width).mean()
        std = close.rolling(width, min_periods=width).std(ddof=0)
        features[f"rv_{width}"] = log_return.rolling(width, min_periods=width).std(ddof=0) * 10000.0
        features[f"z_{width}"] = (close - mean) / std.replace(0.0, np.nan)
        features[f"range_{width}"] = (
            minutes.high.rolling(width, min_periods=width).max()
            / minutes.low.rolling(width, min_periods=width).min() - 1.0
        ) * 10000.0
    features["volume_ratio_5_30"] = (
        minutes.volume.rolling(5, min_periods=5).mean()
        / minutes.volume.rolling(30, min_periods=30).mean().replace(0.0, np.nan)
    )
    features["volume_ratio_10_60"] = (
        minutes.volume.rolling(10, min_periods=10).mean()
        / minutes.volume.rolling(60, min_periods=60).mean().replace(0.0, np.nan)
    )
    candle_range = (minutes.high - minutes.low).replace(0.0, np.nan)
    features["body_position"] = (minutes.close - minutes.open) / candle_range
    features["close_position"] = (minutes.close - minutes.low) / candle_range
    observed = close.notna().astype(float).rolling(120, min_periods=120).mean()
    frame = pd.DataFrame(features, index=minutes.index)
    frame["entry"] = minutes.open.shift(-1)
    frame["settle"] = close.shift(-10)
    frame["move_bps"] = (frame.settle / frame.entry - 1.0) * 10000.0
    frame["label"] = (frame.move_bps > 0.0).astype(int)
    frame["observed"] = observed
    frame = frame[(frame.index.minute % 10 == 0) & frame.observed.ge(0.98)]
    names = list(features)
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=[*names, "entry", "settle", "move_bps"])
    return frame, names


def models() -> tuple[Any, Any]:
    linear = make_pipeline(
        StandardScaler(),
        LogisticRegression(C=0.25, max_iter=1000, class_weight="balanced", random_state=31),
    )
    nonlinear = HistGradientBoostingClassifier(
        learning_rate=0.05, max_iter=100, max_leaf_nodes=7,
        min_samples_leaf=50, l2_regularization=2.0, random_state=31,
    )
    return linear, nonlinear


def predict(train: pd.DataFrame, test: pd.DataFrame, names: list[str], period: str) -> pd.DataFrame:
    linear, nonlinear = models()
    x_train, y_train = train[names].to_numpy(float), train.label.to_numpy(int)
    x_test = test[names].to_numpy(float)
    linear.fit(x_train, y_train)
    nonlinear.fit(x_train, y_train)
    p1 = linear.predict_proba(x_test)[:, 1]
    p2 = nonlinear.predict_proba(x_test)[:, 1]
    probability = (p1 + p2) / 2.0
    confidence = np.maximum(probability, 1.0 - probability)
    admitted = ((p1 >= 0.5) == (p2 >= 0.5)) & (confidence >= CONFIDENCE)
    result = test.loc[admitted].copy()
    result["signal"] = np.where(probability[admitted] >= 0.5, "UP", "DOWN")
    result["confidence"] = confidence[admitted]
    result["period"] = period
    result["train_rows"] = len(train)
    return result


def max_streak(wins: np.ndarray) -> int:
    current = maximum = 0
    for won in wins:
        current = 0 if won else current + 1
        maximum = max(maximum, current)
    return maximum


def metrics(frame: pd.DataFrame, hours: float) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": 0.0, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0, "tradesPerDay": 0.0}
    direction = np.where(frame.signal.eq("UP"), 1.0, -1.0)
    signed = frame.move_bps.to_numpy(float) * direction
    wins = signed > 0.0
    pnl = np.where(wins, 4.0, -5.0)
    equity = np.cumsum(pnl)
    equity_path = np.r_[0.0, equity]
    drawdown = np.maximum.accumulate(equity_path) - equity_path
    return {
        "trades": int(len(frame)), "wins": int(wins.sum()),
        "winRate": round(float(wins.mean() * 100.0), 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float(drawdown.max()), 2),
        "maxLossStreak": max_streak(wins),
        "tradesPerDay": round(len(frame) / max(hours / 24.0, 1e-9), 2),
        "medianSignedBps": round(float(np.median(signed)), 3),
    }


def run() -> dict[str, Any]:
    events, names = build_events(read_minutes())
    train = events[events.index < pd.Timestamp("2026-06-01", tz="UTC")]
    validation = events[(events.index >= pd.Timestamp("2026-06-01", tz="UTC")) & (events.index < pd.Timestamp("2026-07-01", tz="UTC"))]
    july = events[(events.index >= pd.Timestamp("2026-07-01", tz="UTC")) & (events.index < pd.Timestamp("2026-07-12", tz="UTC"))]
    forward = events[events.index >= pd.Timestamp("2026-07-14", tz="UTC")]
    validation_trades = predict(train, validation, names, "validation_june")
    july_trades = predict(pd.concat([train, validation]), july, names, "test_july")
    forward_trades = predict(events[events.index < pd.Timestamp("2026-07-14", tz="UTC")], forward, names, "forward_july14_15")
    trades = pd.concat([validation_trades, july_trades, forward_trades])
    trades.to_csv(OUT_CSV, encoding="utf-8-sig")

    def span(frame: pd.DataFrame) -> float:
        return max((frame.index.max() - frame.index.min()).total_seconds() / 3600.0, 1.0)

    report = {
        "method": {
            "parameterSearch": False, "causalFeatures": True,
            "models": "fixed regularized logistic + shallow gradient boosting",
            "admission": f"models agree and confidence >= {CONFIDENCE}",
            "eventSchedule": "one independent opportunity every ten minutes",
            "features": names,
        },
        "data": {"start": events.index.min(), "end": events.index.max(), "events": len(events)},
        "periods": {
            "validationJune": metrics(validation_trades, span(validation)),
            "testJuly1To11": metrics(july_trades, span(july)),
            "forwardJuly14To15": metrics(forward_trades, span(forward)),
        },
        "forwardByDay": {
            day: metrics(group, 24.0)
            for day, group in forward_trades.groupby(forward_trades.index.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d"))
        },
        "acceptance": {"minTradesPerDay": 10.0, "minWinRate": 55.56, "maxDrawdownU": 20.0, "maxLossStreak": 3},
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
