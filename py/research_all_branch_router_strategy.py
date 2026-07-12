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


OUT_JSON = ROOT / "tmp" / "all_branch_router_strategy.json"
OUT_CSV = ROOT / "tmp" / "all_branch_router_strategy_trades.csv"


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


def payout(won: bool) -> int:
    return 4 if bool(won) else -5


def metrics(rows: pd.DataFrame, hours: float) -> dict[str, Any]:
    if rows.empty:
        return {
            "trades": 0,
            "wins": 0,
            "winRate": 0.0,
            "pnlU": 0,
            "maxDrawdownU": 0,
            "maxLoss": 0,
            "tradesPerDay": 0.0,
        }
    ordered = rows.sort_values("time")
    pnls = [payout(won) for won in ordered["won"].astype(bool)]
    equity = peak = drawdown = loss_streak = max_loss = 0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        if pnl < 0:
            loss_streak += 1
            max_loss = max(max_loss, loss_streak)
        else:
            loss_streak = 0
    wins = int(ordered["won"].astype(bool).sum())
    return {
        "trades": int(len(ordered)),
        "wins": wins,
        "winRate": round(wins / len(ordered) * 100.0, 2),
        "pnlU": int(sum(pnls)),
        "maxDrawdownU": int(drawdown),
        "maxLoss": int(max_loss),
        "tradesPerDay": round(len(ordered) / hours * 24.0, 2) if hours > 0 else 0.0,
    }


def decide(row: pd.Series) -> tuple[str | None, str]:
    trend = str(row["trend"])
    volatility = str(row["volatility"])
    normal_pos = str(row["normal_pos"])
    sprint = str(row["sprint"])
    flow = str(row["flow"])
    book = str(row["book"])
    range_bucket = str(row["range"])

    # Short sprint exhaustion: after fast upward movement, 10m outcome was
    # consistently better on DOWN across all three local periods.
    if trend == "trend_up" and normal_pos == "above_upper" and sprint == "up_sprint":
        return "DOWN", "trend_up_above_upper_up_sprint_exhaustion"
    if trend == "trend_up" and volatility in {"sigma_mid", "sigma_midlow"} and normal_pos == "above_upper":
        return "DOWN", "trend_up_above_upper_fade"
    if trend == "drift_up" and volatility == "sigma_midlow" and normal_pos == "upper_edge":
        return "DOWN", "drift_up_midlow_upper_edge_fade"

    # Down-trend lower extension: the broad minute data says the next 10m often
    # rebounds. This is separate from the older second-level stable-down-break.
    if trend == "trend_down" and volatility in {"sigma_midlow", "sigma_mid"} and normal_pos == "below_lower":
        return "UP", "trend_down_below_lower_rebound"
    if trend == "trend_down" and volatility == "sigma_high" and normal_pos == "lower_inside":
        return "UP", "trend_down_high_lower_inside_rebound"
    if trend == "trend_down" and volatility == "sigma_midlow" and range_bucket == "range_tight" and normal_pos == "below_lower":
        return "UP", "trend_down_tight_below_lower_rebound"

    # Transition below lower did not behave like clean reversion; it continued
    # DOWN in the split test.
    if trend == "transition" and normal_pos in {"below_lower", "lower_inside"} and sprint in {"none", "down_sprint"}:
        return "DOWN", "transition_lower_continuation"

    # Flat-state branches. These are deliberately lower priority because they
    # are closer to statistical drift than strong structural events.
    if trend == "flat" and volatility == "sigma_mid" and sprint == "none":
        return "DOWN", "flat_mid_sigma_down_bias"
    if trend == "flat" and volatility == "sigma_low" and normal_pos == "upper_edge":
        return "UP", "flat_low_sigma_upper_edge_up_bias"

    # More specific order-flow-supported cases.
    if trend == "drift_down" and volatility == "sigma_mid" and normal_pos == "below_lower" and flow == "flow_down" and book == "book_down":
        return "UP", "drift_down_below_lower_flow_book_rebound"
    if trend == "trend_up" and volatility == "sigma_mid" and normal_pos == "above_upper" and flow == "flow_neutral" and book == "book_up":
        return "DOWN", "trend_up_above_upper_book_no_flow_fade"

    return None, "wait"


def apply_router(snapshots: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    last_time_by_source: dict[str, pd.Timestamp] = {}
    for row in snapshots.sort_values(["source", "time"]).to_dict("records"):
        signal, branch = decide(pd.Series(row))
        if signal is None:
            continue
        timestamp = pd.Timestamp(row["time"])
        source = str(row["source"])
        last_time = last_time_by_source.get(source)
        if last_time is not None and (timestamp - last_time).total_seconds() < 600:
            continue
        future = float(row["future10_bps"])
        won = future > 0 if signal == "UP" else future < 0
        record = dict(row)
        record["signal"] = signal
        record["branch"] = branch
        record["won"] = bool(won)
        record["pnl"] = payout(won)
        rows.append(record)
        last_time_by_source[source] = timestamp
    return pd.DataFrame(rows)


def run() -> dict[str, Any]:
    snapshots = []
    source_hours = {}
    for source_name, seconds, orderbook in matrix.SOURCES:
        data = matrix.load_local_data(seconds, orderbook)
        source_hours[source_name] = (data.index.max() - data.index.min()).total_seconds() / 3600.0
        snapshots.append(matrix.build_minute_snapshots(data, source_name))
    all_snapshots = pd.concat(snapshots, ignore_index=True)
    trades = apply_router(all_snapshots)
    if not trades.empty:
        trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    reports = {}
    for source_name, group in trades.groupby("source") if not trades.empty else []:
        reports[str(source_name)] = {
            "result": metrics(group, source_hours[str(source_name)]),
            "byBranch": {
                str(branch): metrics(branch_group, source_hours[str(source_name)])
                for branch, branch_group in group.groupby("branch")
            },
        }
    total_hours = sum(source_hours.values())
    output = {
        "method": "Router from all-branch matrix. Uses only branches whose coarse/mid split showed same direction across local periods. 10-minute cooldown per source.",
        "total": metrics(trades, total_hours),
        "bySource": reports,
        "byBranchTotal": {
            str(branch): metrics(group, total_hours)
            for branch, group in trades.groupby("branch")
        }
        if not trades.empty
        else {},
        "csv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
