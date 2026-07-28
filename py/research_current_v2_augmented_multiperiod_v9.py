"""Replay the current V2 + confirmed supplement across local July sources."""

from __future__ import annotations

import gc
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest_online_strategies_latest import fetch_config, replay_liquidity, variant
from research_current_v2_augmented_v8 import DELAYS, metrics, shared_cooldown
from research_exhaustion_orderbook_confirmation_v6 import SOURCES
from research_normal_liquidity_orderbook import read_orderbook
from research_path_efficiency_router_v1 import clean
from second_backtest.data import load_second_bars


ROOT = Path(__file__).resolve().parents[1]
SUPPLEMENT = ROOT / "tmp" / "exhaustion_post_confirmation_cooldown_v7_trades.csv"
OUT_JSON = ROOT / "tmp" / "current_v2_augmented_multiperiod_v9.json"
OUT_CSV = ROOT / "tmp" / "current_v2_augmented_multiperiod_v9_trades.csv"
STRATEGY_ID = "BTC_10min_NORMAL_LIQ_OB_V2_QUALITY"


def utc(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def period_for(timestamp: pd.Timestamp) -> str:
    if timestamp < pd.Timestamp("2026-07-11", tz="UTC"):
        return "development_july5_10"
    if timestamp < pd.Timestamp("2026-07-14", tz="UTC"):
        return "validation_july11_13"
    return "forward_july14_15"


def confirmation(data: pd.DataFrame, row: dict[str, Any]) -> tuple[int, float, float, float] | None:
    timestamp = pd.Timestamp(row["time"])
    pos = int(data.index.searchsorted(timestamp, side="right") - 1)
    if pos < 59 or abs((data.index[pos] - timestamp).total_seconds()) > 2:
        return None
    window = data.iloc[pos - 59:pos + 1]
    available = window.get("ob_available", pd.Series(False, index=window.index)).fillna(False)
    if float(available.mean()) < 0.90:
        return None
    buy = float(window.buy_qty.fillna(0.0).sum())
    sell = float(window.sell_qty.fillna(0.0).sum())
    flow = (buy - sell) / (buy + sell) if buy + sell > 0.0 else 0.0
    imbalance = float(window.imbalance_20.mean())
    micro = float(window.microprice_edge_bps.mean())
    if not all(np.isfinite(value) for value in (flow, imbalance, micro)):
        return None
    direction = 1.0 if row["signal"] == "UP" else -1.0
    votes = int(direction * flow > 0.0) + int(direction * imbalance > 0.0) + int(direction * micro > 0.0)
    return votes, flow, imbalance, micro


def replay_current() -> tuple[pd.DataFrame, dict[str, Any]]:
    config = fetch_config()
    row = variant(config, STRATEGY_ID)
    accepted: list[dict[str, Any]] = []
    source_audit: dict[str, Any] = {}
    for source in SOURCES:
        if not source.seconds.exists() or not source.orderbook.exists():
            continue
        bars = load_second_bars(source.seconds, include_shards=False)
        data = bars.join(read_orderbook(source.orderbook, bars.index), how="left").sort_index()
        start = utc(source.start) if source.start else utc(data.index.min())
        end = utc(source.end) if source.end else utc(data.index.max())
        trades, counts = replay_liquidity(data, row)
        kept = 0
        for trade in trades:
            timestamp = pd.Timestamp(trade["time"])
            if timestamp < start or timestamp > end:
                continue
            result = confirmation(data, trade)
            if result is None or result[0] < 2:
                continue
            votes, flow, imbalance, micro = result
            item = {
                "time": timestamp, "signal": trade["signal"],
                "branch": "current_v2_original", "priority": 0,
                "reason": trade.get("reason"), "confirmation_votes": votes,
                "flow_60": flow, "imbalance_60": imbalance, "micro_60": micro,
                "source": source.name,
            }
            for delay in DELAYS:
                item[f"signed_bps_d{delay}"] = trade[f"signed_bps_d{delay}"]
            accepted.append(item)
            kept += 1
        source_audit[source.name] = {"rawCounts": counts, "confirmedTrades": kept}
        del data, bars
        gc.collect()
    frame = pd.DataFrame(accepted)
    if not frame.empty:
        frame = frame.sort_values(["time", "source"]).drop_duplicates("time", keep="last")
    return frame, source_audit


def load_supplement() -> pd.DataFrame:
    frame = pd.read_csv(SUPPLEMENT)
    frame["time"] = pd.to_datetime(frame.time, utc=True, errors="coerce") + pd.Timedelta(seconds=59)
    frame["branch"] = "exhaustion_orderbook_supplement"
    frame["priority"] = 1
    frame["reason"] = "one_sided_exhaustion_reclaim_orderbook_2of3"
    direction = np.where(frame.signal.eq("UP"), 1.0, -1.0)
    for delay in DELAYS:
        move = (
            frame[f"move_bps_d{delay}"].fillna(frame.move_bps)
            if f"move_bps_d{delay}" in frame
            else frame.move_bps
        )
        frame[f"signed_bps_d{delay}"] = move * direction
    return frame


def run() -> dict[str, Any]:
    current, source_audit = replay_current()
    supplement = load_supplement()
    combined = pd.concat([current, supplement], ignore_index=True, sort=False)
    combined["period"] = combined.time.map(period_for)
    frames = []
    for _, group in combined.groupby("period"):
        frames.append(shared_cooldown(group))
    trades = pd.concat(frames, ignore_index=True).sort_values("time") if frames else pd.DataFrame()
    trades["beijing_day"] = trades.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    period_hours = {"development_july5_10": 144.0, "validation_july11_13": 72.0, "forward_july14_15": 35.16}
    report = {
        "method": {
            "parameterSearch": False, "causal": True,
            "strategyId": STRATEGY_ID,
            "currentCore": "online liquidity_v2_core replayed with current server config",
            "optimization": "uniform trailing-60s 2-of-3 confirmation plus supplemental exhaustion branch",
            "cooldownSec": 600, "amountU": 5,
        },
        "sourceAudit": source_audit,
        "inputSignals": {"confirmedCurrent": len(current), "supplement": len(supplement)},
        "periods": {
            period: {f"delay{delay}s": metrics(group, delay, period_hours[period]) for delay in DELAYS}
            for period, group in trades.groupby("period")
        },
        "byDayDelay6s": {
            day: metrics(group, 6, 24.0) for day, group in trades.groupby("beijing_day")
        },
        "acceptance": {"minTradesPerDay": 10.0, "minWinRate": 55.56, "maxDrawdownU": 20.0, "maxLossStreak": 3, "allDelaysMustBeProfitable": True},
        "warning": "All periods are reused research evidence, not untouched proof.",
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
