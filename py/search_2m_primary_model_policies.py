"""Search primary 2m model policies without tick/order-book data.

This does not retrain the model. It scans decision policies on the cached 2m
generic model probabilities:
- global thresholds;
- asymmetric up/down thresholds;
- regime-specific thresholds;
- flow-opposes gate;
- margin/near-threshold filters;
- block/stability summaries.
"""
import glob
import itertools
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from research_2m_10min_binary import OUT, HORIZON, BREAKEVEN_WR, metric
from research_regime_models_2m import prepare_frame

REPORT_FILE = os.path.join(OUT, "primary_2m_policy_search_report.json")


def latest_cache():
    paths = sorted(
        glob.glob(os.path.join(OUT, "cache", "regime_models_2m_10m_tr12000_te1500_st1500_*.npz")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not paths:
        raise FileNotFoundError("No 2m model cache found. Run py/research_regime_models_2m.py first.")
    return paths[0]


def load_data():
    path = latest_cache()
    data = np.load(path, allow_pickle=True)
    pred = {
        "time": data["time"].astype(str),
        "y": data["y"].astype(int),
        "prob": data["generic_prob"].astype(float),
        "regime": data["regime"].astype(str),
        "regime_group": data["regime_group"].astype(str),
    }
    _, _, df = prepare_frame()
    df = df.copy()
    df["time_str"] = df["time"].astype(str)
    aligned = df.set_index("time_str").loc[pred["time"]].reset_index(drop=True)
    return path, pred, aligned


def max_loss(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def non_overlap(mask, cooldown=HORIZON):
    out = np.zeros(len(mask), dtype=bool)
    next_allowed = 0
    for i, ok in enumerate(mask):
        if ok and i >= next_allowed:
            out[i] = True
            next_allowed = i + cooldown
    return out


def flow_opposes(direction, frame):
    taker = frame["taker_ratio"].astype(float).to_numpy()
    return ((direction == 1) & (taker < 0.85)) | ((direction == 0) & (taker > 1.15))


def direction_from_policy(prob, regimes, policy):
    direction = np.full(len(prob), -1, dtype=int)
    if "regime_thresholds" in policy:
        for reg, th in policy["regime_thresholds"].items():
            mask = regimes == reg
            direction[mask & (prob >= th)] = 1
            direction[mask & (prob <= 1 - th)] = 0
    else:
        up_th = float(policy.get("up_th", policy.get("threshold", 0.65)))
        down_th = float(policy.get("down_th", policy.get("threshold", 0.65)))
        direction[prob >= up_th] = 1
        direction[prob <= 1 - down_th] = 0
    return direction


def apply_policy(pred, frame, policy):
    prob = pred["prob"].astype(float)
    y = pred["y"].astype(int)
    regimes = pred["regime"].astype(str)
    groups = pred["regime_group"].astype(str)
    direction = direction_from_policy(prob, groups, policy)
    raw = direction >= 0

    if policy.get("block_flow_opposes", False):
        raw &= ~flow_opposes(direction, frame)

    if policy.get("allowed_groups"):
        raw &= np.isin(groups, np.asarray(policy["allowed_groups"], dtype=str))

    if policy.get("blocked_groups"):
        raw &= ~np.isin(groups, np.asarray(policy["blocked_groups"], dtype=str))

    min_strength = policy.get("min_strength")
    if min_strength is not None:
        strength = np.abs(prob - 0.5) * 200
        raw &= strength >= float(min_strength)

    max_strength = policy.get("max_strength")
    if max_strength is not None:
        strength = np.abs(prob - 0.5) * 200
        raw &= strength <= float(max_strength)

    if policy.get("block_low_flow_default_countertrend", False):
        trend = frame["trend_score"].astype(float).to_numpy()
        htf = frame["htf_score"].astype(float).to_numpy()
        taker = frame["taker_ratio"].astype(float).to_numpy()
        low_flow = np.isclose(taker, 1.0)
        contra = ((direction == 1) & (trend <= -3) & (htf <= -2)) | ((direction == 0) & (trend >= 3) & (htf >= 2))
        raw &= ~(low_flow & contra)

    if policy.get("block_squeeze_expansion_conflict", False):
        recent_squeeze = frame["recent_squeeze"].astype(bool).to_numpy()
        expansion = frame["is_expansion"].astype(bool).to_numpy()
        trend = frame["trend_score"].astype(float).to_numpy()
        conflict = ((direction == 1) & (trend < 0)) | ((direction == 0) & (trend > 0))
        raw &= ~(recent_squeeze & expansion & conflict)

    live = non_overlap(raw, HORIZON)
    wins = direction == y
    times = pred["time"].astype(str)
    st = times[live]
    overall = metric(wins[live], st[0] if len(st) else times[0], st[-1] if len(st) else times[-1])
    raw_st = times[raw]
    raw_metric = metric(wins[raw], raw_st[0] if len(raw_st) else times[0], raw_st[-1] if len(raw_st) else times[-1])

    by_group = {}
    for group in sorted(set(groups)):
        m = live & (groups == group)
        gst = times[m]
        by_group[group] = metric(wins[m], gst[0] if len(gst) else times[groups == group][0], gst[-1] if len(gst) else times[groups == group][-1])

    blocks = []
    idx = np.arange(len(wins))
    block_size = max(1, len(wins) // 10)
    for bi in range(10):
        a = bi * block_size
        b = len(wins) if bi == 9 else min(len(wins), (bi + 1) * block_size)
        m = live[a:b]
        bst = times[a:b][m]
        row = metric(wins[a:b][m], bst[0] if len(bst) else times[a], bst[-1] if len(bst) else times[b - 1])
        row["slice"] = f"block_{bi + 1:02d}"
        blocks.append(row)

    active_blocks = [b for b in blocks if b["trades"] > 0]
    score = (
        overall["pnl_5u"]
        + overall["wr"] * 4
        + min(overall["trades"], 2500) * 0.12
        + sum(1 for b in active_blocks if b["pnl_5u"] > 0) * 35
        - max_loss(wins[live]) * 15
        + min((b["wr"] for b in active_blocks), default=0) * 1.2
    )
    return {
        "name": policy["name"],
        "policy": policy,
        "overall": overall,
        "raw_overlap": raw_metric,
        "max_loss": max_loss(wins[live]),
        "by_group": by_group,
        "block_summary": {
            "active_blocks": len(active_blocks),
            "positive_blocks": sum(1 for b in active_blocks if b["pnl_5u"] > 0),
            "min_block_wr": round(float(min((b["wr"] for b in active_blocks), default=0)), 2),
            "worst_block": min(active_blocks, key=lambda b: b["wr"])["slice"] if active_blocks else None,
        },
        "blocks": blocks,
        "score": round(float(score), 2),
    }


def policy_grid():
    policies = []
    for th in [0.58, 0.60, 0.62, 0.64, 0.65, 0.66, 0.68, 0.70, 0.72]:
        for flow in [False, True]:
            policies.append({"name": f"global_th{int(th*100)}_{'flow' if flow else 'noflow'}", "threshold": th, "block_flow_opposes": flow})
            policies.append({"name": f"global_th{int(th*100)}_{'flow' if flow else 'noflow'}_blocklowdata", "threshold": th, "block_flow_opposes": flow, "block_low_flow_default_countertrend": True})
            policies.append({"name": f"global_th{int(th*100)}_{'flow' if flow else 'noflow'}_blocksqueeze", "threshold": th, "block_flow_opposes": flow, "block_squeeze_expansion_conflict": True})
    for up_th, down_th in itertools.product([0.62, 0.65, 0.68, 0.70], repeat=2):
        policies.append({"name": f"asym_up{int(up_th*100)}_dn{int(down_th*100)}_flow", "up_th": up_th, "down_th": down_th, "block_flow_opposes": True})
    group_sets = [
        ["uptrend", "downtrend"],
        ["transition"],
        ["range", "uncertain"],
        ["uptrend"],
        ["downtrend"],
        ["range"],
        ["uncertain"],
    ]
    for th in [0.60, 0.62, 0.65, 0.68]:
        for groups in group_sets:
            policies.append({
                "name": f"global_th{int(th*100)}_flow_only_{'_'.join(groups)}",
                "threshold": th,
                "block_flow_opposes": True,
                "allowed_groups": groups,
            })
    # Regime-group threshold combinations around the best global policy.
    combo_vals = [0.62, 0.65, 0.68]
    for up, down, trans, rang, unc in itertools.product(combo_vals, repeat=5):
        # Keep the grid compact by skipping very low all-around duplicates.
        if sum(v >= 0.65 for v in [up, down, trans, rang, unc]) < 3:
            continue
        policies.append({
            "name": f"regth_u{int(up*100)}_d{int(down*100)}_t{int(trans*100)}_r{int(rang*100)}_x{int(unc*100)}_flow",
            "regime_thresholds": {
                "uptrend": up,
                "downtrend": down,
                "transition": trans,
                "range": rang,
                "uncertain": unc,
            },
            "block_flow_opposes": True,
        })
    return policies


def top(rows, min_trades=80, limit=20, key=None):
    use = [r for r in rows if r["overall"]["trades"] >= min_trades]
    return sorted(use, key=key or (lambda r: (r["score"], r["overall"]["pnl_5u"])), reverse=True)[:limit]


def main():
    cache, pred, frame = load_data()
    rows = [apply_policy(pred, frame, p) for p in policy_grid()]
    report = {
        "method": {
            "type": "primary_2m_policy_search_no_retrain",
            "cache": cache,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Scans decision policies on the cached generic 2m model. No tick/order-book data and no retraining.",
        },
        "results": {
            "top_score": top(rows, min_trades=80, limit=25),
            "top_pnl": top(rows, min_trades=80, limit=25, key=lambda r: (r["overall"]["pnl_5u"], r["overall"]["wr"])),
            "top_wr": top(rows, min_trades=80, limit=25, key=lambda r: (r["overall"]["wr"], r["overall"]["trades"])),
            "top_trade_count_profitable": top([r for r in rows if r["overall"]["pnl_5u"] > 0], min_trades=80, limit=25, key=lambda r: (r["overall"]["trades"], r["overall"]["wr"])),
            "top_low_max_loss": top(rows, min_trades=300, limit=25, key=lambda r: (-r["max_loss"], r["overall"]["pnl_5u"], r["overall"]["wr"])),
        },
        "policy_count": len(rows),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "saved": REPORT_FILE,
        "policy_count": len(rows),
        "top_score": [
            {
                "name": r["name"],
                "wr": r["overall"]["wr"],
                "trades": r["overall"]["trades"],
                "trades_per_day": r["overall"]["trades_per_day"],
                "pnl_5u": r["overall"]["pnl_5u"],
                "max_loss": r["max_loss"],
                "positive_blocks": r["block_summary"]["positive_blocks"],
                "min_block_wr": r["block_summary"]["min_block_wr"],
            }
            for r in report["results"]["top_score"][:12]
        ],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
