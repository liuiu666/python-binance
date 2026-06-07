"""Train the live shadow model for the researched 2m 10m BTC policy.

This trains one final generic model on the latest rolling window, matching the
research setup used by py/search_2m_primary_model_policies.py as closely as a
live model can:
- 1m BTC data is aggregated to 2m bars;
- the same 2m feature/regime pipeline is used;
- the final model is trained on the latest 12000 labeled 2m rows;
- the tested regime-threshold + low-volatility UP gate policy is saved beside
  the model for signal_btc.py to consume as a shadow strategy.
"""
import json
import os
import pickle
import sys
import time

import numpy as np

sys.path.insert(0, "E:/codex/py")
from research_regime_models_2m import TRAIN_SIZE, feature_cols, make_model, prepare_frame, sample_weight


OUT = "E:/codex/data"
MODEL_ID = "BTC_2m_10min_primary_lowvol_up_gate"
MODEL_FILE = os.path.join(OUT, f"prod_{MODEL_ID}_hgb.pkl")
COLS_FILE = os.path.join(OUT, f"prod_{MODEL_ID}_cols.json")
POLICY_FILE = os.path.join(OUT, f"prod_{MODEL_ID}_policy.json")
META_FILE = os.path.join(OUT, f"prod_{MODEL_ID}_meta.json")


POLICY = {
    "name": "regth_u65_d65_t62_r68_x65_flow_gate_raise_lowvol_up_a35_b45_m3",
    "regime_thresholds": {
        "uptrend": 0.65,
        "downtrend": 0.65,
        "transition": 0.62,
        "range": 0.68,
        "uncertain": 0.65,
    },
    "block_flow_opposes": True,
    "gate": {
        "kind": "raise_margin",
        "min_margin": 0.03,
        "atr_max": 0.35,
        "bbw_max": 0.45,
        "directions": ["UP"],
    },
    "interval_min": 10,
    "bar_min": 2,
    "horizon_bars": 5,
    "fixed_amount": 5,
    "validation": {
        "full_oos_wr": 59.92,
        "full_oos_trades": 1724,
        "full_oos_pnl_5u": 935.25,
        "walk_forward_wr": 59.50,
        "walk_forward_trades": 1237,
        "walk_forward_pnl_5u": 623.0,
        "max_loss": 6,
        "source_report": "E:/codex/data/bad_environment_gates_2m_report.json",
    },
}


def main():
    t0 = time.time()
    _, _, df = prepare_frame()
    cols = feature_cols(df)
    train = df.tail(TRAIN_SIZE).copy()
    X = train[cols].replace([np.inf, -np.inf], np.nan).fillna(0).to_numpy(dtype=np.float32)
    y = train["target"].astype(int).to_numpy()
    model = make_model(9007)
    model.fit(X, y, sample_weight=sample_weight(y))

    os.makedirs(OUT, exist_ok=True)
    with open(MODEL_FILE, "wb") as f:
        pickle.dump(model, f)
    with open(COLS_FILE, "w", encoding="utf-8") as f:
        json.dump(cols, f, ensure_ascii=False, indent=2)
    with open(POLICY_FILE, "w", encoding="utf-8") as f:
        json.dump(POLICY, f, ensure_ascii=False, indent=2)

    meta = {
        "model_id": MODEL_ID,
        "model_file": MODEL_FILE,
        "cols_file": COLS_FILE,
        "policy_file": POLICY_FILE,
        "train_size": int(len(train)),
        "feature_count": int(len(cols)),
        "train_start": str(train["time"].iloc[0]),
        "train_end": str(train["time"].iloc[-1]),
        "class_balance": {
            "up": int((y == 1).sum()),
            "down": int((y == 0).sum()),
        },
        "elapsed_sec": round(time.time() - t0, 2),
        "note": "Final live shadow model trained on the latest rolling window. It is shadow-only until live audit confirms behavior.",
    }
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
