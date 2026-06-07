"""Compare delayed and confirmation-based entry policies.

This is research-only. It settles the same walk-forward signal opportunities
under several causal entry policies:
- immediate entry at the closed 5m signal candle
- fixed 1m/2m/3m/5m delays
- next-1m direction confirmation
- within-3m direction confirmation
- pullback/retest before entry

Historical backtest uses 1m closes, so sub-minute policies such as 30s entry
cannot be measured reliably from this dataset.
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import load_symbol  # noqa: E402
from strategy_robustness_profile import trade_frequency  # noqa: E402
from validate_execution_latency import (  # noqa: E402
    PAYOUT,
    STAKE,
    collect_opportunities,
    load_1m,
    load_config,
    metric,
    price_at,
    status_for,
)

OUT = "E:/codex/data"
TRADE_CONFIG_FILE = os.path.join(OUT, "trade_config.json")
REPORT_FILE = os.path.join(OUT, "entry_timing_confirmation_report.json")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def direction_ok(direction, later, reference):
    if later is None or reference is None:
        return False
    return later > reference if direction == "UP" else later < reference


def pullback_ok(direction, price, reference, bps):
    if price is None or reference is None:
        return False
    move = reference * float(bps) / 10000
    return price <= reference - move if direction == "UP" else price >= reference + move


def settle_entry(opp, cfg, times_1m, prices_1m, entry_time):
    entry_price = price_at(times_1m, prices_1m, entry_time)
    expiry_time = entry_time + pd.Timedelta(minutes=int(cfg["duration"]))
    close_price = price_at(times_1m, prices_1m, expiry_time)
    if entry_price is None or close_price is None:
        return None
    status = status_for(opp["direction"], entry_price, close_price)
    return {
        "strategy": opp["strategy"],
        "signal_time": opp["time"],
        "actionable_time": opp["time"] + pd.Timedelta(minutes=5),
        "entry_time": entry_time,
        "expiry_time": expiry_time,
        "direction": opp["direction"],
        "entry_price": entry_price,
        "close_price": close_price,
        "status": status,
        "win": status == "won",
        "confidence": opp.get("confidence"),
        "rsi": opp.get("rsi"),
    }


def entry_time_for_policy(opp, cfg, times_1m, prices_1m, policy):
    actionable = opp["time"] + pd.Timedelta(minutes=5)
    base_price = price_at(times_1m, prices_1m, actionable)
    if base_price is None:
        return None
    name = policy["name"]
    if name == "immediate":
        return actionable
    if name == "delay":
        return actionable + pd.Timedelta(minutes=int(policy["minutes"]))
    if name == "confirm_next_1m":
        t = actionable + pd.Timedelta(minutes=1)
        p = price_at(times_1m, prices_1m, t)
        return t if direction_ok(opp["direction"], p, base_price) else None
    if name == "confirm_within":
        max_wait = int(policy["max_wait_min"])
        for minute in range(1, max_wait + 1):
            t = actionable + pd.Timedelta(minutes=minute)
            p = price_at(times_1m, prices_1m, t)
            if direction_ok(opp["direction"], p, base_price):
                return t
        return None
    if name == "pullback_within":
        max_wait = int(policy["max_wait_min"])
        bps = float(policy["pullback_bps"])
        for minute in range(1, max_wait + 1):
            t = actionable + pd.Timedelta(minutes=minute)
            p = price_at(times_1m, prices_1m, t)
            if pullback_ok(opp["direction"], p, base_price, bps):
                return t
        return None
    if name == "pullback_then_confirm":
        max_wait = int(policy["max_wait_min"])
        bps = float(policy["pullback_bps"])
        for minute in range(1, max_wait + 1):
            pull_time = actionable + pd.Timedelta(minutes=minute)
            pull_price = price_at(times_1m, prices_1m, pull_time)
            if not pullback_ok(opp["direction"], pull_price, base_price, bps):
                continue
            confirm_time = pull_time + pd.Timedelta(minutes=1)
            confirm_price = price_at(times_1m, prices_1m, confirm_time)
            if direction_ok(opp["direction"], confirm_price, pull_price):
                return confirm_time
        return None
    raise ValueError(f"unknown entry policy: {policy}")


def policy_label(policy):
    name = policy["name"]
    if name == "delay":
        return f"delay_{policy['minutes']}m"
    if name == "confirm_within":
        return f"confirm_within_{policy['max_wait_min']}m"
    if name == "pullback_within":
        return f"pullback_{policy['pullback_bps']}bp_within_{policy['max_wait_min']}m"
    if name == "pullback_then_confirm":
        return f"pullback_{policy['pullback_bps']}bp_then_confirm_{policy['max_wait_min']}m"
    return name


def policies():
    rows = [{"name": "immediate"}]
    rows.extend({"name": "delay", "minutes": m} for m in [1, 2, 3, 5])
    rows.append({"name": "confirm_next_1m"})
    rows.extend({"name": "confirm_within", "max_wait_min": m} for m in [2, 3, 5])
    for bps in [0, 5, 10]:
        rows.append({"name": "pullback_within", "pullback_bps": bps, "max_wait_min": 3})
        rows.append({"name": "pullback_then_confirm", "pullback_bps": bps, "max_wait_min": 5})
    return rows


def frequency_frame(rows):
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).rename(columns={"entry_time": "time"})


def chronological_blocks(rows, blocks=10):
    if not rows:
        return [], {"active_blocks": 0, "positive_blocks": 0, "min_block_wr": None, "worst_block": None}
    df = pd.DataFrame(rows).sort_values("entry_time").reset_index(drop=True)
    out = []
    for i, idx in enumerate(np.array_split(np.arange(len(df)), blocks), start=1):
        part = df.iloc[idx]
        m = metric(part["status"].tolist())
        out.append({
            "slice": f"block_{i:02d}",
            "start": str(part["entry_time"].iloc[0]),
            "end": str(part["entry_time"].iloc[-1]),
            **m,
        })
    active = [r for r in out if r["trades"] >= 10]
    if not active:
        return out, {"active_blocks": 0, "positive_blocks": 0, "min_block_wr": None, "worst_block": None}
    worst = min(active, key=lambda r: r["wr"])
    return out, {
        "active_blocks": len(active),
        "positive_blocks": sum(1 for r in active if r["pnl_5u"] > 0),
        "min_block_wr": worst["wr"],
        "worst_block": worst["slice"],
    }


def evaluate_policy(opps, cfg, times_1m, prices_1m, policy):
    rows = []
    for opp in opps:
        entry_time = entry_time_for_policy(opp, cfg, times_1m, prices_1m, policy)
        if entry_time is None:
            continue
        row = settle_entry(opp, cfg, times_1m, prices_1m, entry_time)
        if row is not None:
            rows.append(row)
    m = metric([r["status"] for r in rows])
    blocks, block_sum = chronological_blocks(rows)
    return {
        "policy": policy_label(policy),
        "params": policy,
        "overall": m,
        "frequency": trade_frequency(frequency_frame(rows)),
        "retention_pct": None,
        "time_block_summary": block_sum,
        "time_blocks": blocks,
        "examples": [
            {
                "signal_time": str(r["signal_time"]),
                "entry_time": str(r["entry_time"]),
                "expiry_time": str(r["expiry_time"]),
                "direction": r["direction"],
                "entry_price": r["entry_price"],
                "close_price": r["close_price"],
                "status": r["status"],
                "confidence": r.get("confidence"),
                "rsi": round(float(r.get("rsi") or 0), 2),
            }
            for r in rows[:8]
        ],
    }


def evaluate_strategy(df5, strategy_id, cfg, times_1m, prices_1m):
    opps = collect_opportunities(df5, strategy_id, cfg)
    rows = [evaluate_policy(opps, cfg, times_1m, prices_1m, p) for p in policies()]
    baseline = next(r for r in rows if r["policy"] == "immediate")
    base_trades = max(1, baseline["overall"]["trades"])
    base_wr = baseline["overall"]["wr"]
    for row in rows:
        row["retention_pct"] = round(row["overall"]["trades"] / base_trades * 100, 2)
        row["wr_delta_pp"] = round(row["overall"]["wr"] - base_wr, 2)
        row["trade_delta"] = int(row["overall"]["trades"]) - int(baseline["overall"]["trades"])
    ranked = sorted(
        rows,
        key=lambda r: (
            r["overall"]["wr"],
            r["time_block_summary"]["min_block_wr"] or 0,
            -r["overall"]["max_loss"],
            r["overall"]["trades"],
        ),
        reverse=True,
    )
    usable = [
        r for r in ranked
        if r["overall"]["trades"] >= 80
        and r["retention_pct"] >= 35
        and r["overall"]["wr"] >= base_wr
        and (r["time_block_summary"]["active_blocks"] or 0) >= 4
    ]
    return {
        "opportunities": len(opps),
        "baseline": baseline,
        "top_by_wr": ranked[:20],
        "top_usable": usable[:20],
        "all_policies": rows,
    }


def main():
    df5 = load_symbol("btcusdt")
    configs = load_config()
    times_1m, prices_1m = load_1m()
    trade_cfg = read_json(TRADE_CONFIG_FILE, {})
    report = {
        "method": {
            "type": "entry_timing_confirmation_1m_close_backtest",
            "payout": PAYOUT,
            "stake": STAKE,
            "data_resolution": "1m closes",
            "sub_minute_limitation": "30s entry cannot be validated reliably without tick/trade data; this report validates 1m-or-greater policies.",
            "note": "Research only. No production config is changed.",
        },
        "safety": {
            "autoTrade": trade_cfg.get("autoTrade"),
            "verdict": "research_only_do_not_resume_real_auto_trading",
        },
        "strategies": {},
        "conclusions": [],
    }
    for strategy_id in ["BTC_10min", "BTC_30min"]:
        result = evaluate_strategy(df5, strategy_id, configs[strategy_id], times_1m, prices_1m)
        report["strategies"][strategy_id] = result
        baseline = result["baseline"]
        best = (result["top_usable"] or result["top_by_wr"] or [baseline])[0]
        report["conclusions"].append(
            f"{strategy_id}: immediate WR {baseline['overall']['wr']}%/{baseline['overall']['trades']} trades; "
            f"best usable entry policy {best['policy']} WR {best['overall']['wr']}%/"
            f"{best['overall']['trades']} trades ({best['wr_delta_pp']:+.2f}pp, retention {best['retention_pct']}%)."
        )
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(json.dumps({
        "safety": report["safety"],
        "conclusions": report["conclusions"],
        "top": {
            sid: [
                {
                    "policy": row["policy"],
                    "wr": row["overall"]["wr"],
                    "trades": row["overall"]["trades"],
                    "delta": row["wr_delta_pp"],
                    "retention": row["retention_pct"],
                    "max_loss": row["overall"]["max_loss"],
                }
                for row in payload["top_usable"][:6]
            ]
            for sid, payload in report["strategies"].items()
        },
    }, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
