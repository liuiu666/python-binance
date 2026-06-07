"""Train production BTC models for a chosen binary-options horizon.

Usage:
  python E:/codex/py/train_prod_btc.py 10
  python E:/codex/py/train_prod_btc.py 30

The argument is the option duration in minutes. Features are built on 5m bars,
so 10min -> horizon=2, 30min -> horizon=6.
"""
import json
import os
import pickle
import sys
import warnings

import pandas as pd
from lightgbm import LGBMClassifier
from xgboost import XGBClassifier

warnings.filterwarnings("ignore")

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import build_features, fcols, load_symbol

OUT = "E:/codex/data"
HORIZONS = {10: 2, 30: 6, 60: 12}


def main():
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    if minutes not in HORIZONS:
        raise SystemExit(f"Unsupported minutes={minutes}; choose one of {sorted(HORIZONS)}")

    horizon = HORIZONS[minutes]
    label = f"BTC_{minutes}min"
    prefix = os.path.join(OUT, f"prod_{label}")

    print(f"[Train] {label}: horizon={horizon} (5m bars)")
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")

    fdf = build_features(df5, horizon)
    dfc = fdf[fdf["target"] != 0].reset_index(drop=True)
    cols = fcols(fdf)
    dfc["label"] = (dfc["target"] == 1).astype(int)

    X = dfc[cols].values
    y = dfc["label"].values

    # Train on the most recent 8000 target-bearing rows to match walk-forward scale.
    if len(X) > 8000:
        X = X[-8000:]
        y = y[-8000:]

    print(f"[Train] rows={len(X)}, features={len(cols)}, up_rate={float(y.mean()):.3f}")

    models = [
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
    ]
    lgb = LGBMClassifier(
        n_estimators=240, max_depth=4, learning_rate=0.04,
        subsample=0.75, colsample_bytree=0.65,
        reg_alpha=0.8, reg_lambda=1.8,
        min_child_samples=35, random_state=77, verbose=-1,
    )

    for i, model in enumerate(models, start=1):
        print(f"[Train] fitting XGB m{i}...")
        model.fit(X, y)
        model.save_model(f"{prefix}_m{i}.json")

    print("[Train] fitting LGB...")
    lgb.fit(X, y)
    with open(f"{prefix}_lgb.pkl", "wb") as f:
        pickle.dump(lgb, f)

    with open(f"{prefix}_cols.json", "w") as f:
        json.dump(cols, f)

    print(f"[Train] saved: {prefix}_m1.json, _m2.json, _lgb.pkl, _cols.json")


if __name__ == "__main__":
    main()
