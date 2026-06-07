"""Evaluate the current 10m + 30m production strategies as one queue.

The production executor monitors both strategies in parallel, but Binance UI
actions are still serialized. This report answers the account-level question:
what happens if both validated signal streams are traded together?
"""
import json
import os
import sys

import pandas as pd

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import load_symbol
from strategy_robustness_profile import prediction_frame, strategy_candidate, trade_frequency
from validate_strategy_candidates import PAYOUT, STAKE, collect_predictions, metric

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
REPORT_FILE = os.path.join(OUT, "parallel_portfolio_report.json")
STRATEGY_ORDER = {"BTC_30min": 0, "BTC_10min": 1}


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_strategy_trades(df5, strategy_id, cfg):
    preds = collect_predictions(df5, int(cfg["horizon"]), strategy_id)
    candidate = strategy_candidate(strategy_id, cfg)
    trades = prediction_frame(preds, candidate)
    if trades.empty:
        return trades
    trades = trades.copy()
    trades["strategy_id"] = strategy_id
    trades["interval_min"] = int(cfg.get("interval_min") or 0)
    trades["amount_usdt"] = float(cfg.get("fixed_amount") or cfg.get("amount") or STAKE)
    trades["side"] = trades["direction"].map({1: "UP", 0: "DOWN"})
    return trades


def summarize_trades(trades):
    if trades.empty:
        return {
            "overall": metric([]),
            "frequency": trade_frequency(trades),
            "per_strategy": {},
        }

    combined = trades.sort_values(["time", "queue_order"]).reset_index(drop=True)
    per_strategy = {}
    for strategy_id, part in combined.groupby("strategy_id", sort=True):
        per_strategy[strategy_id] = {
            "interval_min": int(part["interval_min"].iloc[0]),
            "amount_usdt": float(part["amount_usdt"].iloc[0]),
            "metrics": metric(part["win"].to_numpy()),
            "frequency": trade_frequency(part),
        }

    grouped = combined.groupby("time")
    simultaneous = []
    for t, part in grouped:
        strategies = sorted(part["strategy_id"].unique().tolist())
        if len(strategies) < 2:
            continue
        sides = sorted(part["side"].unique().tolist())
        simultaneous.append({
            "time": str(t),
            "strategies": strategies,
            "directions": sides,
            "conflict": len(sides) > 1,
            "wins": int(part["win"].sum()),
            "trades": int(len(part)),
        })

    conflicts = [row for row in simultaneous if row["conflict"]]
    return {
        "overall": metric(combined["win"].to_numpy()),
        "frequency": trade_frequency(combined),
        "per_strategy": per_strategy,
        "queue_policy": {
            "same_candle_order": ["BTC_30min", "BTC_10min"],
            "note": "Signals are monitored in parallel; AutoJS serializes UI actions to avoid operation overlap.",
        },
        "overlap": {
            "same_candle_signal_groups": len(simultaneous),
            "same_direction_groups": len(simultaneous) - len(conflicts),
            "direction_conflict_groups": len(conflicts),
            "conflict_rate_pct": round(len(conflicts) / len(simultaneous) * 100, 2) if simultaneous else 0,
            "sample_conflicts": conflicts[-10:],
        },
    }


def main():
    config = read_json(CONFIG_FILE)
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")

    all_trades = []
    for strategy_id in ["BTC_10min", "BTC_30min"]:
        cfg = config.get(strategy_id) or {}
        if not cfg.get("enabled", True):
            continue
        trades = load_strategy_trades(df5, strategy_id, cfg)
        if not trades.empty:
            trades["queue_order"] = STRATEGY_ORDER.get(strategy_id, 99)
            all_trades.append(trades)

    combined = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    report = {
        "method": {
            "type": "current_production_parallel_portfolio",
            "stake_reference": STAKE,
            "payout": PAYOUT,
            "note": "Uses the same strict walk-forward prediction cache as robustness validation.",
        },
        **summarize_trades(combined),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
