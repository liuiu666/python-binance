"""Causal phase gate for the multi-scale migration candidate.

The candidate definition is fixed by the existing multi-scale morphology
research. This audit changes only the action by the preceding 60-minute move:
fade countertrend pullbacks and mature migrations, skip startup/middle moves.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from multiscale_phase_gate_core import (  # noqa: E402
    MultiscalePhaseGateConfig,
    build_snapshots as build_phase_snapshots,
)
from research_normal_liquidity_orderbook import read_orderbook  # noqa: E402
from research_normal_shape_1m_10m import clean  # noqa: E402
from run_multi_normal_hf_stable_backtest import (  # noqa: E402
    DEFAULT_SOURCES,
    LoadedSource,
    SourceSpec,
    metrics,
    price_at_or_after,
    utc,
)
from second_backtest.data import load_second_bars  # noqa: E402


OUT_JSON = ROOT / "tmp" / "multiscale_phase_gate_latest.json"
OUT_CSV = ROOT / "tmp" / "multiscale_phase_gate_trades.csv"
HORIZON_SEC = 600
DELAY_SEC = 10
GAP_SEC = 600


def load_live_parity_sources() -> list[LoadedSource]:
    independent = SourceSpec(
        "independent_before_today",
        DEFAULT_SOURCES[3].seconds,
        DEFAULT_SOURCES[3].orderbook,
        start=DEFAULT_SOURCES[3].start,
        end="2026-07-11T16:00:00Z",
        role="independent",
    )
    fresh_root = ROOT / "tmp" / "latest_pull_20260712_migration_fix" / "extracted" / "data"
    today = SourceSpec(
        "today",
        fresh_root / "btcusdt_1s_trades.csv",
        fresh_root / "btcusdt_orderbook_1s.csv",
        start="2026-07-11T16:00:00Z",
        role="today",
    )
    loaded: list[LoadedSource] = []
    for spec in (*DEFAULT_SOURCES[:3], independent, today):
        bars = load_second_bars(spec.seconds, include_shards=False)
        orderbook = read_orderbook(spec.orderbook, bars.index)
        # Live keeps the complete second axis and marks book availability per
        # row. Never delete missing-book seconds before time-based windows.
        data = bars.join(orderbook, how="left").sort_index()
        start = utc(spec.start) if spec.start else utc(data.index.min())
        end = utc(spec.end) if spec.end else utc(data.index.max())
        loaded.append(LoadedSource(
            spec=spec,
            data=data,
            snapshots=pd.DataFrame(),
            test_start=start,
            test_end=end,
            hours=max(1.0 / 60.0, (end - start).total_seconds() / 3600.0),
        ))
    return loaded


def run_source(
    source: Any,
    *,
    maturity_quantile: float = 0.75,
    delay_sec: int = DELAY_SEC,
) -> tuple[pd.DataFrame, dict[str, int]]:
    cfg = replace(
        MultiscalePhaseGateConfig(),
        maturity_quantile=maturity_quantile,
    )
    snapshots = build_phase_snapshots(source.data, cfg)
    snapshots = snapshots[
        (pd.to_datetime(snapshots["detected_time"], utc=True) >= source.test_start)
        & (pd.to_datetime(snapshots["detected_time"], utc=True) <= source.test_end)
    ]
    close = source.data["close"].astype(float)
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    last_emit: pd.Timestamp | None = None
    for candidate in snapshots.to_dict("records"):
        phase = str(candidate.get("phase") or "")
        if phase == "no_migration_candidate":
            continue
        detected = utc(candidate["detected_time"])
        crowd_signal = str(candidate.get("crowd_direction") or "")
        long_move = float(candidate.get("aligned_ret3600_bps", np.nan))
        threshold = float(candidate.get("maturity_threshold_bps", np.nan))
        counts[phase] = counts.get(phase, 0) + 1
        signal = candidate.get("signal")
        if signal not in {"UP", "DOWN"}:
            continue
        if last_emit is not None and (detected - last_emit).total_seconds() < GAP_SEC:
            continue
        entry = price_at_or_after(close, detected + pd.Timedelta(seconds=delay_sec))
        settle = price_at_or_after(close, detected + pd.Timedelta(seconds=delay_sec + HORIZON_SEC))
        if entry is None or settle is None:
            continue
        direction = 1.0 if signal == "UP" else -1.0
        outcome = (settle[1] / entry[1] - 1.0) * 10000.0 * direction
        rows.append({
            "source": source.spec.name,
            "role": source.spec.role,
            "detected_time": detected,
            "entry_time": entry[0],
            "settle_time": settle[0],
            "signal": signal,
            "crowd_direction": crowd_signal,
            "phase": phase,
            "aligned_ret3600_bps": long_move,
            "maturity_threshold": threshold,
            "entry": entry[1],
            "settle": settle[1],
            "signed_outcome_bps": outcome,
            "won": bool(outcome > 0.0),
        })
        last_emit = detected
    return pd.DataFrame(rows), counts


def grouped(frame: pd.DataFrame, hours_by_role: dict[str, float]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for role in ("history", "independent", "today"):
        subset = frame[frame["role"] == role] if not frame.empty else frame
        result[role] = metrics(subset, hours_by_role.get(role, 0.0))
    total_hours = sum(hours_by_role.values())
    result["combined"] = metrics(frame, total_hours)
    result["byPhase"] = {
        str(name): metrics(group, total_hours) for name, group in frame.groupby("phase")
    } if not frame.empty else {}
    if not frame.empty:
        local = frame.copy()
        local["day"] = pd.to_datetime(local["entry_time"], utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
        result["byDay"] = {str(name): metrics(group, 24.0) for name, group in local.groupby("day")}
    return result


def main() -> None:
    sources = load_live_parity_sources()
    frames: list[pd.DataFrame] = []
    phase_counts: dict[str, int] = {}
    hours_by_role: dict[str, float] = {}
    for source in sources:
        frame, counts = run_source(source)
        frames.append(frame)
        hours_by_role[source.spec.role] = hours_by_role.get(source.spec.role, 0.0) + source.hours
        for name, count in counts.items():
            phase_counts[name] = phase_counts.get(name, 0) + count
    trades = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    total_hours = sum(source.hours for source in sources)
    delay_sensitivity: dict[str, Any] = {}
    for delay in (0, 2, 5, 10):
        delay_frames = [run_source(source, delay_sec=delay)[0] for source in sources]
        delay_trades = pd.concat(delay_frames, ignore_index=True) if delay_frames else pd.DataFrame()
        delay_sensitivity[str(delay)] = metrics(delay_trades, total_hours)
    quantile_sensitivity: dict[str, Any] = {}
    for quantile in (0.65, 0.75, 0.85):
        quantile_frames = [run_source(source, maturity_quantile=quantile)[0] for source in sources]
        quantile_trades = pd.concat(quantile_frames, ignore_index=True) if quantile_frames else pd.DataFrame()
        quantile_sensitivity[str(quantile)] = metrics(quantile_trades, total_hours)
    report = {
        "method": {
            "candidate": "Fixed 2m/3m/5m migration with 10m old-area context and flow/book confirmation.",
            "countertrend": "Crowd direction is opposite to the preceding 60-minute return; fade.",
            "mature": "Aligned 60-minute return is above its causal prior 60-minute 75th percentile; fade.",
            "startupOrMiddle": "Aligned but below the causal maturity threshold; skip until near-touch evidence is validated.",
            "parameterSearch": False,
            "executionDelaySec": DELAY_SEC,
            "horizonSec": HORIZON_SEC,
            "gapSec": GAP_SEC,
        },
        "sampleHours": round(sum(source.hours for source in sources), 4),
        "phaseCandidateCounts": phase_counts,
        "result": grouped(trades, hours_by_role),
        "delaySensitivity": delay_sensitivity,
        "maturityQuantileSensitivity": quantile_sensitivity,
        "validationStatus": "invalidated_by_forward_live",
        "forwardLive": {"trades": 8, "wins": 1, "winRate": 12.5, "pnlU": -62.0},
        "selectionBias": "The fade direction and phase branches were selected after inspecting the same historical outcomes; prior role labels are not untouched holdouts.",
        "caution": "Near-touch absorption/vacuum is not used because historical sources do not contain those fields.",
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(clean(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
