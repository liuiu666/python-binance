"""Corrected past-only regime router with hard gates and continuous confirmation."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_market_regime_classifier as regime_features  # noqa: E402
import research_v2_persistent_reclaim as normal  # noqa: E402


OUT_JSON = ROOT / "tmp" / "regime_router_corrected_research.json"
OUT_TRADES = ROOT / "tmp" / "regime_router_corrected_trades.csv"


@dataclass(frozen=True)
class LossFilterSpec:
    name: str
    normal_ret600_min_bps: float
    normal_flow120_min: float
    trend_hot_ret60_bps: float
    trend_hot_ret1800_bps: float
    trend_hot_flow120: float
    trend_hot_sigma_expand: float
    trend_hot_votes_max: int = 1


FILTER_SPECS = (
    LossFilterSpec("baseline", -1e9, -1e9, 1e9, 1e9, 1e9, 1e9, 4),
    LossFilterSpec("loose", -12.0, -0.08, 18.0, 60.0, 0.50, 1.25),
    LossFilterSpec("balanced", -10.0, -0.08, 15.0, 50.0, 0.40, 1.20),
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


def build_states(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    rules = normal.load_rules()
    direction = np.sign(out["ret_600s_bps"]).replace(0.0, np.nan)
    data_quality = (
        (out["observed_pct"] >= 90.0)
        & out["ob_available"].fillna(False).astype(bool)
        & (out["ob_age_sec"] <= 3.0)
        & (out["ob_coverage_60"] >= 0.90)
    )
    position_ok = np.where(direction > 0.0, out["pos_600s"] >= 0.80, out["pos_600s"] <= 0.20)
    aligned_bandwalk = direction * out["bandwalk_signed"]
    trend_core = (
        data_quality
        & position_ok
        & (out["ret_600s_bps"].abs() >= 20.0)
        & (out["efficiency_600"] >= 0.10)
        & (direction * out["ret_60s_bps"] >= 4.0)
        & (direction * out["imbalance_60_mean"] >= 0.16)
        & (aligned_bandwalk >= 0.0)
        & (aligned_bandwalk <= 0.60)
        & (out["sigma_expand"] <= 1.60)
    )
    trend_votes = pd.DataFrame(
        {
            "slope": direction * out["center_slope_bps"] >= 6.0,
            "long_alignment": direction * out["ret_1800s_bps"] >= 20.0,
            "flow": direction * out["flow_120_mean"] >= 0.04,
        },
        index=out.index,
    ).sum(axis=1)
    trend_formation = trend_core & (trend_votes >= 2)

    normal_slope_max = min(rules.center_slope_max_bps, rules.trend_space_center_slope_abs_max_bps)
    normal_state = (
        data_quality
        & (out["inside1_ratio"] >= rules.inside_min)
        & (out["center_slope_bps"].abs() <= normal_slope_max)
        & (out["sigma_bps"] >= rules.sigma_min_bps)
        & (out["sigma_bps"] <= rules.sigma_max_bps)
        & (out["sigma_expand"] <= min(rules.sigma_expand_max, rules.trend_space_sigma_expand_max))
    )
    out["data_quality_ready"] = data_quality
    out["trend_direction"] = direction
    out["trend_votes_corrected"] = trend_votes
    out["trend_formation"] = trend_formation
    out["normal_state"] = normal_state
    out["state"] = "transition"
    out.loc[normal_state, "state"] = "normal"
    out.loc[trend_formation, "state"] = "trend_formation"
    return out


def prepare_dataset(data: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    rules = normal.load_rules()
    features = regime_features.build_regime_features(data)
    features["ob_coverage_60"] = features["ob_available"].astype(float).rolling(60, min_periods=60).mean()
    states = build_states(features)
    _, candidates = normal.candidate_stream(data, rules)
    return {
        "data": data,
        "states": states,
        "candidate_map": {int(row["idx"]): row for row in candidates if start <= row["time"] < end},
    }


def replay(
    name: str,
    prepared: dict,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    scan_interval: int,
    scan_phase: int,
    loss_filter: LossFilterSpec,
) -> list[dict]:
    rules = normal.load_rules()
    data = prepared["data"]
    states = prepared["states"]
    candidate_map = prepared["candidate_map"]
    close = data["close"].to_numpy(float)
    first_idx = int(data.index.searchsorted(start))
    last_idx = min(len(data) - rules.horizon_sec, int(data.index.searchsorted(end)))

    trend_direction: str | None = None
    trend_start_idx: int | None = None
    trend_last_idx: int | None = None
    normal_direction: str | None = None
    normal_hits: deque[int] = deque()
    last_entry_idx = -10**12
    rows: list[dict] = []

    for idx in range(first_idx, last_idx):
        if int(data.index[idx].timestamp()) % scan_interval != scan_phase:
            continue
        state = str(states["state"].iloc[idx])
        current_trend_direction = "UP" if float(states["trend_direction"].iloc[idx]) > 0.0 else "DOWN"

        if state == "trend_formation":
            is_continuous = (
                trend_direction == current_trend_direction
                and trend_last_idx is not None
                and idx - trend_last_idx == scan_interval
            )
            if not is_continuous:
                trend_direction = current_trend_direction
                trend_start_idx = idx
            trend_last_idx = idx
        else:
            trend_direction = None
            trend_start_idx = None
            trend_last_idx = None

        candidate = candidate_map.get(idx)
        normal_ready = False
        normal_signal = None
        normal_reason = None
        if state != "normal":
            normal_direction = None
            normal_hits.clear()
        elif candidate is not None:
            candidate_direction = str(candidate["signal"])
            if normal_direction is not None and candidate_direction != normal_direction:
                normal_hits.clear()
            normal_direction = candidate_direction
            normal_hits.append(idx)
            while normal_hits and idx - normal_hits[0] > 30:
                normal_hits.popleft()
            normal_ready = len(normal_hits) >= 3 and idx - normal_hits[0] >= 8
            normal_signal = candidate_direction
            normal_reason = str(candidate["reason"])

        if idx - last_entry_idx < rules.min_gap_sec:
            continue
        trend_ready = (
            trend_direction is not None
            and trend_start_idx is not None
            and idx - trend_start_idx >= 20
        )
        if trend_ready:
            trend_sign = 1.0 if trend_direction == "UP" else -1.0
            trend_ready = (
                trend_sign * float(states["imbalance_20"].iloc[idx]) >= 0.08
                and trend_sign * float(states["micro_bps"].iloc[idx]) >= 0.001
            )
            if trend_ready:
                hot_votes = sum(
                    (
                        trend_sign * float(states["ret_60s_bps"].iloc[idx]) >= loss_filter.trend_hot_ret60_bps,
                        trend_sign * float(states["ret_1800s_bps"].iloc[idx]) >= loss_filter.trend_hot_ret1800_bps,
                        trend_sign * float(states["flow_120_mean"].iloc[idx]) >= loss_filter.trend_hot_flow120,
                        float(states["sigma_expand"].iloc[idx]) >= loss_filter.trend_hot_sigma_expand,
                    )
                )
                trend_ready = hot_votes <= loss_filter.trend_hot_votes_max
        if normal_ready:
            normal_sign = 1.0 if normal_signal == "UP" else -1.0
            normal_ready = (
                normal_sign * float(states["ret_600s_bps"].iloc[idx]) >= loss_filter.normal_ret600_min_bps
                and normal_sign * float(states["flow_120_mean"].iloc[idx]) >= loss_filter.normal_flow120_min
            )
        signal = reason = kind = None
        if trend_ready:
            signal = trend_direction
            reason = "hard_trend_formation_continuous_20s"
            kind = "trend"
        elif normal_ready:
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
                "kind": kind,
                "state": state,
                "signal": signal,
                "reason": reason,
                "entry": entry,
                "settle": settle,
                "signed_outcome_bps": float(margin),
                "won": bool(margin > 0.0),
                "observed_pct": float(states["observed_pct"].iloc[idx]),
                "ob_coverage_60": float(states["ob_coverage_60"].iloc[idx]),
                "scan_interval": scan_interval,
                "scan_phase": scan_phase,
                "loss_filter": loss_filter.name,
            }
        )
        last_entry_idx = idx
        trend_direction = None
        trend_start_idx = None
        trend_last_idx = None
        normal_direction = None
        normal_hits.clear()
    return rows


def metrics(rows: list[dict]) -> dict:
    ordered = sorted(rows, key=lambda row: (row["dataset"], row["time"]))
    equity = peak = drawdown = wins = 0
    margins = []
    for row in ordered:
        won = bool(row["won"])
        wins += int(won)
        equity += 4 if won else -5
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        margins.append(float(row["signed_outcome_bps"]))
    count = len(ordered)
    return {
        "trades": count,
        "winRate": round(wins / count * 100.0, 2) if count else 0.0,
        "pnlU": int(equity),
        "maxDrawdownU": int(drawdown),
        "medianSignedBps": round(float(np.median(margins)), 3) if margins else 0.0,
        "thinAbsLe5": sum(abs(value) <= 5.0 for value in margins),
    }


def summarize(rows: list[dict], dataset_names) -> dict:
    return {
        "overall": metrics(rows),
        "byKind": {kind: metrics([row for row in rows if row["kind"] == kind]) for kind in ("normal", "trend")},
        "byDataset": {name: metrics([row for row in rows if row["dataset"] == name]) for name in dataset_names},
    }


def load_sources(extra_dir: Path | None = None, extra_start: pd.Timestamp | None = None) -> dict:
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
        end = pd.Timestamp(item["end"]) if item["end"] else full.index.max() - pd.Timedelta(seconds=rules.horizon_sec)
        data = full[
            (full.index >= start - pd.Timedelta(seconds=3700))
            & (full.index < end + pd.Timedelta(seconds=rules.horizon_sec + 5))
        ].copy()
        loaded[name] = (prepare_dataset(data, start, end), start, end)
    return loaded


def run(extra_dir: Path | None = None, extra_start: pd.Timestamp | None = None) -> dict:
    loaded = load_sources(extra_dir, extra_start)
    dataset_names = list(loaded)
    reports = []
    all_rows = []
    for loss_filter in FILTER_SPECS:
        one_second = []
        for name, (prepared, start, end) in loaded.items():
            one_second.extend(
                replay(
                    name,
                    prepared,
                    start,
                    end,
                    scan_interval=1,
                    scan_phase=0,
                    loss_filter=loss_filter,
                )
            )
        phases = []
        for phase in range(5):
            phase_rows = []
            for name, (prepared, start, end) in loaded.items():
                phase_rows.extend(
                    replay(
                        name,
                        prepared,
                        start,
                        end,
                        scan_interval=5,
                        scan_phase=phase,
                        loss_filter=loss_filter,
                    )
                )
            phases.append({"phase": phase, **summarize(phase_rows, dataset_names)})
        reports.append(
            {
                "filter": asdict(loss_filter),
                "oneSecond": summarize(one_second, dataset_names),
                "fiveSecondPhaseStress": phases,
            }
        )
        all_rows.extend(one_second)
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "rules": {
            "trend": "hard data/10m-return/efficiency/position/recent-move/rolling-orderbook/bandwalk/sigma gates + at least 2 of 3 context votes + continuous 20 seconds; current orderbook confirms entry",
            "normal": "shared V2 normal-state hard gates + stricter data quality + 3 same-direction candidates in 30 seconds spanning 8 seconds",
            "transition": "no trade and reset confirmations",
            "dataQuality": "observed>=90%, current orderbook fresh, 60s orderbook coverage>=90%",
        },
        "reports": reports,
        "note": "Parameters are frozen. No grid search or ranking is performed in this script.",
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(all_rows)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extra-dir", type=Path)
    parser.add_argument("--extra-start")
    args = parser.parse_args()
    extra_start = pd.Timestamp(args.extra_start) if args.extra_start else None
    print(json.dumps(clean(run(args.extra_dir, extra_start)), ensure_ascii=False, indent=2))
