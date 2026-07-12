"""Test fixed auction-state hypotheses on local 1-second data.

This is a research classifier, not a live strategy.  It samples one completed
market snapshot every ten minutes so that each label has an independent binary
option settlement window.  The definitions are fixed before examining results:
balance/reversion, failed auction, accepted migration, and transition.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from liquidity_v2_core import LiquidityV2Rules, build_features
from research_normal_liquidity_orderbook import load_local_data


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECONDS = ROOT / "data" / "server_latest" / "btcusdt_1s_trades.csv"
DEFAULT_ORDERBOOK = ROOT / "data" / "server_latest" / "btcusdt_orderbook_1s.csv"
DEFAULT_OUT = ROOT / "tmp" / "auction_state_hypotheses_20260712.json"
DEFAULT_TRADES = ROOT / "tmp" / "auction_state_hypotheses_20260712.csv"
HORIZON_SEC = 600
EXECUTION_DELAY_SEC = 2


def finite(row: pd.Series, *keys: str) -> bool:
    return all(np.isfinite(float(row.get(key, np.nan))) for key in keys)


def sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def classify(row: pd.Series) -> tuple[str, str | None, str]:
    """Return mutually exclusive auction state, direction, and explanation."""

    required = (
        "z",
        "inside1_ratio",
        "center_slope_bps",
        "sigma_expand",
        "ret_300s_bps",
        "slope_90_bps",
        "flow_60",
        "imbalance_20",
    )
    if not finite(row, *required):
        return "transition", None, "指标尚未完整"

    z = float(row["z"])
    edge = sign(z)
    ret300 = float(row["ret_300s_bps"])
    ret90 = float(row["slope_90_bps"])
    flow = float(row["flow_60"])
    book = float(row["imbalance_20"])
    flat_normal = (
        float(row["inside1_ratio"]) >= 0.55
        and abs(float(row["center_slope_bps"])) <= 5.0
        and float(row["sigma_expand"]) <= 1.25
    )

    if edge and abs(z) >= 1.2:
        # Breakout failed: price was outside the old area, then price, taker
        # flow, and passive book all turn back toward value.
        rejection = (
            sign(ret300) == edge
            and abs(ret300) >= 8.0
            and sign(ret90) == -edge
            and abs(ret90) >= 3.0
            and sign(flow) == -edge
            and abs(flow) >= 0.12
            and sign(book) == -edge
            and abs(book) >= 0.08
        )
        if rejection:
            return "failed_auction", "DOWN" if edge > 0 else "UP", "区间外突破后，价格、主动成交和挂单均回到价值区方向"

        # Breakout accepted: price stays outside and all three auction inputs
        # keep pointing away from the old value area.
        acceptance = (
            sign(ret300) == edge
            and abs(ret300) >= 12.0
            and sign(ret90) == edge
            and abs(ret90) >= 4.0
            and sign(flow) == edge
            and abs(flow) >= 0.12
            and sign(book) == edge
            and abs(book) >= 0.08
        )
        if acceptance:
            return "accepted_migration", "UP" if edge > 0 else "DOWN", "区间外被接受，价格、主动成交和挂单同向延续"

    # A stable local value area can support an edge fade only when there is no
    # five-minute directional displacement and the passive book opposes price.
    if (
        flat_normal
        and 1.0 <= abs(z) <= 1.8
        and abs(ret300) <= 8.0
        and sign(book) == -edge
        and abs(book) >= 0.08
        and sign(flow) != edge
    ):
        return "balance_reversion", "DOWN" if edge > 0 else "UP", "稳定价值区边缘，未形成五分钟位移，挂单不支持继续突破"

    return "transition", None, "未同时满足平衡回归、失败拍卖或价值区迁移"


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"samples": 0, "trades": 0, "winRate": None, "pnlU": 0.0, "avgSignedBps": None, "maxLossStreak": 0}
    tradable = [row for row in rows if row["signal"]]
    if not tradable:
        return {"samples": len(rows), "trades": 0, "winRate": None, "pnlU": 0.0, "avgSignedBps": None, "maxLossStreak": 0}
    wins = sum(bool(row["won"]) for row in tradable)
    pnls = [4.0 if row["won"] else -5.0 for row in tradable]
    streak = current = 0
    for row in tradable:
        if row["won"]:
            current = 0
        else:
            current += 1
            streak = max(streak, current)
    return {
        "samples": len(rows),
        "trades": len(tradable),
        "winRate": round(wins / len(tradable) * 100.0, 2),
        "pnlU": round(sum(pnls), 2),
        "avgSignedBps": round(float(np.mean([row["signed_bps"] for row in tradable])), 3),
        "maxLossStreak": streak,
    }


def build_row(
    features: pd.DataFrame,
    close: pd.Series,
    index: int,
) -> dict[str, Any]:
    timestamp = features.index[index]
    state, signal, reason = classify(features.iloc[index])
    entry_index = index + EXECUTION_DELAY_SEC
    settle_index = entry_index + HORIZON_SEC
    entry = float(close.iloc[entry_index])
    settle = float(close.iloc[settle_index])
    raw_bps = (settle / entry - 1.0) * 10000.0
    signed_bps = raw_bps * (1 if signal == "UP" else -1 if signal == "DOWN" else 0)
    return {
        "detected_time": timestamp.isoformat(),
        "entry_time": features.index[entry_index].isoformat(),
        "settle_time": features.index[settle_index].isoformat(),
        "state": state,
        "signal": signal,
        "reason": reason,
        "entry": entry,
        "settle": settle,
        "raw_bps": raw_bps,
        "signed_bps": signed_bps if signal else None,
        "won": bool(signed_bps > 0.0) if signal else None,
        **{key: float(features.iloc[index][key]) for key in ("z", "ret_300s_bps", "slope_90_bps", "flow_60", "imbalance_20", "inside1_ratio", "center_slope_bps", "sigma_expand")},
    }


def run(seconds: Path, orderbook: Path) -> tuple[dict[str, Any], pd.DataFrame]:
    data = load_local_data(seconds, orderbook)
    rules = LiquidityV2Rules(normal_window_sec=600, horizon_sec=HORIZON_SEC)
    features = build_features(data, rules)
    close = data["close"].astype(float)
    observed = data["observed"].astype(bool)
    grid_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []

    # Fixed UTC ten-minute grid: no candidate-dependent resampling and no
    # overlapping expiry windows.
    for index, timestamp in enumerate(features.index):
        if index < 900 or index + EXECUTION_DELAY_SEC + HORIZON_SEC >= len(features):
            continue
        if timestamp.minute % 10 != 9 or timestamp.second != 59:
            continue
        if float(observed.iloc[index - 599 : index + 1].mean()) < 0.95:
            continue
        grid_rows.append(build_row(features, close, index))

    # This is the executable replay: evaluate every completed second, enter
    # only after a directional state appears, then enforce the same 10-minute
    # lock used by a binary option horizon.
    last_entry_index = -HORIZON_SEC
    for index in range(900, len(features) - EXECUTION_DELAY_SEC - HORIZON_SEC):
        if index - last_entry_index < HORIZON_SEC:
            continue
        if float(observed.iloc[index - 599 : index + 1].mean()) < 0.95:
            continue
        row = build_row(features, close, index)
        if not row["signal"]:
            continue
        event_rows.append(row)
        last_entry_index = index

    frame = pd.DataFrame(event_rows)
    state_names = ("balance_reversion", "failed_auction", "accepted_migration", "transition")
    grid_by_state = {state: metrics([row for row in grid_rows if row["state"] == state]) for state in state_names}
    event_by_state = {state: metrics([row for row in event_rows if row["state"] == state]) for state in state_names}
    report = {
        "method": {
            "purpose": "Fixed auction-state hypothesis test; not parameter search and not a live strategy.",
            "gridEntry": "One completed snapshot at the end of each UTC ten-minute block.",
            "eventEntry": "Every completed second may trigger; successful entries lock the next 600 seconds.",
            "execution": "Entry uses the first available close two seconds after detection.",
            "settlement": "Entry close plus 600 seconds.",
            "payout": "5U stake, 80% payout, reported only for states with a direction.",
            "causality": "All state inputs are available at or before the entry second.",
        },
        "sample": {
            "start": data.index.min().isoformat(),
            "end": data.index.max().isoformat(),
            "hours": round((data.index.max() - data.index.min()).total_seconds() / 3600.0, 2),
            "observedPct": round(float(data["observed"].mean() * 100.0), 2),
            "scheduledWindows": len(grid_rows),
        },
        "gridSnapshotByState": grid_by_state,
        "gridSnapshotCombined": metrics([row for row in grid_rows if row["signal"]]),
        "eventReplayByState": event_by_state,
        "eventReplayCombined": metrics(event_rows),
        "caution": "This uses old one-second order-book snapshots as a proxy for passive liquidity. New depth-delta data is required to validate absorption and cancellation claims.",
    }
    return report, frame


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=Path, default=DEFAULT_SECONDS)
    parser.add_argument("--orderbook", type=Path, default=DEFAULT_ORDERBOOK)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES)
    args = parser.parse_args()
    report, rows = run(args.seconds, args.orderbook)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rows.to_csv(args.trades, index=False, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
