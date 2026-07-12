from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_all_branch_matrix as matrix  # noqa: E402
import research_all_branch_vote_router as vote  # noqa: E402
from research_all_branch_router_strategy import metrics, payout  # noqa: E402


OUT_JSON = ROOT / "tmp" / "vote_router_dynamic_guard.json"
OUT_CSV = ROOT / "tmp" / "vote_router_dynamic_guard_trades.csv"


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


def compile_both(train: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    return {
        "balanced": vote.compile_rules(train, "balanced"),
        "strict": vote.compile_rules(train, "strict"),
    }


def signal_for_mode(row: dict[str, Any], compiled: dict[str, list[dict[str, Any]]], mode: str) -> tuple[str | None, dict[str, Any]]:
    if mode == "balanced2":
        return vote.vote_for(row, compiled["balanced"])
    if mode == "strict2":
        return vote.vote_for(row, compiled["strict"])
    if mode == "balanced3":
        return vote.vote_for(row, compiled["balanced"])
    if mode == "strict3":
        return vote.vote_for(row, compiled["strict"])
    raise ValueError(mode)


def min_votes_for_mode(mode: str) -> int:
    return 3 if mode.endswith("3") else 2


def mode_from_state(loss_streak: int, win_streak: int, variant: str) -> str:
    if variant == "loss2_strict2_loss3_strict3_win2_recover":
        if loss_streak >= 3:
            return "strict3"
        if loss_streak >= 2:
            return "strict2"
        return "balanced2"
    if variant == "loss2_strict2_cool3_win2_recover":
        if loss_streak >= 2:
            return "strict2"
        return "balanced2"
    if variant == "loss1_strict2_loss3_strict3_win2_recover":
        if loss_streak >= 3:
            return "strict3"
        if loss_streak >= 1:
            return "strict2"
        return "balanced2"
    if variant == "loss2_balanced3_loss3_strict3_win2_recover":
        if loss_streak >= 3:
            return "strict3"
        if loss_streak >= 2:
            return "balanced3"
        return "balanced2"
    raise ValueError(variant)


def apply_dynamic_guard(test: pd.DataFrame, compiled: dict[str, list[dict[str, Any]]], variant: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    loss_streak = 0
    win_streak = 0
    for row in test.sort_values("time").to_dict("records"):
        mode = mode_from_state(loss_streak, win_streak, variant)
        signal, info = signal_for_mode(row, compiled, mode)
        total_votes = int(info["upVotes"]) + int(info["downVotes"])
        if signal is None or total_votes < min_votes_for_mode(mode):
            continue
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < 600:
            continue
        future = float(row["future10_bps"])
        won = future > 0 if signal == "UP" else future < 0
        record = dict(row)
        record["signal"] = signal
        record["won"] = bool(won)
        record["pnl"] = payout(won)
        record["mode"] = mode
        record["preLossStreak"] = loss_streak
        record["upVotes"] = int(info["upVotes"])
        record["downVotes"] = int(info["downVotes"])
        rows.append(record)
        last_time = timestamp
        if won:
            win_streak += 1
            loss_streak = 0
        else:
            loss_streak += 1
            win_streak = 0
    return pd.DataFrame(rows)


def run() -> dict[str, Any]:
    snapshots = []
    source_hours = {}
    source_ranges = {}
    for source_name, seconds, orderbook in matrix.SOURCES:
        data = matrix.load_local_data(seconds, orderbook)
        source_hours[source_name] = (data.index.max() - data.index.min()).total_seconds() / 3600.0
        source_ranges[source_name] = {
            "start": data.index.min(),
            "end": data.index.max(),
            "hours": round(source_hours[source_name], 4),
        }
        snapshots.append(matrix.build_minute_snapshots(data, source_name))
    all_snapshots = pd.concat(snapshots, ignore_index=True)
    sources = sorted(all_snapshots["source"].unique())
    variants = (
        "loss2_strict2_loss3_strict3_win2_recover",
        "loss2_strict2_cool3_win2_recover",
        "loss1_strict2_loss3_strict3_win2_recover",
        "loss2_balanced3_loss3_strict3_win2_recover",
    )
    reports = []
    all_trades = []
    for variant in variants:
        fold_results = []
        variant_trades = []
        for test_source in sources:
            train = all_snapshots[all_snapshots["source"] != test_source].copy()
            test = all_snapshots[all_snapshots["source"] == test_source].copy()
            compiled = compile_both(train)
            trades = apply_dynamic_guard(test, compiled, variant)
            if not trades.empty:
                trades["variant"] = variant
                trades["testSource"] = test_source
                variant_trades.append(trades)
                all_trades.append(trades)
            fold_results.append(
                {
                    "testSource": str(test_source),
                    "result": metrics(trades, source_hours[str(test_source)]),
                    "byMode": {
                        str(mode): metrics(mode_group, source_hours[str(test_source)])
                        for mode, mode_group in trades.groupby("mode")
                    }
                    if not trades.empty
                    else {},
                }
            )
        combined = pd.concat(variant_trades, ignore_index=True) if variant_trades else pd.DataFrame()
        reports.append(
            {
                "variant": variant,
                "total": metrics(combined, sum(source_hours.values())),
                "folds": fold_results,
                "byMode": {
                    str(mode): metrics(mode_group, sum(source_hours.values()))
                    for mode, mode_group in combined.groupby("mode")
                }
                if not combined.empty
                else {},
            }
        )

    if all_trades:
        pd.concat(all_trades, ignore_index=True).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    output = {
        "method": "Leave-one-source-out dynamic guard. Rules are learned only from the other two periods. Guard uses only prior realized wins/losses, no market lookahead.",
        "sources": source_ranges,
        "reports": reports,
        "best": sorted(
            reports,
            key=lambda item: (
                item["total"]["pnlU"],
                item["total"]["winRate"],
                -item["total"]["maxLoss"],
                -item["total"]["maxDrawdownU"],
            ),
            reverse=True,
        ),
        "csv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
