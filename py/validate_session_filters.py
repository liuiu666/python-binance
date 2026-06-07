"""Validate small, explainable UTC-hour filters for production strategies.

This is a narrow confirmation pass. It does not scan arbitrary hour
combinations; it tests a few broad policies against the current production
strategy outputs to see whether known weak sessions can be avoided without
destroying trade count.
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import load_symbol
from strategy_robustness_profile import prediction_frame, strategy_candidate
from validate_strategy_candidates import STAKE, PAYOUT, collect_predictions, metric

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
REPORT_FILE = os.path.join(OUT, "session_filter_validation.json")

POLICIES = [
    {
        "name": "all_hours",
        "skip_hours_utc": [],
        "reason": "Current production behavior.",
    },
    {
        "name": "skip_utc_22_23",
        "skip_hours_utc": [22, 23],
        "reason": "Avoids the weakest late-UTC session seen in both strategies.",
    },
    {
        "name": "skip_utc_13_15",
        "skip_hours_utc": [13, 14, 15],
        "reason": "Avoids a broad midday-UTC weak band without picking single hours.",
    },
    {
        "name": "skip_utc_13_15_22_23",
        "skip_hours_utc": [13, 14, 15, 22, 23],
        "reason": "Combines both broad weak bands; expected to reduce trade count more.",
    },
]


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def max_loss_streak_from_df(df):
    if df.empty:
        return 0
    return metric(df["win"].to_numpy())["max_loss"]


def chronological_blocks(df, blocks=10):
    if df.empty:
        return []
    rows = []
    for i, idx in enumerate(np.array_split(np.arange(len(df)), blocks), start=1):
        part = df.iloc[idx]
        rows.append({
            "slice": f"block_{i:02d}",
            "start": str(part["time"].iloc[0]),
            "end": str(part["time"].iloc[-1]),
            **metric(part["win"].to_numpy()),
        })
    return rows


def evaluate_policy(trades, policy):
    filtered = trades[~trades["time"].dt.hour.isin(policy["skip_hours_utc"])].reset_index(drop=True)
    blocks = chronological_blocks(filtered, 10)
    min_block_wr = min((b["wr"] for b in blocks if b["trades"] >= 20), default=None)
    positive_blocks = sum(1 for b in blocks if b["pnl_5u"] > 0)
    full = metric(filtered["win"].to_numpy())
    return {
        "name": policy["name"],
        "skip_hours_utc": policy["skip_hours_utc"],
        "reason": policy["reason"],
        "full_oos": full,
        "trade_retention_pct": round((full["trades"] / max(1, len(trades))) * 100, 2),
        "blocks_10": blocks,
        "positive_blocks": positive_blocks,
        "min_block_wr": min_block_wr,
        "score": round(
            full["pnl_5u"]
            + full["wr"] * 2
            + min(full["trades"], 1000) * 0.12
            + positive_blocks * 20
            - full["max_loss"] * 6
            + float(min_block_wr or 0),
            2,
        ),
    }


def validate_strategy(df5, strategy_id, cfg):
    preds = collect_predictions(df5, int(cfg["horizon"]), strategy_id)
    raw_cfg = dict(cfg)
    raw_cfg.pop("skip_hours_utc", None)
    trades = prediction_frame(preds, strategy_candidate(strategy_id, raw_cfg))
    rows = [evaluate_policy(trades, p) for p in POLICIES]
    rows.sort(key=lambda r: r["score"], reverse=True)
    return {
        "base_trades": len(trades),
        "ranked": rows,
    }


def main():
    config = read_json(CONFIG_FILE)
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")

    report = {
        "method": {
            "type": "small_policy_validation",
            "stake": STAKE,
            "payout": PAYOUT,
            "note": "Only four broad UTC-hour policies are tested; do not treat this as a wide optimizer.",
        },
        "results": {},
    }
    for strategy_id in ["BTC_10min", "BTC_30min"]:
        report["results"][strategy_id] = validate_strategy(df5, strategy_id, config[strategy_id])

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
