"""Causal search for the dual 10m + 30m binary-options strategy.

The goal is not to find the prettiest full-sample backtest. Each candidate is
validated with rolling chronological folds: weak UTC hours are selected only
from earlier trades, then judged on later trades. The final production hint uses
only hours that repeatedly appear weak across those causal folds.
"""
import itertools
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import load_symbol
from strategy_robustness_profile import prediction_frame, trade_frequency
from validate_strategy_candidates import PAYOUT, STAKE, collect_predictions, metric

OUT = "E:/codex/data"
REPORT_FILE = os.path.join(OUT, "dual_strategy_causal_filter_search.json")
BREAKEVEN_WR = 100 / (1 + PAYOUT)

THRESHOLDS = {
    "BTC_10min": [0.55, 0.56, 0.58, 0.60, 0.62, 0.65, 0.70],
    "BTC_30min": [0.52, 0.55, 0.58, 0.60, 0.62, 0.65, 0.70],
}
RSI_FILTERS = [(25, 75), (30, 70), (35, 65), (40, 60)]
AGREE_MODES = ["all3", "majority"]
MAX_SKIP_HOURS = [0, 2, 4, 6]
STRATEGY_ORDER = {"BTC_30min": 0, "BTC_10min": 1}


def compact_metric(df):
    m = metric(df["win"].to_numpy() if not df.empty else [])
    freq = trade_frequency(df)
    return {
        **m,
        "edge_over_breakeven": round(float(m["wr"]) - BREAKEVEN_WR, 2),
        "trades_per_day": freq["trades_per_day"],
    }


def make_candidate(strategy_id, threshold, rsi, agree_mode, skip_hours=None):
    return {
        "name": f"{strategy_id}_th{int(threshold * 100)}_rsi{rsi[0]}_{rsi[1]}_{agree_mode}",
        "threshold": threshold,
        "rsi": rsi,
        "vol_min_rank": None,
        "agree_mode": agree_mode,
        "skip_hours_utc": sorted(skip_hours or []),
    }


def load_base_trade_frames(df5):
    frames = {}
    for strategy_id, horizon in [("BTC_10min", 2), ("BTC_30min", 6)]:
        preds = collect_predictions(df5, horizon, strategy_id)
        rows = []
        for threshold, rsi, agree_mode in itertools.product(
            THRESHOLDS[strategy_id], RSI_FILTERS, AGREE_MODES
        ):
            cand = make_candidate(strategy_id, threshold, rsi, agree_mode)
            trades = prediction_frame(preds, cand)
            if trades.empty:
                continue
            trades = trades.copy()
            trades["strategy_id"] = strategy_id
            trades["hour_utc"] = trades["time"].dt.hour
            trades["queue_order"] = STRATEGY_ORDER[strategy_id]
            trades["candidate_key"] = cand["name"]
            trades["candidate"] = json.dumps(cand, sort_keys=True)
            rows.append(trades)
        frames[strategy_id] = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return frames


def weak_hours(calibration, max_skip):
    if max_skip <= 0 or calibration.empty:
        return []
    rows = []
    for hour, part in calibration.groupby("hour_utc"):
        if len(part) < 20:
            continue
        m = compact_metric(part)
        if m["edge_over_breakeven"] < 0:
            rows.append({
                "hour_utc": int(hour),
                "trades": m["trades"],
                "wr": m["wr"],
                "edge_over_breakeven": m["edge_over_breakeven"],
            })
    rows.sort(key=lambda r: (r["edge_over_breakeven"], -r["trades"]))
    return rows[:max_skip]


def fold_indices(times, folds=5):
    unique_times = pd.Series(pd.to_datetime(times)).sort_values().reset_index(drop=True)
    cuts = np.array_split(unique_times, folds)
    bounds = []
    for part in cuts:
        if len(part) == 0:
            continue
        bounds.append((part[0], part[-1]))
    return bounds


def evaluate_strategy_frame(strategy_id, trades, max_skip):
    bounds = fold_indices(trades["time"], 5)
    fold_rows = []
    selected_counter = {}
    kept_parts = []
    base_parts = []
    for i, (start, end) in enumerate(bounds):
        validation = trades[(trades["time"] >= start) & (trades["time"] <= end)].copy()
        if i == 0:
            calibration = pd.DataFrame()
        else:
            calibration = trades[trades["time"] < start].copy()
        selected = weak_hours(calibration, max_skip)
        skip_hours = [r["hour_utc"] for r in selected]
        for hour in skip_hours:
            selected_counter[hour] = selected_counter.get(hour, 0) + 1
        filtered = validation[~validation["hour_utc"].isin(skip_hours)].copy()
        base_parts.append(validation)
        kept_parts.append(filtered)
        fold_rows.append({
            "fold": i + 1,
            "start": str(start),
            "end": str(end),
            "selected_skip_hours": selected,
            "baseline": compact_metric(validation),
            "filtered": compact_metric(filtered),
            "retention_pct": round(len(filtered) / max(1, len(validation)) * 100, 2),
        })
    baseline = pd.concat(base_parts, ignore_index=True) if base_parts else pd.DataFrame()
    filtered = pd.concat(kept_parts, ignore_index=True) if kept_parts else pd.DataFrame()
    return {
        "strategy_id": strategy_id,
        "max_skip_hours": max_skip,
        "rolling_baseline": compact_metric(baseline),
        "rolling_filtered": compact_metric(filtered),
        "folds": fold_rows,
        "selected_hour_counts": dict(sorted(selected_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        "stable_skip_hours": [
            int(hour) for hour, count in sorted(selected_counter.items())
            if count >= 2
        ],
    }


def score(result):
    m = result["rolling_filtered"]
    trades = int(m["trades"])
    wr = float(m["wr"])
    max_loss = int(m["max_loss"])
    per_day = float(m["trades_per_day"])
    folds = result["folds"][1:] or result["folds"]
    weak_folds = sum(1 for row in folds if float(row["filtered"]["wr"]) <= BREAKEVEN_WR)
    tiny_fold = sum(1 for row in folds if int(row["filtered"]["trades"]) < 50)
    return round(
        (wr - BREAKEVEN_WR) * 20
        + min(trades, 1000) * 0.04
        + min(per_day, 20) * 4
        - max_loss * 3
        - weak_folds * 30
        - tiny_fold * 12,
        2,
    )


def evaluate_all(frames):
    ranked = {}
    for strategy_id, frame in frames.items():
        rows = []
        for candidate_key, trades in frame.groupby("candidate_key", sort=False):
            cand = json.loads(trades["candidate"].iloc[0])
            trades = trades.sort_values("time").reset_index(drop=True)
            if len(trades) < 120:
                continue
            for max_skip in MAX_SKIP_HOURS:
                result = evaluate_strategy_frame(strategy_id, trades, max_skip)
                result["candidate"] = cand
                result["score"] = score(result)
                rows.append(result)
        rows.sort(key=lambda r: r["score"], reverse=True)
        ranked[strategy_id] = rows
    return ranked


def apply_stable_hours(frames, best):
    rows = []
    per_strategy = {}
    for strategy_id, result in best.items():
        cand = result["candidate"].copy()
        cand["skip_hours_utc"] = result["stable_skip_hours"]
        trades = frames[strategy_id][frames[strategy_id]["candidate_key"] == result["candidate"]["name"]].copy()
        trades = trades[~trades["hour_utc"].isin(cand["skip_hours_utc"])].copy()
        trades["strategy_id"] = strategy_id
        rows.append(trades)
        per_strategy[strategy_id] = {
            "candidate": cand,
            "metrics": compact_metric(trades),
            "frequency": trade_frequency(trades),
        }
    combined = pd.concat(rows, ignore_index=True).sort_values(["time", "queue_order"]) if rows else pd.DataFrame()
    return {
        "per_strategy": per_strategy,
        "combined": {
            "metrics": compact_metric(combined),
            "frequency": trade_frequency(combined),
        },
    }


def main():
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")
    frames = load_base_trade_frames(df5)
    ranked = evaluate_all(frames)
    best = {sid: rows[0] for sid, rows in ranked.items() if rows}
    production_hint = apply_stable_hours(frames, best)
    report = {
        "method": {
            "type": "rolling_causal_filter_search",
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "folds": 5,
            "note": "Each validation fold can skip only hours selected from earlier folds. Stable production hours require selection in at least two causal folds.",
        },
        "best_by_strategy": best,
        "top_by_strategy": {sid: rows[:12] for sid, rows in ranked.items()},
        "production_hint": production_hint,
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "best_by_strategy": {
            sid: {
                "candidate": row["candidate"],
                "max_skip_hours": row["max_skip_hours"],
                "rolling_filtered": row["rolling_filtered"],
                "stable_skip_hours": row["stable_skip_hours"],
                "score": row["score"],
            }
            for sid, row in best.items()
        },
        "production_hint": production_hint,
    }, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
