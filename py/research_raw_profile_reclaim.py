"""Causal raw-depth test of absorbed value-area breakouts and reclaims.

The chain is intentionally strict: freeze the old volume-profile value area,
observe a directional breakout, require near-touch absorption while outside,
then wait for price, taker flow, and near-touch liquidity to reclaim the old
area before emitting a ten-minute reversal measurement.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research_volume_profile_auction_state import PRICE_BIN, VALUE_AREA_PCT, value_area


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "tmp" / "auction-live-features" / "BTCUSDT" / "futures" / "date=2026-07-12" / "features.jsonl"
DEFAULT_OUT = ROOT / "tmp" / "raw_profile_reclaim_20260712.json"
DEFAULT_TRADES = ROOT / "tmp" / "raw_profile_reclaim_20260712.csv"
PROFILE_SEC = 1800
THRESHOLD_SEC = 900
HORIZON_SEC = 600
COOLDOWN_SEC = 600
MAX_PROBE_SEC = 300


@dataclass
class Probe:
    side: str
    boundary: float
    started_index: int
    absorbed: bool = False
    absorption_count: int = 0
    vacuum_count: int = 0


def load_rows(path: Path) -> pd.DataFrame:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    frame = pd.DataFrame(rows)
    frame["time"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.set_index("time").sort_index()


def metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"trades": 0, "winRate": None, "pnlU": 0.0, "maxLossStreak": 0, "avgSignedBps": None}
    wins = sum(bool(row["won"]) for row in rows)
    current = max_streak = 0
    for row in rows:
        if row["won"]:
            current = 0
        else:
            current += 1
            max_streak = max(max_streak, current)
    return {
        "trades": len(rows),
        "winRate": round(wins / len(rows) * 100.0, 2),
        "pnlU": round(wins * 4.0 - (len(rows) - wins) * 5.0, 2),
        "maxLossStreak": max_streak,
        "avgSignedBps": round(float(np.mean([row["signed_bps"] for row in rows])), 3),
    }


def run(frame: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    required = {
        "mid",
        "buy_qty",
        "sell_qty",
        "flow_imbalance_30s",
        "ret_30s_bps",
        "near_liquidity_pressure_ratio_30s",
        "near_liquidity_coverage_30s",
        "depth_coverage_60s",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        return {
            "status": "waiting_near_touch_data",
            "missing": missing,
            "caution": "The strategy cannot fall back to full-book absolute liquidity fields.",
        }, pd.DataFrame()

    mid = frame["mid"].astype(float)
    volume = frame["buy_qty"].astype(float) + frame["sell_qty"].astype(float)
    flow = frame["flow_imbalance_30s"].astype(float)
    ret = frame["ret_30s_bps"].astype(float)
    pressure = frame["near_liquidity_pressure_ratio_30s"].astype(float)
    near_coverage = frame["near_liquidity_coverage_30s"].astype(float)
    depth_coverage = frame["depth_coverage_60s"].astype(float)
    min_periods = max(300, THRESHOLD_SEC // 3)
    flow_high = flow.abs().shift(1).rolling(THRESHOLD_SEC, min_periods=min_periods).quantile(0.75)
    ret_low = ret.abs().shift(1).rolling(THRESHOLD_SEC, min_periods=min_periods).quantile(0.50)
    ret_high = ret.abs().shift(1).rolling(THRESHOLD_SEC, min_periods=min_periods).quantile(0.75)
    pressure_high = pressure.abs().shift(1).rolling(THRESHOLD_SEC, min_periods=min_periods).quantile(0.75)

    histogram: dict[int, float] = defaultdict(float)
    profile_rows: deque[tuple[int, float]] = deque()
    probe: Probe | None = None
    last_entry_index = -COOLDOWN_SEC
    trades: list[dict[str, Any]] = []
    probe_audit: list[dict[str, Any]] = []
    diagnostics = {
        "breakouts": 0,
        "absorbedOutside": 0,
        "reclaims": 0,
        "confirmedReclaims": 0,
        "reclaimWithoutAbsorption": 0,
        "reclaimVacuumVeto": 0,
        "reclaimMissingInwardFlow": 0,
        "reclaimMissingInwardPrice": 0,
        "reclaimMissingInwardLiquidity": 0,
        "unsettledConfirmedReclaims": 0,
        "expired": 0,
    }

    for index in range(len(frame)):
        price = float(mid.iloc[index]) if np.isfinite(mid.iloc[index]) else 0.0
        amount = max(0.0, float(volume.iloc[index]))
        bucket = int(np.floor(price / PRICE_BIN)) if price > 0.0 else 0
        values = (
            price,
            flow.iloc[index],
            ret.iloc[index],
            pressure.iloc[index],
            near_coverage.iloc[index],
            depth_coverage.iloc[index],
            flow_high.iloc[index],
            ret_low.iloc[index],
            ret_high.iloc[index],
            pressure_high.iloc[index],
        )
        eligible = (
            index >= max(PROFILE_SEC, THRESHOLD_SEC)
            and all(np.isfinite(float(value)) for value in values)
            and near_coverage.iloc[index] >= 0.95
            and depth_coverage.iloc[index] >= 0.95
        )

        # The profile contains only seconds before the current event.
        if eligible:
            area = value_area(histogram, sum(histogram.values()))
            if area:
                _, val, vah = area
                flow_now = float(flow.iloc[index])
                ret_now = float(ret.iloc[index])
                pressure_now = float(pressure.iloc[index])
                break_bps = max(0.5, float(ret_high.iloc[index]))

                if probe is None:
                    side = "UP" if price >= vah * (1.0 + break_bps / 10000.0) else "DOWN" if price <= val * (1.0 - break_bps / 10000.0) else None
                    direction = 1.0 if side == "UP" else -1.0 if side == "DOWN" else 0.0
                    if side and direction * flow_now >= float(flow_high.iloc[index]):
                        probe = Probe(side=side, boundary=vah if side == "UP" else val, started_index=index)
                        diagnostics["breakouts"] += 1
                else:
                    direction = 1.0 if probe.side == "UP" else -1.0
                    outside = price > probe.boundary if probe.side == "UP" else price < probe.boundary
                    age = index - probe.started_index
                    if outside:
                        aggressive_push = direction * flow_now >= float(flow_high.iloc[index])
                        stalled = abs(ret_now) <= float(ret_low.iloc[index])
                        opposite_liquidity = direction * pressure_now <= -float(pressure_high.iloc[index])
                        advancing = direction * ret_now >= float(ret_high.iloc[index])
                        aligned_liquidity = direction * pressure_now >= float(pressure_high.iloc[index])
                        if aggressive_push and stalled and opposite_liquidity:
                            probe.absorbed = True
                            probe.absorption_count += 1
                            diagnostics["absorbedOutside"] += 1
                        if aggressive_push and advancing and aligned_liquidity:
                            probe.vacuum_count += 1
                    else:
                        diagnostics["reclaims"] += 1
                        inward_flow = direction * flow_now <= -max(0.05, float(flow_high.iloc[index]) * 0.5)
                        inward_price = direction * ret_now <= -float(ret_high.iloc[index])
                        inward_liquidity = direction * pressure_now <= -float(pressure_high.iloc[index])
                        if not probe.absorbed:
                            diagnostics["reclaimWithoutAbsorption"] += 1
                        if probe.vacuum_count > 0:
                            diagnostics["reclaimVacuumVeto"] += 1
                        if not inward_flow:
                            diagnostics["reclaimMissingInwardFlow"] += 1
                        if not inward_price:
                            diagnostics["reclaimMissingInwardPrice"] += 1
                        if not inward_liquidity:
                            diagnostics["reclaimMissingInwardLiquidity"] += 1
                        # Any observed vacuum evidence means the outside auction
                        # achieved efficient progress; a later return is not a
                        # clean failed auction and is not traded.
                        confirmed = probe.absorbed and probe.vacuum_count == 0 and inward_flow and inward_price and inward_liquidity
                        probe_audit.append({
                            "started_time": frame.index[probe.started_index].isoformat(),
                            "ended_time": frame.index[index].isoformat(),
                            "side": probe.side,
                            "end_state": "reclaimed",
                            "age_sec": age,
                            "absorbed": probe.absorbed,
                            "absorption_count": probe.absorption_count,
                            "vacuum_count": probe.vacuum_count,
                            "inward_flow": inward_flow,
                            "inward_price": inward_price,
                            "inward_liquidity": inward_liquidity,
                            "confirmed": confirmed,
                        })
                        if confirmed:
                            diagnostics["confirmedReclaims"] += 1
                            if index - last_entry_index >= COOLDOWN_SEC:
                                signal = "DOWN" if probe.side == "UP" else "UP"
                                if index + HORIZON_SEC < len(frame):
                                    entry = price
                                    settle = float(mid.iloc[index + HORIZON_SEC])
                                    signed_bps = (settle / entry - 1.0) * 10000.0 * (1.0 if signal == "UP" else -1.0)
                                    trades.append({
                                        "detected_time": frame.index[index].isoformat(),
                                        "settle_time": frame.index[index + HORIZON_SEC].isoformat(),
                                        "signal": signal,
                                        "probe_side": probe.side,
                                        "probe_age_sec": age,
                                        "absorption_count": probe.absorption_count,
                                        "vacuum_count": probe.vacuum_count,
                                        "old_boundary": probe.boundary,
                                        "entry": entry,
                                        "settle": settle,
                                        "signed_bps": signed_bps,
                                        "won": bool(signed_bps > 0.0),
                                    })
                                else:
                                    diagnostics["unsettledConfirmedReclaims"] += 1
                                last_entry_index = index
                        probe = None
                    if probe is not None and age >= MAX_PROBE_SEC:
                        diagnostics["expired"] += 1
                        probe_audit.append({
                            "started_time": frame.index[probe.started_index].isoformat(),
                            "ended_time": frame.index[index].isoformat(),
                            "side": probe.side,
                            "end_state": "expired",
                            "age_sec": age,
                            "absorbed": probe.absorbed,
                            "absorption_count": probe.absorption_count,
                            "vacuum_count": probe.vacuum_count,
                            "confirmed": False,
                        })
                        probe = None

        if price > 0.0:
            histogram[bucket] += amount
            profile_rows.append((bucket, amount))
            if len(profile_rows) > PROFILE_SEC:
                old_bucket, old_amount = profile_rows.popleft()
                histogram[old_bucket] -= old_amount
                if histogram[old_bucket] <= 1e-12:
                    histogram.pop(old_bucket, None)

    active_probe = None
    if probe is not None:
        active_probe = {
            "started_time": frame.index[probe.started_index].isoformat(),
            "side": probe.side,
            "age_sec_at_sample_end": len(frame) - 1 - probe.started_index,
            "absorbed": probe.absorbed,
            "absorption_count": probe.absorption_count,
            "vacuum_count": probe.vacuum_count,
        }

    report = {
        "method": {
            "chain": "Frozen old value-area breakout -> raw near-touch absorption outside -> full reclaim with opposite price, flow and liquidity.",
            "profile": f"Previous {PROFILE_SEC}s, ${PRICE_BIN:g} bins, contiguous {int(VALUE_AREA_PCT * 100)}% value area.",
            "thresholds": f"Causal rolling quantiles from the preceding {THRESHOLD_SEC}s; no full-day threshold leakage.",
            "veto": "Any efficient liquidity-vacuum observation during the outside probe vetoes reversal.",
            "execution": f"Completed-second detection, {HORIZON_SEC}s settlement and {COOLDOWN_SEC}s lock.",
            "parameterSearch": False,
        },
        "sample": {
            "start": frame.index.min().isoformat(),
            "end": frame.index.max().isoformat(),
            "hours": round((frame.index.max() - frame.index.min()).total_seconds() / 3600.0, 2),
            "seconds": len(frame),
        },
        "diagnostics": diagnostics,
        "probeAudit": probe_audit,
        "activeProbe": active_probe,
        "result": metric(trades),
        "byDirection": {side: metric([row for row in trades if row["signal"] == side]) for side in ("UP", "DOWN")},
        "caution": "Do not deploy until several independent near-touch raw-data segments are available.",
    }
    return report, pd.DataFrame(trades)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES)
    args = parser.parse_args()
    report, trades = run(load_rows(args.input))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(args.trades, index=False, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
