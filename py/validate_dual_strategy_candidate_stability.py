"""Stability check for the causal dual-strategy production hint."""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from analyze_parallel_portfolio import load_strategy_trades
from backtest_enhanced import load_symbol
from strategy_robustness_profile import prediction_frame, trade_frequency
from validate_strategy_candidates import PAYOUT, STAKE, collect_predictions, metric

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
REFERENCE_CONFIG_FILE = os.path.join(OUT, "prod_config.before_causal_v2.json")
SEARCH_FILE = os.path.join(OUT, "dual_strategy_causal_filter_search.json")
REPORT_FILE = os.path.join(OUT, "dual_strategy_candidate_stability.json")
BREAKEVEN_WR = 100 / (1 + PAYOUT)
STRATEGY_ORDER = {"BTC_30min": 0, "BTC_10min": 1}


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def candidate_to_cfg(strategy_id, cand):
    lo, hi = cand["rsi"]
    return {
        "horizon": 2 if strategy_id == "BTC_10min" else 6,
        "interval_min": 10 if strategy_id == "BTC_10min" else 30,
        "threshold": float(cand["threshold"]),
        "rsi_lo": float(lo),
        "rsi_hi": float(hi),
        "agree_mode": cand["agree_mode"],
        "skip_hours_utc": [int(h) for h in cand.get("skip_hours_utc", [])],
        "fixed_amount": 5 if strategy_id == "BTC_10min" else None,
    }


def candidate_trades(df5, strategy_id, cand):
    preds = collect_predictions(df5, 2 if strategy_id == "BTC_10min" else 6, strategy_id)
    trades = prediction_frame(preds, {
        "name": cand["name"],
        "threshold": float(cand["threshold"]),
        "rsi": tuple(cand["rsi"]),
        "vol_min_rank": None,
        "agree_mode": cand["agree_mode"],
        "skip_hours_utc": [int(h) for h in cand.get("skip_hours_utc", [])],
    })
    if trades.empty:
        return trades
    trades = trades.copy()
    trades["strategy_id"] = strategy_id
    trades["queue_order"] = STRATEGY_ORDER[strategy_id]
    trades["hour_utc"] = trades["time"].dt.hour
    return trades


def current_trades(df5, strategy_id, cfg):
    trades = load_strategy_trades(df5, strategy_id, cfg)
    if trades.empty:
        return trades
    trades = trades.copy()
    trades["queue_order"] = STRATEGY_ORDER[strategy_id]
    trades["hour_utc"] = trades["time"].dt.hour
    return trades


def compact(df):
    m = metric(df["win"].to_numpy() if not df.empty else [])
    freq = trade_frequency(df)
    return {
        **m,
        "edge_over_breakeven": round(float(m["wr"]) - BREAKEVEN_WR, 2),
        "trades_per_day": freq["trades_per_day"],
    }


def blocks(base, candidate, n=10):
    if base.empty and candidate.empty:
        return []
    all_times = pd.concat([base["time"], candidate["time"]]).sort_values().reset_index(drop=True)
    rows = []
    for i, idx in enumerate(np.array_split(np.arange(len(all_times)), n), start=1):
        if len(idx) == 0:
            continue
        start, end = all_times.iloc[idx[0]], all_times.iloc[idx[-1]]
        b = base[(base["time"] >= start) & (base["time"] <= end)]
        c = candidate[(candidate["time"] >= start) & (candidate["time"] <= end)]
        bm = compact(b)
        cm = compact(c)
        rows.append({
            "slice": f"block_{i:02d}",
            "start": str(start),
            "end": str(end),
            "current": bm,
            "candidate": cm,
            "wr_delta_pp": round(float(cm["wr"]) - float(bm["wr"]), 2),
            "trade_delta": int(cm["trades"]) - int(bm["trades"]),
            "max_loss_delta": int(cm["max_loss"]) - int(bm["max_loss"]),
        })
    return rows


def summary(rows):
    candidate_active = [r for r in rows if int(r["candidate"]["trades"]) >= 30]
    return {
        "improved_blocks": sum(1 for r in rows if r["wr_delta_pp"] > 0),
        "worsened_blocks": sum(1 for r in rows if r["wr_delta_pp"] < 0),
        "non_positive_candidate_blocks": sum(
            1 for r in candidate_active
            if float(r["candidate"]["wr"]) <= BREAKEVEN_WR
        ),
        "blocks_with_more_trades": sum(1 for r in rows if r["trade_delta"] > 0),
        "blocks_with_higher_max_loss": sum(1 for r in rows if r["max_loss_delta"] > 0),
        "min_candidate_wr": min((float(r["candidate"]["wr"]) for r in candidate_active), default=0),
        "min_current_wr": min((float(r["current"]["wr"]) for r in rows if int(r["current"]["trades"]) >= 30), default=0),
    }


def main():
    config = read_json(CONFIG_FILE)
    reference_config = read_json(REFERENCE_CONFIG_FILE) if os.path.exists(REFERENCE_CONFIG_FILE) else config
    search = read_json(SEARCH_FILE)
    hint = search["production_hint"]["per_strategy"]
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")

    per_strategy = {}
    current_all = []
    candidate_all = []
    for strategy_id in ["BTC_10min", "BTC_30min"]:
        current = current_trades(df5, strategy_id, reference_config[strategy_id])
        cand = hint[strategy_id]["candidate"]
        candidate = candidate_trades(df5, strategy_id, cand)
        current_all.append(current)
        candidate_all.append(candidate)
        rows = blocks(current, candidate, 10)
        per_strategy[strategy_id] = {
            "candidate_config": candidate_to_cfg(strategy_id, cand),
            "reference_config": reference_config[strategy_id],
            "current": compact(current),
            "candidate": compact(candidate),
            "summary": summary(rows),
            "blocks": rows,
        }

    current_combined = pd.concat(current_all, ignore_index=True).sort_values(["time", "queue_order"])
    candidate_combined = pd.concat(candidate_all, ignore_index=True).sort_values(["time", "queue_order"])
    combined_rows = blocks(current_combined, candidate_combined, 10)
    combined_summary = summary(combined_rows)
    report = {
        "method": {
            "type": "reference_before_causal_v2_vs_causal_candidate_block_stability",
            "blocks": 10,
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "reference_config_file": REFERENCE_CONFIG_FILE,
        },
        "per_strategy": per_strategy,
        "combined": {
            "current": compact(current_combined),
            "candidate": compact(candidate_combined),
            "summary": combined_summary,
            "blocks": combined_rows,
        },
        "decision": {
            "status": "production_candidate"
            if (
                combined_summary["non_positive_candidate_blocks"] == 0
                and combined_summary["worsened_blocks"] <= combined_summary["improved_blocks"]
                and per_strategy["BTC_10min"]["summary"]["non_positive_candidate_blocks"] == 0
            )
            else "shadow_only",
            "reason": "Candidate must keep all active blocks above binary-options breakeven and improve at least as many blocks as it worsens.",
        },
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "decision": report["decision"],
        "per_strategy": {
            k: {
                "current": v["current"],
                "candidate": v["candidate"],
                "summary": v["summary"],
                "candidate_config": v["candidate_config"],
            }
            for k, v in per_strategy.items()
        },
        "combined": {
            "current": report["combined"]["current"],
            "candidate": report["combined"]["candidate"],
            "summary": report["combined"]["summary"],
        },
    }, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
