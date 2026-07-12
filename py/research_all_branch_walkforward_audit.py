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
from research_all_branch_router_strategy import metrics, payout  # noqa: E402


OUT_JSON = ROOT / "tmp" / "all_branch_walkforward_audit.json"
OUT_CSV = ROOT / "tmp" / "all_branch_walkforward_audit_trades.csv"


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


LAYERS: dict[str, list[str]] = {
    "trend_vol_pos": ["trend", "volatility", "normal_pos"],
    "trend_vol_sprint": ["trend", "volatility", "sprint"],
    "trend_pos_sprint": ["trend", "normal_pos", "sprint"],
    "trend_vol_pos_flow_book": ["trend", "volatility", "normal_pos", "flow", "book"],
    "trend_vol_range_pos": ["trend", "volatility", "range", "normal_pos"],
}


def key_for(row: pd.Series, keys: list[str]) -> str:
    return "|".join(str(row[key]) for key in keys)


def group_key(name: Any) -> str:
    if isinstance(name, tuple):
        return "|".join(str(item) for item in name)
    return str(name)


def summarize_direction(group: pd.DataFrame) -> tuple[str, float, int]:
    n = len(group)
    up_wins = int(group["up_win"].sum())
    down_wins = int(group["down_win"].sum())
    up_rate = up_wins / n * 100.0 if n else 0.0
    down_rate = down_wins / n * 100.0 if n else 0.0
    if up_rate >= down_rate:
        return "UP", up_rate, up_wins * 4 - (n - up_wins) * 5
    return "DOWN", down_rate, down_wins * 4 - (n - down_wins) * 5


def select_rules(
    train: pd.DataFrame,
    keys: list[str],
    min_total: int,
    min_per_source: int,
    min_rate: float,
) -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    for name, group in train.groupby(keys):
        key = group_key(name)
        if len(group) < min_total:
            continue
        signal, rate, pnl = summarize_direction(group)
        if rate < min_rate or pnl <= 0:
            continue
        ok = True
        source_stats = {}
        for source_name, source_group in group.groupby("source"):
            source_signal, source_rate, source_pnl = summarize_direction(source_group)
            source_stats[str(source_name)] = {
                "n": int(len(source_group)),
                "signal": source_signal,
                "rate": round(source_rate, 2),
                "pnl": int(source_pnl),
            }
            if len(source_group) < min_per_source or source_signal != signal or source_pnl < 0:
                ok = False
        if not ok:
            continue
        rules[key] = {
            "signal": signal,
            "trainSamples": int(len(group)),
            "trainWinRate": round(rate, 2),
            "trainPnlU": int(pnl),
            "sourceStats": source_stats,
        }
    return rules


def apply_rules(test: pd.DataFrame, keys: list[str], rules: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    ordered = test.sort_values("time")
    for row in ordered.to_dict("records"):
        key = "|".join(str(row[column]) for column in keys)
        rule = rules.get(key)
        if rule is None:
            continue
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < 600:
            continue
        signal = str(rule["signal"])
        future = float(row["future10_bps"])
        won = future > 0 if signal == "UP" else future < 0
        record = dict(row)
        record["signal"] = signal
        record["ruleKey"] = key
        record["won"] = bool(won)
        record["pnl"] = payout(won)
        rows.append(record)
        last_time = timestamp
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

    audits = []
    all_trades = []
    params = (
        {"min_total": 25, "min_per_source": 4, "min_rate": 58.0},
        {"min_total": 35, "min_per_source": 6, "min_rate": 60.0},
        {"min_total": 50, "min_per_source": 8, "min_rate": 60.0},
    )
    sources = sorted(all_snapshots["source"].unique())
    for layer_name, keys in LAYERS.items():
        for param in params:
            fold_results = []
            for test_source in sources:
                train = all_snapshots[all_snapshots["source"] != test_source].copy()
                test = all_snapshots[all_snapshots["source"] == test_source].copy()
                rules = select_rules(train, keys, **param)
                trades = apply_rules(test, keys, rules)
                if not trades.empty:
                    trades["layer"] = layer_name
                    trades["testSource"] = test_source
                    trades["param"] = f"n{param['min_total']}_s{param['min_per_source']}_r{int(param['min_rate'])}"
                    all_trades.append(trades)
                fold_results.append(
                    {
                        "testSource": str(test_source),
                        "rules": len(rules),
                        "result": metrics(trades, source_hours[str(test_source)]),
                    }
                )
            total_trades = pd.concat(
                [
                    trade
                    for trade in all_trades
                    if not trade.empty
                    and str(trade["layer"].iloc[0]) == layer_name
                    and str(trade["param"].iloc[0])
                    == f"n{param['min_total']}_s{param['min_per_source']}_r{int(param['min_rate'])}"
                ],
                ignore_index=True,
            ) if any(
                not trade.empty
                and str(trade["layer"].iloc[0]) == layer_name
                and str(trade["param"].iloc[0])
                == f"n{param['min_total']}_s{param['min_per_source']}_r{int(param['min_rate'])}"
                for trade in all_trades
            ) else pd.DataFrame()
            total = metrics(total_trades, sum(source_hours.values()))
            audits.append(
                {
                    "layer": layer_name,
                    "keys": keys,
                    "params": param,
                    "total": total,
                    "folds": fold_results,
                }
            )

    if all_trades:
        pd.concat(all_trades, ignore_index=True).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    output = {
        "method": "Leave-one-source-out. Select branch rules on two periods only, trade the unseen held-out period with 10-minute cooldown.",
        "sources": source_ranges,
        "audits": audits,
        "best": sorted(
            audits,
            key=lambda item: (
                item["total"]["pnlU"],
                item["total"]["winRate"],
                -item["total"]["maxDrawdownU"],
            ),
            reverse=True,
        )[:10],
        "csv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
