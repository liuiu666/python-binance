"""Search higher-timeframe regime filters for BTC 10m/30m options.

This is research-only. It compares the current ML reversal signal with
higher-timeframe trend gates and simple regime hybrids built from 1h/4h/24h
features. It must not enable real trading.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import build_features, load_symbol  # noqa: E402
from validate_strategy_candidates import PAYOUT, STAKE, collect_predictions  # noqa: E402

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
TRADE_CONFIG_FILE = os.path.join(OUT, "trade_config.json")
REPORT_FILE = os.path.join(OUT, "htf_regime_filter_report.json")
HORIZONS = {"BTC_10min": 2, "BTC_30min": 6}
BREAKEVEN_WR = 100 / (1 + PAYOUT)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


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


def htf_score_frame(frame):
    score = np.zeros(len(frame), dtype=int)
    thresholds = {
        "htf_ret_1h": 0.0010,
        "htf_ret_4h": 0.0025,
        "htf_ret_24h": 0.0060,
    }
    for col, eps in thresholds.items():
        vals = frame[col].astype(float).to_numpy()
        score += (vals > eps).astype(int)
        score -= (vals < -eps).astype(int)
    for col in ["htf_pos_4h", "htf_pos_24h"]:
        vals = frame[col].astype(float).to_numpy()
        score += (vals >= 0.65).astype(int)
        score -= (vals <= 0.35).astype(int)
    return score


def htf_label(score):
    if score >= 3:
        return "strong_up"
    if score <= -3:
        return "strong_down"
    if score > 0:
        return "mild_up"
    if score < 0:
        return "mild_down"
    return "range"


def build_frame(df5, strategy_id, horizon):
    preds = collect_predictions(df5, horizon, strategy_id, use_cache=True)
    fdf = build_features(df5, horizon)
    fdf = fdf[fdf["target"] != 0].reset_index(drop=True)
    keep = [
        "time", "rsi14", "bbp", "bbw", "atrp", "atr_exp", "vr", "trend6",
        "trend12", "trend30", "pre50", "ema_stack", "htf_ret_1h",
        "htf_ret_4h", "htf_ret_24h", "htf_pos_1h", "htf_pos_4h",
        "htf_pos_24h", "htf_rng_1h", "htf_rng_4h", "htf_rng_24h",
        "taker_ratio", "ls_ratio", "fund_rate",
    ]
    feat = fdf[[c for c in keep if c in fdf.columns]].copy()
    feat["time_key"] = pd.to_datetime(feat["time"], utc=True)
    pred = pd.DataFrame({
        "time": pd.to_datetime(preds["time"], utc=True),
        "target": preds["y"].astype(int),
        "avg": preds["avg"].astype(float),
        "vote_sum": preds["vote_sum"].astype(int),
        "agree_all": preds["agree_all"].astype(bool),
    })
    frame = pred.merge(feat.drop(columns=["time"]), left_on="time", right_on="time_key", how="left")
    frame = frame.drop(columns=["time_key"]).sort_values("time").reset_index(drop=True)
    frame["hour_utc"] = frame["time"].dt.hour
    frame["htf_score"] = htf_score_frame(frame)
    frame["htf_label"] = frame["htf_score"].map(htf_label)
    frame["strength"] = np.round(np.abs(frame["avg"].astype(float) - 0.5) * 200, 1)
    frame["ml_dir_majority"] = (frame["vote_sum"].astype(int) >= 2).astype(int)
    frame["ml_dir_all3"] = (frame["avg"].astype(float) >= 0.5).astype(int)
    return frame


def ml_direction(frame, agree_mode):
    if agree_mode == "all3":
        return frame["ml_dir_all3"].astype(int).to_numpy(), frame["agree_all"].astype(bool).to_numpy()
    return frame["ml_dir_majority"].astype(int).to_numpy(), np.ones(len(frame), dtype=bool)


def base_ml_mask(frame, cand):
    avg = frame["avg"].astype(float).to_numpy()
    rsi = frame["rsi14"].astype(float).to_numpy()
    direction, agree = ml_direction(frame, cand.get("agree_mode", "majority"))
    th = float(cand.get("threshold", 0.55))
    mask = agree & ((avg >= th) | (avg <= 1 - th))
    if cand.get("rsi"):
        lo, hi = cand["rsi"]
        mask &= (rsi < lo) | (rsi > hi)
    if cand.get("skip_hours_utc"):
        mask &= ~frame["hour_utc"].isin(cand["skip_hours_utc"]).to_numpy()
    return direction, mask


def evaluate_signals(frame, cand):
    n = len(frame)
    target = frame["target"].astype(int).to_numpy()
    score = frame["htf_score"].astype(int).to_numpy()
    avg = frame["avg"].astype(float).to_numpy()
    rsi = frame["rsi14"].astype(float).to_numpy()
    bbp = frame["bbp"].astype(float).to_numpy()
    kind = cand["kind"]

    if kind in ("current_ml", "ml_skip_htf_counter", "ml_htf_align_only"):
        direction, mask = base_ml_mask(frame, cand)
        align = np.where(direction == 1, score, -score)
        if kind == "ml_skip_htf_counter":
            mask &= align > -int(cand["score_min"])
        elif kind == "ml_htf_align_only":
            mask &= align >= int(cand["score_min"])

    elif kind == "htf_trend_follow_model":
        direction = np.where(score >= cand["score_min"], 1, np.where(score <= -cand["score_min"], 0, -1))
        th = float(cand["threshold"])
        direction_ok = ((direction == 1) & (avg >= th)) | ((direction == 0) & (avg <= 1 - th))
        mask = (direction >= 0) & direction_ok

    elif kind == "htf_pullback_model":
        direction = np.where(score >= cand["score_min"], 1, np.where(score <= -cand["score_min"], 0, -1))
        th = float(cand["threshold"])
        model_ok = ((direction == 1) & (avg >= th)) | ((direction == 0) & (avg <= 1 - th))
        pullback_ok = ((direction == 1) & (rsi <= cand["up_rsi_max"]) & (bbp <= cand["up_bbp_max"])) | (
            (direction == 0) & (rsi >= cand["down_rsi_min"]) & (bbp >= cand["down_bbp_min"])
        )
        mask = (direction >= 0) & model_ok & pullback_ok

    elif kind == "range_reversal_model":
        lo, hi = cand["rsi"]
        rev_dir = np.where(rsi < lo, 1, np.where(rsi > hi, 0, -1))
        th = float(cand["threshold"])
        model_ok = ((rev_dir == 1) & (avg >= th)) | ((rev_dir == 0) & (avg <= 1 - th))
        direction = rev_dir
        mask = (np.abs(score) <= int(cand["range_score_max"])) & (rev_dir >= 0) & model_ok

    elif kind == "hybrid_htf_trend_range_reversal":
        trend_dir = np.where(score >= cand["score_min"], 1, np.where(score <= -cand["score_min"], 0, -1))
        trend_th = float(cand["trend_threshold"])
        trend_mask = ((trend_dir == 1) & (avg >= trend_th)) | ((trend_dir == 0) & (avg <= 1 - trend_th))
        trend_mask &= trend_dir >= 0

        lo, hi = cand["rsi"]
        rev_dir = np.where(rsi < lo, 1, np.where(rsi > hi, 0, -1))
        range_th = float(cand["range_threshold"])
        range_mask = np.abs(score) <= int(cand["range_score_max"])
        range_mask &= ((rev_dir == 1) & (avg >= range_th)) | ((rev_dir == 0) & (avg <= 1 - range_th))
        range_mask &= rev_dir >= 0

        direction = np.where(trend_mask, trend_dir, rev_dir)
        mask = trend_mask | range_mask

    elif kind == "rule_htf_regime":
        trend_dir = np.where(score >= cand["score_min"], 1, np.where(score <= -cand["score_min"], 0, -1))
        lo, hi = cand["rsi"]
        rev_dir = np.where(rsi < lo, 1, np.where(rsi > hi, 0, -1))
        range_mask = np.abs(score) <= int(cand["range_score_max"])
        direction = np.where(trend_dir >= 0, trend_dir, rev_dir)
        mask = (trend_dir >= 0) | (range_mask & (rev_dir >= 0))

    else:
        raise ValueError(f"unknown candidate kind: {kind}")

    mask &= direction >= 0
    wins = direction[mask] == target[mask]
    return direction, mask, wins


def time_blocks(frame, direction, mask):
    target = frame["target"].astype(int).to_numpy()
    rows = []
    for i, idx in enumerate(np.array_split(np.arange(len(frame)), 10), start=1):
        if len(idx) == 0:
            continue
        use = mask[idx]
        rows.append({
            "slice": f"block_{i:02d}",
            "start": str(frame["time"].iloc[idx[0]]),
            "end": str(frame["time"].iloc[idx[-1]]),
            **metric(direction[idx][use] == target[idx][use]),
        })
    return rows


def block_summary(blocks):
    active = [b for b in blocks if b["trades"] >= 5]
    if not active:
        return {"active_blocks": 0, "positive_blocks": 0, "min_block_wr": None, "worst_block": None}
    worst = min(active, key=lambda b: b["wr"])
    return {
        "active_blocks": len(active),
        "positive_blocks": sum(1 for b in active if b["pnl_5u"] > 0),
        "min_block_wr": worst["wr"],
        "worst_block": worst["slice"],
    }


def summarize_candidate(frame, cand):
    direction, mask, wins = evaluate_signals(frame, cand)
    overall = metric(wins)
    blocks = time_blocks(frame, direction, mask)
    bsum = block_summary(blocks)
    by_regime = {}
    if overall["trades"]:
        selected = frame.loc[mask, ["htf_label"]].copy()
        selected["win"] = wins
        for name, part in selected.groupby("htf_label", sort=True):
            by_regime[name] = metric(part["win"].to_numpy())
    days = max(1e-9, (frame["time"].iloc[-1] - frame["time"].iloc[0]).total_seconds() / 86400)
    score = (
        overall["pnl_5u"]
        + overall["wr"] * 4
        + (bsum["min_block_wr"] or 0) * 3
        + bsum["positive_blocks"] * 35
        - overall["max_loss"] * 24
        + min(overall["trades"], 1200) * 0.08
    )
    return {
        "name": cand["name"],
        "kind": cand["kind"],
        "candidate": cand,
        "overall": overall,
        "trades_per_day": round(float(overall["trades"] / days), 2),
        "time_block_summary": bsum,
        "time_blocks": blocks,
        "by_htf_regime": by_regime,
        "score": round(float(score), 2),
    }


def build_candidates(strategy_id, cfg):
    base = cfg[strategy_id]
    skip_hours = sorted({int(h) for h in base.get("skip_hours_utc", [])})
    current = {
        "name": "current_prod",
        "kind": "current_ml",
        "threshold": float(base.get("threshold", 0.55)),
        "rsi": (float(base.get("rsi_lo", 30)), float(base.get("rsi_hi", 70))),
        "agree_mode": base.get("agree_mode", "majority"),
        "skip_hours_utc": skip_hours,
    }
    cands = [current]

    for score_min in [2, 3]:
        cands.append({
            **current,
            "name": f"ml_current_skip_htf_counter_s{score_min}",
            "kind": "ml_skip_htf_counter",
            "score_min": score_min,
        })
        cands.append({
            **current,
            "name": f"ml_current_htf_align_only_s{score_min}",
            "kind": "ml_htf_align_only",
            "score_min": score_min,
        })

    for th in [0.52, 0.55, 0.58, 0.60, 0.62]:
        for score_min in [2, 3, 4]:
            cands.append({
                "name": f"htf_trend_follow_model_th{int(th * 100)}_s{score_min}",
                "kind": "htf_trend_follow_model",
                "threshold": th,
                "score_min": score_min,
            })
            for up_rsi, up_bbp, down_rsi, down_bbp in [(60, 0.70, 40, 0.30), (65, 0.80, 35, 0.20)]:
                cands.append({
                    "name": (
                        f"htf_pullback_model_th{int(th * 100)}_s{score_min}"
                        f"_u{up_rsi}_{int(up_bbp * 100)}_d{down_rsi}_{int(down_bbp * 100)}"
                    ),
                    "kind": "htf_pullback_model",
                    "threshold": th,
                    "score_min": score_min,
                    "up_rsi_max": up_rsi,
                    "up_bbp_max": up_bbp,
                    "down_rsi_min": down_rsi,
                    "down_bbp_min": down_bbp,
                })

        for lo, hi in [(30, 70), (35, 65), (40, 60)]:
            for range_score_max in [0, 1, 2]:
                cands.append({
                    "name": f"range_reversal_model_th{int(th * 100)}_rsi{lo}_{hi}_rng{range_score_max}",
                    "kind": "range_reversal_model",
                    "threshold": th,
                    "rsi": (lo, hi),
                    "range_score_max": range_score_max,
                })
                for trend_th in [0.52, 0.55, 0.58]:
                    cands.append({
                        "name": (
                            f"hybrid_htf_trend{int(trend_th * 100)}_range{int(th * 100)}"
                            f"_s3_rsi{lo}_{hi}_rng{range_score_max}"
                        ),
                        "kind": "hybrid_htf_trend_range_reversal",
                        "trend_threshold": trend_th,
                        "range_threshold": th,
                        "score_min": 3,
                        "rsi": (lo, hi),
                        "range_score_max": range_score_max,
                    })

    for lo, hi in [(30, 70), (35, 65), (40, 60)]:
        for range_score_max in [0, 1, 2]:
            cands.append({
                "name": f"rule_htf_regime_s3_rsi{lo}_{hi}_rng{range_score_max}",
                "kind": "rule_htf_regime",
                "score_min": 3,
                "rsi": (lo, hi),
                "range_score_max": range_score_max,
            })
    return cands


def usable(rows, strategy_id):
    min_trades = 120 if strategy_id == "BTC_10min" else 80
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
            "type": "higher_timeframe_regime_filter_walkforward",
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "features": [
                "htf_ret_1h", "htf_ret_4h", "htf_ret_24h",
                "htf_pos_1h", "htf_pos_4h", "htf_pos_24h",
                "htf_rng_1h", "htf_rng_4h", "htf_rng_24h",
            ],
            "note": "Research only. No production config is changed and real auto trading must stay off.",
        },
        "safety": {
            "autoTrade": trade_cfg.get("autoTrade"),
            "verdict": "research_only_do_not_resume_real_auto_trading",
        },
        "data": {
            "start": str(df5["time"].min()),
            "end": str(df5["time"].max()),
            "rows_5m": int(len(df5)),
        },
        "strategies": {},
        "conclusions": [],
    }

    for strategy_id, horizon in HORIZONS.items():
        print(f"\n=== {strategy_id} h={horizon} ===", flush=True)
        frame = build_frame(df5, strategy_id, horizon)
        rows = [summarize_candidate(frame, cand) for cand in build_candidates(strategy_id, cfg)]
        rows_by_score = sorted(rows, key=lambda r: r["score"], reverse=True)
        rows_usable = sorted(
            usable(rows, strategy_id),
            key=lambda r: (
                r["overall"]["wr"],
                r["time_block_summary"]["min_block_wr"] or 0,
                -r["overall"]["max_loss"],
                r["overall"]["trades"],
            ),
            reverse=True,
        )
        current = next(r for r in rows if r["name"] == "current_prod")
        best = rows_usable[0] if rows_usable else rows_by_score[0]
        delta = round(float(best["overall"]["wr"]) - float(current["overall"]["wr"]), 2)
        conclusion = (
            f"{strategy_id}: current WR {current['overall']['wr']}%/{current['overall']['trades']} trades/"
            f"maxL {current['overall']['max_loss']}; best HTF candidate {best['name']} "
            f"WR {best['overall']['wr']}%/{best['overall']['trades']} trades/maxL {best['overall']['max_loss']} "
            f"({delta:+.2f}pp), {best['trades_per_day']} trades/day."
        )
        report["conclusions"].append(conclusion)
        report["strategies"][strategy_id] = {
            "horizon": horizon,
            "interval_min": int(horizon * 5),
            "oos_range": {
                "start": str(frame["time"].iloc[0]),
                "end": str(frame["time"].iloc[-1]),
                "rows": int(len(frame)),
            },
            "current": current,
            "top_usable_by_wr": rows_usable[:20],
            "top_by_score": rows_by_score[:20],
            "best_selected_for_review": best,
        }
        print(conclusion)
        print("top usable:")
        for row in rows_usable[:5]:
            print(row["name"], row["overall"], row["time_block_summary"], f"tpd={row['trades_per_day']}")
        print("top score:")
        for row in rows_by_score[:5]:
            print(row["name"], row["overall"], row["time_block_summary"], f"tpd={row['trades_per_day']}")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nSaved {REPORT_FILE}")


if __name__ == "__main__":
    main()
