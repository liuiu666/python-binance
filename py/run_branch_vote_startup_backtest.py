"""Backtest the independent branch-vote startup strategy.

This script uses branch_vote_startup_core, the same module imported by the live
signal strategy. In folded mode it compiles rules on the other sources only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from branch_vote_startup_core import (  # noqa: E402
    BranchVoteStartupConfig,
    add_lag_features,
    build_minute_snapshots,
    clean_json,
    compile_rules,
    evaluate_row,
    load_rules,
)
from research_all_branch_router_strategy import payout  # noqa: E402
from research_normal_liquidity_orderbook import load_local_data  # noqa: E402
from research_parameter_stability_audit import SOURCES  # noqa: E402
from research_top_exhaustion_confirmation import EXTRA_SOURCES, EXTRA_TRAIN_EXCLUDES  # noqa: E402


DEFAULT_OUT = ROOT / "tmp" / "branch_vote_startup_backtest.json"
DEFAULT_TRADES_OUT = ROOT / "tmp" / "branch_vote_startup_backtest_trades.csv"


def metrics(rows: pd.DataFrame, hours: float) -> dict[str, Any]:
    if rows is None or rows.empty:
        return {
            "trades": 0,
            "wins": 0,
            "winRate": 0.0,
            "pnlU": 0,
            "maxDrawdownU": 0,
            "maxLoss": 0,
            "tradesPerDay": 0.0,
        }
    equity = peak = drawdown = 0
    max_loss = loss = wins = 0
    for row in rows.to_dict("records"):
        pnl = int(row["pnl"])
        wins += int(bool(row["won"]))
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        loss = 0 if row["won"] else loss + 1
        max_loss = max(max_loss, loss)
    count = len(rows)
    return {
        "trades": int(count),
        "wins": int(wins),
        "winRate": round(wins / count * 100.0, 2) if count else 0.0,
        "pnlU": int(equity),
        "maxDrawdownU": int(drawdown),
        "maxLoss": int(max_loss),
        "tradesPerDay": round(count / max(hours, 1e-9) * 24.0, 2),
    }


def apply_strategy(test: pd.DataFrame, compiled: list[dict[str, Any]], cfg: BranchVoteStartupConfig) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    for row in test.sort_values("time").to_dict("records"):
        decision = evaluate_row(row, compiled, cfg)
        if not decision.get("signal"):
            continue
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < cfg.min_gap_sec:
            continue
        signal = str(decision["signal"])
        future = float(row["future_bps"])
        won = future > 0 if signal == "UP" else future < 0
        record = dict(row)
        record.update(
            {
                "rawSignal": decision.get("raw_signal"),
                "signal": signal,
                "won": bool(won),
                "pnl": payout(won),
                "reason": decision.get("reason"),
                "startupScore": decision.get("startupScore"),
                "upVotes": int(decision.get("upVotes", 0)),
                "downVotes": int(decision.get("downVotes", 0)),
                "voteLayers": ";".join(vote["layer"] for vote in decision.get("votes", [])),
            }
        )
        rows.append(record)
        last_time = timestamp
    return pd.DataFrame(rows)


def load_sources(include_extra: bool) -> tuple[pd.DataFrame, dict[str, float], dict[str, Any]]:
    cfg = BranchVoteStartupConfig()
    source_defs = tuple(SOURCES) + (tuple(EXTRA_SOURCES) if include_extra else tuple())
    frames = []
    hours: dict[str, float] = {}
    info: dict[str, Any] = {}
    for source_name, seconds, orderbook in source_defs:
        data = load_local_data(Path(seconds), Path(orderbook))
        frame = build_minute_snapshots(data, str(source_name), cfg, include_future=True)
        frames.append(frame)
        duration = (data.index.max() - data.index.min()).total_seconds() / 3600.0
        hours[str(source_name)] = duration
        info[str(source_name)] = {
            "seconds": str(seconds),
            "orderbook": str(orderbook),
            "start": data.index.min(),
            "end": data.index.max(),
            "hours": round(duration, 4),
            "snapshots": int(len(frame)),
        }
    return add_lag_features(pd.concat(frames, ignore_index=True)), hours, info


def run_folded(args) -> tuple[dict[str, Any], pd.DataFrame]:
    cfg = BranchVoteStartupConfig(startup_skip_threshold=args.startup_threshold)
    snapshots, hours, info = load_sources(args.include_extra)
    source_names = sorted(str(name) for name in snapshots["source"].unique())
    fold_reports = []
    all_trades = []
    for test_source in source_names:
        if args.mode == "extra":
            train_names = set(source_names) - set(EXTRA_TRAIN_EXCLUDES.get(test_source, set())) - {test_source}
        else:
            train_names = set(source_names) - {test_source}
        train = snapshots[snapshots["source"].isin(train_names)].copy()
        test = snapshots[snapshots["source"] == test_source].copy()
        compiled = compile_rules(train)
        trades = apply_strategy(test, compiled, cfg)
        if not trades.empty:
            trades["testSource"] = test_source
            all_trades.append(trades)
        fold_reports.append(
            {
                "testSource": test_source,
                "trainSources": sorted(train_names),
                "rules": {layer["layer"]: len(layer["rules"]) for layer in compiled},
                "result": metrics(trades, hours[test_source]),
                "byReason": {str(reason): metrics(group, hours[test_source]) for reason, group in trades.groupby("reason")} if not trades.empty else {},
            }
        )
    combined = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    return (
        {
            "mode": args.mode,
            "includeExtra": bool(args.include_extra),
            "startupThreshold": int(args.startup_threshold),
            "sources": info,
            "folds": fold_reports,
            "overall": metrics(combined, sum(hours.values())),
        },
        combined,
    )


def run_fixed_rules(args) -> tuple[dict[str, Any], pd.DataFrame]:
    cfg = BranchVoteStartupConfig(startup_skip_threshold=args.startup_threshold)
    snapshots, hours, info = load_sources(args.include_extra)
    compiled = load_rules(args.rules)
    trades_by_source = []
    reports = []
    for source_name, test in snapshots.groupby("source"):
        trades = apply_strategy(test, compiled, cfg)
        if not trades.empty:
            trades["testSource"] = source_name
            trades_by_source.append(trades)
        reports.append({"source": str(source_name), "result": metrics(trades, hours[str(source_name)])})
    combined = pd.concat(trades_by_source, ignore_index=True) if trades_by_source else pd.DataFrame()
    return (
        {
            "mode": "fixed_rules",
            "rules": str(Path(args.rules).resolve()),
            "includeExtra": bool(args.include_extra),
            "startupThreshold": int(args.startup_threshold),
            "sources": info,
            "folds": reports,
            "overall": metrics(combined, sum(hours.values())),
        },
        combined,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["leave_one_out", "extra", "fixed_rules"], default="leave_one_out")
    parser.add_argument("--include-extra", action="store_true")
    parser.add_argument("--rules", default=str(ROOT / "data" / "branch_vote_startup_rules.json"))
    parser.add_argument("--startup-threshold", type=int, default=4)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--trades-out", default=str(DEFAULT_TRADES_OUT))
    args = parser.parse_args()

    if args.mode == "fixed_rules":
        result, trades = run_fixed_rules(args)
    else:
        result, trades = run_folded(args)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(clean_json(result), ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(args.trades_out, index=False, encoding="utf-8-sig")
    print(json.dumps(clean_json(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

