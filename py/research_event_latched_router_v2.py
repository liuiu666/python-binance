"""Test execution-time validation for latched normal/trend signals.

The detector still runs every second and the executor still runs every five
seconds. A latched normal signal may only be consumed while the passive order
book evidence that created it is still present. Invalid signals remain latched
until expiry, allowing a later executor tick to consume them if support or
resistance returns.
"""

from __future__ import annotations

import json
import math
import sys
from collections import deque
from dataclasses import dataclass
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


OUT_JSON = ROOT / "tmp" / "event_latched_router_v2_research.json"
OUT_TRADES = ROOT / "tmp" / "event_latched_router_v2_trades.csv"


@dataclass(frozen=True)
class ValidationSpec:
    name: str
    recheck_book: bool
    recheck_flow: bool = False
    normal_window_sec: int = 0
    normal_ratio_min: float = 0.0


SPECS = (
    ValidationSpec("no_recheck", False),
    ValidationSpec("book_recheck", True),
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


def normal_filter_ok(states: pd.DataFrame, idx: int, direction: str, params: fine.BandParams) -> bool:
    sign = 1.0 if direction == "UP" else -1.0
    return (
        sign * float(states["ret_600s_bps"].iloc[idx]) >= params.ret600_min_bps
        and sign * float(states["flow_120_mean"].iloc[idx]) >= params.flow120_min
    )


def passive_book_valid(states: pd.DataFrame, idx: int, signal: str, rules, require_flow: bool) -> bool:
    row = states.iloc[idx]
    sign = 1.0 if signal == "UP" else -1.0
    imbalance = sign * float(row["imbalance_20"])
    micro = sign * float(row["micro_bps"])
    bid = float(row["bid_qty_20"])
    ask = float(row["ask_qty_20"])
    supporting = bid if signal == "UP" else ask
    opposing = ask if signal == "UP" else bid
    wall_change = float(row["bid20_chg_30"] if signal == "UP" else row["ask20_chg_30"])
    valid = (
        np.isfinite([imbalance, micro, supporting, opposing, wall_change]).all()
        and imbalance >= rules.ob_imbalance_min
        and micro >= rules.micro_min_bps
        and supporting >= max(1e-9, opposing * rules.wall_ratio_min)
        and wall_change > -0.55
    )
    if require_flow:
        flow = sign * float(row["flow_60"])
        valid = valid and np.isfinite(flow) and flow >= -rules.flow_guard
    return bool(valid)


def normal_persistence_ok(states: pd.DataFrame, idx: int, spec: ValidationSpec) -> bool:
    if spec.normal_window_sec <= 0:
        return True
    start = max(0, idx - spec.normal_window_sec + 1)
    window = states["state"].iloc[start : idx + 1]
    return bool((window == "normal").mean() >= spec.normal_ratio_min)


def replay(name, prepared, start, end, fine_spec, validation, *, latch_sec, scan_phase):
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
    latched = None
    last_entry_idx = -10**12
    rows = []

    for idx in range(first_idx, last_idx):
        sigma_bps = float(states["sigma_bps"].iloc[idx])
        band = fine.band_name(sigma_bps)
        params = fine.band_params(fine_spec, band)
        state = str(states["state"].iloc[idx])
        direction = "UP" if float(states["trend_direction"].iloc[idx]) > 0 else "DOWN"

        if state == "trend_formation" and band in {"mid", "elevated", "high"}:
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
                        "kind": "normal", "signal": candidate_direction,
                        "reason": str(candidate["reason"]), "band": band,
                        "created_idx": idx, "expires_idx": idx + latch_sec,
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
                "kind": "trend", "signal": trend_direction,
                "reason": "latched_fine_vol_trend_formation", "band": band,
                "created_idx": idx, "expires_idx": idx + latch_sec,
            }

        if latched is not None and idx > int(latched["expires_idx"]):
            latched = None
        if int(data.index[idx].timestamp()) % 5 != scan_phase:
            continue
        if latched is None or idx - last_entry_idx < rules.min_gap_sec:
            continue
        if not bool(states["data_quality_ready"].iloc[idx]):
            continue
        if latched["kind"] == "normal":
            if validation.recheck_book and not passive_book_valid(
                states, idx, str(latched["signal"]), rules, validation.recheck_flow
            ):
                continue
            if not normal_persistence_ok(states, idx, validation):
                continue

        signal = str(latched["signal"])
        settle_idx = idx + rules.horizon_sec
        entry = float(close[idx])
        settle = float(close[settle_idx])
        sign = 1.0 if signal == "UP" else -1.0
        margin = (settle / entry - 1.0) * 10000.0 * sign
        rows.append({
            "dataset": name, "time": data.index[idx],
            "event_time": data.index[int(latched["created_idx"])],
            "settle_time": data.index[settle_idx], "validation": validation.name,
            "latch_sec": latch_sec, "scan_phase": scan_phase,
            "delay_sec": idx - int(latched["created_idx"]), "kind": latched["kind"],
            "band": latched["band"], "signal": signal, "reason": latched["reason"],
            "entry": entry, "settle": settle, "signed_outcome_bps": float(margin),
            "won": bool(margin > 0.0),
        })
        last_entry_idx = idx
        latched = None
        trend_direction = None
        trend_start_idx = trend_last_idx = None
        normal_direction = normal_band = None
        normal_hits.clear()
    return rows


def metrics(rows):
    equity = peak = drawdown = wins = 0
    for row in sorted(rows, key=lambda item: (item["dataset"], item["time"])):
        wins += int(row["won"])
        equity += 4 if row["won"] else -5
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    count = len(rows)
    return {
        "trades": count,
        "winRate": round(wins / count * 100.0, 2) if count else 0.0,
        "pnlU": int(equity),
        "maxDrawdownU": int(drawdown),
    }


def summarize(rows, datasets):
    return {
        "overall": metrics(rows),
        "byKind": {kind: metrics([row for row in rows if row["kind"] == kind]) for kind in ("normal", "trend")},
        "byDataset": {name: metrics([row for row in rows if row["dataset"] == name]) for name in datasets},
    }


def run():
    fine_spec = next(item for item in fine.SPECS if item.name == "fine_stable")
    print("[1/3] loading local second bars and orderbook data", flush=True)
    raw = coarse.load_data(None, None)
    prepared = {}
    for position, (name, (data, start, end)) in enumerate(raw.items(), start=1):
        print(f"[2/3] preparing {name} ({position}/{len(raw)})", flush=True)
        prepared[name] = (fine.prepare(data, fine_spec), start, end)
    reports = []
    all_rows = []
    for validation in SPECS:
        phases = []
        for phase in range(5):
            print(f"[3/3] replaying {validation.name}, phase {phase}/4", flush=True)
            rows = []
            for name, (item, start, end) in prepared.items():
                rows.extend(replay(name, item, start, end, fine_spec, validation, latch_sec=6, scan_phase=phase))
            phases.append({"phase": phase, **summarize(rows, prepared)})
            all_rows.extend(rows)
        reports.append({"validation": validation.name, "phases": phases})
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "baseSpec": fine_spec.name,
        "latchSec": 6,
        "reports": reports,
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(all_rows)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
