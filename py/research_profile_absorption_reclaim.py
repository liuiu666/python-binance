"""Causal local study of absorption followed by value-area reclaim.

This deliberately trades no accepted breakout.  It asks a narrower question:
after an aggressive breakout fails to progress, does opposite passive liquidity
and a reclaim of the frozen old value area create a usable ten-minute reversal?
Old order-book snapshots are only a proxy; raw depth deltas are the later
validation source.
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

from research_normal_liquidity_orderbook import load_local_data
from research_volume_profile_auction_state import PRICE_BIN, VALUE_AREA_PCT, bps_move, value_area


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECONDS = ROOT / "tmp" / "latest_live_pull_20260709_101331" / "data" / "btcusdt_1s_trades.csv"
DEFAULT_ORDERBOOK = ROOT / "tmp" / "latest_live_pull_20260709_101331" / "data" / "btcusdt_orderbook_1s.csv"
DEFAULT_OUT = ROOT / "tmp" / "profile_absorption_reclaim_20260708.json"
DEFAULT_TRADES = ROOT / "tmp" / "profile_absorption_reclaim_20260708.csv"
PROFILE_SEC = 1800
FLOW_WINDOW_SEC = 30
HORIZON_SEC = 600
EXECUTION_DELAY_SEC = 2


@dataclass
class Probe:
    side: str
    boundary: float
    started_index: int
    flow_threshold: float
    ret_threshold_bps: float
    start_bid: float
    start_ask: float
    peak_directional_flow: float
    max_outer_bps: float = 0.0


def finite(value: Any) -> bool:
    try:
        return np.isfinite(float(value))
    except (TypeError, ValueError):
        return False


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


def run(data: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    close = data["close"].astype(float)
    volume = data["volume"].astype(float).clip(lower=0.0)
    observed = data["observed"].astype(bool)
    buy = data["buy_qty"].astype(float).clip(lower=0.0)
    sell = data["sell_qty"].astype(float).clip(lower=0.0)
    bid = data["bid_qty_20"].astype(float)
    ask = data["ask_qty_20"].astype(float)
    imbalance = data["imbalance_20"].astype(float)
    flow = (buy.rolling(FLOW_WINDOW_SEC, min_periods=10).sum() - sell.rolling(FLOW_WINDOW_SEC, min_periods=10).sum()) / (
        buy.rolling(FLOW_WINDOW_SEC, min_periods=10).sum() + sell.rolling(FLOW_WINDOW_SEC, min_periods=10).sum()
    ).replace(0.0, np.nan)
    ret = (close / close.shift(FLOW_WINDOW_SEC) - 1.0) * 10000.0
    # These are rolling distribution thresholds, not fixed BTC price moves.
    flow_threshold = flow.abs().rolling(PROFILE_SEC, min_periods=600).quantile(0.75)
    ret_threshold = ret.abs().rolling(PROFILE_SEC, min_periods=600).quantile(0.75)

    histogram: dict[int, float] = defaultdict(float)
    values: deque[tuple[int, float]] = deque()
    probe: Probe | None = None
    rows: list[dict[str, Any]] = []
    probes = {"started": 0, "reclaimed": 0, "absorptionConfirmed": 0, "expired": 0}
    last_entry_index = -HORIZON_SEC

    for index, timestamp in enumerate(data.index):
        price = float(close.iloc[index])
        amount = float(volume.iloc[index])
        bucket = int(np.floor(price / PRICE_BIN))
        eligible = (
            index >= PROFILE_SEC
            and index + EXECUTION_DELAY_SEC + HORIZON_SEC < len(data)
            and float(observed.iloc[index - PROFILE_SEC : index].mean()) >= 0.95
            and finite(flow.iloc[index])
            and finite(flow_threshold.iloc[index])
            and finite(ret_threshold.iloc[index])
            and finite(bid.iloc[index])
            and finite(ask.iloc[index])
            and finite(imbalance.iloc[index])
        )

        # The profile is the preceding 30 minutes only; current data cannot
        # rewrite the old value area that a probe must later reclaim.
        if eligible:
            area = value_area(histogram, sum(histogram.values()))
            if area:
                _, val, vah = area
                flow_now = float(flow.iloc[index])
                ret_now = float(ret.iloc[index]) if finite(ret.iloc[index]) else 0.0
                dynamic_break_bps = max(0.5, float(ret_threshold.iloc[index]))

                if probe is None:
                    side = "UP" if price >= vah * (1.0 + dynamic_break_bps / 10000.0) else "DOWN" if price <= val * (1.0 - dynamic_break_bps / 10000.0) else None
                    direction = 1.0 if side == "UP" else -1.0 if side == "DOWN" else 0.0
                    if side and direction * flow_now >= float(flow_threshold.iloc[index]):
                        probe = Probe(
                            side=side,
                            boundary=vah if side == "UP" else val,
                            started_index=index,
                            flow_threshold=float(flow_threshold.iloc[index]),
                            ret_threshold_bps=float(ret_threshold.iloc[index]),
                            start_bid=float(bid.iloc[index]),
                            start_ask=float(ask.iloc[index]),
                            peak_directional_flow=direction * flow_now,
                        )
                        probes["started"] += 1
                else:
                    direction = 1.0 if probe.side == "UP" else -1.0
                    is_outside = price > probe.boundary if probe.side == "UP" else price < probe.boundary
                    probe.peak_directional_flow = max(probe.peak_directional_flow, direction * flow_now)
                    probe.max_outer_bps = max(probe.max_outer_bps, max(0.0, direction * bps_move(price, probe.boundary)))
                    age = index - probe.started_index

                    if not is_outside:
                        probes["reclaimed"] += 1
                        current_opposite_depth = float(ask.iloc[index]) if probe.side == "UP" else float(bid.iloc[index])
                        starting_opposite_depth = probe.start_ask if probe.side == "UP" else probe.start_bid
                        opposite_book = (
                            direction * float(imbalance.iloc[index]) <= -0.08
                            and current_opposite_depth >= starting_opposite_depth * 0.8
                        )
                        inward_flow = direction * flow_now <= -max(0.05, probe.flow_threshold * 0.5)
                        inward_price = direction * ret_now <= -probe.ret_threshold_bps
                        # A large aggressive push that cannot travel more than
                        # four local 30-second moves is treated as absorption.
                        absorbed = (
                            probe.peak_directional_flow >= probe.flow_threshold
                            and probe.max_outer_bps <= 4.0 * probe.ret_threshold_bps
                        )
                        if absorbed and opposite_book and inward_flow and inward_price:
                            probes["absorptionConfirmed"] += 1
                            if index - last_entry_index >= HORIZON_SEC:
                                signal = "DOWN" if probe.side == "UP" else "UP"
                                entry_index = index + EXECUTION_DELAY_SEC
                                settle_index = entry_index + HORIZON_SEC
                                entry = float(close.iloc[entry_index])
                                settle = float(close.iloc[settle_index])
                                signed_bps = bps_move(settle, entry) * (1.0 if signal == "UP" else -1.0)
                                rows.append({
                                    "detected_time": timestamp.isoformat(),
                                    "entry_time": data.index[entry_index].isoformat(),
                                    "settle_time": data.index[settle_index].isoformat(),
                                    "signal": signal,
                                    "probe_side": probe.side,
                                    "probe_age_sec": age,
                                    "old_boundary": probe.boundary,
                                    "peak_directional_flow": probe.peak_directional_flow,
                                    "max_outer_bps": probe.max_outer_bps,
                                    "ret_threshold_bps": probe.ret_threshold_bps,
                                    "entry": entry,
                                    "settle": settle,
                                    "signed_bps": signed_bps,
                                    "won": bool(signed_bps > 0.0),
                                })
                                last_entry_index = index
                        probe = None
                    elif age >= 300:
                        probes["expired"] += 1
                        probe = None

        histogram[bucket] += amount
        values.append((bucket, amount))
        if len(values) > PROFILE_SEC:
            old_bucket, old_amount = values.popleft()
            histogram[old_bucket] -= old_amount
            if histogram[old_bucket] <= 1e-12:
                histogram.pop(old_bucket, None)

    report = {
        "method": {
            "goal": "Only trade an absorbed breakout after the frozen old value area is reclaimed.",
            "profile": f"Previous {PROFILE_SEC}s volume profile, ${PRICE_BIN:g} bins, contiguous {int(VALUE_AREA_PCT * 100)}% value area.",
            "aggressiveBreakout": "30-second taker-flow imbalance must be above its prior 30-minute 75th percentile.",
            "absorption": "Peak directional flow is strong but the probe cannot progress beyond four local 30-second return thresholds.",
            "reclaim": "Price returns inside old value area with opposite taker flow, opposite book imbalance, and non-withdrawn opposite depth.",
            "execution": f"2s delay, {HORIZON_SEC}s settlement, {HORIZON_SEC}s lock, 5U stake and 80% payout.",
            "parameterSearch": False,
            "proxyWarning": "Historical book snapshots proxy liquidity changes; only new depth deltas can validate replenishment exactly.",
        },
        "sample": {
            "start": data.index.min().isoformat(),
            "end": data.index.max().isoformat(),
            "hours": round((data.index.max() - data.index.min()).total_seconds() / 3600.0, 2),
            "observedPct": round(float(observed.mean() * 100.0), 2),
        },
        "probes": probes,
        "result": metric(rows),
        "byDirection": {side: metric([row for row in rows if row["signal"] == side]) for side in ("UP", "DOWN")},
        "caution": "This is a fixed hypothesis test, not evidence for deployment until it survives independent raw-depth validation.",
    }
    return report, pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=Path, default=DEFAULT_SECONDS)
    parser.add_argument("--orderbook", type=Path, default=DEFAULT_ORDERBOOK)
    parser.add_argument("--start", default="2026-07-08T00:00:00Z")
    parser.add_argument("--end", default="2026-07-09T00:00:00Z")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES)
    args = parser.parse_args()
    data = load_local_data(args.seconds, args.orderbook)
    data = data[(data.index >= pd.Timestamp(args.start)) & (data.index < pd.Timestamp(args.end))].copy()
    report, rows = run(data)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rows.to_csv(args.trades, index=False, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
