"""Research adding 1m micro features to the 2m BTC 10m binary model.

This is research-only and does not touch live services.

It compares:
- cached 2m generic model predictions;
- cached 2m generic model with 1m entry filters;
- newly trained 2m+1m generic model predictions;
- 2m+1m model with the same flow-opposes decision gate.
"""
import glob
import hashlib
import json
import os
import sys
import time

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

sys.path.insert(0, "E:/codex/py")
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
    rsi_np,
)
from research_regime_models_2m import prepare_frame

REPORT_FILE = os.path.join(OUT, "hybrid_1m_2m_model_report.json")
CACHE_DIR = os.path.join(OUT, "cache")
TRAIN_SIZE = int(os.environ.get("HYBRID_1M2M_TRAIN_SIZE", "12000"))
TEST_SIZE = int(os.environ.get("HYBRID_1M2M_TEST_SIZE", "1500"))
STEP = int(os.environ.get("HYBRID_1M2M_STEP", "1500"))


def ema(a, period):
    a = np.asarray(a, dtype=np.float64)
    out = np.empty(len(a), dtype=np.float64)
    out[0] = a[0]
    k = 2 / (period + 1)
    for i in range(1, len(a)):
        out[i] = a[i] * k + out[i - 1] * (1 - k)
    return out


def max_loss(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def non_overlap_mask(mask, cooldown_bars=HORIZON):
    mask = np.asarray(mask, dtype=bool)
    out = np.zeros(len(mask), dtype=bool)
    next_allowed = 0
    for i, ok in enumerate(mask):
        if ok and i >= next_allowed:
            out[i] = True
            next_allowed = i + cooldown_bars
    return out


def make_model(seed):
    return HistGradientBoostingClassifier(
        max_iter=80,
        learning_rate=0.055,
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


def compute_1m_features(df1):
    df = df1.copy().sort_values("open_time").reset_index(drop=True)
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    o = df["open"].astype(float)
    v = df["volume"].astype(float)
    ret = c.pct_change().fillna(0.0)
    out = pd.DataFrame({"m1_time": pd.to_datetime(df["open_time"], utc=True)})

    for p in [1, 2, 3, 5, 10, 15]:
        out[f"m1_ret_{p}"] = c.pct_change(p)

    for p in [5, 10, 20, 50]:
        e = pd.Series(ema(c.values, p))
        out[f"m1_pre_ema{p}"] = c / e - 1
        out[f"m1_ema{p}_slope5"] = e.pct_change(5)

    for p in [7, 14, 21]:
        out[f"m1_rsi{p}"] = rsi_np(c.values, p)

    bb_mid = c.rolling(20, min_periods=20).mean()
    bb_std = c.rolling(20, min_periods=20).std(ddof=0)
    bbu = bb_mid + 2 * bb_std
    bbl = bb_mid - 2 * bb_std
    out["m1_bbp20"] = (c - bbl) / (bbu - bbl).replace(0, np.nan)
    out["m1_bbw20"] = (bbu - bbl) / bb_mid.replace(0, np.nan)

    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1).fillna(h - l)
    atr = pd.Series(ema(tr.values, 14))
    out["m1_atrp14"] = atr / c
    out["m1_atr_exp"] = atr / pd.Series(ema(tr.values, 50)).replace(0, np.nan)
    out["m1_vr20"] = v / v.rolling(20, min_periods=20).mean().replace(0, np.nan)

    full = (h - l).clip(lower=0.01)
    body = (c - o).abs()
    out["m1_body_ratio"] = body / full
    out["m1_upper_wick"] = (h - np.maximum(c, o)) / full
    out["m1_lower_wick"] = (np.minimum(c, o) - l) / full
    out["m1_bull"] = (c > o).astype(float)

    hp10 = h.rolling(10, min_periods=10).max()
    lp10 = l.rolling(10, min_periods=10).min()
    hp20 = h.rolling(20, min_periods=20).max()
    lp20 = l.rolling(20, min_periods=20).min()
    out["m1_hlp10"] = (c - lp10) / (hp10 - lp10).replace(0, np.nan)
    out["m1_hlp20"] = (c - lp20) / (hp20 - lp20).replace(0, np.nan)
    out["m1_rng20"] = (hp20 - lp20) / c

    e5 = pd.Series(ema(c.values, 5))
    e10 = pd.Series(ema(c.values, 10))
    e20 = pd.Series(ema(c.values, 20))
    stack = np.zeros(len(df), dtype=float)
    stack[(e5 >= e10) & (e10 >= e20)] = 1.0
    stack[(e5 <= e10) & (e10 <= e20)] = -1.0
    out["m1_ema_stack"] = stack

    mom_score = (
        np.sign(out["m1_ret_1"].fillna(0))
        + np.sign(out["m1_ret_3"].fillna(0))
        + np.sign(out["m1_ret_5"].fillna(0))
        + out["m1_ema_stack"].fillna(0)
    )
    out["m1_mom_score"] = mom_score
    out["m1_vol_rank"] = pd.Series(v).rank(pct=True).to_numpy()
    out["m1_abs_ret_rank"] = pd.Series(ret.abs()).rank(pct=True).to_numpy()
    return out.replace([np.inf, -np.inf], np.nan)


def add_1m_to_2m(df2, df1):
    out = df2.copy()
    out["time"] = pd.to_datetime(out["time"], utc=True)
    out["m1_lookup_time"] = out["time"] + pd.Timedelta(minutes=BAR_MIN - 1)
    m1 = compute_1m_features(df1)
    merged = pd.merge_asof(
        out.sort_values("m1_lookup_time"),
        m1.sort_values("m1_time"),
        left_on="m1_lookup_time",
        right_on="m1_time",
        direction="backward",
    )
    return merged.drop(columns=["m1_lookup_time", "m1_time"]).sort_values("time").reset_index(drop=True)


def feature_cols(df):
    exclude = {
        "time", "target", "future_close_10m", "future_ret_10m", "regime", "regime_group",
        "open", "high", "low", "close", "volume",
    }
    cols = []
    for col in df.columns:
        if col in exclude:
            continue
        if pd.api.types.is_numeric_dtype(df[col]) or pd.api.types.is_bool_dtype(df[col]):
            cols.append(col)
    return cols


def cache_path(df, cols):
    last_time = str(df["time"].iloc[-1]).replace(":", "-").replace(" ", "_")
    key = hashlib.sha1("|".join(cols).encode("utf-8")).hexdigest()[:10]
    return os.path.join(
        CACHE_DIR,
        f"hybrid_1m2m_generic_10m_tr{TRAIN_SIZE}_te{TEST_SIZE}_st{STEP}_n{len(df)}_c{key}_{last_time}.npz",
    )


def collect_hybrid_predictions(df, cols):
    path = cache_path(df, cols)
    if os.path.exists(path):
        data = np.load(path, allow_pickle=True)
        pred = {k: data[k] for k in data.files}
        pred["time"] = pred["time"].astype(str)
        pred["regime"] = pred["regime"].astype(str)
        print(f"[Hybrid] cache hit: {path}")
        return path, pred

    X = df[cols].replace([np.inf, -np.inf], np.nan).fillna(0).to_numpy(dtype=np.float32)
    y = df["target"].astype(int).to_numpy()
    times = df["time"].astype(str).to_numpy()
    regimes = df["regime"].astype(str).to_numpy()
    chunks = []
    i = TRAIN_SIZE
    win = 0
    while i + TEST_SIZE <= len(df):
        win += 1
        t0 = time.time()
        train_idx = np.arange(i - TRAIN_SIZE, i)
        test_idx = np.arange(i, i + TEST_SIZE)
        model = make_model(3100 + win)
        model.fit(X[train_idx], y[train_idx], sample_weight=sample_weight(y[train_idx]))
        prob = model.predict_proba(X[test_idx])[:, 1]
        chunks.append({
            "time": times[test_idx],
            "y": y[test_idx].astype(np.int8),
            "regime": regimes[test_idx],
            "prob": prob.astype(np.float32),
        })
        print(f"[Hybrid] window {win} {i}-{i + TEST_SIZE} {time.time() - t0:.1f}s")
        i += STEP

    pred = {}
    for key in chunks[0]:
        pred[key] = np.concatenate([c[key] for c in chunks])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savez_compressed(path, **pred)
    print(f"[Hybrid] cache saved: {path}")
    return path, pred


def latest_2m_cache():
    paths = sorted(
        glob.glob(os.path.join(OUT, "cache", "regime_models_2m_10m_tr12000_te1500_st1500_*.npz")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not paths:
        return None, None
    data = np.load(paths[0], allow_pickle=True)
    pred = {
        "time": data["time"].astype(str),
        "y": data["y"].astype(int),
        "regime": data["regime"].astype(str),
        "prob": data["generic_prob"].astype(float),
    }
    return paths[0], pred


def prediction_direction(prob, threshold):
    return np.where(prob >= threshold, 1, np.where(prob <= 1 - threshold, 0, -1))


def align_frame(df, pred):
    frame = df.copy()
    frame["time_str"] = frame["time"].astype(str)
    return frame.set_index("time_str").loc[pred["time"]].reset_index(drop=True)


def risk_flow_opposes(direction, frame):
    taker = frame["taker_ratio"].astype(float).to_numpy()
    return ((direction == 1) & (taker < 0.85)) | ((direction == 0) & (taker > 1.15))


def m1_filter_mask(name, direction, frame):
    if name == "none":
        return np.ones(len(direction), dtype=bool)
    ret1 = frame["m1_ret_1"].astype(float).to_numpy()
    ret2 = frame["m1_ret_2"].astype(float).to_numpy()
    mom = frame["m1_mom_score"].astype(float).to_numpy()
    rsi = frame["m1_rsi14"].astype(float).to_numpy()
    bbp = frame["m1_bbp20"].astype(float).to_numpy()
    lower = frame["m1_lower_wick"].astype(float).to_numpy()
    upper = frame["m1_upper_wick"].astype(float).to_numpy()

    if name == "m1_last_align":
        return ((direction == 1) & (ret1 > 0)) | ((direction == 0) & (ret1 < 0))
    if name == "m1_2bar_align":
        return ((direction == 1) & (ret2 > 0)) | ((direction == 0) & (ret2 < 0))
    if name == "m1_mom_align":
        return ((direction == 1) & (mom > 0)) | ((direction == 0) & (mom < 0))
    if name == "m1_no_sharp_counter":
        return ((direction == 1) & (ret1 > -0.0008)) | ((direction == 0) & (ret1 < 0.0008))
    if name == "m1_micro_reversal":
        up_ok = (direction == 1) & ((ret1 > 0) | ((lower >= 0.45) & (rsi <= 45)) | (bbp <= 0.15))
        dn_ok = (direction == 0) & ((ret1 < 0) | ((upper >= 0.45) & (rsi >= 55)) | (bbp >= 0.85))
        return up_ok | dn_ok
    raise ValueError(name)


def evaluate(pred, frame, name, threshold=0.65, block_flow=False, m1_filter="none"):
    prob = pred["prob"].astype(float)
    y = pred["y"].astype(int)
    direction = prediction_direction(prob, threshold)
    raw = direction >= 0
    raw &= m1_filter_mask(m1_filter, direction, frame)
    if block_flow:
        raw &= ~risk_flow_opposes(direction, frame)
    live = non_overlap_mask(raw, HORIZON)
    wins = direction == y
    times = pred["time"].astype(str)
    st = times[live]
    overall = metric(wins[live], st[0] if len(st) else times[0], st[-1] if len(st) else times[-1])
    raw_st = times[raw]
    raw_metric = metric(wins[raw], raw_st[0] if len(raw_st) else times[0], raw_st[-1] if len(raw_st) else times[-1])
    return {
        "name": name,
        "threshold": threshold,
        "block_flow_opposes": block_flow,
        "m1_filter": m1_filter,
        "overall": overall,
        "raw_overlap": raw_metric,
        "max_loss": max_loss(wins[live]),
    }


def scan(pred, frame, prefix):
    rows = []
    for threshold in [0.60, 0.62, 0.65, 0.66, 0.68, 0.70]:
        for block_flow in [False, True]:
            for filt in ["none", "m1_last_align", "m1_2bar_align", "m1_mom_align", "m1_no_sharp_counter", "m1_micro_reversal"]:
                rows.append(evaluate(
                    pred,
                    frame,
                    f"{prefix}_th{int(threshold * 100)}_{'blockflow' if block_flow else 'noflow'}_{filt}",
                    threshold,
                    block_flow,
                    filt,
                ))
    rows.sort(key=lambda r: (
        r["overall"]["pnl_5u"],
        r["overall"]["wr"],
        -r["max_loss"],
    ), reverse=True)
    return rows


def top(rows, min_trades=80, limit=12, key=None):
    use = [r for r in rows if r["overall"]["trades"] >= min_trades]
    return sorted(use, key=key or (lambda r: (r["overall"]["pnl_5u"], r["overall"]["wr"])), reverse=True)[:limit]


def main():
    t0 = time.time()
    one = load_1m(SYMBOL)
    _, _, base_df = prepare_frame()
    hybrid_df = add_1m_to_2m(base_df, one)
    hybrid_df = hybrid_df[hybrid_df["target"].isin([0, 1])].reset_index(drop=True)
    cols = feature_cols(hybrid_df)
    hybrid_cache, hybrid_pred = collect_hybrid_predictions(hybrid_df, cols)
    frame_hybrid = align_frame(hybrid_df, hybrid_pred)
    hybrid_rows = scan(hybrid_pred, frame_hybrid, "hybrid1m2m")

    base_cache, base_pred = latest_2m_cache()
    base_rows = []
    if base_pred is not None:
        frame_base = align_frame(hybrid_df, base_pred)
        base_rows = scan(base_pred, frame_base, "base2m")

    report = {
        "method": {
            "type": "hybrid_1m_2m_10min_binary_research",
            "symbol": SYMBOL.upper(),
            "bar_min": BAR_MIN,
            "option_min": OPTION_MIN,
            "horizon_bars": HORIZON,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "train_size": TRAIN_SIZE,
            "test_size": TEST_SIZE,
            "step": STEP,
            "note": "1m features are merged up to the close of the current 2m bar only. Metrics use 10m non-overlap.",
        },
        "data": {
            "one_min_rows": int(len(one)),
            "hybrid_rows": int(len(hybrid_df)),
            "features": int(len(cols)),
            "hybrid_cache": hybrid_cache,
            "base_2m_cache": base_cache,
            "oos_rows": int(len(hybrid_pred["y"])),
            "oos_start": str(hybrid_pred["time"][0]),
            "oos_end": str(hybrid_pred["time"][-1]),
        },
        "results": {
            "hybrid_top_pnl": top(hybrid_rows, min_trades=80, limit=15),
            "hybrid_top_wr": top(hybrid_rows, min_trades=80, limit=15, key=lambda r: (r["overall"]["wr"], r["overall"]["trades"])),
            "hybrid_top_trade_count_profitable": top([r for r in hybrid_rows if r["overall"]["pnl_5u"] > 0], min_trades=80, limit=15, key=lambda r: (r["overall"]["trades"], r["overall"]["wr"])),
            "base2m_with_1m_filters_top_pnl": top(base_rows, min_trades=80, limit=15) if base_rows else [],
            "base2m_with_1m_filters_top_wr": top(base_rows, min_trades=80, limit=15, key=lambda r: (r["overall"]["wr"], r["overall"]["trades"])) if base_rows else [],
        },
        "runtime_sec": round(time.time() - t0, 1),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "saved": REPORT_FILE,
        "data": report["data"],
        "hybrid_top": [
            {
                "name": r["name"],
                "wr": r["overall"]["wr"],
                "trades": r["overall"]["trades"],
                "trades_per_day": r["overall"]["trades_per_day"],
                "pnl_5u": r["overall"]["pnl_5u"],
                "max_loss": r["max_loss"],
                "raw_trades": r["raw_overlap"]["trades"],
            }
            for r in report["results"]["hybrid_top_pnl"][:8]
        ],
        "base2m_filter_top": [
            {
                "name": r["name"],
                "wr": r["overall"]["wr"],
                "trades": r["overall"]["trades"],
                "trades_per_day": r["overall"]["trades_per_day"],
                "pnl_5u": r["overall"]["pnl_5u"],
                "max_loss": r["max_loss"],
                "raw_trades": r["raw_overlap"]["trades"],
            }
            for r in report["results"]["base2m_with_1m_filters_top_pnl"][:8]
        ],
        "runtime_sec": report["runtime_sec"],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
