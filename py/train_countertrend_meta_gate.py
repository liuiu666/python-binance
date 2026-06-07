"""Train production shadow meta gates for signal quality.

This trains a second-stage model on historical first-stage signal rows. The
saved model is used only by signal_btc.py shadow candidates; it does not change
production strategy config or enable trading.
"""
import json
import os
import pickle
import sys

import numpy as np

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import load_symbol  # noqa: E402
from search_countertrend_meta_gate import (  # noqa: E402
    CONFIG_FILE,
    OUT,
    build_signal_trades,
    feature_cols,
    make_model,
    read_json,
)


SHADOW_META_GATES = [
    {
        "strategy_id": "BTC_30min",
        "model_id": "BTC_30min_signal_quality",
        "threshold": 0.65,
        "candidate_id": "SHADOW_META_30m_signal_quality_th65",
        "note": "30m second-stage signal-quality gate selected from meta-OOS validation; shadow only.",
    },
]


def train_gate(df5, cfg, spec):
    strategy_id = spec["strategy_id"]
    trades = build_signal_trades(df5, strategy_id, cfg[strategy_id])
    cols = feature_cols(trades)
    X = trades[cols].astype(float).replace([np.inf, -np.inf], np.nan).fillna(0).to_numpy()
    y = trades["signal_win_label"].astype(int).to_numpy()
    if len(np.unique(y)) < 2:
        raise RuntimeError(f"{strategy_id}: signal quality labels contain only one class")

    model = make_model(9300 + len(trades))
    model.fit(X, y)
    prefix = os.path.join(OUT, f"meta_gate_{spec['model_id']}")
    with open(f"{prefix}_lgb.pkl", "wb") as f:
        pickle.dump(model, f)
    with open(f"{prefix}_cols.json", "w", encoding="utf-8") as f:
        json.dump(cols, f, indent=2)
    meta = {
        **spec,
        "train_rows": int(len(trades)),
        "features": cols,
        "positive_rate": round(float(y.mean()), 4),
        "shadow_only": True,
    }
    with open(f"{prefix}_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    return meta


def main():
    cfg = read_json(CONFIG_FILE, {})
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")
    trained = []
    for spec in SHADOW_META_GATES:
        meta = train_gate(df5, cfg, spec)
        trained.append(meta)
        print(
            f"[MetaGateTrain] {meta['candidate_id']} rows={meta['train_rows']} "
            f"pos_rate={meta['positive_rate']} threshold={meta['threshold']}"
        )
    print(json.dumps({"trained": trained}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
