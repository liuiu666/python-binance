"""Backtest per-second signal detection with a latched event consumed by a 5s executor."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_regime_router_corrected as corrected  # noqa: E402
import research_v2_persistent_reclaim as normal  # noqa: E402
import research_volatility_adaptive_router as coarse  # noqa: E402
import research_volatility_fine_router as fine  # noqa: E402


OUT_JSON = ROOT / "tmp" / "event_latched_router_research.json"
OUT_TRADES = ROOT / "tmp" / "event_latched_router_trades.csv"
LATCH_SECONDS = (6, 10, 20)


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


def normal_filter_ok(states: pd.DataFrame, idx: int, direction: str, params: fine.BandParams) -> bool:
    sign = 1.0 if direction == "UP" else -1.0
    return (
        sign * float(states["ret_600s_bps"].iloc[idx]) >= params.ret600_min_bps
        and sign * float(states["flow_120_mean"].iloc[idx]) >= params.flow120_min
    )


def replay_latched(
    name: str,
    prepared: dict,
    start: pd.Timestamp,
    end: pd.Timestamp,
    spec: fine.FineSpec,
    *,
    latch_sec: int,
    scan_phase: int,
) -> list[dict]:
    rules = normal.load_rules()
    loss_filter = next(item for item in corrected.FILTER_SPECS if item.name == "loose")
    data = prepared["data"]
    states = prepared["states"]
    candidates = prepared["candidate_map"]
    close = data["close"].to_numpy(float)
    first_idx = int(data.index.searchsorted(start))
    last_idx = min(len(data) - rules.horizon_sec, int(data.index.searchsorted(end)))

    trend_direction = None
    trend_start_idx = trend_last_idx = None
    normal_direction = normal_band = None
    normal_hits: deque[int] = deque()
    latched: dict | None = None
    last_entry_idx = -10**12
    rows = []

    for idx in range(first_idx, last_idx):
        sigma_bps = float(states["sigma_bps"].iloc[idx])
        band = fine.band_name(sigma_bps)
        params = fine.band_params(spec, band)
        state = str(states["state"].iloc[idx])
        direction = "UP" if float(states["trend_direction"].iloc[idx]) > 0 else "DOWN"

        trend_allowed = band in {"mid", "elevated", "high"}
        if state == "trend_formation" and trend_allowed:
            continuous = trend_direction == direction and trend_last_idx is not None and idx - trend_last_idx == 1
            if not continuous:
                trend_direction = direction
                trend_start_idx = idx
            trend_last_idx = idx
        else:
            trend_direction = None
            trend_start_idx = trend_last_idx = None

        candidate = candidates.get(idx)
        if state != "normal" or not params.enabled:
            normal_direction = normal_band = None
            normal_hits.clear()
        elif candidate is not None:
            candidate_direction = str(candidate["signal"])
            candidate_band = str(candidate["band"])
            if normal_direction is not None and candidate_direction != normal_direction:
                if latched is not None and latched["kind"] == "normal" and latched["signal"] != candidate_direction:
                    latched = None
                normal_hits.clear()
            if normal_band is not None and normal_band != candidate_band:
                normal_hits.clear()
            normal_direction = candidate_direction
            normal_band = candidate_band
            normal_hits.append(idx)
            while normal_hits and idx - normal_hits[0] > 30:
                normal_hits.popleft()
            confirmed = len(normal_hits) >= params.confirm_hits and idx - normal_hits[0] >= params.confirm_span
            if confirmed and normal_filter_ok(states, idx, candidate_direction, params):
                if latched is None or latched["kind"] != "trend":
                    latched = {
                        "kind": "normal",
                        "signal": candidate_direction,
                        "reason": str(candidate["reason"]),
                        "band": band,
                        "created_idx": idx,
                        "expires_idx": idx + latch_sec,
                    }

        trend_confirmed = (
            trend_direction is not None
            and trend_start_idx is not None
            and idx - trend_start_idx >= 20
            and coarse.trend_entry_ready(states, idx, trend_direction, loss_filter)
        )
        if trend_confirmed:
            if latched is not None and latched["signal"] != trend_direction:
                latched = None
            latched = {
                "kind": "trend",
                "signal": trend_direction,
                "reason": "latched_fine_vol_trend_formation",
                "band": band,
                "created_idx": idx,
                "expires_idx": idx + latch_sec,
            }

        if latched is not None and idx > int(latched["expires_idx"]):
            latched = None
        if int(data.index[idx].timestamp()) % 5 != scan_phase:
            continue
        if latched is None or idx - last_entry_idx < rules.min_gap_sec:
            continue
        if not bool(states["data_quality_ready"].iloc[idx]):
            continue

        signal = str(latched["signal"])
        settle_idx = idx + rules.horizon_sec
        entry = float(close[idx])
        settle = float(close[settle_idx])
        sign = 1.0 if signal == "UP" else -1.0
        margin = (settle / entry - 1.0) * 10000.0 * sign
        rows.append(
            {
                "dataset": name,
                "time": data.index[idx],
                "event_time": data.index[int(latched["created_idx"])],
                "settle_time": data.index[settle_idx],
                "latch_sec": latch_sec,
                "scan_phase": scan_phase,
                "delay_sec": idx - int(latched["created_idx"]),
                "kind": latched["kind"],
                "band": latched["band"],
                "signal": signal,
                "reason": latched["reason"],
                "entry": entry,
                "settle": settle,
                "signed_outcome_bps": float(margin),
                "won": bool(margin > 0.0),
            }
        )
        last_entry_idx = idx
        latched = None
        trend_direction = None
        trend_start_idx = trend_last_idx = None
        normal_direction = normal_band = None
        normal_hits.clear()
    return rows


def metrics(rows: list[dict]) -> dict:
    equity = peak = drawdown = wins = 0
    margins = []
    delays = []
    for row in sorted(rows, key=lambda item: (item["dataset"], item["time"])):
        won = bool(row["won"])
        wins += int(won)
        equity += 4 if won else -5
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        margins.append(float(row["signed_outcome_bps"]))
        delays.append(int(row["delay_sec"]))
    count = len(rows)
    return {
        "trades": count,
        "winRate": round(wins / count * 100.0, 2) if count else 0.0,
        "pnlU": int(equity),
        "maxDrawdownU": int(drawdown),
        "medianSignedBps": round(float(np.median(margins)), 3) if margins else 0.0,
        "medianDelaySec": round(float(np.median(delays)), 2) if delays else 0.0,
        "maxDelaySec": max(delays) if delays else 0,
    }


def summarize(rows, datasets):
    return {
        "overall": metrics(rows),
        "byKind": {kind: metrics([row for row in rows if row["kind"] == kind]) for kind in ("normal", "trend")},
        "byBand": {band: metrics([row for row in rows if row["band"] == band]) for band in ("ultra_low", "low", "mid", "elevated", "high")},
        "byDataset": {name: metrics([row for row in rows if row["dataset"] == name]) for name in datasets},
    }


def run(extra_dir=None, extra_start=None):
    spec = next(item for item in fine.SPECS if item.name == "fine_stable")
    raw = coarse.load_data(extra_dir, extra_start)
    prepared = {name: (fine.prepare(data, spec), start, end) for name, (data, start, end) in raw.items()}
    reports = []
    all_rows = []
    for latch_sec in LATCH_SECONDS:
        phases = []
        for phase in range(5):
            rows = []
            for name, (item, start, end) in prepared.items():
                rows.extend(replay_latched(name, item, start, end, spec, latch_sec=latch_sec, scan_phase=phase))
            phases.append({"phase": phase, **summarize(rows, prepared)})
            all_rows.extend(rows)
        reports.append({"latchSec": latch_sec, "phases": phases})
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "baseSpec": spec.name,
        "reports": reports,
        "note": "Signals are detected every second and consumed by a 5-second executor. Opposite events cancel the latch.",
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(all_rows)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extra-dir", type=Path)
    parser.add_argument("--extra-start")
    args = parser.parse_args()
    start = pd.Timestamp(args.extra_start) if args.extra_start else None
    print(json.dumps(clean(run(args.extra_dir, start)), ensure_ascii=False, indent=2))
