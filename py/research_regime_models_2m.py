"""Walk-forward regime-specific model research for BTC 10m binary options.

This trains small OOS models on 2m bars:
- one generic model across all regimes;
- one separate model per regime group.

It is research-only and does not touch live services.
"""
import hashlib
import json
import os
import sys
import time

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

sys.path.insert(0, "E:/codex/py")
from research_regime_strategy_2m import (
    REPORT_FILE as REGIME_RULE_REPORT,
    classify_regime,
    enrich_features,
    non_overlap_mask,
)
from research_2m_10min_binary import (
    OUT,
    SYMBOL,
    BAR_MIN,
    OPTION_MIN,
    HORIZON,
    BREAKEVEN_WR,
    aggregate_bars,
    build_features,
    load_1m,
    merge_external,
    metric,
)

REPORT_FILE = os.path.join(OUT, "regime_models_2m_10min_report.json")
CACHE_DIR = os.path.join(OUT, "cache")
TRAIN_SIZE = int(os.environ.get("REGIME_MODEL_TRAIN_SIZE", "12000"))
TEST_SIZE = int(os.environ.get("REGIME_MODEL_TEST_SIZE", "1500"))
STEP = int(os.environ.get("REGIME_MODEL_STEP", "1500"))
MIN_GROUP_TRAIN = int(os.environ.get("REGIME_MODEL_MIN_GROUP_TRAIN", "450"))


def regime_group(regime):
    if str(regime).startswith("transition"):
        return "transition"
    return str(regime)


def prepare_frame():
    one = load_1m(SYMBOL)
    two = merge_external(aggregate_bars(one))
    fdf = enrich_features(build_features(two), two)
    df = classify_regime(fdf)
    df["regime_group"] = df["regime"].map(regime_group)
    df = df[df["target"].isin([0, 1])].reset_index(drop=True)
    return one, two, df


def feature_cols(df):
    exclude = {
        "time", "target", "future_close_10m", "future_ret_10m", "regime", "regime_group",
        "open", "high", "low", "close", "volume",
    }
    cols = []
    for c in df.columns:
        if c in exclude:
            continue
        if pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_bool_dtype(df[c]):
            cols.append(c)
    return cols


def make_model(seed):
    return HistGradientBoostingClassifier(
        max_iter=70,
        learning_rate=0.06,
        max_leaf_nodes=15,
        min_samples_leaf=35,
        l2_regularization=0.08,
        random_state=seed,
    )


def sample_weight(y):
    y = np.asarray(y, dtype=int)
    pos = max(int((y == 1).sum()), 1)
    neg = max(int((y == 0).sum()), 1)
    n = len(y)
    return np.where(y == 1, n / (2 * pos), n / (2 * neg))


def cache_path(df, cols):
    last_time = str(df["time"].iloc[-1]).replace(":", "-").replace(" ", "_")
    key = hashlib.sha1("|".join(cols).encode("utf-8")).hexdigest()[:10]
    return os.path.join(
        CACHE_DIR,
        f"regime_models_2m_10m_tr{TRAIN_SIZE}_te{TEST_SIZE}_st{STEP}_n{len(df)}_c{key}_{last_time}.npz",
    )


def load_cache(path):
    if not os.path.exists(path):
        return None
    try:
        data = np.load(path, allow_pickle=True)
        out = {k: data[k] for k in data.files}
        out["time"] = out["time"].astype(str)
        out["regime"] = out["regime"].astype(str)
        out["regime_group"] = out["regime_group"].astype(str)
        print(f"[RegimeModel] cache hit: {path}")
        return out
    except Exception as e:
        print(f"[RegimeModel] cache ignored: {e}")
        return None


def collect_predictions(df, cols):
    path = cache_path(df, cols)
    cached = load_cache(path)
    if cached is not None:
        return cached

    X = df[cols].replace([np.inf, -np.inf], np.nan).fillna(0).to_numpy(dtype=np.float32)
    y = df["target"].astype(int).to_numpy()
    groups = df["regime_group"].astype(str).to_numpy()
    regimes = df["regime"].astype(str).to_numpy()
    times = df["time"].astype(str).to_numpy()
    chunks = []
    i = TRAIN_SIZE
    win = 0
    while i + TEST_SIZE <= len(df):
        win += 1
        t0 = time.time()
        train_idx = np.arange(i - TRAIN_SIZE, i)
        test_idx = np.arange(i, i + TEST_SIZE)

        generic = make_model(1000 + win)
        generic.fit(X[train_idx], y[train_idx], sample_weight=sample_weight(y[train_idx]))
        generic_prob = generic.predict_proba(X[test_idx])[:, 1]

        regime_prob = np.full(TEST_SIZE, np.nan, dtype=np.float32)
        model_used = np.full(TEST_SIZE, "none", dtype=object)
        trained_groups = []
        for group in sorted(set(groups[train_idx])):
            tr = train_idx[groups[train_idx] == group]
            te_local = np.where(groups[test_idx] == group)[0]
            if len(tr) < MIN_GROUP_TRAIN or len(te_local) == 0:
                continue
            yy = y[tr]
            if len(np.unique(yy)) < 2:
                continue
            model = make_model(2000 + win + abs(hash(group)) % 500)
            model.fit(X[tr], yy, sample_weight=sample_weight(yy))
            regime_prob[te_local] = model.predict_proba(X[test_idx[te_local]])[:, 1]
            model_used[te_local] = group
            trained_groups.append(group)

        chunks.append({
            "time": times[test_idx],
            "y": y[test_idx].astype(np.int8),
            "regime": regimes[test_idx],
            "regime_group": groups[test_idx],
            "generic_prob": generic_prob.astype(np.float32),
            "regime_prob": regime_prob.astype(np.float32),
            "model_used": model_used.astype(str),
        })
        print(f"[RegimeModel] window {win} {i}-{i+TEST_SIZE} groups={trained_groups} {time.time()-t0:.1f}s")
        i += STEP

    pred = {}
    for key in chunks[0]:
        pred[key] = np.concatenate([c[key] for c in chunks])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savez_compressed(path, **pred)
    print(f"[RegimeModel] cache saved: {path}")
    return pred


def evaluate_pred(pred, prob_key, name, group_filter=None, threshold=0.55):
    prob = pred[prob_key].astype(float)
    y = pred["y"].astype(int)
    valid = np.isfinite(prob)
    if group_filter is not None:
        valid &= np.isin(pred["regime_group"].astype(str), np.asarray(group_filter, dtype=str))
    direction = np.where(prob >= threshold, 1, np.where(prob <= 1 - threshold, 0, -1))
    raw_mask = valid & (direction >= 0)
    live_mask = non_overlap_mask(raw_mask, HORIZON)
    wins = direction == y
    times = pred["time"].astype(str)
    st = times[live_mask]
    overall = metric(wins[live_mask], st[0] if len(st) else times[0], st[-1] if len(st) else times[-1])
    raw_st = times[raw_mask]
    raw = metric(wins[raw_mask], raw_st[0] if len(raw_st) else times[0], raw_st[-1] if len(raw_st) else times[-1])
    by_group = {}
    groups = pred["regime_group"].astype(str)
    for g in sorted(set(groups)):
        idx = groups == g
        m = live_mask & idx
        gst = times[m]
        by_group[g] = metric(wins[m], gst[0] if len(gst) else times[idx][0], gst[-1] if len(gst) else times[idx][-1])
    score = (
        overall["pnl_5u"]
        + overall["wr"] * 3
        + min(overall["trades"], 2200) * 0.16
        - overall["max_loss"] * 10
    )
    return {
        "name": name,
        "prob_key": prob_key,
        "threshold": threshold,
        "group_filter": group_filter,
        "overall": overall,
        "raw_overlap": raw,
        "by_group": by_group,
        "score": round(float(score), 2),
    }


def scan(pred):
    rows = []
    thresholds = [0.52, 0.55, 0.58, 0.60, 0.62, 0.65]
    group_sets = [
        None,
        ["range"],
        ["uptrend"],
        ["downtrend"],
        ["transition"],
        ["uncertain"],
        ["range", "uncertain"],
        ["uptrend", "downtrend"],
        ["range", "transition"],
    ]
    for prob_key, label in [("generic_prob", "generic"), ("regime_prob", "regime_specific")]:
        for th in thresholds:
            for groups in group_sets:
                suffix = "all" if groups is None else "_".join(groups)
                rows.append(evaluate_pred(pred, prob_key, f"{label}_th{int(th*100)}_{suffix}", groups, th))
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def top(rows, min_trades=50, limit=12, key=None):
    use = [r for r in rows if r["overall"]["trades"] >= min_trades]
    return sorted(use, key=key or (lambda r: r["score"]), reverse=True)[:limit]


def main():
    t0 = time.time()
    one, two, df = prepare_frame()
    cols = feature_cols(df)
    pred = collect_predictions(df, cols)
    rows = scan(pred)
    report = {
        "method": {
            "type": "regime_specific_walkforward_model_research_2m_10min",
            "symbol": SYMBOL.upper(),
            "bar_min": BAR_MIN,
            "option_min": OPTION_MIN,
            "horizon_bars": HORIZON,
            "train_size": TRAIN_SIZE,
            "test_size": TEST_SIZE,
            "step": STEP,
            "min_group_train": MIN_GROUP_TRAIN,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Primary metrics use non-overlap 10m cooldown. Raw overlap counts are included for diagnostics.",
            "rule_report": REGIME_RULE_REPORT,
        },
        "data": {
            "one_min_rows": int(len(one)),
            "two_min_rows": int(len(two)),
            "feature_rows": int(len(df)),
            "oos_rows": int(len(pred["y"])),
            "oos_start": str(pred["time"][0]),
            "oos_end": str(pred["time"][-1]),
            "features": len(cols),
        },
        "results": {
            "top_balanced": top(rows, min_trades=80, limit=15),
            "top_high_wr": top(rows, min_trades=50, limit=15, key=lambda r: (r["overall"]["wr"], r["overall"]["trades"])),
            "top_trade_count_profitable": top([r for r in rows if r["overall"]["pnl_5u"] > 0], min_trades=80, limit=15, key=lambda r: (r["overall"]["trades"], r["overall"]["wr"])),
            "top_regime_specific": top([r for r in rows if r["prob_key"] == "regime_prob"], min_trades=80, limit=15),
            "top_generic": top([r for r in rows if r["prob_key"] == "generic_prob"], min_trades=80, limit=15),
        },
        "candidate_count": len(rows),
        "runtime_sec": round(time.time() - t0, 1),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "saved": REPORT_FILE,
        "data": report["data"],
        "top_balanced": [
            {
                "name": r["name"],
                "wr": r["overall"]["wr"],
                "trades": r["overall"]["trades"],
                "trades_per_day": r["overall"]["trades_per_day"],
                "raw_trades": r["raw_overlap"]["trades"],
                "pnl_5u": r["overall"]["pnl_5u"],
                "max_loss": r["overall"]["max_loss"],
            }
            for r in report["results"]["top_balanced"][:10]
        ],
        "runtime_sec": report["runtime_sec"],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
