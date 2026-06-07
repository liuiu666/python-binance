"""Longer-history walk-forward validation for selected BTC option strategies.

This is a confirmation pass, not a wide optimizer. It validates a small set of
candidate filters across all available BTC data after a long rolling train
period, then reports full and chronological block metrics.
"""
import json
import os
import sys
import time
import warnings
import hashlib

import numpy as np
from lightgbm import LGBMClassifier
from xgboost import XGBClassifier

warnings.filterwarnings("ignore")

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import build_features, fcols, load_symbol

OUT = "E:/codex/data"
CACHE_DIR = os.path.join(OUT, "cache")
PAYOUT = 0.85
STAKE = 5
TRAIN_SIZE = 8000
TEST_SIZE = 500
STEP = 500


CANDIDATES = {
    "BTC_10min": [
        {
            "name": "current_th62_rsi35_65_vol_hi_all3",
            "threshold": 0.62,
            "rsi": (35, 65),
            "vol_min_rank": 0.60,
            "agree_mode": "all3",
        },
        {
            "name": "more_trades_th60_rsi35_65_vol_hi_majority",
            "threshold": 0.60,
            "rsi": (35, 65),
            "vol_min_rank": 0.60,
            "agree_mode": "majority",
        },
        {
            "name": "no_vol_th58_rsi30_70_all3",
            "threshold": 0.58,
            "rsi": (30, 70),
            "vol_min_rank": None,
            "agree_mode": "all3",
        },
        {
            "name": "baseline_th65_no_rsi_all3",
            "threshold": 0.65,
            "rsi": None,
            "vol_min_rank": None,
            "agree_mode": "all3",
        },
        {
            "name": "recent_scan_th65_rsi40_60_all3",
            "threshold": 0.65,
            "rsi": (40, 60),
            "vol_min_rank": None,
            "agree_mode": "all3",
        },
        {
            "name": "recent_scan_th65_rsi35_65_all3",
            "threshold": 0.65,
            "rsi": (35, 65),
            "vol_min_rank": None,
            "agree_mode": "all3",
        },
    ],
    "BTC_30min": [
        {
            "name": "current_th55_rsi30_70_majority",
            "threshold": 0.55,
            "rsi": (30, 70),
            "vol_min_rank": None,
            "agree_mode": "majority",
        },
        {
            "name": "stable_th58_rsi30_70_all3",
            "threshold": 0.58,
            "rsi": (30, 70),
            "vol_min_rank": None,
            "agree_mode": "all3",
        },
        {
            "name": "high_wr_th80_none_all3",
            "threshold": 0.80,
            "rsi": None,
            "vol_min_rank": None,
            "agree_mode": "all3",
        },
        {
            "name": "baseline_th78_none_all3",
            "threshold": 0.78,
            "rsi": None,
            "vol_min_rank": None,
            "agree_mode": "all3",
        },
    ],
}


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


def metric(wins):
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


def cache_path(label, horizon, fdf):
    last_time = str(fdf["time"].iloc[-1]).replace(":", "-").replace(" ", "_")
    cols_hash = hashlib.sha1("|".join(fcols(fdf)).encode("utf-8")).hexdigest()[:10]
    return os.path.join(
        CACHE_DIR,
        f"walkforward_{label}_h{horizon}_tr{TRAIN_SIZE}_te{TEST_SIZE}_st{STEP}_n{len(fdf)}_c{cols_hash}_{last_time}.npz",
    )


def load_prediction_cache(path, label):
    try:
        if not os.path.exists(path):
            return None
        data = np.load(path, allow_pickle=False)
        preds = {
            "time": data["time"].astype(str).tolist(),
            "y": data["y"],
            "avg": data["avg"],
            "vote_sum": data["vote_sum"],
            "agree_all": data["agree_all"],
            "rsi14": data["rsi14"],
            "atrp": data["atrp"],
        }
        print(f"{label} validation cache hit: {path}")
        return preds
    except Exception as e:
        print(f"{label} validation cache ignored: {e}")
        return None


def save_prediction_cache(path, merged):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savez_compressed(
        path,
        time=np.asarray(merged["time"], dtype=str),
        y=merged["y"],
        avg=merged["avg"],
        vote_sum=merged["vote_sum"],
        agree_all=merged["agree_all"],
        rsi14=merged["rsi14"],
        atrp=merged["atrp"],
    )


def collect_predictions(df5, horizon, label, use_cache=True):
    fdf = build_features(df5, horizon)
    fdf = fdf[fdf["target"] != 0].reset_index(drop=True)
    path = cache_path(label, horizon, fdf)
    if use_cache:
        cached = load_prediction_cache(path, label)
        if cached is not None:
            return cached

    cols = fcols(fdf)
    X = fdf[cols].values
    y = (fdf["target"].values == 1).astype(int)
    chunks = []
    i = TRAIN_SIZE
    while i + TEST_SIZE <= len(fdf):
        t0 = time.time()
        models = make_models()
        Xtr, ytr = X[i - TRAIN_SIZE:i], y[i - TRAIN_SIZE:i]
        Xte, yte = X[i:i + TEST_SIZE], y[i:i + TEST_SIZE]
        probs = []
        for model in models:
            model.fit(Xtr, ytr)
            probs.append(model.predict_proba(Xte)[:, 1])
        probs = np.vstack(probs).T
        avg = probs.mean(axis=1)
        votes = (probs >= 0.5).astype(int)
        chunk = {
            "time": fdf["time"].iloc[i:i + TEST_SIZE].astype(str).tolist(),
            "y": yte.tolist(),
            "avg": avg.tolist(),
            "vote_sum": votes.sum(axis=1).tolist(),
            "agree_all": ((votes[:, 0] == votes[:, 1]) & (votes[:, 1] == votes[:, 2])).tolist(),
            "rsi14": fdf["rsi14"].iloc[i:i + TEST_SIZE].astype(float).tolist(),
            "atrp": fdf["atrp"].iloc[i:i + TEST_SIZE].astype(float).tolist(),
        }
        chunks.append(chunk)
        print(f"{label} validation window {i}-{i + TEST_SIZE}: {time.time() - t0:.1f}s")
        i += STEP

    merged = {}
    for key in chunks[0]:
        vals = []
        for chunk in chunks:
            vals.extend(chunk[key])
        merged[key] = vals if key == "time" else np.asarray(vals)
    if use_cache:
        save_prediction_cache(path, merged)
        print(f"{label} validation cache saved: {path}")
    return merged


def evaluate_candidate(preds, candidate):
    y = preds["y"].astype(int)
    avg = preds["avg"].astype(float)
    vote_sum = preds["vote_sum"].astype(int)
    if candidate["agree_mode"] == "all3":
        agree_ok = preds["agree_all"].astype(bool)
        direction = (avg >= 0.5).astype(int)
    else:
        agree_ok = np.ones(len(y), dtype=bool)
        direction = (vote_sum >= 2).astype(int)

    th = candidate["threshold"]
    mask = agree_ok & ((avg >= th) | (avg <= (1 - th)))

    if candidate["rsi"]:
        lo, hi = candidate["rsi"]
        rsi = preds["rsi14"].astype(float)
        mask &= (rsi < lo) | (rsi > hi)

    if candidate["vol_min_rank"] is not None:
        atr = preds["atrp"].astype(float)
        rank = np.argsort(np.argsort(atr)) / max(1, len(atr) - 1)
        mask &= rank >= float(candidate["vol_min_rank"])

    wins = direction == y
    full = metric(wins[mask])
    n = len(y)
    block_size = n // 4
    blocks = []
    for bi in range(4):
        a = bi * block_size
        b = n if bi == 3 else (bi + 1) * block_size
        m = mask[a:b]
        block = metric(wins[a:b][m])
        block["start"] = preds["time"][a]
        block["end"] = preds["time"][b - 1]
        blocks.append(block)

    positive_blocks = sum(1 for b in blocks if b["pnl_5u"] > 0)
    min_block_wr = min((b["wr"] for b in blocks if b["trades"] > 0), default=0)
    score = (
        full["pnl_5u"]
        + full["wr"] * 2
        + min(full["trades"], 500) * 0.2
        + positive_blocks * 25
        - full["max_loss"] * 6
        + min_block_wr
    )
    return {
        "name": candidate["name"],
        "params": candidate,
        "full_oos": full,
        "blocks": blocks,
        "positive_blocks": positive_blocks,
        "min_block_wr": round(float(min_block_wr), 2),
        "score": round(float(score), 2),
    }


def main():
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")
    jobs = [("BTC_10min", 2), ("BTC_30min", 6)]
    output = {
        "method": {
            "train_size": TRAIN_SIZE,
            "test_size": TEST_SIZE,
            "step": STEP,
            "stake": STAKE,
            "payout": PAYOUT,
            "note": "Small candidate set validated over all available OOS windows; no grid search here.",
        },
        "results": {},
    }
    for label, horizon in jobs:
        preds = collect_predictions(df5, horizon, label)
        rows = []
        for cand in CANDIDATES[label]:
            rows.append(evaluate_candidate(preds, cand))
        rows.sort(key=lambda x: x["score"], reverse=True)
        output["results"][label] = {
            "oos_rows": len(preds["y"]),
            "ranked": rows,
        }
        print(f"\n{label} validation ranking:")
        for row in rows:
            f = row["full_oos"]
            print(
                f"{row['name']}: WR={f['wr']}% n={f['trades']} pnl={f['pnl_5u']} "
                f"maxLoss={f['max_loss']} positiveBlocks={row['positive_blocks']}/4"
            )
    out_path = os.path.join(OUT, "strategy_candidate_validation.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\nSaved {out_path}")


if __name__ == "__main__":
    main()
