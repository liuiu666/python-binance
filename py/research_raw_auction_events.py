"""Diagnose raw trade/depth-delta auction events without changing strategies.

Rows are classified causally using only rolling quantiles from prior seconds:
absorption (strong aggressive flow but no price progress and opposite liquidity)
or liquidity vacuum (flow, price progress and liquidity align).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "tmp" / "auction-live-features" / "BTCUSDT" / "futures" / "date=2026-07-12" / "features.jsonl"
DEFAULT_OUT = ROOT / "tmp" / "raw_auction_event_diagnostics_20260712.json"
DEFAULT_EVENTS = ROOT / "tmp" / "raw_auction_event_diagnostics_20260712.csv"
LOOKBACK_SEC = 900
HORIZON_SEC = 600
COOLDOWN_SEC = 600


def load_rows(path: Path) -> pd.DataFrame:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    frame = pd.DataFrame(rows)
    frame["time"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.set_index("time").sort_index()
    return frame


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"trades": 0, "winRate": None, "pnlU": 0.0, "avgSignedBps": None}
    wins = sum(bool(row["won"]) for row in rows)
    return {
        "trades": len(rows),
        "winRate": round(wins / len(rows) * 100.0, 2),
        "pnlU": round(wins * 4.0 - (len(rows) - wins) * 5.0, 2),
        "avgSignedBps": round(float(np.mean([row["signed_bps"] for row in rows])), 3),
    }


def detect(frame: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    required = {
        "flow_imbalance_30s",
        "ret_30s_bps",
        "near_liquidity_pressure_ratio_30s",
        "near_liquidity_coverage_30s",
        "mid",
        "depth_coverage_60s",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        return {
            "status": "waiting_near_touch_data",
            "missing": missing,
            "caution": "Older raw events lack near-touch liquidity deltas and must not be used for absorption conclusions.",
        }, pd.DataFrame()
    flow = frame["flow_imbalance_30s"].astype(float)
    ret = frame["ret_30s_bps"].astype(float)
    pressure = frame["near_liquidity_pressure_ratio_30s"].astype(float)
    mid = frame["mid"].astype(float)
    coverage = frame["depth_coverage_60s"].astype(float)
    near_coverage = frame["near_liquidity_coverage_30s"].astype(float)
    min_periods = max(300, LOOKBACK_SEC // 3)
    flow_high = flow.abs().shift(1).rolling(LOOKBACK_SEC, min_periods=min_periods).quantile(0.75)
    ret_low = ret.abs().shift(1).rolling(LOOKBACK_SEC, min_periods=min_periods).quantile(0.50)
    ret_high = ret.abs().shift(1).rolling(LOOKBACK_SEC, min_periods=min_periods).quantile(0.75)
    pressure_high = pressure.abs().shift(1).rolling(LOOKBACK_SEC, min_periods=min_periods).quantile(0.75)

    events: dict[str, list[dict[str, Any]]] = {"absorption_reversal": [], "liquidity_vacuum": []}
    last_index = {name: -COOLDOWN_SEC for name in events}
    for index in range(LOOKBACK_SEC, len(frame) - HORIZON_SEC):
        values = (flow.iloc[index], ret.iloc[index], pressure.iloc[index], mid.iloc[index], coverage.iloc[index], near_coverage.iloc[index], flow_high.iloc[index], ret_low.iloc[index], ret_high.iloc[index], pressure_high.iloc[index])
        if not all(np.isfinite(float(value)) for value in values) or float(coverage.iloc[index]) < 0.95 or float(near_coverage.iloc[index]) < 0.95:
            continue
        side = 1.0 if flow.iloc[index] > 0.0 else -1.0 if flow.iloc[index] < 0.0 else 0.0
        if side == 0.0 or abs(flow.iloc[index]) < flow_high.iloc[index]:
            continue
        opposite_liquidity = side * pressure.iloc[index] <= -pressure_high.iloc[index]
        aligned_liquidity = side * pressure.iloc[index] >= pressure_high.iloc[index]
        stalled_price = abs(ret.iloc[index]) <= ret_low.iloc[index]
        advancing_price = side * ret.iloc[index] >= ret_high.iloc[index]
        event_name = None
        signal = None
        if stalled_price and opposite_liquidity:
            event_name, signal = "absorption_reversal", "DOWN" if side > 0 else "UP"
        elif advancing_price and aligned_liquidity:
            event_name, signal = "liquidity_vacuum", "UP" if side > 0 else "DOWN"
        if not event_name or index - last_index[event_name] < COOLDOWN_SEC:
            continue
        entry = float(mid.iloc[index])
        settle = float(mid.iloc[index + HORIZON_SEC])
        if entry <= 0.0 or settle <= 0.0:
            continue
        signed_bps = (settle / entry - 1.0) * 10000.0 * (1.0 if signal == "UP" else -1.0)
        events[event_name].append({
            "detected_time": frame.index[index].isoformat(),
            "event": event_name,
            "signal": signal,
            "flow_imbalance_30s": float(flow.iloc[index]),
            "ret_30s_bps": float(ret.iloc[index]),
            "liquidity_pressure_30s": float(pressure.iloc[index]),
            "entry": entry,
            "settle": settle,
            "signed_bps": signed_bps,
            "won": bool(signed_bps > 0.0),
        })
        last_index[event_name] = index

    all_events = [row for rows in events.values() for row in rows]
    report = {
        "method": {
            "causal": "All thresholds are rolling quantiles of preceding raw-event seconds only.",
            "absorption": "Top-quartile 30s aggressive flow, bottom-half price movement, and top-quartile opposite liquidity pressure.",
            "liquidityVacuum": "Top-quartile aggressive flow, top-quartile same-direction price movement, and top-quartile aligned liquidity pressure.",
            "execution": f"Detection at second close; measurement settles {HORIZON_SEC}s later; each event class has {COOLDOWN_SEC}s cooldown.",
            "parameterSearch": False,
        },
        "sample": {
            "start": frame.index.min().isoformat(),
            "end": frame.index.max().isoformat(),
            "hours": round((frame.index.max() - frame.index.min()).total_seconds() / 3600.0, 2),
            "seconds": len(frame),
        },
        "byEvent": {name: metrics(rows) for name, rows in events.items()},
        "combined": metrics(all_events),
        "caution": "The current raw sample is pipeline validation only and is too short for a strategy conclusion.",
    }
    return report, pd.DataFrame(all_events)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    args = parser.parse_args()
    report, events = detect(load_rows(args.input))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    events.to_csv(args.events, index=False, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
