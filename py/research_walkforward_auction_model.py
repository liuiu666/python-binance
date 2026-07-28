"""Strict chronological 10-minute auction-direction baseline.

The model is fitted once on sources labelled ``history``.  Independent,
today and forward-live rows are never used to select features, coefficients or
the fixed 0.62 confidence gate.  Samples are spaced ten minutes apart so their
settlement windows do not overlap.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_auction_confirmation_router_v1 import load_forward_live  # noqa: E402
from research_multiscale_phase_gate import load_live_parity_sources  # noqa: E402
from research_normal_shape_1m_10m import clean  # noqa: E402


ENTRY_DELAY_SEC = 5
HORIZON_SEC = 600
CONFIDENCE_GATE = 0.62
PAYOUT_RATE = 0.8
AMOUNT_U = 5.0
OUT_JSON = ROOT / "tmp" / "walkforward_auction_model_latest.json"
OUT_CSV = ROOT / "tmp" / "walkforward_auction_model_predictions.csv"

FEATURES = [
    "ret_10", "ret_30", "ret_60", "ret_120", "ret_300", "ret_600",
    "vol_60", "vol_300", "vol_600", "z_120", "z_300", "z_600",
    "flow_10", "flow_60", "flow_300", "volume_ratio_60",
    "imbalance_10", "imbalance_60", "imbalance_300",
    "micro_10", "micro_60", "spread_60",
]


def _ratio(buy: pd.Series, sell: pd.Series) -> float:
    b = float(buy.fillna(0.0).sum())
    s = float(sell.fillna(0.0).sum())
    return (b - s) / (b + s) if b + s > 0.0 else 0.0


def _window(data: pd.DataFrame, pos: int, width: int) -> pd.DataFrame:
    return data.iloc[pos - width + 1:pos + 1]


def _mean(frame: pd.DataFrame, name: str) -> float:
    values = frame[name].astype(float).replace([np.inf, -np.inf], np.nan)
    return float(values.mean())


def build_samples(source: Any) -> pd.DataFrame:
    data = source.data
    index = data.index
    first = max(source.test_start, index.min() + pd.Timedelta(seconds=600))
    last = min(source.test_end, index.max() - pd.Timedelta(seconds=HORIZON_SEC + ENTRY_DELAY_SEC))
    start = first.ceil("10min")
    rows: list[dict[str, Any]] = []
    for timestamp in pd.date_range(start, last, freq="10min", tz="UTC"):
        pos = int(index.searchsorted(timestamp))
        if pos >= len(data) or abs((index[pos] - timestamp).total_seconds()) > 1 or pos < 600:
            continue
        history = _window(data, pos, 600)
        observed = history.get("observed", pd.Series(True, index=history.index)).fillna(False)
        available = history.get("ob_available", pd.Series(False, index=history.index)).fillna(False)
        if float(observed.mean()) < 0.90 or float(available.mean()) < 0.80:
            continue
        entry_pos = int(index.searchsorted(timestamp + pd.Timedelta(seconds=ENTRY_DELAY_SEC)))
        settle_pos = int(index.searchsorted(timestamp + pd.Timedelta(seconds=ENTRY_DELAY_SEC + HORIZON_SEC)))
        if settle_pos >= len(data) or entry_pos >= len(data):
            continue
        entry = float(data["close"].iloc[entry_pos])
        settle = float(data["close"].iloc[settle_pos])
        if entry <= 0.0 or settle <= 0.0:
            continue
        row: dict[str, Any] = {
            "source": source.spec.name,
            "role": source.spec.role,
            "time": timestamp,
            "entry_time": index[entry_pos],
            "settle_time": index[settle_pos],
            "entry": entry,
            "settle": settle,
            "up": int(settle > entry),
            "raw_move_bps": (settle / entry - 1.0) * 10000.0,
        }
        for width in (10, 30, 60, 120, 300, 600):
            close = _window(data, pos, width)["close"].astype(float)
            row[f"ret_{width}"] = (float(close.iloc[-1]) / float(close.iloc[0]) - 1.0) * 10000.0
            if width in (60, 300, 600):
                returns = close.pct_change().dropna() * 10000.0
                row[f"vol_{width}"] = float(returns.std(ddof=0)) * math.sqrt(width)
            if width in (120, 300, 600):
                sigma = float(close.std(ddof=0))
                row[f"z_{width}"] = (float(close.iloc[-1]) - float(close.mean())) / sigma if sigma > 0.0 else 0.0
        for width in (10, 60, 300):
            frame = _window(data, pos, width)
            row[f"flow_{width}"] = _ratio(frame["buy_qty"], frame["sell_qty"])
            row[f"imbalance_{width}"] = _mean(frame, "imbalance_20")
        row["micro_10"] = _mean(_window(data, pos, 10), "microprice_edge_bps")
        row["micro_60"] = _mean(_window(data, pos, 60), "microprice_edge_bps")
        row["spread_60"] = _mean(_window(data, pos, 60), "spread_bps")
        vol60 = float(_window(data, pos, 60)["volume"].fillna(0.0).sum())
        vol600 = float(history["volume"].fillna(0.0).sum()) / 10.0
        row["volume_ratio_60"] = vol60 / vol600 if vol600 > 0.0 else 0.0
        rows.append(row)
    return pd.DataFrame(rows)


def metrics(frame: pd.DataFrame, gated: bool) -> dict[str, Any]:
    data = frame[frame["confidence"] >= CONFIDENCE_GATE].copy() if gated else frame.copy()
    if data.empty:
        return {"trades": 0, "winRate": 0.0, "pnlU": 0.0, "maxDrawdownU": 0.0, "maxLossStreak": 0}
    data = data.sort_values("time")
    wins = data["won"].astype(bool)
    pnl = np.where(wins, AMOUNT_U * PAYOUT_RATE, -AMOUNT_U)
    equity = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0.0, equity])[:-1]
    drawdown = peak - equity
    streak = maximum = 0
    for won in wins:
        streak = 0 if won else streak + 1
        maximum = max(maximum, streak)
    return {
        "trades": int(len(data)),
        "wins": int(wins.sum()),
        "winRate": round(float(wins.mean()) * 100.0, 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float(drawdown.max()), 2),
        "maxLossStreak": maximum,
        "avgConfidence": round(float(data["confidence"].mean()), 4),
    }


def main() -> None:
    sources = [*load_live_parity_sources(), load_forward_live()]
    samples = pd.concat([build_samples(source) for source in sources], ignore_index=True)
    samples = samples.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)
    train = samples[samples["role"] == "history"].copy()
    holdout = samples[samples["role"] != "history"].copy()
    model = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("model", LogisticRegression(C=0.1, max_iter=2000, random_state=7)),
    ])
    model.fit(train[FEATURES], train["up"])
    probabilities = model.predict_proba(holdout[FEATURES])[:, 1]
    holdout["prob_up"] = probabilities
    holdout["signal"] = np.where(probabilities >= 0.5, "UP", "DOWN")
    holdout["confidence"] = np.maximum(probabilities, 1.0 - probabilities)
    holdout["won"] = (holdout["signal"] == "UP") == holdout["up"].astype(bool)
    holdout["signed_outcome_bps"] = np.where(
        holdout["signal"] == "UP", holdout["raw_move_bps"], -holdout["raw_move_bps"]
    )
    roles = {
        role: {"all": metrics(group, False), "confidence62": metrics(group, True)}
        for role, group in holdout.groupby("role")
    }
    report = {
        "method": {
            "fit": "Fit once on history only; no refit or threshold selection on holdout roles.",
            "sampleSpacingSec": 600,
            "entryDelaySec": ENTRY_DELAY_SEC,
            "horizonFromEntrySec": HORIZON_SEC,
            "confidenceGate": CONFIDENCE_GATE,
            "features": FEATURES,
        },
        "train": {"rows": len(train), "start": train["time"].min(), "end": train["time"].max()},
        "holdout": {"rows": len(holdout), "start": holdout["time"].min(), "end": holdout["time"].max()},
        "roles": roles,
        "holdoutOverall": {"all": metrics(holdout, False), "confidence62": metrics(holdout, True)},
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    holdout.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(clean(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
