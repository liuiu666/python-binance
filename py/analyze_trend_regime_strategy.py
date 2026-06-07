"""Trend-regime audit and candidate search for BTC binary-option strategies.

This is an offline safety report. It uses the existing strict walk-forward
prediction cache, where each test window is predicted by models retrained only
on earlier rows. The goal is to answer whether the current mean-reversion style
signals fail in strong trend regimes, and whether a trend gate improves OOS
behavior before any live trading is considered.
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import build_features, load_symbol
from validate_strategy_candidates import PAYOUT, STAKE, collect_predictions

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
REPORT_FILE = os.path.join(OUT, "trend_regime_strategy_report.json")
BREAKEVEN_WR = 100 / (1 + PAYOUT)

TREND_FEATURES = ["trend6", "trend12", "trend30", "pre50", "ema_stack"]
THRESHOLDS = [0.55, 0.58, 0.60, 0.62, 0.65, 0.70]
RSI_FILTERS = [(30, 70), (35, 65), (40, 60)]
AGREE_MODES = ["majority", "all3"]
TREND_MODES = [
    "none",
    "skip_opposite_score2",
    "skip_opposite_score3",
    "align_or_neutral",
    "align_score2",
    "no_strong_trend_score3",
]


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def max_loss_streak(statuses):
    best = cur = 0
    for ok in statuses:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def metrics(wins):
    wins = np.asarray(wins, dtype=bool)
    total = int(len(wins))
    won = int(wins.sum()) if total else 0
    lost = total - won
    return {
        "trades": total,
        "wins": won,
        "losses": lost,
        "wr": round(won / max(1, total) * 100, 2),
        "edge_over_breakeven": round(won / max(1, total) * 100 - BREAKEVEN_WR, 2),
        "pnl_5u": round(float(won * STAKE * PAYOUT - lost * STAKE), 2),
        "max_loss": max_loss_streak(wins.tolist()),
    }


def chronological_blocks(trades, blocks=10):
    rows = []
    if trades.empty:
        return rows
    for i, idx in enumerate(np.array_split(np.arange(len(trades)), blocks), start=1):
        if len(idx) == 0:
            continue
        part = trades.iloc[idx]
        m = metrics(part["win"].to_numpy())
        rows.append({
            "slice": f"block_{i:02d}",
            "start": str(part["time"].iloc[0]),
            "end": str(part["time"].iloc[-1]),
            **m,
        })
    return rows


def block_summary(blocks):
    active = [b for b in blocks if b["trades"] >= 20]
    if not active:
        return {
            "positive_blocks": 0,
            "active_blocks": 0,
            "min_block_wr": None,
            "worst_block": None,
        }
    worst = min(active, key=lambda b: b["wr"])
    return {
        "positive_blocks": sum(1 for b in active if b["pnl_5u"] > 0),
        "active_blocks": len(active),
        "min_block_wr": worst["wr"],
        "worst_block": worst["slice"],
    }


def score_trend(row):
    score = 0
    for col in ["trend6", "trend12", "trend30", "pre50"]:
        v = float(row.get(col, 0) or 0)
        eps = 0.00005
        if v > eps:
            score += 1
        elif v < -eps:
            score -= 1
    stack = float(row.get("ema_stack", 0) or 0)
    if stack > 0:
        score += 1
    elif stack < 0:
        score -= 1
    return int(score)


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


def align_with_direction(direction, trend_score):
    if direction == 1:
        return trend_score
    return -trend_score


def trend_mask(direction, trend_score, mode):
    # direction: 1=UP, 0=DOWN. Positive trend_score means uptrend.
    align_score = np.where(direction == 1, trend_score, -trend_score)
    opposite_score = -align_score
    if mode == "none":
        return np.ones(len(direction), dtype=bool)
    if mode == "skip_opposite_score2":
        return opposite_score < 2
    if mode == "skip_opposite_score3":
        return opposite_score < 3
    if mode == "align_or_neutral":
        return align_score >= 0
    if mode == "align_score2":
        return align_score >= 2
    if mode == "no_strong_trend_score3":
        return np.abs(trend_score) < 3
    raise ValueError(f"unknown trend mode: {mode}")


def build_prediction_frame(df5, strategy_id, cfg):
    horizon = int(cfg["horizon"])
    preds = collect_predictions(df5, horizon, strategy_id)
    fdf = build_features(df5, horizon)
    fdf = fdf[fdf["target"] != 0].reset_index(drop=True)

    feat = fdf[["time", "target", "rsi14", "atrp", *TREND_FEATURES]].copy()
    feat["time_key"] = pd.to_datetime(feat["time"], utc=True)
    pred = pd.DataFrame({
        "time": pd.to_datetime(preds["time"], utc=True),
        "target": preds["y"].astype(int),
        "avg": preds["avg"].astype(float),
        "vote_sum": preds["vote_sum"].astype(int),
        "agree_all": preds["agree_all"].astype(bool),
    })
    merged = pred.merge(feat.drop(columns=["target"]), left_on="time", right_on="time_key", how="left")
    merged = merged.drop(columns=["time_key", "time_y"]).rename(columns={"time_x": "time"})
    merged["trend_score"] = merged.apply(score_trend, axis=1)
    merged["trend_label"] = merged["trend_score"].map(trend_label)
    merged["hour_utc"] = merged["time"].dt.hour
    return merged


def select_trades(frame, candidate):
    avg = frame["avg"].to_numpy(float)
    vote_sum = frame["vote_sum"].to_numpy(int)
    target = frame["target"].to_numpy(int)
    trend_score = frame["trend_score"].to_numpy(int)

    if candidate["agree_mode"] == "all3":
        agree_ok = frame["agree_all"].to_numpy(bool)
        direction = (avg >= 0.5).astype(int)
    else:
        agree_ok = np.ones(len(frame), dtype=bool)
        direction = (vote_sum >= 2).astype(int)

    th = float(candidate["threshold"])
    strength = np.abs(avg - 0.5) * 200
    mask = agree_ok & ((avg >= th) | (avg <= 1 - th))

    lo, hi = candidate["rsi"]
    rsi = frame["rsi14"].to_numpy(float)
    mask &= (rsi < lo) | (rsi > hi)

    skip_hours = candidate.get("skip_hours_utc") or []
    if skip_hours:
        mask &= ~frame["hour_utc"].isin(skip_hours).to_numpy()

    mask &= trend_mask(direction, trend_score, candidate["trend_mode"])

    out = frame.loc[mask, [
        "time", "avg", "vote_sum", "agree_all", "rsi14", "atrp",
        "trend_score", "trend_label", "hour_utc",
    ]].copy()
    out["direction"] = np.where(direction[mask] == 1, "UP", "DOWN")
    out["direction_num"] = direction[mask]
    out["strength"] = np.round(strength[mask], 1)
    out["target"] = target[mask]
    out["win"] = direction[mask] == target[mask]
    out = out.sort_values("time").reset_index(drop=True)
    return out


def summarize_candidate(frame, candidate):
    trades = select_trades(frame, candidate)
    overall = metrics(trades["win"].to_numpy() if not trades.empty else [])
    blocks = chronological_blocks(trades, 10)
    bsum = block_summary(blocks)

    by_trend = {}
    if not trades.empty:
        for name, part in trades.groupby("trend_label", sort=True):
            by_trend[name] = metrics(part["win"].to_numpy())

    score = (
        overall["pnl_5u"]
        + overall["wr"] * 4
        + (bsum["min_block_wr"] or 0) * 3
        + bsum["positive_blocks"] * 30
        - overall["max_loss"] * 18
        + min(overall["trades"], 800) * 0.1
    )
    return {
        "candidate": candidate,
        "overall": overall,
        "block_summary": bsum,
        "by_trend": by_trend,
        "score": round(float(score), 2),
        "blocks": blocks,
    }


def confidence_bins(trades):
    bins = [0, 20, 30, 40, 50, 60, 70, 101]
    labels = ["0-20", "20-30", "30-40", "40-50", "50-60", "60-70", "70+"]
    if trades.empty:
        return {}
    tmp = trades.copy()
    tmp["strength_bin"] = pd.cut(tmp["strength"], bins=bins, labels=labels, right=False)
    out = {}
    for name, part in tmp.groupby("strength_bin", observed=True):
        out[str(name)] = metrics(part["win"].to_numpy())
    return out


def recent_live_summary():
    path = os.path.join(OUT, "live_trade_audit_report.json")
    if not os.path.exists(path):
        return None
    try:
        report = read_json(path)
        return {
            "overall": report.get("overall"),
            "by_strategy": report.get("by_strategy"),
            "recent": [
                {
                    "strategyId": r.get("strategyId"),
                    "status": r.get("status"),
                    "direction": r.get("direction"),
                    "amount": r.get("amount"),
                    "duration": r.get("duration"),
                    "confidence": r.get("confidence"),
                    "rsi": r.get("rsi_value"),
                    "openPrice": r.get("openPrice"),
                    "closePrice": r.get("closePrice"),
                    "signalTime": r.get("signalTime"),
                }
                for r in report.get("recent", [])
            ],
        }
    except Exception:
        return None


def main():
    cfg = read_json(CONFIG_FILE)
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")

    report = {
        "method": {
            "type": "trend_regime_walkforward_search",
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": (
                "Predictions come from strict rolling walk-forward retraining. "
                "Trend filters are causal and use only current/past feature values."
            ),
            "trend_score": {
                "features": TREND_FEATURES,
                "meaning": "positive=uptrend, negative=downtrend; abs(score)>=3 is strong trend",
            },
        },
        "data_range": {
            "start": str(df5["time"].min()),
            "end": str(df5["time"].max()),
            "rows_5m": int(len(df5)),
        },
        "live_autojs_sample": recent_live_summary(),
        "strategies": {},
    }

    for strategy_id in ["BTC_10min", "BTC_30min"]:
        frame = build_prediction_frame(df5, strategy_id, cfg[strategy_id])
        current_candidate = {
            "name": "current_prod",
            "threshold": float(cfg[strategy_id]["threshold"]),
            "rsi": (float(cfg[strategy_id].get("rsi_lo", 30)), float(cfg[strategy_id].get("rsi_hi", 70))),
            "agree_mode": cfg[strategy_id].get("agree_mode", "majority"),
            "skip_hours_utc": sorted({int(h) for h in cfg[strategy_id].get("skip_hours_utc", [])}),
            "trend_mode": "none",
        }
        current = summarize_candidate(frame, current_candidate)
        current_trades = select_trades(frame, current_candidate)
        current["confidence_bins"] = confidence_bins(current_trades)

        rows = []
        for th in THRESHOLDS:
            for rsi in RSI_FILTERS:
                for agree in AGREE_MODES:
                    for trend_mode in TREND_MODES:
                        cand = {
                            "name": f"th{int(th*100)}_rsi{rsi[0]}_{rsi[1]}_{agree}_{trend_mode}",
                            "threshold": th,
                            "rsi": rsi,
                            "agree_mode": agree,
                            "skip_hours_utc": current_candidate["skip_hours_utc"],
                            "trend_mode": trend_mode,
                        }
                        rows.append(summarize_candidate(frame, cand))

        usable = [
            r for r in rows
            if r["overall"]["trades"] >= 80
            and r["overall"]["wr"] >= BREAKEVEN_WR
            and r["block_summary"]["active_blocks"] >= 6
        ]
        usable.sort(
            key=lambda r: (
                r["overall"]["wr"],
                r["block_summary"]["min_block_wr"] or 0,
                -r["overall"]["max_loss"],
                r["overall"]["trades"],
            ),
            reverse=True,
        )
        score_ranked = sorted(rows, key=lambda r: r["score"], reverse=True)

        report["strategies"][strategy_id] = {
            "current": current,
            "top_wr_usable": usable[:12],
            "top_score": score_ranked[:12],
        }

        print(f"\n{strategy_id}")
        print("current:", json.dumps(current["overall"], ensure_ascii=False), "blocks", current["block_summary"])
        print("current bins:", json.dumps(current["confidence_bins"], ensure_ascii=False))
        print("top usable:")
        for r in usable[:5]:
            print(
                r["candidate"]["name"],
                r["overall"],
                "blocks",
                r["block_summary"],
            )

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nSaved {REPORT_FILE}")


if __name__ == "__main__":
    main()
