"""Causal portfolio-level filter search for the 10m + 30m queue.

This is intentionally small and conservative: the first half of the validated
trade stream is used to pick weak UTC hours, then the second half is used as a
holdout check. Candidates here are not production changes by themselves.
"""
import json
import os
import sys

import pandas as pd

sys.path.insert(0, "E:/codex/py")
from analyze_parallel_portfolio import load_strategy_trades
from backtest_enhanced import load_symbol
from strategy_robustness_profile import trade_frequency
from validate_strategy_candidates import PAYOUT, STAKE, collect_predictions, metric

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
REPORT_FILE = os.path.join(OUT, "portfolio_risk_filter_search.json")
BREAKEVEN_WR = 100 / (1 + PAYOUT)
STRATEGY_ORDER = {"BTC_30min": 0, "BTC_10min": 1}


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_combined_trades():
    config = read_json(CONFIG_FILE)
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")

    rows = []
    for strategy_id in ["BTC_10min", "BTC_30min"]:
        cfg = config.get(strategy_id) or {}
        if not cfg.get("enabled", True):
            continue
        trades = load_strategy_trades(df5, strategy_id, cfg)
        if not trades.empty:
            trades = trades.copy()
            trades["queue_order"] = STRATEGY_ORDER.get(strategy_id, 99)
            rows.append(trades)
    if not rows:
        return pd.DataFrame()
    combined = pd.concat(rows, ignore_index=True)
    combined["hour_utc"] = combined["time"].dt.hour
    return combined.sort_values(["time", "queue_order"]).reset_index(drop=True)


def summarize(df):
    if df.empty:
        return {"metrics": metric([]), "frequency": trade_frequency(df)}
    return {
        "metrics": metric(df["win"].to_numpy()),
        "frequency": trade_frequency(df),
    }


def score_candidate(validation_metrics, retention_pct):
    wr = float(validation_metrics.get("wr") or 0)
    trades = int(validation_metrics.get("trades") or 0)
    max_loss = int(validation_metrics.get("max_loss") or 0)
    pnl = float(validation_metrics.get("pnl_5u") or 0)
    return round(
        (wr - BREAKEVEN_WR) * 18
        + min(trades, 800) * 0.05
        + pnl * 0.03
        + retention_pct * 0.25
        - max_loss * 4,
        2,
    )


def hour_edges(calibration, per_strategy):
    group_cols = ["strategy_id", "hour_utc"] if per_strategy else ["hour_utc"]
    rows = []
    for key, part in calibration.groupby(group_cols):
        if len(part) < 20:
            continue
        m = metric(part["win"].to_numpy())
        if per_strategy:
            strategy_id, hour = key
        else:
            strategy_id = None
            hour = key[0] if isinstance(key, tuple) else key
        rows.append({
            "strategy_id": strategy_id,
            "hour_utc": int(hour),
            "trades": m["trades"],
            "wr": m["wr"],
            "edge_over_breakeven": round(m["wr"] - BREAKEVEN_WR, 2),
        })
    rows.sort(key=lambda r: (r["edge_over_breakeven"], -r["trades"]))
    return rows


def mask_for_candidate(df, candidate):
    keep = pd.Series(True, index=df.index)
    kind = candidate["kind"]

    if kind == "skip_strategy_hours":
        for strategy_id, hours in candidate.get("skip_hours_by_strategy", {}).items():
            keep &= ~((df["strategy_id"] == strategy_id) & (df["hour_utc"].isin(hours)))
    elif kind == "skip_global_hours":
        keep &= ~df["hour_utc"].isin(candidate.get("skip_hours_utc", []))
    elif kind == "same_candle_keep_one":
        keep_strategy = candidate["keep_strategy"]
        duplicate_time = df.groupby("time")["strategy_id"].transform("nunique") > 1
        keep &= ~(duplicate_time & (df["strategy_id"] != keep_strategy))
    elif kind == "skip_direction_conflicts":
        side_count = df.groupby("time")["side"].transform("nunique")
        strategy_count = df.groupby("time")["strategy_id"].transform("nunique")
        keep &= ~((strategy_count > 1) & (side_count > 1))
    elif kind != "baseline":
        raise ValueError(f"Unknown candidate kind: {kind}")
    return keep


def make_candidates(calibration):
    candidates = [{"name": "baseline_parallel", "kind": "baseline"}]

    per_strategy_bad = hour_edges(calibration, per_strategy=True)
    for n in [1, 2, 3, 4]:
        selected = [r for r in per_strategy_bad if r["edge_over_breakeven"] < 0][:n]
        by_strategy = {}
        for row in selected:
            by_strategy.setdefault(row["strategy_id"], []).append(row["hour_utc"])
        if by_strategy:
            candidates.append({
                "name": f"skip_strategy_bad_hours_top{n}",
                "kind": "skip_strategy_hours",
                "skip_hours_by_strategy": by_strategy,
                "calibration_selected": selected,
            })

    global_bad = hour_edges(calibration, per_strategy=False)
    for n in [1, 2, 3, 4]:
        selected = [r for r in global_bad if r["edge_over_breakeven"] < 0][:n]
        if selected:
            candidates.append({
                "name": f"skip_global_bad_hours_top{n}",
                "kind": "skip_global_hours",
                "skip_hours_utc": [r["hour_utc"] for r in selected],
                "calibration_selected": selected,
            })

    candidates.extend([
        {
            "name": "same_candle_keep_30min_only",
            "kind": "same_candle_keep_one",
            "keep_strategy": "BTC_30min",
            "note": "Reduces doubled exposure when both strategies trigger on the same 5m candle.",
        },
        {
            "name": "same_candle_keep_10min_only",
            "kind": "same_candle_keep_one",
            "keep_strategy": "BTC_10min",
            "note": "Reduces doubled exposure when both strategies trigger on the same 5m candle.",
        },
        {
            "name": "skip_direction_conflicts",
            "kind": "skip_direction_conflicts",
            "note": "Only removes same-candle signals where the two strategies disagree on direction.",
        },
    ])
    return candidates


def evaluate_candidate(df, calibration_idx, validation_idx, candidate):
    keep = mask_for_candidate(df, candidate)
    full = df[keep].copy()
    calibration = df[calibration_idx & keep].copy()
    validation = df[validation_idx & keep].copy()
    base_validation_trades = int(validation_idx.sum())
    validation_metrics = summarize(validation)["metrics"]
    retention_pct = round(len(validation) / max(1, base_validation_trades) * 100, 2)
    return {
        **candidate,
        "full": summarize(full),
        "calibration": summarize(calibration),
        "validation": summarize(validation),
        "validation_trade_retention_pct": retention_pct,
        "validation_edge_over_breakeven": round(validation_metrics["wr"] - BREAKEVEN_WR, 2),
        "score": score_candidate(validation_metrics, retention_pct),
    }


def main():
    df = load_combined_trades()
    if df.empty:
        raise SystemExit("No trades found")

    split_time = df["time"].quantile(0.5)
    calibration_idx = df["time"] <= split_time
    validation_idx = df["time"] > split_time
    candidates = [
        evaluate_candidate(df, calibration_idx, validation_idx, cand)
        for cand in make_candidates(df[calibration_idx].copy())
    ]
    candidates.sort(key=lambda r: r["score"], reverse=True)
    baseline = next(r for r in candidates if r["name"] == "baseline_parallel")
    report = {
        "method": {
            "type": "causal_half_split_filter_search",
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "split_time": str(split_time),
            "note": "Weak hours are selected only on the first half, then judged on the second half. Treat winners as shadow candidates before production.",
        },
        "baseline": baseline,
        "ranked": candidates,
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
