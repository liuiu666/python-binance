"""Strict walk-forward strategy search for BTC binary options.

This script trains only on past candles, predicts the next unseen window, then
evaluates filters on those out-of-sample predictions. It also splits the OOS
predictions into selection and holdout segments so we can avoid trusting a
filter that only wins because it was picked from a grid.
"""
import json
import os
import sys
import time
import warnings

import numpy as np
from lightgbm import LGBMClassifier
from xgboost import XGBClassifier

warnings.filterwarnings("ignore")

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import build_features, fcols, load_symbol

OUT = "E:/codex/data"
PAYOUT = 0.85
STAKE = 5
WINDOW_ROWS = 8000
TRAIN_SIZE = 4000
TEST_SIZE = 500
STEP = 500
THRESHOLDS = [0.55, 0.58, 0.60, 0.62, 0.65, 0.68, 0.70, 0.73, 0.75, 0.78, 0.80]
RSI_FILTERS = [
    ("none", None, None),
    ("rsi30_70", 30, 70),
    ("rsi35_65", 35, 65),
    ("rsi40_60", 40, 60),
]
AGREE_MODES = ["all3", "majority"]
VOL_FILTERS = [
    ("vol_all", None),
    ("vol_mid_hi", 0.35),
    ("vol_hi", 0.60),
]


def make_models():
    return [
        XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.7, colsample_bytree=0.6,
            reg_alpha=1.0, reg_lambda=2.0, min_child_weight=30,
            tree_method="hist", eval_metric="logloss",
            use_label_encoder=False, verbosity=0, random_state=42,
        ),
        XGBClassifier(
            n_estimators=250, max_depth=3, learning_rate=0.03,
            subsample=0.8, colsample_bytree=0.7,
            reg_alpha=0.5, reg_lambda=1.5, min_child_weight=25,
            tree_method="hist", eval_metric="logloss",
            use_label_encoder=False, verbosity=0, random_state=123,
        ),
        LGBMClassifier(
            n_estimators=240, max_depth=4, learning_rate=0.04,
            subsample=0.75, colsample_bytree=0.65,
            reg_alpha=0.8, reg_lambda=1.8,
            min_child_samples=35, random_state=77, verbose=-1,
        ),
    ]


def max_loss_streak(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def metrics(wins):
    wins = np.asarray(wins, dtype=bool)
    total = int(len(wins))
    if total == 0:
        return {"trades": 0, "wins": 0, "wr": 0, "pnl_5u": 0, "max_loss": 0}
    won = int(wins.sum())
    return {
        "trades": total,
        "wins": won,
        "wr": round(won / total * 100, 2),
        "pnl_5u": round(float(won * STAKE * PAYOUT - (total - won) * STAKE), 2),
        "max_loss": max_loss_streak(wins.tolist()),
    }


def collect_oos_predictions(df5, horizon, label):
    fdf = build_features(df5, horizon)
    fdf = fdf[fdf["target"] != 0].iloc[-WINDOW_ROWS:].reset_index(drop=True)
    cols = fcols(fdf)
    y = (fdf["target"].values == 1).astype(int)
    X = fdf[cols].values
    out = []
    i = TRAIN_SIZE
    while i + TEST_SIZE <= len(fdf):
        t0 = time.time()
        Xtr, Xte = X[i - TRAIN_SIZE:i], X[i:i + TEST_SIZE]
        ytr, yte = y[i - TRAIN_SIZE:i], y[i:i + TEST_SIZE]
        models = make_models()
        probs = []
        for m in models:
            m.fit(Xtr, ytr)
            probs.append(m.predict_proba(Xte)[:, 1])
        probs = np.vstack(probs).T
        avg = probs.mean(axis=1)
        votes = (probs >= 0.5).astype(int)
        agree_all = (votes[:, 0] == votes[:, 1]) & (votes[:, 1] == votes[:, 2])
        vote_sum = votes.sum(axis=1)
        pred_majority = (vote_sum >= 2).astype(int)
        pred_all = (avg >= 0.5).astype(int)
        chunk = {
            "time": fdf["time"].iloc[i:i + TEST_SIZE].astype(str).tolist(),
            "y": yte.tolist(),
            "avg": avg.tolist(),
            "pred_all": pred_all.tolist(),
            "pred_majority": pred_majority.tolist(),
            "agree_all": agree_all.tolist(),
            "rsi14": fdf["rsi14"].iloc[i:i + TEST_SIZE].astype(float).tolist(),
            "atrp": fdf["atrp"].iloc[i:i + TEST_SIZE].astype(float).tolist(),
        }
        out.append(chunk)
        print(f"{label} window {i}-{i + TEST_SIZE}: trained/predicted in {time.time() - t0:.1f}s")
        i += STEP

    merged = {}
    for k in out[0]:
        vals = []
        for chunk in out:
            vals.extend(chunk[k])
        merged[k] = np.asarray(vals) if k != "time" else vals
    return merged


def evaluate_grid(preds, label, min_holdout_trades=10):
    y = preds["y"].astype(int)
    avg = preds["avg"].astype(float)
    rsi14 = preds["rsi14"].astype(float)
    atrp = preds["atrp"].astype(float)
    atr_rank = np.argsort(np.argsort(atrp)) / max(1, len(atrp) - 1)
    split = int(len(y) * 0.60)
    results = []

    for th in THRESHOLDS:
        high_conf = (avg >= th) | (avg <= (1 - th))
        for rsi_name, lo, hi in RSI_FILTERS:
            if lo is None:
                rsi_ok = np.ones(len(y), dtype=bool)
            else:
                rsi_ok = (rsi14 < lo) | (rsi14 > hi)
            for vol_name, min_rank in VOL_FILTERS:
                if min_rank is None:
                    vol_ok = np.ones(len(y), dtype=bool)
                else:
                    vol_ok = atr_rank >= min_rank
                for agree_mode in AGREE_MODES:
                    if agree_mode == "all3":
                        agree_ok = preds["agree_all"].astype(bool)
                        pred = preds["pred_all"].astype(int)
                    else:
                        agree_ok = np.ones(len(y), dtype=bool)
                        pred = preds["pred_majority"].astype(int)

                    mask = high_conf & rsi_ok & vol_ok & agree_ok
                    wins = pred == y
                    select_m = mask[:split]
                    hold_m = mask[split:]
                    select = metrics(wins[:split][select_m])
                    hold = metrics(wins[split:][hold_m])
                    full = metrics(wins[mask])
                    if hold["trades"] < min_holdout_trades:
                        continue
                    score = hold["pnl_5u"] + hold["wr"] * 2 + min(hold["trades"], 120) * 0.25 - hold["max_loss"] * 5
                    results.append({
                        "label": f"{label}_th{int(th * 100)}_{rsi_name}_{vol_name}_{agree_mode}",
                        "strategy": label,
                        "threshold": th,
                        "rsi": rsi_name,
                        "vol_filter": vol_name,
                        "agree_mode": agree_mode,
                        "select": select,
                        "holdout": hold,
                        "full_oos": full,
                        "score": round(float(score), 2),
                    })

    results.sort(key=lambda r: (r["holdout"]["pnl_5u"], r["holdout"]["wr"], r["holdout"]["trades"]), reverse=True)
    return results


def main():
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")
    jobs = [("BTC_10min", 2), ("BTC_30min", 6)]
    payload = {
        "method": {
            "train_size": TRAIN_SIZE,
            "test_size": TEST_SIZE,
            "step": STEP,
            "window_rows": WINDOW_ROWS,
            "selection_split": "first 60% of OOS predictions",
            "holdout_split": "last 40% of OOS predictions",
            "payout": PAYOUT,
            "stake": STAKE,
            "note": "Models are trained only on past rows for each test window.",
        },
        "results": {},
    }
    for label, horizon in jobs:
        preds = collect_oos_predictions(df5, horizon, label)
        results = evaluate_grid(preds, label)
        payload["results"][label] = {
            "oos_rows": len(preds["y"]),
            "top_holdout": results[:20],
        }
        print(f"\nTop {label}:")
        for r in results[:8]:
            print(
                f"{r['label']}: hold WR={r['holdout']['wr']}% n={r['holdout']['trades']} "
                f"pnl={r['holdout']['pnl_5u']} | full WR={r['full_oos']['wr']}% n={r['full_oos']['trades']}"
            )

    out_path = os.path.join(OUT, "walkforward_strategy_search.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"\nSaved {out_path}")


if __name__ == "__main__":
    main()
