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
import research_all_branch_walkforward_audit as wf  # noqa: E402
from research_all_branch_router_strategy import metrics, payout  # noqa: E402


OUT_JSON = ROOT / "tmp" / "all_branch_vote_router.json"
OUT_CSV = ROOT / "tmp" / "all_branch_vote_router_trades.csv"


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


RULE_SETS = {
    "balanced": [
        ("trend_vol_sprint", ["trend", "volatility", "sprint"], {"min_total": 50, "min_per_source": 8, "min_rate": 60.0}),
        ("trend_vol_pos", ["trend", "volatility", "normal_pos"], {"min_total": 35, "min_per_source": 6, "min_rate": 60.0}),
        ("trend_pos_sprint", ["trend", "normal_pos", "sprint"], {"min_total": 35, "min_per_source": 6, "min_rate": 60.0}),
        ("trend_vol_pos_flow_book", ["trend", "volatility", "normal_pos", "flow", "book"], {"min_total": 25, "min_per_source": 4, "min_rate": 58.0}),
    ],
    "strict": [
        ("trend_vol_sprint", ["trend", "volatility", "sprint"], {"min_total": 50, "min_per_source": 8, "min_rate": 60.0}),
        ("trend_vol_pos", ["trend", "volatility", "normal_pos"], {"min_total": 50, "min_per_source": 8, "min_rate": 60.0}),
        ("trend_pos_sprint", ["trend", "normal_pos", "sprint"], {"min_total": 50, "min_per_source": 8, "min_rate": 60.0}),
        ("trend_vol_pos_flow_book", ["trend", "volatility", "normal_pos", "flow", "book"], {"min_total": 25, "min_per_source": 4, "min_rate": 58.0}),
    ],
}


def compile_rules(train: pd.DataFrame, rule_set: str) -> list[dict[str, Any]]:
    compiled = []
    for layer_name, keys, params in RULE_SETS[rule_set]:
        compiled.append(
            {
                "layer": layer_name,
                "keys": keys,
                "rules": wf.select_rules(train, keys, **params),
            }
        )
    return compiled


def vote_for(row: dict[str, Any], compiled: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    votes = []
    for layer in compiled:
        key = "|".join(str(row[column]) for column in layer["keys"])
        rule = layer["rules"].get(key)
        if rule is None:
            continue
        votes.append({"layer": layer["layer"], "key": key, "signal": rule["signal"], "rate": rule["trainWinRate"]})
    up = sum(1 for vote in votes if vote["signal"] == "UP")
    down = sum(1 for vote in votes if vote["signal"] == "DOWN")
    if up == down:
        return None, {"votes": votes, "upVotes": up, "downVotes": down}
    return ("UP" if up > down else "DOWN"), {"votes": votes, "upVotes": up, "downVotes": down}


def apply_vote_router(
    test: pd.DataFrame,
    compiled: list[dict[str, Any]],
    min_votes: int,
    require_no_conflict: bool,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    for row in test.sort_values("time").to_dict("records"):
        signal, info = vote_for(row, compiled)
        total_votes = int(info["upVotes"]) + int(info["downVotes"])
        if signal is None or total_votes < min_votes:
            continue
        if require_no_conflict and info["upVotes"] > 0 and info["downVotes"] > 0:
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
        record["upVotes"] = info["upVotes"]
        record["downVotes"] = info["downVotes"]
        record["voteLayers"] = ";".join(vote["layer"] for vote in info["votes"])
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
    sources = sorted(all_snapshots["source"].unique())
    variants = []
    all_trades = []
    for rule_set in RULE_SETS:
        for min_votes in (1, 2, 3):
            for require_no_conflict in (False, True):
                fold_results = []
                variant_trades = []
                for test_source in sources:
                    train = all_snapshots[all_snapshots["source"] != test_source].copy()
                    test = all_snapshots[all_snapshots["source"] == test_source].copy()
                    compiled = compile_rules(train, rule_set)
                    trades = apply_vote_router(test, compiled, min_votes, require_no_conflict)
                    if not trades.empty:
                        trades["testSource"] = test_source
                        trades["ruleSet"] = rule_set
                        trades["minVotes"] = min_votes
                        trades["requireNoConflict"] = require_no_conflict
                        variant_trades.append(trades)
                        all_trades.append(trades)
                    fold_results.append(
                        {
                            "testSource": str(test_source),
                            "result": metrics(trades, source_hours[str(test_source)]),
                        }
                    )
                combined = pd.concat(variant_trades, ignore_index=True) if variant_trades else pd.DataFrame()
                variants.append(
                    {
                        "ruleSet": rule_set,
                        "minVotes": min_votes,
                        "requireNoConflict": require_no_conflict,
                        "total": metrics(combined, sum(source_hours.values())),
                        "folds": fold_results,
                    }
                )

    if all_trades:
        pd.concat(all_trades, ignore_index=True).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    output = {
        "method": "Leave-one-source-out vote router. Each held-out period is traded only by rules learned on the other two periods.",
        "sources": source_ranges,
        "variants": variants,
        "best": sorted(
            variants,
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
