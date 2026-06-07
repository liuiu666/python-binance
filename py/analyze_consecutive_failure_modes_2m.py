"""Analyze consecutive wrong calls for the 2m generic 10m binary model.

The goal is not a blunt "stop after losses" rule. This report identifies
repeatable failure modes, then compares concrete decisions:
- block a risk pattern;
- raise the probability threshold;
- wait for 2m/4m confirmation before entering;
- wait only when a signal has high-risk failure features.
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from research_2m_10min_binary import OUT, HORIZON, BREAKEVEN_WR, metric
from research_regime_models_2m import prepare_frame

REPORT_FILE = os.path.join(OUT, "consecutive_failure_modes_2m_report.json")
THRESHOLD = 0.65


def latest_cache():
    paths = sorted(
        glob.glob(os.path.join(OUT, "cache", "regime_models_2m_10m_tr12000_te1500_st1500_*.npz")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not paths:
        raise FileNotFoundError("No regime model cache found. Run py/research_regime_models_2m.py first.")
    return paths[0]


def load_predictions():
    path = latest_cache()
    data = np.load(path, allow_pickle=True)
    pred = {k: data[k] for k in data.files}
    pred["time"] = pred["time"].astype(str)
    pred["regime"] = pred["regime"].astype(str)
    pred["regime_group"] = pred["regime_group"].astype(str)
    return path, pred


def prediction_direction(prob, threshold):
    return np.where(prob >= threshold, 1, np.where(prob <= 1 - threshold, 0, -1))


def signal_rows(pred, df, threshold=THRESHOLD):
    df = df.copy()
    df["time_str"] = df["time"].astype(str)
    aligned = df.set_index("time_str").loc[pred["time"]].reset_index(drop=True)
    prob = pred["generic_prob"].astype(float)
    direction = prediction_direction(prob, threshold)
    raw = np.where(direction >= 0)[0]
    rows = []
    next_allowed = 0
    for j in raw:
        if j < next_allowed:
            continue
        if j + HORIZON >= len(aligned):
            continue
        target = int(pred["y"][j])
        win = bool(direction[j] == target)
        row = aligned.iloc[j].copy()
        rows.append({
            "pred_idx": int(j),
            "entry_idx": int(j),
            "time": str(pred["time"][j]),
            "prob": float(prob[j]),
            "strength": round(float(abs(prob[j] - 0.5) * 200), 2),
            "direction": "UP" if direction[j] == 1 else "DOWN",
            "direction_num": int(direction[j]),
            "target": "UP" if target == 1 else "DOWN",
            "target_num": target,
            "win": win,
            "regime": str(pred["regime"][j]),
            "regime_group": str(pred["regime_group"][j]),
            "future_ret_10m_bps": float(row["future_ret_10m"] * 10000),
            "rsi14": float(row["rsi14"]),
            "rsi35": float(row["rsi35"]),
            "bbp": float(row["bbp"]),
            "bbw_rank": float(row["bbw_rank"]),
            "atr_rank": float(row["atr_rank"]),
            "vr_rank": float(row["vr_rank"]),
            "atr_exp": float(row["atr_exp"]),
            "trend_score": float(row["trend_score"]),
            "htf_score": float(row["htf_score"]),
            "taker_ratio": float(row["taker_ratio"]),
            "ls_ratio": float(row["ls_ratio"]),
            "recent_squeeze": bool(row["recent_squeeze"]),
            "is_expansion": bool(row["is_expansion"]),
            "close": float(row["close"]),
        })
        next_allowed = j + HORIZON
    return pd.DataFrame(rows), aligned, prob


def add_patterns(signals):
    s = signals.copy()
    up = s["direction"] == "UP"
    down = s["direction"] == "DOWN"
    s["strong_countertrend"] = (
        (up & (s["trend_score"] <= -3) & (s["htf_score"] <= -2))
        | (down & (s["trend_score"] >= 3) & (s["htf_score"] >= 2))
    )
    s["extreme_countertrend"] = (
        (up & (s["trend_score"] <= -3) & (s["htf_score"] <= -2) & (s["bbp"] <= 0.25) & (s["rsi14"] <= 35))
        | (down & (s["trend_score"] >= 3) & (s["htf_score"] >= 2) & (s["bbp"] >= 0.75) & (s["rsi14"] >= 65))
    )
    s["transition_conflict"] = (
        ((s["regime"] == "transition_up") & down)
        | ((s["regime"] == "transition_down") & up)
    )
    s["flow_opposes"] = (
        (up & (s["taker_ratio"] < 0.85))
        | (down & (s["taker_ratio"] > 1.15))
    )
    s["squeeze_expansion"] = s["recent_squeeze"] & s["is_expansion"]
    s["near_threshold"] = s["strength"] <= 34
    s["low_data_flow"] = np.isclose(s["taker_ratio"], 1.0)
    s["high_risk"] = (
        s["strong_countertrend"]
        | s["transition_conflict"]
        | (s["squeeze_expansion"] & s["flow_opposes"])
        | (s["low_data_flow"] & s["strong_countertrend"])
        | (s["near_threshold"] & s["strong_countertrend"])
    )
    return s


def loss_streaks(signals):
    streaks = []
    cur = []
    for _, row in signals.iterrows():
        if row["win"]:
            if cur:
                streaks.append(cur)
                cur = []
        else:
            cur.append(row.to_dict())
    if cur:
        streaks.append(cur)
    streaks.sort(key=len, reverse=True)
    return streaks


def summarize_streak(streak):
    df = pd.DataFrame(streak)
    return {
        "length": int(len(df)),
        "start": str(df["time"].iloc[0]),
        "end": str(df["time"].iloc[-1]),
        "directions": df["direction"].value_counts().to_dict(),
        "regimes": df["regime"].value_counts().to_dict(),
        "avg_strength": round(float(df["strength"].mean()), 2),
        "strong_countertrend_rate": round(float(df["strong_countertrend"].mean() * 100), 2),
        "transition_conflict_rate": round(float(df["transition_conflict"].mean() * 100), 2),
        "flow_opposes_rate": round(float(df["flow_opposes"].mean() * 100), 2),
        "squeeze_expansion_rate": round(float(df["squeeze_expansion"].mean() * 100), 2),
        "rows": [
            {
                "time": r["time"],
                "regime": r["regime"],
                "prob": round(float(r["prob"]), 4),
                "strength": round(float(r["strength"]), 1),
                "direction": r["direction"],
                "actual": r["target"],
                "future_ret_10m_bps": round(float(r["future_ret_10m_bps"]), 2),
                "rsi14": round(float(r["rsi14"]), 2),
                "rsi35": round(float(r["rsi35"]), 2),
                "bbp": round(float(r["bbp"]), 3),
                "trend_score": round(float(r["trend_score"]), 1),
                "htf_score": round(float(r["htf_score"]), 1),
                "taker_ratio": round(float(r["taker_ratio"]), 4),
                "patterns": [
                    name for name in [
                        "strong_countertrend",
                        "extreme_countertrend",
                        "transition_conflict",
                        "flow_opposes",
                        "squeeze_expansion",
                        "near_threshold",
                        "low_data_flow",
                    ] if bool(r.get(name))
                ],
            }
            for r in streak
        ],
    }


def pattern_lift(signals, cluster_loss_mask):
    rows = []
    win = signals["win"].astype(bool)
    for col in [
        "strong_countertrend",
        "extreme_countertrend",
        "transition_conflict",
        "flow_opposes",
        "squeeze_expansion",
        "near_threshold",
        "low_data_flow",
        "high_risk",
    ]:
        p = signals[col].astype(bool)
        cluster_rate = float(p[cluster_loss_mask].mean() * 100) if cluster_loss_mask.any() else 0.0
        loss_rate = float(p[~win].mean() * 100) if (~win).any() else 0.0
        win_rate = float(p[win].mean() * 100) if win.any() else 0.0
        rows.append({
            "pattern": col,
            "cluster_loss_rate": round(cluster_rate, 2),
            "all_loss_rate": round(loss_rate, 2),
            "win_rate": round(win_rate, 2),
            "lift_vs_wins": round(cluster_rate / win_rate, 3) if win_rate else None,
            "pattern_count": int(p.sum()),
        })
    rows.sort(key=lambda r: (r["lift_vs_wins"] or 0, r["cluster_loss_rate"]), reverse=True)
    return rows


def max_loss(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def evaluate_decision(name, pred, aligned, threshold=THRESHOLD, decision=None, confirm_bars=0, high_risk_only=False):
    prob = pred["generic_prob"].astype(float)
    direction = prediction_direction(prob, threshold)
    candidates = np.where(direction >= 0)[0]
    entries = []
    next_allowed = 0

    for j in candidates:
        if j < next_allowed or j + HORIZON >= len(aligned):
            continue
        risk = decision(j, direction[j], prob[j], aligned) if decision else False
        entry = j
        if confirm_bars and ((not high_risk_only) or risk):
            k = j + confirm_bars
            if k + HORIZON >= len(aligned):
                continue
            p0 = float(aligned.iloc[j]["close"])
            pk = float(aligned.iloc[k]["close"])
            confirmed = (direction[j] == 1 and pk > p0) or (direction[j] == 0 and pk < p0)
            if not confirmed:
                continue
            entry = k
        elif risk and decision is not None:
            continue
        if entry < next_allowed:
            continue
        target = 1 if float(aligned.iloc[entry + HORIZON]["close"]) > float(aligned.iloc[entry]["close"]) else 0
        entries.append({
            "entry_idx": int(entry),
            "pred_idx": int(j),
            "time": str(aligned.iloc[entry]["time"]),
            "direction": int(direction[j]),
            "target": int(target),
            "win": bool(direction[j] == target),
            "risk": bool(risk),
        })
        next_allowed = entry + HORIZON

    if not entries:
        return {"name": name, "overall": metric([], None, None), "max_loss": 0}
    wins = np.asarray([e["win"] for e in entries], dtype=bool)
    times = np.asarray([e["time"] for e in entries], dtype=str)
    out = metric(wins, times[0], times[-1])
    return {
        "name": name,
        "threshold": threshold,
        "confirm_bars": confirm_bars,
        "confirm_minutes": confirm_bars * 2,
        "high_risk_only": high_risk_only,
        "overall": out,
        "max_loss": max_loss(wins),
        "risk_entries": int(sum(e["risk"] for e in entries)),
    }


def decision_functions():
    def strong_countertrend(j, d, p, df):
        r = df.iloc[j]
        return (
            (d == 1 and r["trend_score"] <= -3 and r["htf_score"] <= -2)
            or (d == 0 and r["trend_score"] >= 3 and r["htf_score"] >= 2)
        )

    def extreme_countertrend(j, d, p, df):
        r = df.iloc[j]
        return (
            (d == 1 and r["trend_score"] <= -3 and r["htf_score"] <= -2 and r["bbp"] <= 0.25 and r["rsi14"] <= 35)
            or (d == 0 and r["trend_score"] >= 3 and r["htf_score"] >= 2 and r["bbp"] >= 0.75 and r["rsi14"] >= 65)
        )

    def transition_conflict(j, d, p, df):
        r = df.iloc[j]
        return (str(r["regime"]) == "transition_up" and d == 0) or (str(r["regime"]) == "transition_down" and d == 1)

    def flow_opposes(j, d, p, df):
        r = df.iloc[j]
        return (d == 1 and r["taker_ratio"] < 0.85) or (d == 0 and r["taker_ratio"] > 1.15)

    def lowdata_strong_countertrend(j, d, p, df):
        r = df.iloc[j]
        low_flow = np.isclose(float(r["taker_ratio"]), 1.0)
        return low_flow and strong_countertrend(j, d, p, df)

    def flow_or_lowdata_countertrend(j, d, p, df):
        return flow_opposes(j, d, p, df) or lowdata_strong_countertrend(j, d, p, df)

    def high_risk(j, d, p, df):
        r = df.iloc[j]
        squeeze_expansion = bool(r["recent_squeeze"]) and bool(r["is_expansion"])
        return (
            strong_countertrend(j, d, p, df)
            or transition_conflict(j, d, p, df)
            or lowdata_strong_countertrend(j, d, p, df)
            or (squeeze_expansion and flow_opposes(j, d, p, df))
        )

    return {
        "block_strong_countertrend": strong_countertrend,
        "block_extreme_countertrend": extreme_countertrend,
        "block_transition_conflict": transition_conflict,
        "block_flow_opposes": flow_opposes,
        "block_lowdata_strong_countertrend": lowdata_strong_countertrend,
        "block_flow_or_lowdata_countertrend": flow_or_lowdata_countertrend,
        "block_high_risk_combo": high_risk,
        "high_risk_combo": high_risk,
    }


def run_decisions(pred, aligned):
    funcs = decision_functions()
    rows = [
        evaluate_decision("base_th65", pred, aligned, 0.65),
        evaluate_decision("raise_threshold_66", pred, aligned, 0.66),
        evaluate_decision("raise_threshold_68", pred, aligned, 0.68),
        evaluate_decision("raise_threshold_70", pred, aligned, 0.70),
    ]
    for name, fn in funcs.items():
        if name == "high_risk_combo":
            continue
        rows.append(evaluate_decision(name, pred, aligned, 0.65, decision=fn))
    for bars in [1, 2]:
        rows.append(evaluate_decision(f"wait_confirm_{bars*2}m_all", pred, aligned, 0.65, confirm_bars=bars))
        rows.append(evaluate_decision(
            f"wait_confirm_{bars*2}m_high_risk_only",
            pred,
            aligned,
            0.65,
            decision=funcs["high_risk_combo"],
            confirm_bars=bars,
            high_risk_only=True,
        ))
    rows.sort(key=lambda r: (
        r["overall"]["pnl_5u"],
        r["overall"]["wr"],
        -r["max_loss"],
    ), reverse=True)
    return rows


def main():
    cache, pred = load_predictions()
    _, _, df = prepare_frame()
    signals, aligned, _ = signal_rows(pred, df)
    signals = add_patterns(signals)
    streaks = loss_streaks(signals)
    cluster_loss_mask = np.zeros(len(signals), dtype=bool)
    for streak in streaks:
        if len(streak) >= 3:
            times = {r["time"] for r in streak}
            cluster_loss_mask |= signals["time"].isin(times).to_numpy()

    report = {
        "method": {
            "type": "consecutive_failure_mode_analysis_2m_generic_10m",
            "cache": cache,
            "threshold": THRESHOLD,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Primary signal stream is generic_th65_all with non-overlap 10m entries.",
        },
        "base": {
            "signals": int(len(signals)),
            "wins": int(signals["win"].sum()),
            "losses": int((~signals["win"]).sum()),
            "wr": round(float(signals["win"].mean() * 100), 2),
            "max_loss": max_loss(signals["win"].to_numpy()),
            "loss_streak_ge3_count": int(sum(1 for s in streaks if len(s) >= 3)),
            "loss_streak_ge3_losses": int(cluster_loss_mask.sum()),
        },
        "top_loss_streaks": [summarize_streak(s) for s in streaks[:8]],
        "pattern_lift": pattern_lift(signals, cluster_loss_mask),
        "decision_tests": run_decisions(pred, aligned),
        "decision": {
            "recommended_for_shadow": "block_flow_or_lowdata_countertrend",
            "why": [
                "It targets identifiable failure features before entry instead of reacting after losses.",
                "The two clearest failure inputs are active flow opposing the signal, and strong countertrend signals when flow data is missing/defaulted.",
                "Use it in shadow first; do not assume it is production-ready until validated on newer live data and true live taker/order-book flow.",
            ],
            "do_not_use_as_primary": [
                "Hard blocking all strong-countertrend signals: it removes many valid reversals and reduced PnL in this sample.",
                "Simple cooldown after a loss: it changes behavior after damage is done and can still miss the next failure cluster.",
                "Waiting 2m/4m confirmation globally: it reduced or destroyed the edge in this sample.",
            ],
        },
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "saved": REPORT_FILE,
        "base": report["base"],
        "top_patterns": report["pattern_lift"][:6],
        "top_decisions": [
            {
                "name": r["name"],
                "wr": r["overall"]["wr"],
                "trades": r["overall"]["trades"],
                "pnl_5u": r["overall"]["pnl_5u"],
                "max_loss": r["max_loss"],
                "confirm_minutes": r.get("confirm_minutes", 0),
                "risk_entries": r.get("risk_entries", 0),
            }
            for r in report["decision_tests"][:8]
        ],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
