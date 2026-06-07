"""Validate whether the primary 2m policy search is overfit.

The base model predictions are already rolling OOS. This script adds a second
validation layer for the decision policy:
- choose policy parameters only on an earlier time slice;
- evaluate on later unseen slices;
- repeat as walk-forward block validation.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
import search_2m_primary_model_policies as search
from research_2m_10min_binary import PAYOUT, STAKE


OUT = "E:/codex/data"
REPORT_FILE = os.path.join(OUT, "primary_2m_policy_overfit_validation.json")
MIN_TRAIN_TRADES = 120
MIN_TEST_TRADES = 20


def max_loss_streak(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def metric_from_wins(wins, times):
    wins = np.asarray(wins, dtype=bool)
    total = int(len(wins))
    if total == 0:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "wr": 0.0,
            "edge_over_breakeven": round(-search.BREAKEVEN_WR, 2),
            "pnl_5u": 0.0,
            "max_loss": 0,
            "trades_per_day": 0.0,
        }
    won = int(wins.sum())
    pnl = won * STAKE * PAYOUT - (total - won) * STAKE
    start = pd.to_datetime(times[0])
    end = pd.to_datetime(times[-1])
    days = max((end - start).total_seconds() / 86400, 1 / 24)
    return {
        "trades": total,
        "wins": won,
        "losses": total - won,
        "wr": round(won / total * 100, 2),
        "edge_over_breakeven": round(won / total * 100 - search.BREAKEVEN_WR, 2),
        "pnl_5u": round(float(pnl), 2),
        "max_loss": max_loss_streak(wins.tolist()),
        "trades_per_day": round(total / days, 2),
    }


def selected_trades(pred, frame, policy, index_mask):
    prob = pred["prob"].astype(float)
    y = pred["y"].astype(int)
    groups = pred["regime_group"].astype(str)
    direction = search.direction_from_policy(prob, groups, policy)
    raw = (direction >= 0) & np.asarray(index_mask, dtype=bool)

    if policy.get("block_flow_opposes", False):
        raw &= ~search.flow_opposes(direction, frame)
    if policy.get("allowed_groups"):
        raw &= np.isin(groups, np.asarray(policy["allowed_groups"], dtype=str))
    if policy.get("blocked_groups"):
        raw &= ~np.isin(groups, np.asarray(policy["blocked_groups"], dtype=str))

    min_strength = policy.get("min_strength")
    if min_strength is not None:
        raw &= np.abs(prob - 0.5) * 200 >= float(min_strength)

    max_strength = policy.get("max_strength")
    if max_strength is not None:
        raw &= np.abs(prob - 0.5) * 200 <= float(max_strength)

    if policy.get("block_low_flow_default_countertrend", False):
        trend = frame["trend_score"].astype(float).to_numpy()
        htf = frame["htf_score"].astype(float).to_numpy()
        taker = frame["taker_ratio"].astype(float).to_numpy()
        low_flow = np.isclose(taker, 1.0)
        contra = ((direction == 1) & (trend <= -3) & (htf <= -2)) | (
            (direction == 0) & (trend >= 3) & (htf >= 2)
        )
        raw &= ~(low_flow & contra)

    if policy.get("block_squeeze_expansion_conflict", False):
        recent_squeeze = frame["recent_squeeze"].astype(bool).to_numpy()
        expansion = frame["is_expansion"].astype(bool).to_numpy()
        trend = frame["trend_score"].astype(float).to_numpy()
        conflict = ((direction == 1) & (trend < 0)) | ((direction == 0) & (trend > 0))
        raw &= ~(recent_squeeze & expansion & conflict)

    live = search.non_overlap(raw, search.HORIZON)
    wins = direction == y
    return live, wins[live], pred["time"].astype(str)[live]


def evaluate(pred, frame, policy, index_mask):
    live, wins, times = selected_trades(pred, frame, policy, index_mask)
    return {
        "policy": policy,
        "metric": metric_from_wins(wins, times) if len(times) else metric_from_wins([], []),
        "live_mask": live,
        "wins": wins.tolist(),
        "times": times.tolist(),
    }


def subblock_score(metric, wins, times, subblocks=5):
    trades = metric["trades"]
    if trades < MIN_TRAIN_TRADES:
        return -1e9
    wins = np.asarray(wins, dtype=bool)
    times = np.asarray(times, dtype=str)
    block_wrs = []
    positive_blocks = 0
    if len(wins):
        chunks = np.array_split(np.arange(len(wins)), min(subblocks, len(wins)))
        for chunk in chunks:
            m = metric_from_wins(wins[chunk], times[chunk])
            if m["trades"] >= 5:
                block_wrs.append(m["wr"])
                positive_blocks += int(m["pnl_5u"] > 0)
    min_block_wr = min(block_wrs) if block_wrs else 0.0
    return round(
        metric["pnl_5u"]
        + metric["wr"] * 4
        + min(metric["trades"], 2500) * 0.12
        + positive_blocks * 25
        - metric["max_loss"] * 15
        + min_block_wr * 0.8,
        4,
    )


def select_policy(pred, frame, policies, train_mask):
    rows = []
    for policy in policies:
        row = evaluate(pred, frame, policy, train_mask)
        score = subblock_score(row["metric"], row["wins"], row["times"])
        rows.append({"name": policy["name"], "policy": policy, "train": row["metric"], "score": score})
    rows.sort(key=lambda r: (r["score"], r["train"]["pnl_5u"], r["train"]["wr"]), reverse=True)
    return rows[0], rows[:10]


def combine_fold_results(folds):
    wins = []
    times = []
    for fold in folds:
        wins.extend(fold["test_wins"])
        times.extend(fold["test_times"])
    if not times:
        return metric_from_wins([], [])
    order = np.argsort(pd.to_datetime(times))
    wins = np.asarray(wins, dtype=bool)[order]
    times = np.asarray(times, dtype=str)[order]
    return metric_from_wins(wins, times)


def fixed_policy_reports(pred, frame, masks):
    fixed = {
        "global_th65_flow": {"name": "global_th65_flow", "threshold": 0.65, "block_flow_opposes": True},
        "full_oos_best": {
            "name": "regth_u65_d65_t62_r68_x65_flow",
            "regime_thresholds": {
                "uptrend": 0.65,
                "downtrend": 0.65,
                "transition": 0.62,
                "range": 0.68,
                "uncertain": 0.65,
            },
            "block_flow_opposes": True,
        },
    }
    out = {}
    for label, policy in fixed.items():
        out[label] = {name: evaluate(pred, frame, policy, mask)["metric"] for name, mask in masks.items()}
    return out


def make_time_mask(n, start_frac, end_frac):
    a = int(round(n * start_frac))
    b = int(round(n * end_frac))
    mask = np.zeros(n, dtype=bool)
    mask[a:b] = True
    return mask


def run_holdout(pred, frame, policies, train_frac):
    n = len(pred["time"])
    train_mask = make_time_mask(n, 0.0, train_frac)
    test_mask = make_time_mask(n, train_frac, 1.0)
    winner, top_train = select_policy(pred, frame, policies, train_mask)
    test = evaluate(pred, frame, winner["policy"], test_mask)
    masks = {"train": train_mask, "test": test_mask, "all": np.ones(n, dtype=bool)}
    return {
        "split": f"{int(train_frac * 100)}_{int((1 - train_frac) * 100)}",
        "selected": winner,
        "selected_test": test["metric"],
        "top_train": top_train,
        "fixed": fixed_policy_reports(pred, frame, masks),
    }


def run_walk_forward(pred, frame, policies, blocks=10, min_train_blocks=3):
    n = len(pred["time"])
    block_size = n // blocks
    folds = []
    for test_block in range(min_train_blocks, blocks):
        train_mask = np.zeros(n, dtype=bool)
        test_mask = np.zeros(n, dtype=bool)
        train_mask[: test_block * block_size] = True
        a = test_block * block_size
        b = n if test_block == blocks - 1 else (test_block + 1) * block_size
        test_mask[a:b] = True
        winner, top_train = select_policy(pred, frame, policies, train_mask)
        test = evaluate(pred, frame, winner["policy"], test_mask)
        folds.append(
            {
                "fold": f"train_blocks_01_{test_block:02d}_test_block_{test_block + 1:02d}",
                "selected_name": winner["name"],
                "selected_policy": winner["policy"],
                "train": winner["train"],
                "test": test["metric"],
                "test_wins": test["wins"],
                "test_times": test["times"],
                "top_train_names": [r["name"] for r in top_train[:5]],
            }
        )
    return {"folds": folds, "combined_test": combine_fold_results(folds)}


def main():
    cache, pred, frame = search.load_data()
    policies = search.policy_grid()
    n = len(pred["time"])
    times = pd.to_datetime(pred["time"])
    report = {
        "method": {
            "type": "primary_2m_policy_overfit_validation",
            "cache": cache,
            "policy_count": len(policies),
            "oos_start": str(times[0]),
            "oos_end": str(times[-1]),
            "oos_points": n,
            "breakeven_wr": round(search.BREAKEVEN_WR, 2),
            "note": "Model probabilities are rolling OOS. Policy parameters are selected on earlier slices and tested on later unseen slices.",
        },
        "holdout": [
            run_holdout(pred, frame, policies, 0.60),
            run_holdout(pred, frame, policies, 0.70),
        ],
        "walk_forward": run_walk_forward(pred, frame, policies, blocks=10, min_train_blocks=3),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report["method"], ensure_ascii=False, indent=2))
    print("HOLDOUT")
    for row in report["holdout"]:
        print(json.dumps({
            "split": row["split"],
            "selected": row["selected"]["name"],
            "train": row["selected"]["train"],
            "test": row["selected_test"],
        }, ensure_ascii=False))
    print("WALK_FORWARD_COMBINED")
    print(json.dumps(report["walk_forward"]["combined_test"], ensure_ascii=False, indent=2))
    print("REPORT", REPORT_FILE)


if __name__ == "__main__":
    main()
