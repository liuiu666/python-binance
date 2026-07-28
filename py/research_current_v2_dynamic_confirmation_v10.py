"""Current V2 with dynamic book admission and emitted-signal cooldown only."""

from __future__ import annotations

import gc
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest_online_strategies_latest import (
    causal_incident_blocked,
    fetch_config,
    liquidity_rules,
    outcome_fields,
    variant,
)
from liquidity_v2_core import build_features, evaluate_candidate, normal_ready
from research_current_v2_augmented_multiperiod_v9 import (
    DELAYS,
    STRATEGY_ID,
    confirmation,
    load_supplement,
    metrics,
    period_for,
    shared_cooldown,
    utc,
)
from research_exhaustion_orderbook_confirmation_v6 import SOURCES
from research_normal_liquidity_orderbook import read_orderbook
from research_path_efficiency_router_v1 import clean
from second_backtest.data import load_second_bars


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "current_v2_dynamic_confirmation_v10.json"
OUT_CSV = ROOT / "tmp" / "current_v2_dynamic_confirmation_v10_trades.csv"
OUT_CANDIDATES = ROOT / "tmp" / "current_v2_dynamic_confirmation_v10_candidates.csv"


def replay_dynamic(data: pd.DataFrame, row: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rules = liquidity_rules(row)
    features = build_features(data, rules)
    warmup = max(3600, rules.normal_window_sec, rules.center_slope_sec, rules.retest_sec) + 10
    last_emit = -10**9
    trades: list[dict[str, Any]] = []
    counts = {"wait": 0, "nonWait": 0, "bookRejected": 0, "incident": 0, "emitted": 0}
    for pos in range(warmup, len(data) - 610):
        feature = features.iloc[pos]
        if not bool(feature.get("ob_available", False)) or not normal_ready(feature, rules):
            continue
        if pos - last_emit < rules.min_gap_sec:
            continue
        decision = evaluate_candidate(feature, rules)
        status = str(decision.get("status") or "wait")
        if status == "wait":
            counts["wait"] += 1
            continue
        counts["nonWait"] += 1
        signal = str(decision.get("signal") or decision.get("candidate_signal") or "")
        if signal not in {"UP", "DOWN"}:
            counts["bookRejected"] += 1
            continue
        timestamp = data.index[pos]
        result = confirmation(data, {"time": timestamp, "signal": signal})
        if result is None or result[0] < 2:
            counts["bookRejected"] += 1
            continue
        if causal_incident_blocked(data, pos, signal, row):
            counts["incident"] += 1
            continue
        outcome = outcome_fields(data, timestamp, signal)
        if outcome is None:
            continue
        votes, flow, imbalance, micro = result
        trades.append({
            "time": timestamp, "signal": signal,
            "branch": "current_v2_dynamic_book", "priority": 0,
            "reason": decision.get("reason") or decision.get("candidate_reason"),
            "original_status": status, "confirmation_votes": votes,
            "flow_60": flow, "imbalance_60": imbalance, "micro_60": micro,
            **outcome,
        })
        last_emit = pos
        counts["emitted"] += 1
    return trades, counts


def replay_sources() -> tuple[pd.DataFrame, dict[str, Any]]:
    row = variant(fetch_config(), STRATEGY_ID)
    rows: list[dict[str, Any]] = []
    audit: dict[str, Any] = {}
    for source in SOURCES:
        if not source.seconds.exists() or not source.orderbook.exists():
            continue
        bars = load_second_bars(source.seconds, include_shards=False)
        data = bars.join(read_orderbook(source.orderbook, bars.index), how="left").sort_index()
        start = utc(source.start) if source.start else utc(data.index.min())
        end = utc(source.end) if source.end else utc(data.index.max())
        trades, counts = replay_dynamic(data, row)
        kept = 0
        for trade in trades:
            timestamp = pd.Timestamp(trade["time"])
            if timestamp < start or timestamp > end:
                continue
            item = {
                "time": timestamp, "signal": trade["signal"],
                "branch": trade["branch"], "priority": 0,
                "reason": trade.get("reason"), "original_status": trade.get("original_status"),
                "confirmation_votes": trade.get("confirmation_votes"), "source": source.name,
            }
            for delay in DELAYS:
                item[f"signed_bps_d{delay}"] = trade[f"signed_bps_d{delay}"]
            rows.append(item)
            kept += 1
        audit[source.name] = {"counts": counts, "keptInSourceRange": kept}
        del data, bars
        gc.collect()
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(["time", "source"]).drop_duplicates("time", keep="last")
    return frame, audit


def run() -> dict[str, Any]:
    current, audit = replay_sources()
    supplement = load_supplement()
    combined = pd.concat([current, supplement], ignore_index=True, sort=False)
    combined["period"] = combined.time.map(period_for)
    combined.sort_values("time").to_csv(OUT_CANDIDATES, index=False, encoding="utf-8-sig")
    frames = [shared_cooldown(group) for _, group in combined.groupby("period")]
    trades = pd.concat(frames, ignore_index=True).sort_values("time") if frames else pd.DataFrame()
    trades["beijing_day"] = trades.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    period_hours = {"development_july5_10": 144.0, "validation_july11_13": 72.0, "forward_july14_15": 35.16}
    report = {
        "method": {
            "parameterSearch": False, "causal": True,
            "strategyId": STRATEGY_ID,
            "optimization": "current V2 candidate core; all non-wait candidates use trailing-book 2-of-3 admission; only emitted signals own cooldown; add exhaustion supplement under same strategy cooldown",
            "cooldownSec": 600, "amountU": 5,
        },
        "sourceAudit": audit,
        "inputSignals": {"dynamicCurrent": len(current), "supplement": len(supplement)},
        "periods": {
            period: {f"delay{delay}s": metrics(group, delay, period_hours[period]) for delay in DELAYS}
            for period, group in trades.groupby("period")
        },
        "byDayDelay6s": {
            day: metrics(group, 6, 24.0) for day, group in trades.groupby("beijing_day")
        },
        "byBranchDelay6s": {
            branch: metrics(group, 6, sum(period_hours.values())) for branch, group in trades.groupby("branch")
        },
        "acceptance": {"minTradesPerDay": 10.0, "minWinRate": 55.56, "maxDrawdownU": 20.0, "maxLossStreak": 3, "allDelaysMustBeProfitable": True},
        "warning": "All periods are reused research evidence; no deployment.",
        "tradesCsv": str(OUT_CSV),
        "candidatesCsv": str(OUT_CANDIDATES),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
