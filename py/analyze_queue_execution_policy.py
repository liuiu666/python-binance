"""Compare AutoJS queue execution policies with 1m entry/expiry settlement.

The live tablet can only click Binance sequentially. When 10m and 30m signals
arrive on the same actionable candle, this report applies a conservative
extra delay to the second order and compares queue policies.
"""
import json
import os
import sys

import pandas as pd
import numpy as np

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import load_symbol
from strategy_robustness_profile import trade_frequency
from validate_execution_latency import (
    load_1m,
    load_config,
    collect_opportunities,
    price_at,
    status_for,
    metric,
    STAKE,
    PAYOUT,
)

OUT = "E:/codex/data"
REPORT_FILE = os.path.join(OUT, "queue_execution_policy_report.json")
QUEUE_ORDER = ["BTC_30min", "BTC_10min"]
SECOND_ORDER_EXTRA_DELAY_MIN = 1
SECOND_ORDER_DELAY_STRESS_MIN = [0, 1, 2, 3, 5]


def collect_all_opportunities():
    df5 = load_symbol("btcusdt")
    configs = load_config()
    rows = []
    for strategy_id in QUEUE_ORDER:
        cfg = configs[strategy_id]
        for opp in collect_opportunities(df5, strategy_id, cfg):
            rows.append({
                **opp,
                "strategy_id": strategy_id,
                "duration": int(cfg["duration"]),
                "actionable_time": opp["time"] + pd.Timedelta(minutes=5),
                "queue_order": QUEUE_ORDER.index(strategy_id),
            })
    return pd.DataFrame(rows), configs


def order_policy(group, policy):
    group = group.copy()
    if policy in ("both_10_then_30", "skip_direction_conflicts_10_then_30"):
        order = {"BTC_10min": 0, "BTC_30min": 1}
        return group.assign(policy_order=group["strategy_id"].map(order)).sort_values("policy_order")
    if policy in ("both_confidence_desc", "skip_direction_conflicts_confidence_desc"):
        return group.assign(policy_order=-group["confidence"].astype(float)).sort_values("policy_order")
    return group.sort_values("queue_order")


def filter_policy(group, policy):
    if policy in ("both_30_then_10", "both_10_then_30", "both_confidence_desc"):
        return order_policy(group, policy)
    if policy == "skip_direction_conflicts":
        if group["direction"].nunique() > 1 and group["strategy_id"].nunique() > 1:
            return group.iloc[0:0]
        return order_policy(group, policy)
    if policy == "skip_direction_conflicts_10_then_30":
        if group["direction"].nunique() > 1 and group["strategy_id"].nunique() > 1:
            return group.iloc[0:0]
        return order_policy(group, policy)
    if policy == "skip_direction_conflicts_confidence_desc":
        if group["direction"].nunique() > 1 and group["strategy_id"].nunique() > 1:
            return group.iloc[0:0]
        return order_policy(group, policy)
    if policy == "same_candle_keep_30min_only":
        if group["strategy_id"].nunique() > 1:
            return group[group["strategy_id"] == "BTC_30min"]
        return group
    if policy == "same_candle_keep_10min_only":
        if group["strategy_id"].nunique() > 1:
            return group[group["strategy_id"] == "BTC_10min"]
        return group
    raise ValueError(f"unknown policy: {policy}")


def settle(policy, opps, times_1m, prices_1m, second_delay_min):
    statuses = []
    rows = []
    for _, group in opps.sort_values(["actionable_time", "queue_order"]).groupby("actionable_time", sort=True):
        selected = filter_policy(group.sort_values("queue_order"), policy)
        for pos, (_, opp) in enumerate(selected.iterrows()):
            extra_delay = second_delay_min if pos > 0 else 0
            entry_time = opp["actionable_time"] + pd.Timedelta(minutes=extra_delay)
            expiry_time = entry_time + pd.Timedelta(minutes=int(opp["duration"]))
            open_price = price_at(times_1m, prices_1m, entry_time)
            close_price = price_at(times_1m, prices_1m, expiry_time)
            if open_price is None or close_price is None:
                continue
            status = status_for(opp["direction"], open_price, close_price)
            statuses.append(status)
            rows.append({
                "time": opp["actionable_time"],
                "strategy_id": opp["strategy_id"],
                "direction": opp["direction"],
                "duration": int(opp["duration"]),
                "queue_position": pos + 1,
                "extra_delay_min": extra_delay,
                "entry_time": entry_time,
                "expiry_time": expiry_time,
                "open_price": open_price,
                "close_price": close_price,
                "status": status,
                "win": status == "won",
            })
    out = pd.DataFrame(rows)
    overall = metric(statuses)
    per_strategy = {}
    if not out.empty:
        for strategy_id, part in out.groupby("strategy_id"):
            per_strategy[strategy_id] = {
                "metrics": metric(part["status"].tolist()),
                "frequency": trade_frequency(frequency_frame(part)),
            }
    return {
        "metrics": overall,
        "frequency": trade_frequency(frequency_frame(out) if not out.empty else out),
        "per_strategy": per_strategy,
        "trades": out,
        "same_candle_groups": int((opps.groupby("actionable_time")["strategy_id"].nunique() > 1).sum()),
        "direction_conflict_groups": int(
            sum(
                1
                for _, g in opps.groupby("actionable_time")
                if g["strategy_id"].nunique() > 1 and g["direction"].nunique() > 1
            )
        ),
    }


def compact_settlement(df):
    if df.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "wr": 0.0,
            "pnl_5u": 0.0,
            "max_loss": 0,
        }
    return metric(df["status"].tolist())


def stability_blocks(results, baseline_policy, candidate_policy, blocks=10):
    base = results[baseline_policy]["trades"]
    cand = results[candidate_policy]["trades"]
    if base.empty and cand.empty:
        return {"blocks": [], "summary": {}}
    all_times = pd.concat([base["entry_time"], cand["entry_time"]]).sort_values().reset_index(drop=True)
    rows = []
    for i, idx in enumerate(np.array_split(np.arange(len(all_times)), blocks), start=1):
        if len(idx) == 0:
            continue
        start, end = all_times.iloc[idx[0]], all_times.iloc[idx[-1]]
        b = base[(base["entry_time"] >= start) & (base["entry_time"] <= end)]
        c = cand[(cand["entry_time"] >= start) & (cand["entry_time"] <= end)]
        bm = compact_settlement(b)
        cm = compact_settlement(c)
        rows.append({
            "slice": f"block_{i:02d}",
            "start": str(start),
            "end": str(end),
            "baseline": bm,
            "candidate": cm,
            "wr_delta_pp": round(float(cm["wr"]) - float(bm["wr"]), 2),
            "trade_delta": int(cm["trades"]) - int(bm["trades"]),
            "max_loss_delta": int(cm["max_loss"]) - int(bm["max_loss"]),
        })
    active = [r for r in rows if int(r["candidate"]["trades"]) >= 30]
    return {
        "baseline_policy": baseline_policy,
        "candidate_policy": candidate_policy,
        "blocks": rows,
        "summary": {
            "improved_blocks": sum(1 for r in rows if r["wr_delta_pp"] > 0),
            "worsened_blocks": sum(1 for r in rows if r["wr_delta_pp"] < 0),
            "unchanged_blocks": sum(1 for r in rows if r["wr_delta_pp"] == 0),
            "blocks_with_trade_loss": sum(1 for r in rows if r["trade_delta"] < 0),
            "blocks_with_higher_max_loss": sum(1 for r in rows if r["max_loss_delta"] > 0),
            "min_candidate_wr": min((float(r["candidate"]["wr"]) for r in active), default=0),
            "min_baseline_wr": min((float(r["baseline"]["wr"]) for r in rows if int(r["baseline"]["trades"]) >= 30), default=0),
        },
    }


def frequency_frame(df):
    return df.drop(columns=["time"], errors="ignore").rename(columns={"entry_time": "time"})


def public_results(results):
    return {
        name: {k: v for k, v in row.items() if k != "trades"}
        for name, row in results.items()
    }


def ranking_for(results, baseline_policy):
    baseline = results[baseline_policy]["metrics"]
    return sorted(
        [
            {
                "policy": name,
                "wr": row["metrics"]["wr"],
                "trades": row["metrics"]["trades"],
                "pnl_5u": row["metrics"]["pnl_5u"],
                "max_loss": row["metrics"]["max_loss"],
                "trades_per_day": row["frequency"].get("trades_per_day"),
                "wr_delta_vs_baseline": round(row["metrics"]["wr"] - baseline["wr"], 2),
                "trade_delta_vs_baseline": int(row["metrics"]["trades"] - baseline["trades"]),
            }
            for name, row in results.items()
        ],
        key=lambda r: (r["wr"], r["trades"], r["pnl_5u"]),
        reverse=True,
    )


def build_results_for_delay(policies, opps, times_1m, prices_1m, delay_min):
    return {
        policy: settle(policy, opps, times_1m, prices_1m, delay_min)
        for policy in policies
    }


def delay_sensitivity(policies, opps, times_1m, prices_1m, baseline_policy):
    rows = {}
    for delay in SECOND_ORDER_DELAY_STRESS_MIN:
        results = build_results_for_delay(policies, opps, times_1m, prices_1m, delay)
        ranking = ranking_for(results, baseline_policy)
        rows[str(delay)] = {
            "ranking": ranking,
            "results": {
                name: {
                    "win_rate": row["metrics"]["wr"],
                    "trades": row["metrics"]["trades"],
                    "pnl_5u": row["metrics"]["pnl_5u"],
                    "max_loss": row["metrics"]["max_loss"],
                    "trades_per_day": row["frequency"].get("trades_per_day"),
                }
                for name, row in results.items()
            },
            "best_policy": ranking[0] if ranking else None,
        }
    watched = ["both_confidence_desc", "skip_direction_conflicts_confidence_desc"]
    summary = {}
    for policy in watched:
        policy_rows = []
        for delay, row in rows.items():
            result = row["results"].get(policy) or {}
            baseline = row["results"].get(baseline_policy) or {}
            policy_rows.append({
                "delay_min": int(delay),
                "win_rate": result.get("win_rate"),
                "trades": result.get("trades"),
                "max_loss": result.get("max_loss"),
                "wr_delta_vs_baseline": round(float(result.get("win_rate") or 0) - float(baseline.get("win_rate") or 0), 2),
                "trade_delta_vs_baseline": int(result.get("trades") or 0) - int(baseline.get("trades") or 0),
            })
        summary[policy] = {
            "by_delay": policy_rows,
            "min_wr": min((float(r["win_rate"]) for r in policy_rows), default=0),
            "min_wr_delta_vs_baseline": min((float(r["wr_delta_vs_baseline"]) for r in policy_rows), default=0),
            "max_loss_worst": max((int(r["max_loss"] or 0) for r in policy_rows), default=0),
        }
    return {
        "delays_min": SECOND_ORDER_DELAY_STRESS_MIN,
        "baseline_policy": baseline_policy,
        "by_delay": rows,
        "summary": summary,
    }


def main():
    opps, _configs = collect_all_opportunities()
    times_1m, prices_1m = load_1m()
    policies = [
        "both_30_then_10",
        "both_10_then_30",
        "both_confidence_desc",
        "skip_direction_conflicts",
        "skip_direction_conflicts_10_then_30",
        "skip_direction_conflicts_confidence_desc",
        "same_candle_keep_30min_only",
        "same_candle_keep_10min_only",
    ]
    results = build_results_for_delay(policies, opps, times_1m, prices_1m, SECOND_ORDER_EXTRA_DELAY_MIN)

    baseline_policy = "both_30_then_10"
    report = {
        "method": {
            "type": "queue_execution_policy_1m_settlement",
            "stake": STAKE,
            "payout": PAYOUT,
            "queue_order": QUEUE_ORDER,
            "second_order_extra_delay_min": SECOND_ORDER_EXTRA_DELAY_MIN,
            "note": "Same-actionable-time orders are sorted as AutoJS executes them. The second order is conservatively settled with an extra 1m entry delay.",
        },
        "baseline_policy": baseline_policy,
        "results": public_results(results),
        "stability": {
            "both_confidence_desc": stability_blocks(results, "both_30_then_10", "both_confidence_desc"),
            "skip_direction_conflicts_confidence_desc": stability_blocks(results, "both_30_then_10", "skip_direction_conflicts_confidence_desc"),
        },
        "delay_sensitivity": delay_sensitivity(policies, opps, times_1m, prices_1m, baseline_policy),
        "ranking": ranking_for(results, baseline_policy),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
