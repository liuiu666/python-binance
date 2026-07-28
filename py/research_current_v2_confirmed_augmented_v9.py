"""Current V2 plus supplement, both using one trailing-book confirmation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from research_current_v2_augmented_v8 import (
    DELAYS,
    load_current,
    load_supplement,
    metrics,
    shared_cooldown,
)
from research_normal_liquidity_orderbook import read_orderbook
from research_path_efficiency_router_v1 import clean
from second_backtest.data import load_second_bars


ROOT = Path(__file__).resolve().parents[1]
SECONDS = ROOT / "tmp" / "frozen_position_forward" / "btcusdt_1s_trades.csv"
ORDERBOOK = ROOT / "tmp" / "frozen_position_forward" / "btcusdt_orderbook_1s.csv"
OUT_JSON = ROOT / "tmp" / "current_v2_confirmed_augmented_v9.json"
OUT_CSV = ROOT / "tmp" / "current_v2_confirmed_augmented_v9_trades.csv"


def confirm_current(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    bars = load_second_bars(SECONDS, include_shards=False)
    data = bars.join(read_orderbook(ORDERBOOK, bars.index), how="left").sort_index()
    accepted: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for row in frame.to_dict("records"):
        timestamp = pd.Timestamp(row["time"])
        pos = int(data.index.searchsorted(timestamp, side="right") - 1)
        if pos < 59 or abs((data.index[pos] - timestamp).total_seconds()) > 2:
            continue
        window = data.iloc[pos - 59:pos + 1]
        available = window.get("ob_available", pd.Series(False, index=window.index)).fillna(False)
        if float(available.mean()) < 0.90:
            continue
        buy = float(window.buy_qty.fillna(0.0).sum())
        sell = float(window.sell_qty.fillna(0.0).sum())
        flow = (buy - sell) / (buy + sell) if buy + sell > 0.0 else 0.0
        imbalance = float(window.imbalance_20.mean())
        micro = float(window.microprice_edge_bps.mean())
        direction = 1.0 if row["signal"] == "UP" else -1.0
        votes = int(direction * flow > 0.0) + int(direction * imbalance > 0.0) + int(direction * micro > 0.0)
        item = dict(row)
        item.update(flow_60=flow, imbalance_60=imbalance, micro_60=micro, confirmation_votes=votes)
        audit.append({"time": timestamp, "signal": row["signal"], "votes": votes, "accepted": votes >= 2})
        if votes >= 2:
            accepted.append(item)
    return pd.DataFrame(accepted), audit


def run() -> dict[str, Any]:
    original = load_current()
    current, audit = confirm_current(original)
    supplement = load_supplement()
    trades = shared_cooldown(pd.concat([current, supplement], ignore_index=True))
    trades["beijing_day"] = trades.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    report = {
        "method": {
            "parameterSearch": False, "causal": True,
            "strategyId": "BTC_10min_NORMAL_LIQ_OB_V2_QUALITY",
            "optimization": "retain current V2 core; require the same trailing-60s 2-of-3 book confirmation for original and supplemental branches",
            "cooldownSec": 600,
            "amountU": 5,
        },
        "currentDisposition": {
            "original": len(original), "confirmed": len(current), "audit": audit,
        },
        "supplementSignals": len(supplement),
        "combinedDelays": {f"delay{delay}s": metrics(trades, delay, 35.16) for delay in DELAYS},
        "byBranchDelay6s": {
            branch: metrics(group, 6, 35.16) for branch, group in trades.groupby("branch")
        },
        "byDayDelay6s": {
            day: metrics(group, 6, 24.0) for day, group in trades.groupby("beijing_day")
        },
        "acceptance": {"minTradesPerDay": 10.0, "minWinRate": 55.56, "maxDrawdownU": 20.0, "maxLossStreak": 3, "allDelaysMustBeProfitable": True},
        "warning": "Latest period was already inspected; freeze before collecting new forward evidence.",
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
