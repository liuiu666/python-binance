"""Test five volatility bands with band-specific normal-entry parameters."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_market_regime_classifier as regime_features  # noqa: E402
import research_regime_router_corrected as corrected  # noqa: E402
import research_v2_persistent_reclaim as normal  # noqa: E402
import research_volatility_adaptive_router as coarse  # noqa: E402
from liquidity_v2_core import evaluate_candidate, normal_ready  # noqa: E402


OUT_JSON = ROOT / "tmp" / "volatility_fine_router_research.json"
OUT_TRADES = ROOT / "tmp" / "volatility_fine_router_trades.csv"


@dataclass(frozen=True)
class BandParams:
    z_entry: float
    z_reclaim: float
    confirm_hits: int
    confirm_span: int
    ret600_min_bps: float
    flow120_min: float
    enabled: bool = True


@dataclass(frozen=True)
class FineSpec:
    name: str
    ultra_low: BandParams
    low: BandParams
    mid: BandParams
    elevated: BandParams
    high: BandParams


OFF = BandParams(1.5, 0.7, 4, 12, 0.0, 0.0, False)
SPECS = (
    FineSpec(
        "fine_stable",
        BandParams(0.80, 0.80, 2, 5, -15.0, -0.12),
        BandParams(0.90, 0.85, 2, 5, -15.0, -0.12),
        BandParams(1.00, 0.90, 2, 5, -12.0, -0.08),
        BandParams(1.20, 0.85, 3, 8, -10.0, -0.08),
        OFF,
    ),
    FineSpec(
        "fine_balanced",
        BandParams(0.75, 0.75, 1, 0, -16.0, -0.14),
        BandParams(0.85, 0.825, 2, 5, -15.0, -0.12),
        BandParams(1.00, 0.90, 2, 5, -12.0, -0.08),
        BandParams(1.20, 0.85, 3, 8, -10.0, -0.08),
        OFF,
    ),
    FineSpec(
        "fine_conservative",
        BandParams(0.90, 0.85, 2, 5, -12.0, -0.08),
        BandParams(1.00, 0.90, 3, 8, -12.0, -0.08),
        BandParams(1.10, 0.85, 3, 8, -10.0, -0.05),
        BandParams(1.30, 0.80, 4, 12, -8.0, -0.03),
        OFF,
    ),
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


def band_name(sigma_bps: float) -> str:
    if sigma_bps < 6.5:
        return "ultra_low"
    if sigma_bps < 8.0:
        return "low"
    if sigma_bps < 10.0:
        return "mid"
    if sigma_bps < 14.0:
        return "elevated"
    return "high"


def band_params(spec: FineSpec, band: str) -> BandParams:
    return getattr(spec, band)


def prepare(data: pd.DataFrame, spec: FineSpec) -> dict:
    base_rules = normal.load_rules()
    features = regime_features.build_regime_features(data)
    features["ob_coverage_60"] = features["ob_available"].astype(float).rolling(60, min_periods=60).mean()
    states = corrected.build_states(features)
    rules_by_band = {}
    for band in ("ultra_low", "low", "mid", "elevated"):
        params = band_params(spec, band)
        rules_by_band[band] = replace(
            base_rules,
            z_entry=params.z_entry,
            z_reclaim=params.z_reclaim,
            sigma_min_bps=4.5 if band in {"ultra_low", "low"} else base_rules.sigma_min_bps,
        )
    candidate_map = {}
    warmup = max(base_rules.normal_window_sec, base_rules.center_slope_sec, base_rules.retest_sec, 3600) + 10
    limit = len(data) - base_rules.horizon_sec
    for idx in range(warmup, max(warmup, limit)):
        row = features.iloc[idx]
        band = band_name(float(row["sigma_bps"]))
        params = band_params(spec, band)
        if not params.enabled or band == "high" or not bool(data["ob_available"].iloc[idx]):
            continue
        rules = rules_by_band[band]
        if not normal_ready(row, rules):
            continue
        decision = evaluate_candidate(row, rules)
        if decision["status"] == "accepted":
            candidate_map[idx] = {
                "signal": decision["signal"],
                "reason": decision["reason"],
                "band": band,
            }
    return {"data": data, "states": states, "candidate_map": candidate_map}


def replay(name, prepared, start, end, spec, scan_interval, scan_phase):
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
    normal_hits = deque()
    last_entry_idx = -10**12
    rows = []
    for idx in range(first_idx, last_idx):
        if int(data.index[idx].timestamp()) % scan_interval != scan_phase:
            continue
        sigma_bps = float(states["sigma_bps"].iloc[idx])
        band = band_name(sigma_bps)
        params = band_params(spec, band)
        state = str(states["state"].iloc[idx])
        direction = "UP" if float(states["trend_direction"].iloc[idx]) > 0 else "DOWN"
        trend_allowed = band in {"mid", "elevated", "high"}
        if state == "trend_formation" and trend_allowed:
            continuous = trend_direction == direction and trend_last_idx is not None and idx - trend_last_idx == scan_interval
            if not continuous:
                trend_direction = direction
                trend_start_idx = idx
            trend_last_idx = idx
        else:
            trend_direction = None
            trend_start_idx = trend_last_idx = None

        normal_ready_now = False
        normal_signal = normal_reason = None
        candidate = candidates.get(idx)
        if state != "normal" or not params.enabled or candidate is None:
            if state != "normal" or not params.enabled:
                normal_direction = normal_band = None
                normal_hits.clear()
        else:
            candidate_direction = str(candidate["signal"])
            candidate_band = str(candidate["band"])
            if normal_direction != candidate_direction or normal_band != candidate_band:
                normal_hits.clear()
            normal_direction = candidate_direction
            normal_band = candidate_band
            normal_hits.append(idx)
            while normal_hits and idx - normal_hits[0] > 30:
                normal_hits.popleft()
            normal_ready_now = len(normal_hits) >= params.confirm_hits and idx - normal_hits[0] >= params.confirm_span
            normal_signal = candidate_direction
            normal_reason = str(candidate["reason"])

        if idx - last_entry_idx < rules.min_gap_sec:
            continue
        trend_ready = (
            trend_direction is not None
            and trend_start_idx is not None
            and idx - trend_start_idx >= 20
            and coarse.trend_entry_ready(states, idx, trend_direction, loss_filter)
        )
        if normal_ready_now:
            sign = 1.0 if normal_signal == "UP" else -1.0
            normal_ready_now = (
                sign * float(states["ret_600s_bps"].iloc[idx]) >= params.ret600_min_bps
                and sign * float(states["flow_120_mean"].iloc[idx]) >= params.flow120_min
            )
        signal = reason = kind = None
        if trend_ready:
            signal, reason, kind = trend_direction, "fine_vol_trend_formation", "trend"
        elif normal_ready_now:
            signal, reason, kind = normal_signal, normal_reason, "normal"
        if signal is None:
            continue
        settle_idx = idx + rules.horizon_sec
        entry = float(close[idx])
        settle = float(close[settle_idx])
        sign = 1.0 if signal == "UP" else -1.0
        margin = (settle / entry - 1.0) * 10000.0 * sign
        rows.append({
            "dataset": name, "time": data.index[idx], "settle_time": data.index[settle_idx],
            "spec": spec.name, "band": band, "sigma_bps": sigma_bps, "kind": kind,
            "signal": signal, "reason": reason, "entry": entry, "settle": settle,
            "signed_outcome_bps": float(margin), "won": bool(margin > 0),
        })
        last_entry_idx = idx
        trend_direction = None
        trend_start_idx = trend_last_idx = None
        normal_direction = normal_band = None
        normal_hits.clear()
    return rows


def metrics(rows):
    equity = peak = drawdown = wins = 0
    margins = []
    for row in sorted(rows, key=lambda item: (item["dataset"], item["time"])):
        won = bool(row["won"])
        wins += int(won)
        equity += 4 if won else -5
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        margins.append(float(row["signed_outcome_bps"]))
    count = len(rows)
    return {
        "trades": count, "winRate": round(wins / count * 100, 2) if count else 0.0,
        "pnlU": int(equity), "maxDrawdownU": int(drawdown),
        "medianSignedBps": round(float(np.median(margins)), 3) if margins else 0.0,
    }


def summarize(rows, datasets):
    bands = ("ultra_low", "low", "mid", "elevated", "high")
    return {
        "overall": metrics(rows),
        "byBand": {band: metrics([row for row in rows if row["band"] == band]) for band in bands},
        "byKind": {kind: metrics([row for row in rows if row["kind"] == kind]) for kind in ("normal", "trend")},
        "byDataset": {name: metrics([row for row in rows if row["dataset"] == name]) for name in datasets},
    }


def run(extra_dir=None, extra_start=None, spec_name=None):
    raw = coarse.load_data(extra_dir, extra_start)
    specs = [spec for spec in SPECS if spec_name is None or spec.name == spec_name]
    if not specs:
        raise ValueError(f"unknown spec: {spec_name}")
    reports = []
    all_rows = []
    for spec in specs:
        prepared = {name: (prepare(data, spec), start, end) for name, (data, start, end) in raw.items()}
        one_second = []
        for name, (item, start, end) in prepared.items():
            one_second.extend(replay(name, item, start, end, spec, 1, 0))
        phases = []
        for phase in range(5):
            phase_rows = []
            for name, (item, start, end) in prepared.items():
                phase_rows.extend(replay(name, item, start, end, spec, 5, phase))
            phases.append({"phase": phase, **summarize(phase_rows, prepared)})
        reports.append({"spec": asdict(spec), "oneSecond": summarize(one_second, prepared), "fiveSecondPhaseStress": phases})
        all_rows.extend(one_second)
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "bands": {"ultra_low": "sigma<6.5", "low": "6.5<=sigma<8", "mid": "8<=sigma<10", "elevated": "10<=sigma<14", "high": "sigma>=14"},
        "reports": reports,
        "note": "Five fixed volatility bands and three predefined profiles; no parameter grid search.",
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(all_rows)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extra-dir", type=Path)
    parser.add_argument("--extra-start")
    parser.add_argument("--spec")
    args = parser.parse_args()
    start = pd.Timestamp(args.extra_start) if args.extra_start else None
    print(json.dumps(clean(run(args.extra_dir, start, args.spec)), ensure_ascii=False, indent=2))
