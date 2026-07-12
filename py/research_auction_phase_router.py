"""Combine multi-scale migration phase with near-touch auction state.

Research-only. All rolling thresholds use preceding raw-event seconds. The
script never changes the live strategy and never runs on the server.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_multiscale_normal_auction_strategy import early_migration_context  # noqa: E402
from research_normal_shape_1m_10m import clean, shape_features  # noqa: E402
from run_multi_normal_hf_stable_backtest import metrics, price_at_or_after  # noqa: E402


INPUT = ROOT / "tmp" / "auction_compact_features" / "date=2026-07-12" / "features.jsonl"
OUT_JSON = ROOT / "tmp" / "auction_phase_router_latest.json"
OUT_CSV = ROOT / "tmp" / "auction_phase_router_trades.csv"
WINDOWS = (1, 2, 3, 5, 10)


def load_features(path: Path) -> pd.DataFrame:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    frame = pd.DataFrame(rows)
    frame["time"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.set_index("time").sort_index()
    mid = frame["mid"].astype(float)
    frame["ret30_bps"] = (mid / mid.shift(30) - 1.0) * 10000.0
    frame["ret3600_bps"] = (mid / mid.shift(3600) - 1.0) * 10000.0
    near_bid = frame["near_net_bid_liquidity_30s"].astype(float)
    near_ask = frame["near_net_ask_liquidity_30s"].astype(float)
    near_activity = near_bid.abs() + near_ask.abs()
    frame["near_activity_30s"] = near_activity
    frame["near_pressure_share_30s"] = (near_bid - near_ask) / near_activity.replace(0.0, np.nan)
    frame["flow_high"] = frame["flow_imbalance_30s"].abs().shift(1).rolling(900, min_periods=300).quantile(0.75)
    frame["ret_low"] = frame["ret30_bps"].abs().shift(1).rolling(900, min_periods=300).quantile(0.50)
    frame["ret_high"] = frame["ret30_bps"].abs().shift(1).rolling(900, min_periods=300).quantile(0.75)
    frame["pressure_high"] = frame["near_pressure_share_30s"].abs().shift(1).rolling(900, min_periods=300).quantile(0.75)
    frame["activity_floor"] = frame["near_activity_30s"].shift(1).rolling(900, min_periods=300).quantile(0.50)
    frame["maturity_threshold"] = frame["ret3600_bps"].abs().shift(1).rolling(3600, min_periods=1800).quantile(0.75)
    return frame


def liquidity_state(row: pd.Series, crowd_direction: int) -> str:
    required = (
        "flow_imbalance_30s", "ret30_bps", "near_pressure_share_30s", "near_activity_30s",
        "flow_high", "ret_low", "ret_high", "pressure_high", "activity_floor",
        "depth_coverage_60s", "near_liquidity_coverage_30s",
    )
    if any(not math.isfinite(float(row.get(key, np.nan))) for key in required):
        return "unknown"
    if float(row["depth_coverage_60s"]) < 0.90 or float(row["near_liquidity_coverage_30s"]) < 0.90:
        return "unknown"
    flow = crowd_direction * float(row["flow_imbalance_30s"])
    progress = crowd_direction * float(row["ret30_bps"])
    pressure = crowd_direction * float(row["near_pressure_share_30s"])
    if flow < float(row["flow_high"]) or float(row["near_activity_30s"]) < float(row["activity_floor"]):
        return "neutral"
    if abs(float(row["ret30_bps"])) <= float(row["ret_low"]) and pressure <= -float(row["pressure_high"]):
        return "absorption"
    if progress >= float(row["ret_high"]) and pressure >= float(row["pressure_high"]):
        return "vacuum"
    return "neutral"


def migration_phase(row: pd.Series, crowd_direction: int) -> str:
    long_move = crowd_direction * float(row.get("ret3600_bps", np.nan))
    threshold = float(row.get("maturity_threshold", np.nan))
    if not math.isfinite(long_move) or not math.isfinite(threshold):
        return "unknown"
    if long_move <= 0.0:
        return "countertrend_pullback"
    if long_move >= threshold:
        return "mature"
    return "startup_or_middle"


def decide(phase: str, state: str, crowd_direction: int) -> tuple[str | None, str | None]:
    if phase == "countertrend_pullback" and state != "vacuum":
        signal = "DOWN" if crowd_direction > 0 else "UP"
        return signal, "countertrend_pullback_fade"
    if phase == "mature" and state == "absorption":
        signal = "DOWN" if crowd_direction > 0 else "UP"
        return signal, "mature_absorption_fade"
    if phase == "startup_or_middle" and state == "vacuum":
        signal = "UP" if crowd_direction > 0 else "DOWN"
        return signal, "startup_vacuum_follow"
    return None, None


def run(frame: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    mid = frame["mid"].astype(float)
    close = mid.to_numpy(float)
    observed = (frame["depth_coverage_60s"].fillna(0.0).to_numpy(float) >= 0.90)
    raw_candidates = 0
    state_counts: dict[str, int] = {}
    phase_counts: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    last_emit: pd.Timestamp | None = None
    for index in np.flatnonzero(frame.index.second.to_numpy() == 59):
        if index < 7200 or index + 602 >= len(frame):
            continue
        shapes: dict[int, dict[str, Any]] = {}
        for window in WINDOWS:
            width = window * 60
            feature = shape_features(close[index - width + 1 : index + 1], observed[index - width + 1 : index + 1])
            if feature is None:
                break
            shapes[window] = feature
        if len(shapes) != len(WINDOWS):
            continue
        crowd_direction = next((direction for direction in (1, -1) if early_migration_context(shapes, direction)), 0)
        if crowd_direction == 0:
            continue
        raw_candidates += 1
        row = frame.iloc[index]
        state = liquidity_state(row, crowd_direction)
        phase = migration_phase(row, crowd_direction)
        state_counts[state] = state_counts.get(state, 0) + 1
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        candidates.append({
            "detected_time": frame.index[index],
            "crowd_direction": "UP" if crowd_direction > 0 else "DOWN",
            "phase": phase,
            "liquidity_state": state,
            "flow_aligned": crowd_direction * float(row["flow_imbalance_30s"]),
            "flow_high": float(row["flow_high"]),
            "progress_bps": crowd_direction * float(row["ret30_bps"]),
            "ret_low": float(row["ret_low"]),
            "ret_high": float(row["ret_high"]),
            "pressure_aligned": crowd_direction * float(row["near_pressure_share_30s"]),
            "pressure_high": float(row["pressure_high"]),
            "near_activity": float(row["near_activity_30s"]),
            "activity_floor": float(row["activity_floor"]),
        })
        signal, reason = decide(phase, state, crowd_direction)
        if not signal:
            continue
        detected = frame.index[index]
        if last_emit is not None and (detected - last_emit).total_seconds() < 600:
            continue
        entry = price_at_or_after(mid, detected + pd.Timedelta(seconds=2))
        settle = price_at_or_after(mid, detected + pd.Timedelta(seconds=602))
        if entry is None or settle is None:
            continue
        sign = 1.0 if signal == "UP" else -1.0
        outcome = (settle[1] / entry[1] - 1.0) * 10000.0 * sign
        rows.append({
            "detected_time": detected,
            "entry_time": entry[0],
            "settle_time": settle[0],
            "signal": signal,
            "reason": reason,
            "phase": phase,
            "liquidity_state": state,
            "crowd_direction": "UP" if crowd_direction > 0 else "DOWN",
            "ret3600_bps": float(row["ret3600_bps"]),
            "maturity_threshold": float(row["maturity_threshold"]),
            "flow_imbalance_30s": float(row["flow_imbalance_30s"]),
            "ret30_bps": float(row["ret30_bps"]),
            "near_pressure_30s": float(row["near_pressure_share_30s"]),
            "entry": entry[1],
            "settle": settle[1],
            "signed_outcome_bps": outcome,
            "won": bool(outcome > 0.0),
        })
        last_emit = detected
    trades = pd.DataFrame(rows)
    hours = max(0.0, (frame.index.max() - frame.index.min()).total_seconds() / 3600.0)
    report = {
        "sample": {"start": frame.index.min(), "end": frame.index.max(), "hours": round(hours, 3), "seconds": len(frame)},
        "rawCandidates": raw_candidates,
        "phaseCounts": phase_counts,
        "liquidityStateCounts": state_counts,
        "candidateDiagnostics": clean(candidates),
        "result": metrics(trades, hours),
        "byReason": {str(name): metrics(group, hours) for name, group in trades.groupby("reason")} if not trades.empty else {},
        "caution": "Near-touch data covers only the current day. This is a direct joint-state audit, not sufficient long-term evidence.",
    }
    return report, trades


def main() -> None:
    report, trades = run(load_features(INPUT))
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(clean(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
