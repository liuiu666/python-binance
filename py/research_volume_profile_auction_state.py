"""One-day causal test of volume-profile auction states.

This is an exploratory local research tool.  It freezes the old value area at
the moment of a breakout, then distinguishes a return into that area from a
short-lived acceptance outside it.  It is intentionally separate from every
live strategy.
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

from second_backtest.data import load_second_bars


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECONDS = ROOT / "tmp" / "latest_live_pull_20260709_101331" / "data" / "btcusdt_1s_trades.csv"
DEFAULT_OUT = ROOT / "tmp" / "volume_profile_auction_20260708.json"
DEFAULT_TRADES = ROOT / "tmp" / "volume_profile_auction_20260708.csv"
PROFILE_SEC = 1800
VALUE_AREA_PCT = 0.70
PRICE_BIN = 10.0
BREAK_BUFFER_BPS = 3.0
MIN_REJECT_SEC = 10
MIN_ACCEPT_SEC = 60
MIN_ACCEPT_OUTSIDE_PCT = 0.75
MIN_ACCEPT_MOVE_BPS = 3.0
HORIZON_SEC = 600
EXECUTION_DELAY_SEC = 2


@dataclass
class Probe:
    side: str
    boundary: float
    started_index: int
    started_price: float
    outside_volume: float = 0.0
    total_volume: float = 0.0
    outside_seconds: int = 0


def bps_move(price: float, reference: float) -> float:
    return (price / reference - 1.0) * 10000.0 if price > 0.0 and reference > 0.0 else 0.0


def value_area(histogram: dict[int, float], total_volume: float) -> tuple[float, float, float] | None:
    if total_volume <= 0.0 or not histogram:
        return None
    poc = max(histogram, key=histogram.get)
    low = high = poc
    included = float(histogram.get(poc, 0.0))
    target = total_volume * VALUE_AREA_PCT
    while included < target:
        left = float(histogram.get(low - 1, 0.0))
        right = float(histogram.get(high + 1, 0.0))
        if left <= 0.0 and right <= 0.0:
            break
        if right > left:
            high += 1
            included += right
        else:
            low -= 1
            included += left
    return poc * PRICE_BIN, low * PRICE_BIN, (high + 1) * PRICE_BIN


def metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"trades": 0, "winRate": None, "pnlU": 0.0, "maxLossStreak": 0, "avgSignedBps": None}
    wins = sum(bool(row["won"]) for row in rows)
    loss_streak = current = 0
    for row in rows:
        if row["won"]:
            current = 0
        else:
            current += 1
            loss_streak = max(loss_streak, current)
    return {
        "trades": len(rows),
        "winRate": round(wins / len(rows) * 100.0, 2),
        "pnlU": round(wins * 4.0 - (len(rows) - wins) * 5.0, 2),
        "maxLossStreak": loss_streak,
        "avgSignedBps": round(float(np.mean([row["signed_bps"] for row in rows])), 3),
    }


def run(bars: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    close = bars["close"].to_numpy(float)
    volume = bars["volume"].to_numpy(float)
    observed = bars["observed"].to_numpy(bool)
    bins = np.floor(close / PRICE_BIN).astype(int)
    histogram: dict[int, float] = defaultdict(float)
    values: deque[tuple[int, float]] = deque()
    probe: Probe | None = None
    last_entry_index = -HORIZON_SEC
    rows: list[dict[str, Any]] = []
    rejected_probes = accepted_probes = expired_probes = 0

    for index in range(len(bars)):
        bucket = int(bins[index])
        amount = max(0.0, float(volume[index]))
        eligible = (
            index >= PROFILE_SEC
            and index + EXECUTION_DELAY_SEC + HORIZON_SEC < len(bars)
            and observed[index - PROFILE_SEC : index].mean() >= 0.95
        )
        # `histogram` contains only the preceding 30 minutes here. This keeps
        # the value-area edge frozen before the current breakout second.
        if eligible:
            area = value_area(histogram, sum(histogram.values()))
            if area:
                _, val, vah = area
                price = float(close[index])
                upper_trigger = vah * (1.0 + BREAK_BUFFER_BPS / 10000.0)
                lower_trigger = val * (1.0 - BREAK_BUFFER_BPS / 10000.0)

                if probe is None:
                    if price >= upper_trigger:
                        probe = Probe("UP", vah, index, price)
                    elif price <= lower_trigger:
                        probe = Probe("DOWN", val, index, price)
                else:
                    age = index - probe.started_index
                    is_outside = price > probe.boundary if probe.side == "UP" else price < probe.boundary
                    probe.total_volume += amount
                    if is_outside:
                        probe.outside_seconds += 1
                        probe.outside_volume += amount

                    returned = not is_outside
                    if returned and age >= MIN_REJECT_SEC:
                        recent_index = max(probe.started_index, index - 30)
                        recent_move = bps_move(price, float(close[recent_index]))
                        inward = recent_move <= -MIN_ACCEPT_MOVE_BPS if probe.side == "UP" else recent_move >= MIN_ACCEPT_MOVE_BPS
                        if inward and index - last_entry_index >= HORIZON_SEC:
                            signal = "DOWN" if probe.side == "UP" else "UP"
                            entry_index = index + EXECUTION_DELAY_SEC
                            settle_index = entry_index + HORIZON_SEC
                            entry = float(close[entry_index])
                            settle = float(close[settle_index])
                            signed_bps = bps_move(settle, entry) * (1.0 if signal == "UP" else -1.0)
                            rows.append({
                                "detected_time": bars.index[index].isoformat(),
                                "entry_time": bars.index[entry_index].isoformat(),
                                "settle_time": bars.index[settle_index].isoformat(),
                                "state": "failed_auction",
                                "signal": signal,
                                "old_boundary": probe.boundary,
                                "probe_age_sec": age,
                                "outside_seconds": probe.outside_seconds,
                                "outside_volume_pct": probe.outside_volume / probe.total_volume if probe.total_volume else 0.0,
                                "entry": entry,
                                "settle": settle,
                                "signed_bps": signed_bps,
                                "won": bool(signed_bps > 0.0),
                            })
                            last_entry_index = index
                        rejected_probes += 1
                        probe = None
                    else:
                        outside_pct = probe.outside_seconds / max(age, 1)
                        outside_volume_pct = probe.outside_volume / probe.total_volume if probe.total_volume else 0.0
                        displacement = bps_move(price, probe.boundary)
                        accepted = (
                            age >= MIN_ACCEPT_SEC
                            and outside_pct >= MIN_ACCEPT_OUTSIDE_PCT
                            and outside_volume_pct >= MIN_ACCEPT_OUTSIDE_PCT
                            and (displacement >= MIN_ACCEPT_MOVE_BPS if probe.side == "UP" else displacement <= -MIN_ACCEPT_MOVE_BPS)
                        )
                        if accepted:
                            if index - last_entry_index >= HORIZON_SEC:
                                signal = probe.side
                                entry_index = index + EXECUTION_DELAY_SEC
                                settle_index = entry_index + HORIZON_SEC
                                entry = float(close[entry_index])
                                settle = float(close[settle_index])
                                signed_bps = bps_move(settle, entry) * (1.0 if signal == "UP" else -1.0)
                                rows.append({
                                    "detected_time": bars.index[index].isoformat(),
                                    "entry_time": bars.index[entry_index].isoformat(),
                                    "settle_time": bars.index[settle_index].isoformat(),
                                    "state": "accepted_migration",
                                    "signal": signal,
                                    "old_boundary": probe.boundary,
                                    "probe_age_sec": age,
                                    "outside_seconds": probe.outside_seconds,
                                    "outside_volume_pct": outside_volume_pct,
                                    "entry": entry,
                                    "settle": settle,
                                    "signed_bps": signed_bps,
                                    "won": bool(signed_bps > 0.0),
                                })
                                last_entry_index = index
                            accepted_probes += 1
                            probe = None
                        elif age >= 300:
                            expired_probes += 1
                            probe = None

        histogram[bucket] += amount
        values.append((bucket, amount))
        if len(values) > PROFILE_SEC:
            old_bucket, old_amount = values.popleft()
            histogram[old_bucket] -= old_amount
            if histogram[old_bucket] <= 1e-12:
                histogram.pop(old_bucket, None)

    by_state = {state: metric([row for row in rows if row["state"] == state]) for state in ("failed_auction", "accepted_migration")}
    report = {
        "method": {
            "goal": "Test the volume-profile auction logic shown in the reference chart, not a live strategy.",
            "profile": f"Past {PROFILE_SEC}s volume-at-price, ${PRICE_BIN:g} bins, contiguous {int(VALUE_AREA_PCT * 100)}% value area around POC.",
            "breakout": f"Price crosses old value-area edge by {BREAK_BUFFER_BPS:g}bp; edge is frozen for this probe.",
            "failedAuction": f"Return inside old area after {MIN_REJECT_SEC}s and an inward 30s move of at least {MIN_ACCEPT_MOVE_BPS:g}bp.",
            "acceptedMigration": f"Stay outside for {MIN_ACCEPT_SEC}s with at least {int(MIN_ACCEPT_OUTSIDE_PCT * 100)}% time and volume outside the frozen old area.",
            "execution": f"Detect at completed second, enter after {EXECUTION_DELAY_SEC}s, settle after {HORIZON_SEC}s, lock after a trade for {HORIZON_SEC}s.",
            "parameterSearch": False,
        },
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "observedPct": round(float(observed.mean() * 100.0), 2),
        },
        "probes": {"failed": rejected_probes, "accepted": accepted_probes, "expired": expired_probes},
        "byState": by_state,
        "combined": metric(rows),
        "caution": "One day is an exploratory sample only. Do not use it to select production parameters.",
    }
    return report, pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=Path, default=DEFAULT_SECONDS)
    parser.add_argument("--start", default="2026-07-08T00:00:00Z")
    parser.add_argument("--end", default="2026-07-09T00:00:00Z")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES)
    args = parser.parse_args()
    bars = load_second_bars(args.seconds, include_shards=False)
    bars = bars[(bars.index >= pd.Timestamp(args.start)) & (bars.index < pd.Timestamp(args.end))].copy()
    report, rows = run(bars)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rows.to_csv(args.trades, index=False, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
