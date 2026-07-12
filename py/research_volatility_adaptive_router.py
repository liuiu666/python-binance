"""Test volatility-specific parameters on the corrected regime router."""

from __future__ import annotations

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
from liquidity_v2_core import evaluate_candidate, normal_ready  # noqa: E402


OUT_JSON = ROOT / "tmp" / "volatility_adaptive_router_research.json"
OUT_TRADES = ROOT / "tmp" / "volatility_adaptive_router_trades.csv"


@dataclass(frozen=True)
class AdaptiveSpec:
    name: str
    low_z_entry: float
    low_z_reclaim: float
    low_confirm_hits: int
    low_confirm_span: int
    mid_confirm_hits: int = 3
    mid_confirm_span: int = 8


SPECS = (
    AdaptiveSpec("dynamic_conservative", 1.0, 0.90, 3, 8),
    AdaptiveSpec("dynamic_active", 1.0, 0.90, 2, 5),
    AdaptiveSpec("dynamic_deep_active", 0.9, 0.85, 2, 5),
    AdaptiveSpec("dynamic_lowvol_high_frequency", 0.8, 0.80, 1, 0),
    AdaptiveSpec("dynamic_lowvol_z08_confirmed", 0.8, 0.80, 2, 5),
    AdaptiveSpec("dynamic_lowvol_z085_immediate", 0.85, 0.825, 1, 0),
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


def volatility_bucket(sigma_bps: float) -> str:
    if sigma_bps < 8.0:
        return "low"
    if sigma_bps < 14.0:
        return "mid"
    return "high"


def build_candidate_maps(data: pd.DataFrame, features: pd.DataFrame, spec: AdaptiveSpec) -> dict[str, dict[int, dict]]:
    base = normal.load_rules()
    profiles = {
        "low": replace(
            base,
            z_entry=spec.low_z_entry,
            z_reclaim=spec.low_z_reclaim,
            sigma_min_bps=4.5,
        ),
        "mid": base,
    }
    maps = {"low": {}, "mid": {}}
    warmup = max(base.normal_window_sec, base.center_slope_sec, base.retest_sec, 3600) + 10
    limit = len(data) - base.horizon_sec
    for idx in range(warmup, max(warmup, limit)):
        row = features.iloc[idx]
        if not bool(data["ob_available"].iloc[idx]):
            continue
        for bucket, rules in profiles.items():
            if not normal_ready(row, rules):
                continue
            decision = evaluate_candidate(row, rules)
            if decision["status"] != "accepted":
                continue
            maps[bucket][idx] = {
                "idx": idx,
                "time": data.index[idx],
                "signal": decision["signal"],
                "reason": decision["reason"],
            }
    return maps


def prepare(data: pd.DataFrame, spec: AdaptiveSpec) -> dict:
    features = regime_features.build_regime_features(data)
    features["ob_coverage_60"] = features["ob_available"].astype(float).rolling(60, min_periods=60).mean()
    states = corrected.build_states(features)
    return {
        "data": data,
        "features": features,
        "states": states,
        "candidate_maps": build_candidate_maps(data, features, spec),
    }


def trend_entry_ready(states: pd.DataFrame, idx: int, direction: str, loss_filter: corrected.LossFilterSpec) -> bool:
    sign = 1.0 if direction == "UP" else -1.0
    if sign * float(states["imbalance_20"].iloc[idx]) < 0.08:
        return False
    if sign * float(states["micro_bps"].iloc[idx]) < 0.001:
        return False
    hot_votes = sum(
        (
            sign * float(states["ret_60s_bps"].iloc[idx]) >= loss_filter.trend_hot_ret60_bps,
            sign * float(states["ret_1800s_bps"].iloc[idx]) >= loss_filter.trend_hot_ret1800_bps,
            sign * float(states["flow_120_mean"].iloc[idx]) >= loss_filter.trend_hot_flow120,
            float(states["sigma_expand"].iloc[idx]) >= loss_filter.trend_hot_sigma_expand,
        )
    )
    return hot_votes <= loss_filter.trend_hot_votes_max


def replay(
    name: str,
    prepared: dict,
    start: pd.Timestamp,
    end: pd.Timestamp,
    spec: AdaptiveSpec,
    *,
    scan_interval: int,
    scan_phase: int,
) -> list[dict]:
    rules = normal.load_rules()
    loss_filter = next(item for item in corrected.FILTER_SPECS if item.name == "loose")
    data = prepared["data"]
    states = prepared["states"]
    maps = prepared["candidate_maps"]
    close = data["close"].to_numpy(float)
    first_idx = int(data.index.searchsorted(start))
    last_idx = min(len(data) - rules.horizon_sec, int(data.index.searchsorted(end)))
    trend_direction = None
    trend_start_idx = trend_last_idx = None
    normal_direction = normal_bucket = None
    normal_hits: deque[int] = deque()
    last_entry_idx = -10**12
    rows = []

    for idx in range(first_idx, last_idx):
        if int(data.index[idx].timestamp()) % scan_interval != scan_phase:
            continue
        sigma_bps = float(states["sigma_bps"].iloc[idx])
        bucket = volatility_bucket(sigma_bps)
        state = str(states["state"].iloc[idx])
        trend_allowed = bucket in {"mid", "high"}
        current_trend_direction = "UP" if float(states["trend_direction"].iloc[idx]) > 0.0 else "DOWN"

        if state == "trend_formation" and trend_allowed:
            continuous = (
                trend_direction == current_trend_direction
                and trend_last_idx is not None
                and idx - trend_last_idx == scan_interval
            )
            if not continuous:
                trend_direction = current_trend_direction
                trend_start_idx = idx
            trend_last_idx = idx
        else:
            trend_direction = None
            trend_start_idx = None
            trend_last_idx = None

        normal_ready_now = False
        normal_signal = normal_reason = None
        candidate = maps.get(bucket, {}).get(idx) if bucket != "high" else None
        if state != "normal" or bucket == "high":
            normal_direction = normal_bucket = None
            normal_hits.clear()
        elif candidate is not None:
            candidate_direction = str(candidate["signal"])
            if normal_direction != candidate_direction or normal_bucket != bucket:
                normal_hits.clear()
            normal_direction = candidate_direction
            normal_bucket = bucket
            normal_hits.append(idx)
            while normal_hits and idx - normal_hits[0] > 30:
                normal_hits.popleft()
            required_hits = spec.low_confirm_hits if bucket == "low" else spec.mid_confirm_hits
            required_span = spec.low_confirm_span if bucket == "low" else spec.mid_confirm_span
            normal_ready_now = len(normal_hits) >= required_hits and idx - normal_hits[0] >= required_span
            normal_signal = candidate_direction
            normal_reason = str(candidate["reason"])

        if idx - last_entry_idx < rules.min_gap_sec:
            continue
        trend_ready_now = (
            trend_direction is not None
            and trend_start_idx is not None
            and idx - trend_start_idx >= 20
            and trend_entry_ready(states, idx, trend_direction, loss_filter)
        )
        if normal_ready_now:
            sign = 1.0 if normal_signal == "UP" else -1.0
            if bucket == "low":
                normal_ready_now = (
                    sign * float(states["ret_600s_bps"].iloc[idx]) >= -15.0
                    and sign * float(states["flow_120_mean"].iloc[idx]) >= -0.12
                )
            else:
                normal_ready_now = (
                    sign * float(states["ret_600s_bps"].iloc[idx]) >= loss_filter.normal_ret600_min_bps
                    and sign * float(states["flow_120_mean"].iloc[idx]) >= loss_filter.normal_flow120_min
                )

        signal = reason = kind = None
        if trend_ready_now:
            signal = trend_direction
            reason = "dynamic_vol_trend_formation"
            kind = "trend"
        elif normal_ready_now:
            signal = normal_signal
            reason = normal_reason
            kind = "normal"
        if signal is None:
            continue
        settle_idx = idx + rules.horizon_sec
        entry = float(close[idx])
        settle = float(close[settle_idx])
        sign = 1.0 if signal == "UP" else -1.0
        margin = (settle / entry - 1.0) * 10000.0 * sign
        rows.append(
            {
                "dataset": name,
                "time": data.index[idx],
                "settle_time": data.index[settle_idx],
                "spec": spec.name,
                "volatility": bucket,
                "sigma_bps": sigma_bps,
                "kind": kind,
                "signal": signal,
                "reason": reason,
                "entry": entry,
                "settle": settle,
                "signed_outcome_bps": float(margin),
                "won": bool(margin > 0.0),
            }
        )
        last_entry_idx = idx
        trend_direction = None
        trend_start_idx = trend_last_idx = None
        normal_direction = normal_bucket = None
        normal_hits.clear()
    return rows


def metrics(rows: list[dict]) -> dict:
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
        "trades": count,
        "winRate": round(wins / count * 100.0, 2) if count else 0.0,
        "pnlU": int(equity),
        "maxDrawdownU": int(drawdown),
        "medianSignedBps": round(float(np.median(margins)), 3) if margins else 0.0,
    }


def summarize(rows: list[dict], datasets) -> dict:
    return {
        "overall": metrics(rows),
        "byVolatility": {bucket: metrics([row for row in rows if row["volatility"] == bucket]) for bucket in ("low", "mid", "high")},
        "byKind": {kind: metrics([row for row in rows if row["kind"] == kind]) for kind in ("normal", "trend")},
        "byDataset": {name: metrics([row for row in rows if row["dataset"] == name]) for name in datasets},
    }


def load_data(extra_dir: Path | None, extra_start: pd.Timestamp | None):
    rules = normal.load_rules()
    specs = dict(normal.DATASETS)
    if extra_dir is not None:
        specs["out_of_sample"] = {
            "seconds": extra_dir / "btcusdt_1s_trades.csv",
            "orderbook": extra_dir / "btcusdt_orderbook_1s.csv",
            "start": extra_start,
            "end": None,
        }
    loaded = {}
    cache = {}
    for name, item in specs.items():
        key = (str(item["seconds"]), str(item["orderbook"]))
        if key not in cache:
            cache[key] = normal.load_market(Path(item["seconds"]), Path(item["orderbook"]))
        full = cache[key]
        start = pd.Timestamp(item["start"]) if item["start"] is not None else full.index.min() + pd.Timedelta(seconds=3700)
        end = pd.Timestamp(item["end"]) if item["end"] is not None else full.index.max() - pd.Timedelta(seconds=rules.horizon_sec)
        data = full[
            (full.index >= start - pd.Timedelta(seconds=3700))
            & (full.index < end + pd.Timedelta(seconds=rules.horizon_sec + 5))
        ].copy()
        loaded[name] = (data, start, end)
    return loaded


def run(extra_dir: Path | None = None, extra_start: pd.Timestamp | None = None, spec_name: str | None = None) -> dict:
    raw = load_data(extra_dir, extra_start)
    reports = []
    all_rows = []
    selected_specs = [spec for spec in SPECS if spec_name is None or spec.name == spec_name]
    if not selected_specs:
        raise ValueError(f"unknown spec: {spec_name}")
    for spec in selected_specs:
        prepared = {name: (prepare(data, spec), start, end) for name, (data, start, end) in raw.items()}
        one_second = []
        for name, (item, start, end) in prepared.items():
            one_second.extend(replay(name, item, start, end, spec, scan_interval=1, scan_phase=0))
        phases = []
        for phase in range(5):
            phase_rows = []
            for name, (item, start, end) in prepared.items():
                phase_rows.extend(replay(name, item, start, end, spec, scan_interval=5, scan_phase=phase))
            phases.append({"phase": phase, **summarize(phase_rows, prepared)})
        reports.append(
            {
                "spec": asdict(spec),
                "oneSecond": summarize(one_second, prepared),
                "fiveSecondPhaseStress": phases,
            }
        )
        all_rows.extend(one_second)
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "volatilityBuckets": {"low": "sigma<8bp", "mid": "8<=sigma<14bp", "high": "sigma>=14bp"},
        "reports": reports,
        "note": "Fixed volatility buckets and three predefined parameter profiles; no result-driven grid search.",
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(all_rows)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--extra-dir", type=Path)
    parser.add_argument("--extra-start")
    parser.add_argument("--spec")
    args = parser.parse_args()
    start = pd.Timestamp(args.extra_start) if args.extra_start else None
    print(json.dumps(clean(run(args.extra_dir, start, args.spec)), ensure_ascii=False, indent=2))
