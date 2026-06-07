"""Research BTC 10-minute binary options on 2-minute bars.

This is research-only. It aggregates local 1m BTCUSDT data into 2m bars, labels
the next 10 minutes as UP/DOWN, trains a small rolling OOS ensemble, and scans
RSI and non-RSI filters.
"""
import hashlib
import json
import os
import time
import warnings

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import numpy as np
import pandas as pd
from xgboost import XGBClassifier

try:
    from lightgbm import LGBMClassifier
except Exception:
    LGBMClassifier = None

warnings.filterwarnings("ignore")

OUT = "E:/codex/data"
CACHE_DIR = os.path.join(OUT, "cache")
REPORT_FILE = os.path.join(OUT, "research_2m_10min_binary_report.json")
SYMBOL = "btcusdt"
BAR_MIN = 2
OPTION_MIN = 10
HORIZON = OPTION_MIN // BAR_MIN
PAYOUT = 0.85
STAKE = 5
BREAKEVEN_WR = 100 / (1 + PAYOUT)
TRAIN_SIZE = int(os.environ.get("RESEARCH_2M_TRAIN_SIZE", "12000"))
TEST_SIZE = int(os.environ.get("RESEARCH_2M_TEST_SIZE", "1000"))
STEP = int(os.environ.get("RESEARCH_2M_STEP", "1000"))


def bars(minutes):
    return max(1, int(round(float(minutes) / BAR_MIN)))


def ema_np(a, period):
    a = np.asarray(a, dtype=np.float64)
    out = np.empty(len(a), dtype=np.float64)
    out[0] = a[0]
    k = 2 / (period + 1)
    for i in range(1, len(a)):
        out[i] = a[i] * k + out[i - 1] * (1 - k)
    return out


def rsi_np(a, period):
    a = np.asarray(a, dtype=np.float64)
    d = np.diff(a, prepend=a[0])
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag = np.empty(len(a), dtype=np.float64)
    al = np.empty(len(a), dtype=np.float64)
    seed = min(len(a), period + 1)
    ag[0] = gain[:seed].mean()
    al[0] = loss[:seed].mean()
    for i in range(1, len(a)):
        ag[i] = (ag[i - 1] * (period - 1) + gain[i]) / period
        al[i] = (al[i - 1] * (period - 1) + loss[i]) / period
    rs = np.where(al > 0, ag / al, 100.0)
    return 100 - 100 / (1 + rs)


def max_loss_streak(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def metric(wins, first_time=None, last_time=None):
    wins = np.asarray(wins, dtype=bool)
    total = int(len(wins))
    if total == 0:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "wr": 0.0,
            "edge_over_breakeven": round(-BREAKEVEN_WR, 2),
            "pnl_5u": 0.0,
            "max_loss": 0,
            "trades_per_day": 0.0,
        }
    won = int(wins.sum())
    pnl = won * STAKE * PAYOUT - (total - won) * STAKE
    days = None
    if first_time is not None and last_time is not None:
        start = pd.to_datetime(first_time)
        end = pd.to_datetime(last_time)
        days = max((end - start).total_seconds() / 86400, 1 / 24)
    return {
        "trades": total,
        "wins": won,
        "losses": total - won,
        "wr": round(won / total * 100, 2),
        "edge_over_breakeven": round(won / total * 100 - BREAKEVEN_WR, 2),
        "pnl_5u": round(float(pnl), 2),
        "max_loss": max_loss_streak(wins.tolist()),
        "trades_per_day": round(total / days, 2) if days else 0.0,
    }


def load_1m(symbol):
    path = os.path.join(OUT, f"{symbol}_1m.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    df = pd.read_csv(path, parse_dates=["open_time"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna().drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


def aggregate_bars(df1):
    df = df1.copy()
    df["period"] = df["open_time"].dt.floor(f"{BAR_MIN}min")
    out = df.groupby("period").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index().rename(columns={"period": "time"})
    return out.dropna().reset_index(drop=True)


def read_external(path, time_col):
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    if df.empty or time_col not in df.columns:
        return None
    df[time_col] = pd.to_datetime(df[time_col], utc=True, format="ISO8601")
    return df.dropna(subset=[time_col]).sort_values(time_col).reset_index(drop=True)


def merge_external(df):
    out = df.sort_values("time").reset_index(drop=True)
    out["time"] = pd.to_datetime(out["time"], utc=True)

    fund = read_external(os.path.join(OUT, f"{SYMBOL}_funding.csv"), "fundingTime")
    if fund is not None:
        fund["fundingRate"] = pd.to_numeric(fund["fundingRate"], errors="coerce")
        out = pd.merge_asof(out, fund[["fundingTime", "fundingRate"]], left_on="time", right_on="fundingTime", direction="backward")
        out["funding_rate"] = out["fundingRate"].fillna(0.0)
        out = out.drop(columns=[c for c in ["fundingTime", "fundingRate"] if c in out.columns])
    else:
        out["funding_rate"] = 0.0

    ls = read_external(os.path.join(OUT, f"{SYMBOL}_lsratio.csv"), "timestamp")
    if ls is not None:
        for col in ["longShortRatio", "longAccount", "shortAccount"]:
            ls[col] = pd.to_numeric(ls[col], errors="coerce")
        out = pd.merge_asof(
            out,
            ls[["timestamp", "longShortRatio", "longAccount", "shortAccount"]],
            left_on="time",
            right_on="timestamp",
            direction="backward",
        )
        out["ls_ratio"] = out["longShortRatio"].fillna(1.0)
        out["ls_long"] = out["longAccount"].fillna(0.5)
        out["ls_short"] = out["shortAccount"].fillna(0.5)
        out = out.drop(columns=[c for c in ["timestamp", "longShortRatio", "longAccount", "shortAccount"] if c in out.columns])
    else:
        out["ls_ratio"] = 1.0
        out["ls_long"] = 0.5
        out["ls_short"] = 0.5

    tk = read_external(os.path.join(OUT, f"{SYMBOL}_taker.csv"), "timestamp")
    if tk is not None:
        for col in ["buySellRatio", "buyVol", "sellVol"]:
            tk[col] = pd.to_numeric(tk[col], errors="coerce")
        out = pd.merge_asof(
            out,
            tk[["timestamp", "buySellRatio", "buyVol", "sellVol"]],
            left_on="time",
            right_on="timestamp",
            direction="backward",
        )
        out["taker_ratio"] = out["buySellRatio"].fillna(1.0)
        out["taker_buy"] = out["buyVol"].fillna(0.0)
        out["taker_sell"] = out["sellVol"].fillna(0.0)
        out = out.drop(columns=[c for c in ["timestamp", "buySellRatio", "buyVol", "sellVol"] if c in out.columns])
    else:
        out["taker_ratio"] = 1.0
        out["taker_buy"] = 0.0
        out["taker_sell"] = 0.0

    return out


def build_features(df, keep_unlabeled=False):
    c = df["close"].astype(float).reset_index(drop=True)
    h = df["high"].astype(float).reset_index(drop=True)
    l = df["low"].astype(float).reset_index(drop=True)
    o = df["open"].astype(float).reset_index(drop=True)
    v = df["volume"].astype(float).reset_index(drop=True)
    n = len(df)
    ret = c.pct_change()
    F = pd.DataFrame({"time": df["time"].values})

    for p in range(1, 31):
        F[f"rl{p}"] = ret.shift(p)

    for minutes in [10, 20, 40, 60, 120, 240, 480, 960]:
        p = bars(minutes)
        e = pd.Series(ema_np(c.values, p))
        F[f"pre_{minutes}m"] = c / e - 1
        ref = e.shift(bars(10))
        F[f"esl_{minutes}m"] = (e - ref) / ref.abs().replace(0, np.nan)

    for p in [5, 14, 21, 35]:
        F[f"rsi{p}"] = rsi_np(c.values, p)

    for minutes in [4, 10, 20, 40, 80]:
        p = bars(minutes)
        F[f"roc_{minutes}m"] = c.pct_change(p)

    e12 = pd.Series(ema_np(c.values, bars(24)))
    e26 = pd.Series(ema_np(c.values, bars(52)))
    macd = e12 - e26
    sig = pd.Series(ema_np(macd.values, bars(18)))
    hist = macd - sig
    F["macd_h"] = hist
    F["macd_d"] = hist.diff()
    F["macd_s10m"] = hist.rolling(bars(10), min_periods=bars(10)).sum()

    bb_period = bars(40)
    bb_mid = c.rolling(bb_period, min_periods=bb_period).mean()
    bb_std = c.rolling(bb_period, min_periods=bb_period).std(ddof=0)
    bbu = bb_mid + 2 * bb_std
    bbl = bb_mid - 2 * bb_std
    F["bbp"] = (c - bbl) / (bbu - bbl).replace(0, np.nan)
    F["bbw"] = (bbu - bbl) / bb_mid.replace(0, np.nan)

    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1).fillna(h - l)
    atr = pd.Series(ema_np(tr.values, bars(28)))
    atr_slow = pd.Series(ema_np(tr.values, bars(100)))
    F["atrp"] = atr / c
    F["atr_exp"] = atr / atr_slow.replace(0, np.nan)

    vol_ma = v.rolling(bars(40), min_periods=bars(40)).mean()
    F["vr"] = v / vol_ma.replace(0, np.nan)

    obv = np.zeros(n, dtype=np.float64)
    cv = c.values
    vv = v.values
    for i in range(1, n):
        obv[i] = obv[i - 1] + (vv[i] if cv[i] > cv[i - 1] else (-vv[i] if cv[i] < cv[i - 1] else 0))
    obv_ema = pd.Series(ema_np(obv, bars(40)))
    F["obv_sl"] = (pd.Series(obv) - obv_ema) / obv_ema.abs().replace(0, np.nan)

    body = (c - o).abs()
    full = (h - l).clip(lower=0.01)
    F["br"] = body / full
    F["bull"] = (c > o).astype(float)
    consec = np.zeros(n, dtype=np.float64)
    bull = (c > o).to_numpy()
    for i in range(1, n):
        if bull[i] == bull[i - 1]:
            consec[i] = consec[i - 1] + 1
    F["consec"] = consec

    for minutes in [12, 24, 60, 120]:
        p = bars(minutes)
        F[f"mom_{minutes}m"] = c.pct_change(p)

    for minutes in [20, 40, 100, 240]:
        p = bars(minutes)
        hp = h.rolling(p, min_periods=p).max()
        lp = l.rolling(p, min_periods=p).min()
        F[f"hlp_{minutes}m"] = (c - lp) / (hp - lp).replace(0, np.nan)
        F[f"rng_{minutes}m"] = (hp - lp) / c

    for name, minutes in [("1h", 60), ("4h", 240), ("24h", 1440)]:
        p = bars(minutes)
        prev = c.shift(p)
        hp = h.rolling(p, min_periods=p).max()
        lp = l.rolling(p, min_periods=p).min()
        F[f"htf_ret_{name}"] = (c - prev) / prev.replace(0, np.nan)
        F[f"htf_pos_{name}"] = (c - lp) / (hp - lp).replace(0, np.nan)
        F[f"htf_rng_{name}"] = (hp - lp) / c

    for minutes in [20, 40, 100]:
        p = bars(minutes)
        F[f"vreg_{minutes}m"] = ret.rolling(p, min_periods=p).std(ddof=0)

    e10 = pd.Series(ema_np(c.values, bars(10)))
    e20 = pd.Series(ema_np(c.values, bars(20)))
    e40 = pd.Series(ema_np(c.values, bars(40)))
    e100 = pd.Series(ema_np(c.values, bars(100)))
    ema_stack = np.zeros(n, dtype=np.float64)
    up_stack = (e10 >= e20) & (e20 >= e40) & (e40 >= e100)
    down_stack = (e10 <= e20) & (e20 <= e40) & (e40 <= e100)
    ema_stack[up_stack.to_numpy()] = 1
    ema_stack[down_stack.to_numpy()] = -1
    F["ema_stack"] = ema_stack

    for minutes in [12, 24, 60]:
        F[f"trend_{minutes}m"] = c.pct_change(bars(minutes))

    t = pd.to_datetime(df["time"], utc=True)
    hours = t.dt.hour.to_numpy()
    F["h_sin"] = np.sin(2 * np.pi * hours / 24)
    F["h_cos"] = np.cos(2 * np.pi * hours / 24)

    for col in ["funding_rate", "ls_ratio", "ls_long", "ls_short", "taker_ratio", "taker_buy", "taker_sell"]:
        F[col] = pd.to_numeric(df[col], errors="coerce") if col in df.columns else 0.0
    F["fund_rate_ema"] = pd.Series(ema_np(F["funding_rate"].fillna(0).values, bars(30)))
    F["ls_delta_30m"] = F["ls_ratio"] - F["ls_ratio"].shift(bars(30))
    F["taker_delta_30m"] = F["taker_ratio"] - F["taker_ratio"].shift(bars(30))

    short = np.sign(F["trend_12m"].fillna(0)) + np.sign(F["trend_24m"].fillna(0)) + np.sign(F["trend_60m"].fillna(0)) + F["ema_stack"].fillna(0)
    htf = np.sign(F["htf_ret_1h"].fillna(0)) + np.sign(F["htf_ret_4h"].fillna(0)) + np.where(F["htf_pos_4h"].fillna(0.5) > 0.65, 1, np.where(F["htf_pos_4h"].fillna(0.5) < 0.35, -1, 0))
    F["trend_score"] = short
    F["htf_score"] = htf

    target = np.full(n, np.nan)
    future = c.shift(-HORIZON)
    target[:-HORIZON] = np.where(future.iloc[:-HORIZON] > c.iloc[:-HORIZON], 1, np.where(future.iloc[:-HORIZON] < c.iloc[:-HORIZON], 0, np.nan))
    F["target"] = target
    if keep_unlabeled:
        feature_columns = [c for c in F.columns if c != "target"]
        return F.dropna(subset=feature_columns).reset_index(drop=True)
    return F.dropna().reset_index(drop=True)


def feature_cols(fdf):
    return [c for c in fdf.columns if c not in ["time", "target"]]


def make_models():
    models = [
        XGBClassifier(
            n_estimators=120,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.75,
            colsample_bytree=0.65,
            reg_alpha=1.0,
            reg_lambda=2.0,
            min_child_weight=30,
            tree_method="hist",
            eval_metric="logloss",
            n_jobs=1,
            verbosity=0,
            random_state=42,
        ),
        XGBClassifier(
            n_estimators=150,
            max_depth=3,
            learning_rate=0.04,
            subsample=0.8,
            colsample_bytree=0.7,
            reg_alpha=0.5,
            reg_lambda=1.5,
            min_child_weight=25,
            tree_method="hist",
            eval_metric="logloss",
            n_jobs=1,
            verbosity=0,
            random_state=123,
        ),
    ]
    if LGBMClassifier is not None:
        models.append(
            LGBMClassifier(
                n_estimators=160,
                max_depth=4,
                learning_rate=0.04,
                subsample=0.75,
                colsample_bytree=0.65,
                reg_alpha=0.8,
                reg_lambda=1.8,
                min_child_samples=35,
                n_jobs=1,
                random_state=77,
                verbose=-1,
            )
        )
    return models


def cache_path(fdf):
    cols_hash = hashlib.sha1("|".join(feature_cols(fdf)).encode("utf-8")).hexdigest()[:10]
    last_time = str(fdf["time"].iloc[-1]).replace(":", "-").replace(" ", "_")
    return os.path.join(
        CACHE_DIR,
        f"walkforward_BTC_2m_10min_tr{TRAIN_SIZE}_te{TEST_SIZE}_st{STEP}_n{len(fdf)}_c{cols_hash}_{last_time}.npz",
    )


EXTRA_COLS = [
    "rsi5", "rsi14", "rsi21", "rsi35", "atrp", "bbp", "bbw", "vr",
    "trend_score", "htf_score", "taker_ratio", "ls_ratio",
]


def load_cache(path):
    if not os.path.exists(path):
        return None
    try:
        data = np.load(path, allow_pickle=False)
        out = {k: data[k] for k in data.files}
        out["time"] = out["time"].astype(str)
        print(f"[2m] cache hit: {path}")
        return out
    except Exception as e:
        print(f"[2m] cache ignored: {e}")
        return None


def save_cache(path, preds):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savez_compressed(path, **preds)


def collect_predictions(fdf):
    fdf = fdf[fdf["target"].isin([0, 1])].reset_index(drop=True)
    path = cache_path(fdf)
    cached = load_cache(path)
    if cached is not None:
        return cached
    cols = feature_cols(fdf)
    X = fdf[cols].to_numpy(dtype=np.float32)
    y = fdf["target"].astype(int).to_numpy()
    chunks = []
    i = TRAIN_SIZE
    while i + TEST_SIZE <= len(fdf):
        t0 = time.time()
        Xtr, ytr = X[i - TRAIN_SIZE:i], y[i - TRAIN_SIZE:i]
        Xte, yte = X[i:i + TEST_SIZE], y[i:i + TEST_SIZE]
        probs = []
        for model in make_models():
            model.fit(Xtr, ytr)
            probs.append(model.predict_proba(Xte)[:, 1])
        probs = np.vstack(probs).T
        votes = (probs >= 0.5).astype(int)
        chunk = {
            "time": fdf["time"].iloc[i:i + TEST_SIZE].astype(str).to_numpy(),
            "y": yte.astype(np.int8),
            "avg": probs.mean(axis=1).astype(np.float32),
            "vote_sum": votes.sum(axis=1).astype(np.int8),
            "agree_all": ((votes[:, 0] == votes[:, 1]) & (votes[:, 1] == votes[:, 2] if votes.shape[1] > 2 else votes[:, 0] == votes[:, 1])).astype(bool),
        }
        for col in EXTRA_COLS:
            chunk[col] = fdf[col].iloc[i:i + TEST_SIZE].to_numpy(dtype=np.float32)
        chunks.append(chunk)
        print(f"[2m] window {i}-{i + TEST_SIZE} done in {time.time() - t0:.1f}s")
        i += STEP
    if not chunks:
        raise RuntimeError("not enough rows for walk-forward")
    preds = {}
    for key in chunks[0]:
        preds[key] = np.concatenate([chunk[key] for chunk in chunks])
    save_cache(path, preds)
    print(f"[2m] cache saved: {path}")
    return preds


def market_align_score(direction, preds):
    up = direction == 1
    score = np.zeros(len(direction), dtype=np.int16)
    trend = preds["trend_score"].astype(float)
    htf = preds["htf_score"].astype(float)
    taker = preds["taker_ratio"].astype(float)
    score += np.where((up & (trend > 0)) | (~up & (trend < 0)), 1, 0)
    score += np.where((up & (htf > 0)) | (~up & (htf < 0)), 1, 0)
    score += np.where((up & (taker > 1.05)) | (~up & (taker < 0.95)), 1, 0)
    return score


def block_summary(wins, mask, times, blocks=10):
    out = []
    n = len(wins)
    block_size = max(1, n // blocks)
    for bi in range(blocks):
        a = bi * block_size
        b = n if bi == blocks - 1 else min(n, (bi + 1) * block_size)
        if a >= n:
            break
        m = mask[a:b]
        selected_times = times[a:b][m]
        first = selected_times[0] if len(selected_times) else times[a]
        last = selected_times[-1] if len(selected_times) else times[b - 1]
        row = metric(wins[a:b][m], first, last)
        row["slice"] = f"block_{bi + 1:02d}"
        row["start"] = str(times[a])
        row["end"] = str(times[b - 1])
        out.append(row)
    active = [b for b in out if b["trades"] > 0]
    return {
        "active_blocks": len(active),
        "positive_blocks": sum(1 for b in active if b["pnl_5u"] > 0),
        "min_block_wr": round(min((b["wr"] for b in active), default=0.0), 2),
        "worst_block": min(active, key=lambda b: b["wr"])["slice"] if active else None,
        "blocks": out,
    }


def apply_filters(mask, direction, preds, cand):
    if cand.get("rsi"):
        lo, hi = cand["rsi"]
        rsi = preds[cand.get("rsi_col", "rsi14")].astype(float)
        mask &= (rsi < lo) | (rsi > hi)
    if cand.get("bbp_extreme"):
        lo, hi = cand["bbp_extreme"]
        bbp = preds["bbp"].astype(float)
        mask &= (bbp < lo) | (bbp > hi)
    if cand.get("vol_min_rank") is not None:
        atr = preds["atrp"].astype(float)
        rank = pd.Series(atr).rank(pct=True).to_numpy()
        mask &= rank >= float(cand["vol_min_rank"])
    trend = preds["trend_score"].astype(float)
    if cand.get("trend_mode") == "align":
        mask &= np.where(direction == 1, trend >= 0, trend <= 0)
    elif cand.get("trend_mode") == "strong_align":
        mask &= np.where(direction == 1, trend >= 2, trend <= -2)
    elif cand.get("trend_mode") == "no_strong_counter":
        mask &= ~np.where(direction == 1, trend <= -3, trend >= 3)
    if cand.get("market_score_min") is not None:
        mask &= market_align_score(direction, preds) >= int(cand["market_score_min"])
    if cand.get("skip_hours_utc"):
        hours = pd.to_datetime(preds["time"], utc=True).hour.to_numpy()
        mask &= ~np.isin(hours, np.asarray(cand["skip_hours_utc"], dtype=int))
    return mask


def evaluate_ml(preds, cand):
    y = preds["y"].astype(int)
    avg = preds["avg"].astype(float)
    vote_sum = preds["vote_sum"].astype(int)
    if cand["agree_mode"] == "all3":
        direction = (avg >= 0.5).astype(int)
        mask = preds["agree_all"].astype(bool)
    else:
        direction = (vote_sum >= 2).astype(int)
        mask = np.ones(len(y), dtype=bool)
    th = float(cand["threshold"])
    mask &= (avg >= th) | (avg <= (1 - th))
    mask = apply_filters(mask, direction, preds, cand)
    return finish_eval(preds, cand, direction, mask)


def evaluate_rule(preds, cand):
    n = len(preds["y"])
    direction = np.full(n, -1, dtype=np.int8)
    if cand["kind"] == "rule_rsi_reversal":
        rsi = preds[cand.get("rsi_col", "rsi14")].astype(float)
        lo, hi = cand["rsi"]
        direction = np.where(rsi < lo, 1, np.where(rsi > hi, 0, -1))
    elif cand["kind"] == "rule_bbp_reversal":
        bbp = preds["bbp"].astype(float)
        lo, hi = cand["bbp_extreme"]
        direction = np.where(bbp < lo, 1, np.where(bbp > hi, 0, -1))
    elif cand["kind"] == "rule_trend_follow":
        trend = preds["trend_score"].astype(float)
        score_min = float(cand["score_min"])
        direction = np.where(trend >= score_min, 1, np.where(trend <= -score_min, 0, -1))
    mask = direction >= 0
    mask = apply_filters(mask, direction, preds, {k: v for k, v in cand.items() if k not in ["rsi", "bbp_extreme"]})
    return finish_eval(preds, cand, direction, mask)


def finish_eval(preds, cand, direction, mask):
    y = preds["y"].astype(int)
    wins_all = direction == y
    times = preds["time"].astype(str)
    selected_times = times[mask]
    first = selected_times[0] if len(selected_times) else times[0]
    last = selected_times[-1] if len(selected_times) else times[-1]
    overall = metric(wins_all[mask], first, last)
    blocks = block_summary(wins_all, mask, times)
    score = (
        overall["pnl_5u"]
        + overall["wr"] * 3
        + min(overall["trades"], 2500) * 0.18
        + blocks["positive_blocks"] * 45
        + blocks["min_block_wr"] * 1.5
        - overall["max_loss"] * 9
    )
    family = cand.get("family") or ("rule" if cand.get("kind", "").startswith("rule") else ("ml_rsi" if cand.get("rsi") else "ml_no_rsi"))
    return {
        "name": cand["name"],
        "family": family,
        "candidate": cand,
        "overall": overall,
        "time_block_summary": {k: v for k, v in blocks.items() if k != "blocks"},
        "time_blocks": blocks["blocks"],
        "score": round(float(score), 2),
    }


def candidate_grid():
    current_skip = [5, 10, 13, 14, 15, 19, 22, 23]
    cands = []
    for th in [0.53, 0.55, 0.58, 0.60, 0.62, 0.65, 0.68, 0.70]:
        for agree in ["majority", "all3"]:
            base = {"kind": "ml", "threshold": th, "agree_mode": agree}
            variants = [
                ({}, "none"),
                ({"trend_mode": "align"}, "trend_align"),
                ({"trend_mode": "no_strong_counter"}, "no_strong_counter"),
                ({"market_score_min": 1}, "mkt1"),
                ({"market_score_min": 2}, "mkt2"),
                ({"vol_min_rank": 0.55}, "vol55"),
                ({"skip_hours_utc": current_skip}, "current_skip"),
            ]
            for extra, suffix in variants:
                cand = {**base, **extra}
                cand["name"] = f"ml_no_rsi_th{int(th*100)}_{agree}_{suffix}"
                cand["family"] = "ml_no_rsi"
                cands.append(cand)
            for rsi_col in ["rsi14", "rsi35"]:
                for lo, hi in [(30, 70), (35, 65), (40, 60)]:
                    for extra, suffix in [
                        ({}, "none"),
                        ({"trend_mode": "no_strong_counter"}, "no_strong_counter"),
                        ({"market_score_min": 1}, "mkt1"),
                    ]:
                        cand = {**base, **extra, "rsi": (lo, hi), "rsi_col": rsi_col}
                        cand["name"] = f"ml_{rsi_col}_rsi{lo}_{hi}_th{int(th*100)}_{agree}_{suffix}"
                        cand["family"] = "ml_rsi"
                        cands.append(cand)
    for rsi_col in ["rsi14", "rsi35"]:
        for lo, hi in [(25, 75), (30, 70), (35, 65), (40, 60)]:
            for extra, suffix in [({}, "none"), ({"trend_mode": "no_strong_counter"}, "no_strong_counter"), ({"market_score_min": 1}, "mkt1")]:
                cands.append({
                    "name": f"rule_{rsi_col}_rsi_rev_{lo}_{hi}_{suffix}",
                    "kind": "rule_rsi_reversal",
                    "family": "rule_rsi",
                    "rsi": (lo, hi),
                    "rsi_col": rsi_col,
                    **extra,
                })
    for lo, hi in [(0.08, 0.92), (0.12, 0.88), (0.2, 0.8)]:
        cands.append({
            "name": f"rule_bbp_rev_{int(lo*100)}_{int(hi*100)}",
            "kind": "rule_bbp_reversal",
            "family": "rule_no_rsi",
            "bbp_extreme": (lo, hi),
        })
    for score_min in [1, 2, 3, 4]:
        cands.append({
            "name": f"rule_trend_follow_score{score_min}",
            "kind": "rule_trend_follow",
            "family": "rule_no_rsi",
            "score_min": score_min,
        })
    return cands


def trim(rows, family=None, min_trades=80, limit=12, key=None):
    use = [r for r in rows if r["overall"]["trades"] >= min_trades]
    if family:
        use = [r for r in use if r["family"] == family]
    if key is None:
        key = lambda r: r["score"]
    return sorted(use, key=key, reverse=True)[:limit]


def main():
    t0 = time.time()
    one = load_1m(SYMBOL)
    two = merge_external(aggregate_bars(one))
    fdf = build_features(two)
    preds = collect_predictions(fdf)
    rows = []
    for cand in candidate_grid():
        if cand.get("kind") == "ml":
            rows.append(evaluate_ml(preds, cand))
        else:
            rows.append(evaluate_rule(preds, cand))
    rows.sort(key=lambda r: r["score"], reverse=True)
    times = preds["time"].astype(str)
    report = {
        "method": {
            "type": "one_min_to_two_min_10min_binary_research",
            "symbol": SYMBOL.upper(),
            "bar_min": BAR_MIN,
            "option_min": OPTION_MIN,
            "horizon_bars": HORIZON,
            "payout": PAYOUT,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "train_size": TRAIN_SIZE,
            "test_size": TEST_SIZE,
            "step": STEP,
            "model_note": "Rolling walk-forward OOS; two XGBoost models plus LightGBM when available; n_jobs=1.",
            "rsi_note": "RSI is tested as a filter and as a standalone rule, not assumed mandatory.",
        },
        "data": {
            "one_min_rows": int(len(one)),
            "two_min_rows": int(len(two)),
            "feature_rows": int(len(fdf)),
            "oos_rows": int(len(preds["y"])),
            "one_min_start": str(one["open_time"].iloc[0]),
            "one_min_end": str(one["open_time"].iloc[-1]),
            "oos_start": str(times[0]),
            "oos_end": str(times[-1]),
        },
        "summary": {
            "top_balanced": trim(rows, min_trades=120, limit=15),
            "top_high_wr": trim(rows, min_trades=80, limit=15, key=lambda r: (r["overall"]["wr"], r["overall"]["trades"])),
            "top_profitable_trade_count": trim([r for r in rows if r["overall"]["pnl_5u"] > 0], min_trades=120, limit=15, key=lambda r: (r["overall"]["trades"], r["overall"]["wr"])),
            "top_ml_no_rsi": trim(rows, family="ml_no_rsi", min_trades=120, limit=12),
            "top_ml_rsi": trim(rows, family="ml_rsi", min_trades=120, limit=12),
            "top_rule_no_rsi": trim(rows, family="rule_no_rsi", min_trades=80, limit=8),
            "top_rule_rsi": trim(rows, family="rule_rsi", min_trades=80, limit=8),
        },
        "all_candidates_tested": len(rows),
        "runtime_sec": round(time.time() - t0, 1),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "saved": REPORT_FILE,
        "oos_rows": report["data"]["oos_rows"],
        "candidates": len(rows),
        "top_balanced": [
            {
                "name": r["name"],
                "family": r["family"],
                "wr": r["overall"]["wr"],
                "trades": r["overall"]["trades"],
                "trades_per_day": r["overall"]["trades_per_day"],
                "pnl_5u": r["overall"]["pnl_5u"],
                "max_loss": r["overall"]["max_loss"],
            }
            for r in report["summary"]["top_balanced"][:8]
        ],
        "runtime_sec": report["runtime_sec"],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
