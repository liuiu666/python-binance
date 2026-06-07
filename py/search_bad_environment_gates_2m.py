"""Search interpretable bad-environment gates for the 2m 10m BTC policy.

This is research-only. It tests whether weak market contexts, especially
low-volatility UP calls, can be filtered without turning the policy into an
after-the-fact fit.
"""
import itertools
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
import search_2m_primary_model_policies as search
from research_2m_10min_binary import BREAKEVEN_WR, HORIZON, PAYOUT, STAKE


OUT = "E:/codex/data"
REPORT_FILE = os.path.join(OUT, "bad_environment_gates_2m_report.json")
MIN_TRAIN_TRADES = 120


def max_loss(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def metric(wins, times):
    wins = np.asarray(wins, dtype=bool)
    total = int(len(wins))
    if total == 0:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "wr": 0.0,
            "edge_over_breakeven": round(-BREAKEVEN_WR, 2),
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
        "edge_over_breakeven": round(won / total * 100 - BREAKEVEN_WR, 2),
        "pnl_5u": round(float(pnl), 2),
        "max_loss": max_loss(wins.tolist()),
        "trades_per_day": round(total / days, 2),
    }


def edge_margin(prob, direction, groups, policy):
    if "regime_thresholds" in policy:
        th = np.array([policy["regime_thresholds"][g] for g in groups], dtype=float)
    else:
        up_th = float(policy.get("up_th", policy.get("threshold", 0.65)))
        down_th = float(policy.get("down_th", policy.get("threshold", 0.65)))
        th = np.where(direction == 1, up_th, down_th)
    return np.where(direction == 1, prob - th, (1 - prob) - th)


def gate_condition(gate, direction, groups, frame):
    n = len(direction)
    if not gate or gate.get("kind") == "none":
        return np.zeros(n, dtype=bool)

    atr = frame["atr_rank"].astype(float).to_numpy()
    bbw = frame["bbw_rank"].astype(float).to_numpy()
    trend = frame["trend_score"].astype(float).to_numpy()
    htf = frame["htf_score"].astype(float).to_numpy()
    taker = frame["taker_ratio"].astype(float).to_numpy()
    rsi = frame["rsi14"].astype(float).to_numpy()
    bbp = frame["bbp"].astype(float).to_numpy()
    recent_squeeze = frame["recent_squeeze"].astype(bool).to_numpy()
    expansion = frame["is_expansion"].astype(bool).to_numpy()

    lowvol = (atr <= gate.get("atr_max", 1.0)) & (bbw <= gate.get("bbw_max", 1.0))
    cond = lowvol.copy()

    dirs = gate.get("directions")
    if dirs:
        dir_mask = np.zeros(n, dtype=bool)
        if "UP" in dirs:
            dir_mask |= direction == 1
        if "DOWN" in dirs:
            dir_mask |= direction == 0
        cond &= dir_mask

    only_groups = gate.get("groups")
    if only_groups:
        cond &= np.isin(groups, np.asarray(only_groups, dtype=str))

    if gate.get("low_data_flow", False):
        cond &= np.isclose(taker, 1.0)

    if gate.get("strong_countertrend", False):
        cond &= ((direction == 1) & (trend <= -3) & (htf <= -2)) | (
            (direction == 0) & (trend >= 3) & (htf >= 2)
        )

    if gate.get("extreme_countertrend", False):
        cond &= (
            (direction == 1)
            & (trend <= -3)
            & (htf <= -2)
            & (bbp <= 0.25)
            & (rsi <= 35)
        ) | (
            (direction == 0)
            & (trend >= 3)
            & (htf >= 2)
            & (bbp >= 0.75)
            & (rsi >= 65)
        )

    if gate.get("squeeze_conflict", False):
        conflict = ((direction == 1) & (trend < 0)) | ((direction == 0) & (trend > 0))
        cond &= recent_squeeze & expansion & conflict

    return cond


def selected(pred, frame, policy, index_mask=None):
    prob = pred["prob"].astype(float)
    y = pred["y"].astype(int)
    groups = pred["regime_group"].astype(str)
    direction = search.direction_from_policy(prob, groups, policy)
    raw = direction >= 0
    if index_mask is not None:
        raw &= np.asarray(index_mask, dtype=bool)
    if policy.get("block_flow_opposes", False):
        raw &= ~search.flow_opposes(direction, frame)

    gate = policy.get("gate", {"kind": "none"})
    cond = gate_condition(gate, direction, groups, frame)
    if gate.get("kind") == "block":
        raw &= ~cond
    elif gate.get("kind") == "raise_margin":
        margin = edge_margin(prob, direction, groups, policy)
        raw &= ~(cond & (margin < float(gate["min_margin"])))

    live = search.non_overlap(raw, HORIZON)
    wins = direction == y
    times = pred["time"].astype(str)
    return live, wins[live], times[live]


def evaluate(pred, frame, policy, index_mask=None):
    live, wins, times = selected(pred, frame, policy, index_mask)
    return {
        "name": policy["name"],
        "policy": policy,
        "overall": metric(wins, times),
        "max_loss": max_loss(wins.tolist()),
        "live": live,
        "wins": wins.tolist(),
        "times": times.tolist(),
    }


def block_metrics(pred, row, blocks=10):
    live = row["live"]
    prob_wins = np.zeros(len(pred["time"]), dtype=bool)
    prob_wins[live] = np.asarray(row["wins"], dtype=bool)
    times = pred["time"].astype(str)
    block_size = len(times) // blocks
    out = []
    for bi in range(blocks):
        a = bi * block_size
        b = len(times) if bi == blocks - 1 else min(len(times), (bi + 1) * block_size)
        m = live[a:b]
        idx = np.where(m)[0] + a
        wins = prob_wins[idx]
        bt = times[idx]
        item = metric(wins, bt)
        item["slice"] = f"block_{bi + 1:02d}"
        item["start"] = str(times[a])
        item["end"] = str(times[b - 1])
        out.append(item)
    return out


def score(row, pred=None):
    m = row["overall"]
    if m["trades"] < MIN_TRAIN_TRADES:
        return -1e9
    min_block_wr = 0.0
    positive_blocks = 0
    if pred is not None:
        blocks = block_metrics(pred, row)
        active = [b for b in blocks if b["trades"]]
        min_block_wr = min([b["wr"] for b in active], default=0.0)
        positive_blocks = sum(1 for b in active if b["pnl_5u"] > 0)
    return round(
        m["pnl_5u"]
        + m["wr"] * 4
        + min(m["trades"], 2500) * 0.12
        + positive_blocks * 30
        - m["max_loss"] * 18
        + min_block_wr * 1.1,
        4,
    )


def base_policies():
    policies = []
    # Keep this tight: these are the stable neighborhood policies from the
    # primary search and overfit validation, not a fresh wide parameter sweep.
    seeds = [
        (0.65, 0.65, 0.62, 0.68, 0.65),
        (0.65, 0.65, 0.62, 0.62, 0.68),
        (0.65, 0.65, 0.62, 0.62, 0.65),
        (0.65, 0.65, 0.62, 0.65, 0.65),
        (0.65, 0.65, 0.62, 0.65, 0.68),
        (0.65, 0.65, 0.62, 0.68, 0.68),
        (0.68, 0.65, 0.62, 0.62, 0.68),
        (0.62, 0.65, 0.62, 0.65, 0.68),
    ]
    for up, down, trans, rang, unc in seeds:
        policies.append({
            "name": f"regth_u{int(up*100)}_d{int(down*100)}_t{int(trans*100)}_r{int(rang*100)}_x{int(unc*100)}",
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


def gate_grid():
    gates = [{"name": "none", "kind": "none"}]
    for atr, bbw in [(0.35, 0.35), (0.35, 0.45), (0.35, 0.55), (0.45, 0.45)]:
        base = {"atr_max": atr, "bbw_max": bbw}
        variants = [
            ("lowvol_up", {"directions": ["UP"]}),
            ("lowvol_up_lowdata", {"directions": ["UP"], "low_data_flow": True}),
            ("lowvol_transition_up", {"directions": ["UP"], "groups": ["transition"]}),
            ("lowvol_uncertain_up", {"directions": ["UP"], "groups": ["uncertain"]}),
        ]
        for label, extra in variants:
            gate = {**base, **extra}
            gates.append({"name": f"block_{label}_a{int(atr*100)}_b{int(bbw*100)}", "kind": "block", **gate})
            for margin in [0.03, 0.05]:
                gates.append({
                    "name": f"raise_{label}_a{int(atr*100)}_b{int(bbw*100)}_m{int(margin*100)}",
                    "kind": "raise_margin",
                    "min_margin": margin,
                    **gate,
                })
    return gates


def policy_grid():
    policies = []
    for base in base_policies():
        for gate in gate_grid():
            policy = dict(base)
            policy["gate"] = {k: v for k, v in gate.items() if k != "name"}
            policy["name"] = f"{base['name']}_flow_gate_{gate['name']}"
            policies.append(policy)
    return policies


def top_rows(rows, limit=25):
    return sorted(rows, key=lambda r: (r["score"], r["overall"]["pnl_5u"], r["overall"]["wr"]), reverse=True)[:limit]


def compact(row, include_policy=True):
    out = {
        "name": row["name"],
        "overall": row["overall"],
        "max_loss": row["max_loss"],
        "score": row.get("score"),
    }
    if include_policy:
        out["policy"] = row["policy"]
    if "blocks" in row:
        out["blocks"] = row["blocks"]
    return out


def make_mask(n, start_frac, end_frac):
    mask = np.zeros(n, dtype=bool)
    mask[int(round(n * start_frac)): int(round(n * end_frac))] = True
    return mask


def select_on_mask(pred, frame, policies, train_mask):
    rows = []
    for policy in policies:
        row = evaluate(pred, frame, policy, train_mask)
        row["score"] = score(row)
        rows.append(row)
    return top_rows(rows, 10)


def holdout_validate(pred, frame, policies, train_frac):
    n = len(pred["time"])
    train_mask = make_mask(n, 0.0, train_frac)
    test_mask = make_mask(n, train_frac, 1.0)
    top_train = select_on_mask(pred, frame, policies, train_mask)
    selected_row = top_train[0]
    test = evaluate(pred, frame, selected_row["policy"], test_mask)
    return {
        "split": f"{int(train_frac * 100)}_{int((1-train_frac)*100)}",
        "selected": compact(selected_row),
        "selected_test": compact({**test, "score": score(test)}, include_policy=False),
        "top_train": [compact(r, include_policy=False) for r in top_train[:5]],
    }


def walk_forward(pred, frame, policies, blocks=10, min_train_blocks=3):
    n = len(pred["time"])
    block_size = n // blocks
    folds = []
    all_wins = []
    all_times = []
    for test_block in range(min_train_blocks, blocks):
        train_mask = np.zeros(n, dtype=bool)
        test_mask = np.zeros(n, dtype=bool)
        train_mask[: test_block * block_size] = True
        a = test_block * block_size
        b = n if test_block == blocks - 1 else min(n, (test_block + 1) * block_size)
        test_mask[a:b] = True
        top_train = select_on_mask(pred, frame, policies, train_mask)
        selected_row = top_train[0]
        test = evaluate(pred, frame, selected_row["policy"], test_mask)
        all_wins.extend(test["wins"])
        all_times.extend(test["times"])
        folds.append({
            "fold": f"train_blocks_01_{test_block:02d}_test_block_{test_block+1:02d}",
            "selected_name": selected_row["name"],
            "train": selected_row["overall"],
            "test": test["overall"],
        })
    order = np.argsort(pd.to_datetime(all_times)) if all_times else []
    combined = metric(np.asarray(all_wins, dtype=bool)[order], np.asarray(all_times, dtype=str)[order]) if all_times else metric([], [])
    return {"folds": folds, "combined_test": combined}


def main():
    cache, pred, frame = search.load_data()
    policies = policy_grid()
    rows = []
    for policy in policies:
        row = evaluate(pred, frame, policy)
        row["score"] = score(row, pred=pred)
        row["blocks"] = block_metrics(pred, row)
        rows.append(row)

    top = top_rows(rows, 25)
    report = {
        "method": {
            "type": "bad_environment_gate_search_2m",
            "cache": cache,
            "policy_count": len(policies),
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Searches interpretable gates such as low-volatility UP filters. Services are not touched.",
        },
        "top_score": [compact(r) for r in top],
        "top_pnl": [compact(r) for r in sorted(rows, key=lambda r: (r["overall"]["pnl_5u"], r["overall"]["wr"]), reverse=True)[:25]],
        "holdout": [
            holdout_validate(pred, frame, policies, 0.60),
            holdout_validate(pred, frame, policies, 0.70),
        ],
        "walk_forward": walk_forward(pred, frame, policies),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "saved": REPORT_FILE,
        "policy_count": len(policies),
        "top_score": [
            {
                "name": r["name"],
                "wr": r["overall"]["wr"],
                "trades": r["overall"]["trades"],
                "pnl_5u": r["overall"]["pnl_5u"],
                "max_loss": r["max_loss"],
                "min_block_wr": min([b["wr"] for b in r["blocks"] if b["trades"]], default=0),
            }
            for r in top[:10]
        ],
        "holdout": [
            {
                "split": h["split"],
                "selected": h["selected"]["name"],
                "train": h["selected"]["overall"],
                "test": h["selected_test"]["overall"],
            }
            for h in report["holdout"]
        ],
        "walk_forward_combined": report["walk_forward"]["combined_test"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
