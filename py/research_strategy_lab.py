"""Unified research lab for BTC 10m/30m option strategies.

This is an offline-only script. It compares rule-only, ML, and regime hybrid
signals on the same strict walk-forward out-of-sample rows. Do not promote a
candidate to live trading from this report alone; use it to choose shadow tests.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import build_features, load_symbol
from validate_strategy_candidates import PAYOUT, STAKE, collect_predictions

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
TRADE_CONFIG_FILE = os.path.join(OUT, "trade_config.json")
REPORT_FILE = os.path.join(OUT, "strategy_research_lab_report.json")
BREAKEVEN_WR = 100 / (1 + PAYOUT)
HORIZONS = {"BTC_10min": 2, "BTC_30min": 6}
TREND_EPS = 0.00005


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def max_loss_streak(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return int(best)


def metric(wins):
    wins = np.asarray(wins, dtype=bool)
    trades = int(len(wins))
    won = int(wins.sum()) if trades else 0
    lost = trades - won
    wr = won / trades * 100 if trades else 0.0
    return {
        "trades": trades,
        "wins": won,
        "losses": lost,
        "wr": round(float(wr), 2),
        "edge_over_breakeven": round(float(wr - BREAKEVEN_WR), 2),
        "pnl_5u": round(float(won * STAKE * PAYOUT - lost * STAKE), 2),
        "max_loss": max_loss_streak(wins.tolist()),
    }


def trend_score_frame(fdf):
    score = np.zeros(len(fdf), dtype=int)
    for col in ["trend6", "trend12", "trend30", "pre50"]:
        vals = fdf[col].astype(float).to_numpy()
        score += (vals > TREND_EPS).astype(int)
        score -= (vals < -TREND_EPS).astype(int)
    stack = fdf["ema_stack"].astype(float).to_numpy()
    score += (stack > 0).astype(int)
    score -= (stack < 0).astype(int)
    return score


def trend_label(score):
    if score >= 3:
        return "strong_uptrend"
    if score <= -3:
        return "strong_downtrend"
    if score > 0:
        return "mild_uptrend"
    if score < 0:
        return "mild_downtrend"
    return "neutral"


def rsi_zone(rsi):
    if rsi < 30:
        return "rsi_lt30"
    if rsi < 45:
        return "rsi_30_45"
    if rsi <= 55:
        return "rsi_45_55"
    if rsi <= 70:
        return "rsi_55_70"
    return "rsi_gt70"


def build_oos_frame(df5, strategy_id, horizon):
    preds = collect_predictions(df5, horizon, strategy_id, use_cache=True)
    fdf = build_features(df5, horizon)
    fdf = fdf[fdf["target"] != 0].reset_index(drop=True)
    keep = [
        "time", "rsi14", "bbp", "bbw", "atrp", "atr_exp", "pre20", "pre50",
        "roc5", "roc10", "mom_6", "mom_12", "hlp20", "hlp50", "trend6",
        "trend12", "trend30", "ema_stack", "vr", "taker_ratio", "ls_ratio",
    ]
    available = [c for c in keep if c in fdf.columns]
    feat = fdf[available].copy()
    feat["time_key"] = pd.to_datetime(feat["time"], utc=True)

    pred = pd.DataFrame({
        "time": pd.to_datetime(preds["time"], utc=True),
        "target": preds["y"].astype(int),
        "avg": preds["avg"].astype(float),
        "vote_sum": preds["vote_sum"].astype(int),
        "agree_all": preds["agree_all"].astype(bool),
    })
    frame = pred.merge(feat.drop(columns=["time"]), left_on="time", right_on="time_key", how="left")
    frame = frame.drop(columns=["time_key"])
    frame = frame.sort_values("time").reset_index(drop=True)
    frame["trend_score"] = trend_score_frame(frame)
    frame["trend_label"] = frame["trend_score"].map(trend_label)
    frame["rsi_zone"] = frame["rsi14"].astype(float).map(rsi_zone)
    frame["hour_utc"] = frame["time"].dt.hour
    frame["strength"] = np.round(np.abs(frame["avg"].astype(float) - 0.5) * 200, 1)
    frame["ml_dir_all3"] = (frame["avg"].astype(float) >= 0.5).astype(int)
    frame["ml_dir_majority"] = (frame["vote_sum"].astype(int) >= 2).astype(int)
    return frame


def trend_gate(direction, score, mode):
    align = np.where(direction == 1, score, -score)
    opposite = -align
    if mode == "none":
        return np.ones(len(score), dtype=bool)
    if mode == "skip_opposite_score2":
        return opposite < 2
    if mode == "skip_opposite_score3":
        return opposite < 3
    if mode == "align_or_neutral":
        return align >= 0
    if mode == "align_score2":
        return align >= 2
    if mode == "range_only_score2":
        return np.abs(score) <= 2
    if mode == "no_strong_trend_score3":
        return np.abs(score) < 3
    if mode == "strong_only_score3":
        return np.abs(score) >= 3
    raise ValueError(f"unknown trend gate: {mode}")


def apply_skip_hours(frame, mask, skip_hours):
    if skip_hours:
        mask &= ~frame["hour_utc"].isin(skip_hours).to_numpy()
    return mask


def candidate_signals(frame, cand):
    n = len(frame)
    direction = np.full(n, -1, dtype=int)
    mask = np.zeros(n, dtype=bool)
    rsi = frame["rsi14"].astype(float).to_numpy()
    bbp = frame["bbp"].astype(float).to_numpy()
    score = frame["trend_score"].astype(int).to_numpy()
    avg = frame["avg"].astype(float).to_numpy()

    kind = cand["kind"]
    if kind == "rule_rsi_reversal":
        lo, hi = cand["rsi"]
        up = rsi < lo
        down = rsi > hi
        direction[up] = 1
        direction[down] = 0
        mask = up | down
        mask &= trend_gate(direction, score, cand["trend_gate"])

    elif kind == "rule_bb_reversal":
        lo, hi = cand["bbp"]
        up = bbp <= lo
        down = bbp >= hi
        direction[up] = 1
        direction[down] = 0
        mask = up | down
        mask &= trend_gate(direction, score, cand["trend_gate"])

    elif kind == "rule_trend_follow":
        score_min = int(cand["score_min"])
        up = score >= score_min
        down = score <= -score_min
        direction[up] = 1
        direction[down] = 0
        mask = up | down
        if cand.get("rsi_band"):
            lo, hi = cand["rsi_band"]
            mask &= (rsi >= lo) & (rsi <= hi)

    elif kind == "rule_pullback_follow":
        score_min = int(cand["score_min"])
        up = (score >= score_min) & (rsi <= cand["up_rsi_max"]) & (bbp <= cand["up_bbp_max"])
        down = (score <= -score_min) & (rsi >= cand["down_rsi_min"]) & (bbp >= cand["down_bbp_min"])
        direction[up] = 1
        direction[down] = 0
        mask = up | down

    elif kind == "ml_rsi":
        th = float(cand["threshold"])
        if cand["agree_mode"] == "all3":
            agree = frame["agree_all"].to_numpy(bool)
            direction = frame["ml_dir_all3"].to_numpy(int)
        else:
            agree = np.ones(n, dtype=bool)
            direction = frame["ml_dir_majority"].to_numpy(int)
        lo, hi = cand["rsi"]
        conf = (avg >= th) | (avg <= 1 - th)
        mask = agree & conf & ((rsi < lo) | (rsi > hi))
        mask &= trend_gate(direction, score, cand["trend_gate"])

    elif kind == "hybrid_trend_else_ml":
        trend_min = int(cand["score_min"])
        trend_dir = np.where(score >= trend_min, 1, np.where(score <= -trend_min, 0, -1))
        trend_conf = ((trend_dir == 1) & (avg >= cand["trend_threshold"])) | (
            (trend_dir == 0) & (avg <= 1 - cand["trend_threshold"])
        )
        trend_mask = (trend_dir >= 0) & trend_conf

        ml_dir = frame["ml_dir_majority"].to_numpy(int) if cand["agree_mode"] == "majority" else frame["ml_dir_all3"].to_numpy(int)
        agree = np.ones(n, dtype=bool) if cand["agree_mode"] == "majority" else frame["agree_all"].to_numpy(bool)
        lo, hi = cand["rsi"]
        range_mask = (np.abs(score) < trend_min) & agree
        range_mask &= ((avg >= cand["range_threshold"]) | (avg <= 1 - cand["range_threshold"]))
        range_mask &= (rsi < lo) | (rsi > hi)

        direction = np.where(trend_mask, trend_dir, ml_dir)
        mask = trend_mask | range_mask

    elif kind == "hybrid_rule_regime":
        trend_min = int(cand["score_min"])
        trend_dir = np.where(score >= trend_min, 1, np.where(score <= -trend_min, 0, -1))
        lo, hi = cand["rsi"]
        rsi_up = rsi < lo
        rsi_down = rsi > hi
        direction = np.where(trend_dir >= 0, trend_dir, np.where(rsi_up, 1, np.where(rsi_down, 0, -1)))
        mask = (trend_dir >= 0) | ((np.abs(score) < trend_min) & (rsi_up | rsi_down))

    else:
        raise ValueError(f"unknown candidate kind: {kind}")

    mask &= direction >= 0
    mask = apply_skip_hours(frame, mask, cand.get("skip_hours_utc", []))
    return direction, mask


def time_blocks(frame, direction, mask):
    rows = []
    target = frame["target"].astype(int).to_numpy()
    for i, idx in enumerate(np.array_split(np.arange(len(frame)), 10), start=1):
        if len(idx) == 0:
            continue
        use = mask[idx]
        wins = direction[idx][use] == target[idx][use]
        m = metric(wins)
        rows.append({
            "slice": f"time_block_{i:02d}",
            "start": str(frame["time"].iloc[idx[0]]),
            "end": str(frame["time"].iloc[idx[-1]]),
            **m,
        })
    return rows


def block_summary(blocks):
    active = [b for b in blocks if b["trades"] >= 5]
    if not active:
        return {
            "active_blocks": 0,
            "positive_blocks": 0,
            "inactive_blocks": len(blocks),
            "min_block_wr": None,
            "worst_block": None,
        }
    worst = min(active, key=lambda b: b["wr"])
    return {
        "active_blocks": len(active),
        "positive_blocks": sum(1 for b in active if b["pnl_5u"] > 0),
        "inactive_blocks": len(blocks) - len(active),
        "min_block_wr": worst["wr"],
        "worst_block": worst["slice"],
    }


def summarize_candidate(frame, cand):
    direction, mask = candidate_signals(frame, cand)
    target = frame["target"].astype(int).to_numpy()
    wins = direction[mask] == target[mask]
    overall = metric(wins)
    blocks = time_blocks(frame, direction, mask)
    bsum = block_summary(blocks)

    by_regime = {}
    if overall["trades"]:
        tmp = frame.loc[mask, ["trend_label", "rsi_zone"]].copy()
        tmp["win"] = wins
        for name, part in tmp.groupby("trend_label", sort=True):
            by_regime[name] = metric(part["win"].to_numpy())

    score = (
        overall["pnl_5u"]
        + overall["wr"] * 4
        + (bsum["min_block_wr"] or 0) * 3
        + bsum["positive_blocks"] * 35
        - overall["max_loss"] * 24
        - bsum["inactive_blocks"] * 12
        + min(overall["trades"], 1200) * 0.05
    )
    return {
        "name": cand["name"],
        "kind": cand["kind"],
        "candidate": cand,
        "overall": overall,
        "time_block_summary": bsum,
        "time_blocks": blocks,
        "by_regime": by_regime,
        "score": round(float(score), 2),
    }


def current_candidate(strategy_id, cfg):
    return {
        "name": "current_prod",
        "kind": "ml_rsi",
        "threshold": float(cfg[strategy_id]["threshold"]),
        "rsi": (float(cfg[strategy_id].get("rsi_lo", 30)), float(cfg[strategy_id].get("rsi_hi", 70))),
        "agree_mode": cfg[strategy_id].get("agree_mode", "majority"),
        "trend_gate": "none",
        "skip_hours_utc": sorted({int(h) for h in cfg[strategy_id].get("skip_hours_utc", [])}),
    }


def build_candidates(strategy_id, cfg):
    skip_hours = sorted({int(h) for h in cfg[strategy_id].get("skip_hours_utc", [])})
    cands = [current_candidate(strategy_id, cfg)]

    for lo, hi in [(25, 75), (30, 70), (35, 65), (40, 60)]:
        for gate in ["none", "skip_opposite_score2", "skip_opposite_score3", "no_strong_trend_score3", "range_only_score2"]:
            cands.append({
                "name": f"rule_rsi_rev_{lo}_{hi}_{gate}",
                "kind": "rule_rsi_reversal",
                "rsi": (lo, hi),
                "trend_gate": gate,
            })

    for lo, hi in [(0.05, 0.95), (0.10, 0.90), (0.20, 0.80)]:
        for gate in ["none", "skip_opposite_score2", "skip_opposite_score3", "no_strong_trend_score3", "range_only_score2"]:
            cands.append({
                "name": f"rule_bb_rev_{int(lo * 100)}_{int(hi * 100)}_{gate}",
                "kind": "rule_bb_reversal",
                "bbp": (lo, hi),
                "trend_gate": gate,
            })

    for score_min in [2, 3, 4]:
        cands.append({
            "name": f"rule_trend_follow_s{score_min}_all",
            "kind": "rule_trend_follow",
            "score_min": score_min,
        })
        for band in [(35, 75), (40, 70), (45, 65)]:
            cands.append({
                "name": f"rule_trend_follow_s{score_min}_rsi{band[0]}_{band[1]}",
                "kind": "rule_trend_follow",
                "score_min": score_min,
                "rsi_band": band,
            })

    for score_min in [3, 4]:
        for rsi_max, bbp_max, rsi_min, bbp_min in [(55, 0.55, 45, 0.45), (60, 0.65, 40, 0.35), (65, 0.75, 35, 0.25)]:
            cands.append({
                "name": f"rule_pullback_s{score_min}_u{rsi_max}_{int(bbp_max*100)}_d{rsi_min}_{int(bbp_min*100)}",
                "kind": "rule_pullback_follow",
                "score_min": score_min,
                "up_rsi_max": rsi_max,
                "up_bbp_max": bbp_max,
                "down_rsi_min": rsi_min,
                "down_bbp_min": bbp_min,
            })

    for th in [0.55, 0.58, 0.60, 0.62, 0.65, 0.70]:
        for lo, hi in [(30, 70), (35, 65), (40, 60)]:
            for agree in ["majority", "all3"]:
                for gate in ["none", "skip_opposite_score2", "skip_opposite_score3", "align_or_neutral", "range_only_score2"]:
                    cands.append({
                        "name": f"ml_th{int(th*100)}_rsi{lo}_{hi}_{agree}_{gate}",
                        "kind": "ml_rsi",
                        "threshold": th,
                        "rsi": (lo, hi),
                        "agree_mode": agree,
                        "trend_gate": gate,
                        "skip_hours_utc": skip_hours,
                    })

    for trend_th in [0.55, 0.58, 0.60, 0.62]:
        for range_th in [0.55, 0.58, 0.60, 0.62]:
            for score_min in [3, 4]:
                for agree in ["majority", "all3"]:
                    cands.append({
                        "name": f"hybrid_ml_trend{int(trend_th*100)}_range{int(range_th*100)}_s{score_min}_{agree}",
                        "kind": "hybrid_trend_else_ml",
                        "trend_threshold": trend_th,
                        "range_threshold": range_th,
                        "score_min": score_min,
                        "rsi": (30, 70),
                        "agree_mode": agree,
                        "skip_hours_utc": skip_hours,
                    })

    for score_min in [3, 4]:
        for lo, hi in [(30, 70), (35, 65), (40, 60)]:
            cands.append({
                "name": f"hybrid_rule_regime_s{score_min}_rsi{lo}_{hi}",
                "kind": "hybrid_rule_regime",
                "score_min": score_min,
                "rsi": (lo, hi),
            })

    return cands


def pattern_audit(frame):
    out = {}
    grouped = frame.groupby(["trend_label", "rsi_zone"], sort=True)
    for (trend, zone), part in grouped:
        target = part["target"].astype(int).to_numpy()
        up = int((target == 1).sum())
        total = int(len(target))
        out[f"{trend}|{zone}"] = {
            "rows": total,
            "up_rate": round(up / total * 100, 2) if total else 0,
            "down_rate": round((total - up) / total * 100, 2) if total else 0,
        }
    return out


def trim_results(rows, kind=None, limit=12):
    data = [r for r in rows if kind is None or r["kind"] == kind]
    return data[:limit]


def usable_rows(rows, min_trades):
    return [
        r for r in rows
        if r["overall"]["trades"] >= min_trades
        and r["overall"]["wr"] >= BREAKEVEN_WR
        and r["time_block_summary"]["active_blocks"] >= 6
        and (r["time_block_summary"]["min_block_wr"] or 0) >= 45
    ]


def main():
    cfg = read_json(CONFIG_FILE, {})
    trade_cfg = read_json(TRADE_CONFIG_FILE, {})
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")

    report = {
        "method": {
            "type": "unified_rule_ml_regime_research",
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": (
                "All candidates are evaluated on strict rolling walk-forward OOS rows. "
                "Rules use only current/past features. This is research-only."
            ),
        },
        "safety": {
            "autoTrade": trade_cfg.get("autoTrade"),
            "warning": "Do not resume real auto trading until a candidate passes live shadow validation.",
        },
        "data_range": {
            "start": str(df5["time"].min()),
            "end": str(df5["time"].max()),
            "rows_5m": int(len(df5)),
        },
        "strategies": {},
    }

    for strategy_id, horizon in HORIZONS.items():
        print(f"\n=== {strategy_id} horizon={horizon} ===")
        frame = build_oos_frame(df5, strategy_id, horizon)
        rows = [summarize_candidate(frame, cand) for cand in build_candidates(strategy_id, cfg)]
        rows_by_score = sorted(rows, key=lambda r: r["score"], reverse=True)
        rows_by_wr = sorted(
            usable_rows(rows, 120 if strategy_id == "BTC_10min" else 80),
            key=lambda r: (
                r["overall"]["wr"],
                r["time_block_summary"]["min_block_wr"] or 0,
                -r["overall"]["max_loss"],
                r["overall"]["trades"],
            ),
            reverse=True,
        )
        current = next(r for r in rows if r["name"] == "current_prod")
        report["strategies"][strategy_id] = {
            "oos_range": {
                "start": str(frame["time"].iloc[0]),
                "end": str(frame["time"].iloc[-1]),
                "rows": int(len(frame)),
            },
            "current": current,
            "pattern_audit": pattern_audit(frame),
            "top_score": rows_by_score[:20],
            "top_wr_usable": rows_by_wr[:20],
            "top_rule_only": trim_results(rows_by_score, "rule_rsi_reversal", 8)
                + trim_results(rows_by_score, "rule_bb_reversal", 8)
                + trim_results(rows_by_score, "rule_trend_follow", 8)
                + trim_results(rows_by_score, "rule_pullback_follow", 8),
            "top_ml": trim_results(rows_by_score, "ml_rsi", 20),
            "top_hybrid": trim_results(rows_by_score, "hybrid_trend_else_ml", 12)
                + trim_results(rows_by_score, "hybrid_rule_regime", 8),
        }

        print("current:", current["overall"], current["time_block_summary"])
        print("top usable by WR:")
        for row in rows_by_wr[:6]:
            print(row["name"], row["overall"], row["time_block_summary"])
        print("top by score:")
        for row in rows_by_score[:6]:
            print(row["name"], row["overall"], row["time_block_summary"])

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nSaved {REPORT_FILE}")


if __name__ == "__main__":
    main()
