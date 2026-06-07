"""Scan BTC_10min production-model filters on recent labeled history.

This is a live-style filter scan using the saved production models. It is not a
walk-forward retrain, so use it as a filter sanity check alongside
enhanced_results.json.
"""
import json
import os
import pickle
import sys
import warnings

import numpy as np
from lightgbm import LGBMClassifier
from xgboost import XGBClassifier

warnings.filterwarnings("ignore")

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import build_features, fcols, load_symbol

OUT = "E:/codex/data"
MODEL_LABEL = "BTC_10min"
HORIZON = 2
THRESHOLDS = [0.65, 0.70, 0.73, 0.75, 0.78, 0.80]
RSI_FILTERS = [(30, 70), (35, 65), (40, 60)]
PAYOUT = 0.85
STAKE = 5


def max_loss_streak(results):
    best = cur = 0
    for ok in results:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def main():
    models = []
    for i in range(2):
        m = XGBClassifier()
        m.load_model(os.path.join(OUT, f"prod_{MODEL_LABEL}_m{i + 1}.json"))
        models.append(m)
    with open(os.path.join(OUT, f"prod_{MODEL_LABEL}_lgb.pkl"), "rb") as f:
        models.append(pickle.load(f))
    with open(os.path.join(OUT, f"prod_{MODEL_LABEL}_cols.json"), "r", encoding="utf-8") as f:
        cols = json.load(f)

    df5 = load_symbol("btcusdt")
    fdf = build_features(df5, HORIZON)
    fdf = fdf[fdf["target"] != 0].iloc[-8000:].reset_index(drop=True)
    X = fdf[cols].values
    y = (fdf["target"].values == 1).astype(int)
    probs = [m.predict_proba(X)[:, 1] for m in models]
    avg = np.mean(probs, axis=0)
    votes = [(p >= 0.5).astype(int) for p in probs]
    agree = (votes[0] == votes[1]) & (votes[1] == votes[2])
    rsi14 = fdf["rsi14"].values

    results = []
    for lo, hi in RSI_FILTERS:
        rsi_ok = (rsi14 < lo) | (rsi14 > hi)
        for th in THRESHOLDS:
            mask = agree & rsi_ok & ((avg >= th) | (avg <= (1 - th)))
            idx = np.where(mask)[0]
            if len(idx) == 0:
                continue
            pred = (avg[idx] >= 0.5).astype(int)
            wins = pred == y[idx]
            won = int(wins.sum())
            total = int(len(wins))
            pnl = won * STAKE * PAYOUT - (total - won) * STAKE
            results.append({
                "label": f"BTC_10min_th{int(th * 100)}_rsi{lo}_{hi}",
                "threshold": th,
                "rsi_lo": lo,
                "rsi_hi": hi,
                "wr": round(won / total * 100, 2),
                "trades": total,
                "pnl_5u": round(float(pnl), 2),
                "max_loss": max_loss_streak(wins.tolist()),
            })

    results.sort(key=lambda r: (r["wr"], r["trades"]), reverse=True)
    payload = {
        "note": "Production-model recent-history filter scan; compare with walk-forward enhanced_results.json.",
        "stake": STAKE,
        "top": results[:12],
        "all": results,
    }
    out_path = os.path.join(OUT, "optimize_10min_filters.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(json.dumps(payload["top"][:8], indent=2, ensure_ascii=False))
    print(f"Saved {out_path}")


if __name__ == "__main__":
    main()
