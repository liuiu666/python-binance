"""Authoritative shared-core replay for the deployable current V2 augmented V9."""

from __future__ import annotations

import gc
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest_online_strategies_latest import fetch_config, replay_liquidity, variant
from current_v2_augmented_v9_core import (
    AugmentedV9Rules,
    build_confirmed_supplement_candidates,
    trailing_book_confirmation,
)
from research_current_v2_augmented_multiperiod_v9 import DELAYS, STRATEGY_ID, metrics, period_for, shared_cooldown, utc
from research_exhaustion_orderbook_confirmation_v6 import SOURCES
from research_normal_liquidity_orderbook import read_orderbook
from research_path_efficiency_router_v1 import clean
from second_backtest.data import load_second_bars


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "current_v2_augmented_v9_shared.json"
OUT_CSV = ROOT / "tmp" / "current_v2_augmented_v9_shared_trades.csv"


def price_at(data: pd.DataFrame, target: pd.Timestamp) -> float | None:
    pos = int(data.index.searchsorted(target, side="left"))
    if pos >= len(data) or abs((data.index[pos] - target).total_seconds()) > 2:
        return None
    return float(data.close.iloc[pos])


def supplement_outcomes(data: pd.DataFrame, detected: pd.Timestamp, signal: str) -> dict[str, Any] | None:
    direction = 1.0 if signal == "UP" else -1.0
    fields: dict[str, Any] = {}
    for delay in DELAYS:
        entry_time = detected + pd.Timedelta(seconds=1 + delay)
        settle_time = entry_time + pd.Timedelta(seconds=600)
        entry = price_at(data, entry_time)
        settle = price_at(data, settle_time)
        if entry is None or settle is None or entry <= 0.0:
            return None
        signed = (settle / entry - 1.0) * 10000.0 * direction
        fields[f"signed_bps_d{delay}"] = signed
    return fields


def replay_source(source: Any, current_row: dict[str, Any], v9_rules: AugmentedV9Rules) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bars = load_second_bars(source.seconds, include_shards=False)
    data = bars.join(read_orderbook(source.orderbook, bars.index), how="left").sort_index()
    start = utc(source.start) if source.start else utc(data.index.min())
    end = utc(source.end) if source.end else utc(data.index.max())
    rows: list[dict[str, Any]] = []

    current_trades, current_counts = replay_liquidity(data, current_row)
    current_confirmed = 0
    for trade in current_trades:
        timestamp = pd.Timestamp(trade["time"])
        if not start <= timestamp <= end:
            continue
        book = trailing_book_confirmation(data, str(trade["signal"]), timestamp, v9_rules)
        if not book["ok"]:
            continue
        item = {
            "time": timestamp,
            "signal": trade["signal"],
            "branch": "current_v2_original",
            "priority": 0,
            "reason": trade.get("reason"),
            "source": source.name,
            **book,
        }
        for delay in DELAYS:
            item[f"signed_bps_d{delay}"] = trade[f"signed_bps_d{delay}"]
        rows.append(item)
        current_confirmed += 1

    supplement = build_confirmed_supplement_candidates(data, v9_rules)
    supplement_kept = 0
    for candidate in supplement.to_dict("records"):
        detected = pd.Timestamp(candidate["detected_time"])
        if not start <= detected <= end:
            continue
        outcome = supplement_outcomes(data, detected, str(candidate["signal"]))
        if outcome is None:
            continue
        rows.append({
            "time": detected,
            "signal": candidate["signal"],
            "branch": "exhaustion_orderbook_supplement",
            "priority": 1,
            "reason": candidate["reason"],
            "source": source.name,
            "votes": candidate["votes"],
            **outcome,
        })
        supplement_kept += 1

    audit = {
        "start": start,
        "end": end,
        "currentCounts": current_counts,
        "currentConfirmed": current_confirmed,
        "supplementConfirmed": supplement_kept,
    }
    del data, bars
    gc.collect()
    return rows, audit


def run() -> dict[str, Any]:
    config = fetch_config()
    current_row = variant(config, STRATEGY_ID)
    v9_rules = AugmentedV9Rules.from_config(current_row)
    rows: list[dict[str, Any]] = []
    source_audit: dict[str, Any] = {}
    for source in SOURCES:
        if not source.seconds.exists() or not source.orderbook.exists():
            continue
        source_rows, audit = replay_source(source, current_row, v9_rules)
        rows.extend(source_rows)
        source_audit[source.name] = audit
    candidates = pd.DataFrame(rows)
    candidates = candidates.sort_values(["time", "source", "priority"]).drop_duplicates(
        ["time", "branch"], keep="last"
    )
    candidates["period"] = candidates.time.map(period_for)
    frames = [shared_cooldown(group) for _, group in candidates.groupby("period")]
    trades = pd.concat(frames, ignore_index=True).sort_values("time") if frames else pd.DataFrame()
    trades["beijing_day"] = trades.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    period_hours = {"development_july5_10": 144.0, "validation_july11_13": 72.0, "forward_july14_15": 35.16}
    report = {
        "method": {
            "parameterSearch": False,
            "causal": True,
            "sharedCore": "current_v2_augmented_v9_core",
            "strategyId": "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V9",
            "amountU": 5,
            "cooldownSec": 600,
            "supplementEntry": "first second after the completed candidate minute plus delay",
        },
        "sourceAudit": source_audit,
        "candidateCount": len(candidates),
        "tradeCount": len(trades),
        "periods": {
            period: {f"delay{delay}s": metrics(group, delay, period_hours[period]) for delay in DELAYS}
            for period, group in trades.groupby("period")
        },
        "byDayDelay6s": {day: metrics(group, 6, 24.0) for day, group in trades.groupby("beijing_day")},
        "byBranchDelay6s": {
            branch: metrics(group, 6, sum(period_hours.values())) for branch, group in trades.groupby("branch")
        },
        "acceptance": {"minTradesPerDay": 10.0, "minWinRate": 55.56, "maxDrawdownU": 20.0, "maxLossStreak": 3, "allDelaysMustBeProfitable": True},
        "warning": "Reused research evidence; deploy shadow only.",
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
