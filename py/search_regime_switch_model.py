"""Explore regime-switch models for BTC binary options.

This script trains fresh LightGBM models in rolling walk-forward windows:
- a general model on all recent rows
- a trend model on strong-trend rows only

It compares:
- baseline_lgb_reversal: current-style RSI-extreme model direction
- skip_countertrend: baseline signal, but skip if it fights a strong trend
- trend_follow_only: trade only in the strong trend direction when the trend
  model agrees
- hybrid_trend_else_reversal: trend-follow in strong trends; otherwise use the
  baseline reversal signal

The output is research-only and must not be promoted directly to live trading.
"""
import json
import os
import sys
import time

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import build_features, fcols, load_symbol
from validate_strategy_candidates import PAYOUT, STAKE

OUT = "E:/codex/data"
REPORT_FILE = os.path.join(OUT, "regime_switch_model_report.json")
TRAIN_SIZE = 8000
TEST_SIZE = 500
STEP = 500
BREAKEVEN_WR = 100 / (1 + PAYOUT)
THRESHOLDS = [0.55, 0.58, 0.60, 0.62, 0.65, 0.70]
HORIZONS = {"BTC_10min": 2, "BTC_30min": 6}


def make_model(seed):
    return LGBMClassifier(
        n_estimators=180,
        max_depth=4,
        learning_rate=0.04,
        subsample=0.75,
        colsample_bytree=0.65,
        reg_alpha=1.0,
        reg_lambda=2.0,
        min_child_samples=40,
        random_state=seed,
        verbose=-1,
    )


def trend_score_frame(fdf):
    score = np.zeros(len(fdf), dtype=int)
    eps = 0.00005
    for col in ["trend6", "trend12", "trend30", "pre50"]:
        v = fdf[col].astype(float).to_numpy()
        score += (v > eps).astype(int)
        score -= (v < -eps).astype(int)
    stack = fdf["ema_stack"].astype(float).to_numpy()
    score += (stack > 0).astype(int)
    score -= (stack < 0).astype(int)
    return score


def metric(wins):
    wins = np.asarray(wins, dtype=bool)
    n = int(len(wins))
    w = int(wins.sum()) if n else 0
    l = n - w
    cur = best = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return {
        "trades": n,
        "wins": w,
        "losses": l,
        "wr": round(w / max(1, n) * 100, 2),
        "edge_over_breakeven": round(w / max(1, n) * 100 - BREAKEVEN_WR, 2),
        "pnl_5u": round(float(w * STAKE * PAYOUT - l * STAKE), 2),
        "max_loss": best,
    }


def blocks(df, n=10):
    rows = []
    if df.empty:
        return rows
    for i, idx in enumerate(np.array_split(np.arange(len(df)), n), start=1):
        if len(idx) == 0:
            continue
        part = df.iloc[idx]
        rows.append({
            "slice": f"block_{i:02d}",
            "start": str(part["time"].iloc[0]),
            "end": str(part["time"].iloc[-1]),
            **metric(part["win"].to_numpy()),
        })
    return rows


def block_summary(rows):
    active = [r for r in rows if r["trades"] >= 20]
    if not active:
        return {"positive_blocks": 0, "active_blocks": 0, "min_block_wr": None}
    return {
        "positive_blocks": sum(1 for r in active if r["pnl_5u"] > 0),
        "active_blocks": len(active),
        "min_block_wr": min(r["wr"] for r in active),
    }


def add_trade(rows, time_value, strategy, mode, th, direction, target, p, rsi, score):
    rows.append({
        "time": time_value,
        "strategy": strategy,
        "mode": mode,
        "threshold": th,
        "direction": "UP" if direction == 1 else "DOWN",
        "target": int(target),
        "win": int(direction) == int(target),
        "prob": round(float(p), 4),
        "strength": round(abs(float(p) - 0.5) * 200, 1),
        "rsi": round(float(rsi), 2),
        "trend_score": int(score),
    })


def evaluate_rows(all_rows):
    df = pd.DataFrame(all_rows)
    out = {}
    if df.empty:
        return out
    for (mode, th), part in df.groupby(["mode", "threshold"], sort=True):
        part = part.sort_values("time").reset_index(drop=True)
        bl = blocks(part)
        out[f"{mode}_th{int(th * 100)}"] = {
            "mode": mode,
            "threshold": float(th),
            "overall": metric(part["win"].to_numpy()),
            "block_summary": block_summary(bl),
            "blocks": bl,
        }
    return out


def run_strategy(df5, strategy_id, horizon):
    fdf = build_features(df5, horizon)
    fdf = fdf[fdf["target"] != 0].reset_index(drop=True)
    fdf["label"] = (fdf["target"] == 1).astype(int)
    fdf["trend_score"] = trend_score_frame(fdf)
    cols = [c for c in fcols(fdf) if c not in ("label",)]
    X = fdf[cols].values
    y = fdf["label"].to_numpy(int)
    times = pd.to_datetime(fdf["time"], utc=True)

    all_rows = []
    i = TRAIN_SIZE
    while i + TEST_SIZE <= len(fdf):
        t0 = time.time()
        train = fdf.iloc[i - TRAIN_SIZE:i]
        test = fdf.iloc[i:i + TEST_SIZE]
        Xtr, ytr = X[i - TRAIN_SIZE:i], y[i - TRAIN_SIZE:i]
        Xte, yte = X[i:i + TEST_SIZE], y[i:i + TEST_SIZE]

        general = make_model(100 + i)
        general.fit(Xtr, ytr)
        p_general = general.predict_proba(Xte)[:, 1]

        trend_train_mask = np.abs(train["trend_score"].to_numpy(int)) >= 3
        if int(trend_train_mask.sum()) >= 600 and len(np.unique(ytr[trend_train_mask])) == 2:
            trend_model = make_model(200 + i)
            trend_model.fit(Xtr[trend_train_mask], ytr[trend_train_mask])
            p_trend = trend_model.predict_proba(Xte)[:, 1]
        else:
            p_trend = p_general

        rsi = test["rsi14"].to_numpy(float)
        score = test["trend_score"].to_numpy(int)
        strong_up = score >= 3
        strong_down = score <= -3
        strong = strong_up | strong_down
        trend_dir = np.where(strong_up, 1, np.where(strong_down, 0, -1))
        rsi_extreme = (rsi < 30) | (rsi > 70)
        time_values = times.iloc[i:i + TEST_SIZE].tolist()

        for th in THRESHOLDS:
            gen_dir = (p_general >= 0.5).astype(int)
            gen_conf = (p_general >= th) | (p_general <= 1 - th)
            gen_trade = gen_conf & rsi_extreme

            trend_agree = ((trend_dir == 1) & (p_trend >= th)) | ((trend_dir == 0) & (p_trend <= 1 - th))
            trend_trade = strong & trend_agree

            skip_counter = gen_trade & ~(((gen_dir == 1) & strong_down) | ((gen_dir == 0) & strong_up))
            hybrid_trade = trend_trade | (gen_trade & ~strong)

            for j in np.where(gen_trade)[0]:
                add_trade(all_rows, time_values[j], strategy_id, "baseline_lgb_reversal", th, gen_dir[j], yte[j], p_general[j], rsi[j], score[j])
            for j in np.where(skip_counter)[0]:
                add_trade(all_rows, time_values[j], strategy_id, "skip_countertrend", th, gen_dir[j], yte[j], p_general[j], rsi[j], score[j])
            for j in np.where(trend_trade)[0]:
                add_trade(all_rows, time_values[j], strategy_id, "trend_follow_only", th, trend_dir[j], yte[j], p_trend[j], rsi[j], score[j])
            for j in np.where(hybrid_trade)[0]:
                use_trend = trend_trade[j]
                d = trend_dir[j] if use_trend else gen_dir[j]
                p = p_trend[j] if use_trend else p_general[j]
                add_trade(all_rows, time_values[j], strategy_id, "hybrid_trend_else_reversal", th, d, yte[j], p, rsi[j], score[j])

        print(f"{strategy_id} window {i}-{i + TEST_SIZE}: {time.time() - t0:.1f}s")
        i += STEP

    results = evaluate_rows(all_rows)
    ranked = sorted(
        results.values(),
        key=lambda r: (
            r["overall"]["wr"],
            r["block_summary"]["min_block_wr"] or 0,
            -r["overall"]["max_loss"],
            r["overall"]["trades"],
        ),
        reverse=True,
    )
    score_ranked = sorted(
        results.values(),
        key=lambda r: (
            r["overall"]["pnl_5u"]
            + r["overall"]["wr"] * 3
            + (r["block_summary"]["min_block_wr"] or 0) * 2
            - r["overall"]["max_loss"] * 15
        ),
        reverse=True,
    )
    return {
        "rows": len(all_rows),
        "ranked_by_wr": ranked[:20],
        "ranked_by_score": score_ranked[:20],
    }


def main():
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")
    report = {
        "method": {
            "type": "fresh_lgb_regime_switch_walkforward",
            "train_size": TRAIN_SIZE,
            "test_size": TEST_SIZE,
            "step": STEP,
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Exploratory fresh retraining, not production-ready.",
        },
        "data_range": {
            "start": str(df5["time"].min()),
            "end": str(df5["time"].max()),
            "rows_5m": int(len(df5)),
        },
        "strategies": {},
    }
    for sid, horizon in HORIZONS.items():
        print(f"\n=== {sid} horizon={horizon} ===")
        report["strategies"][sid] = run_strategy(df5, sid, horizon)
        print("top by WR:")
        for row in report["strategies"][sid]["ranked_by_wr"][:6]:
            print(row["mode"], row["threshold"], row["overall"], row["block_summary"])

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nSaved {REPORT_FILE}")


if __name__ == "__main__":
    main()
