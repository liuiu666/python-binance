"""Search 2m POC-normal gap30 parameters and simple market-data filters.

The goal is to test whether tuning the normal-distribution parameters and
adding volume/taker/long-short/funding filters improves the current
2m + 30-minute spacing candidate without mixing incompatible sample windows.
"""
import json
import os

import numpy as np
import pandas as pd
from scipy.stats import norm


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_FILE = os.path.join(DATA_DIR, "poc_2m_filter_search.json")

PAYOUT = 0.85
STAKE = 5.0
BREAKEVEN_WR = 100 / (1 + PAYOUT)


def load_1m(name):
    path = os.path.join(DATA_DIR, name)
    df = pd.read_csv(path, parse_dates=["open_time"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return (
        df.dropna(subset=["open_time", "close"])
        .drop_duplicates("open_time")
        .sort_values("open_time")
        .reset_index(drop=True)
    )


def read_external(path, time_col):
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df[time_col] = pd.to_datetime(df[time_col], utc=True, errors="coerce")
    return df.dropna(subset=[time_col]).sort_values(time_col).reset_index(drop=True)


def aggregate_2m(df1):
    d = df1.copy()
    d["time"] = pd.to_datetime(d["open_time"], utc=True).dt.floor("2min")
    bars = d.groupby("time", as_index=False).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    bars = bars.dropna().reset_index(drop=True)
    bars["ret"] = np.log(bars["close"] / bars["close"].shift(1)).fillna(0.0)
    bars["vr60"] = bars["volume"] / bars["volume"].rolling(30).mean()
    bars["vr60_rank"] = bars["vr60"].rolling(720, min_periods=60).rank(pct=True)
    bars["range_pct"] = (bars["high"] - bars["low"]) / bars["close"]
    bars["range_rank"] = bars["range_pct"].rolling(720, min_periods=60).rank(pct=True)
    return bars


def merge_external(bars):
    out = bars.copy()
    taker = read_external(os.path.join(DATA_DIR, "btcusdt_taker.csv"), "timestamp")
    if taker is not None:
        taker["buySellRatio"] = pd.to_numeric(taker["buySellRatio"], errors="coerce")
        out = pd.merge_asof(out, taker[["timestamp", "buySellRatio"]], left_on="time", right_on="timestamp", direction="backward")
        out["taker_age_min"] = (out["time"] - out["timestamp"]).dt.total_seconds() / 60
        out["taker_ratio"] = out["buySellRatio"].where(out["taker_age_min"] <= 30)
    else:
        out["taker_ratio"] = np.nan

    ls = read_external(os.path.join(DATA_DIR, "btcusdt_lsratio.csv"), "timestamp")
    if ls is not None:
        ls["longShortRatio"] = pd.to_numeric(ls["longShortRatio"], errors="coerce")
        out = pd.merge_asof(out, ls[["timestamp", "longShortRatio"]], left_on="time", right_on="timestamp", direction="backward", suffixes=("", "_ls"))
        ls_time_col = "timestamp_ls" if "timestamp_ls" in out.columns else "timestamp"
        out["ls_age_min"] = (out["time"] - out[ls_time_col]).dt.total_seconds() / 60
        out["ls_ratio"] = out["longShortRatio"].where(out["ls_age_min"] <= 30)
    else:
        out["ls_ratio"] = np.nan

    fund = read_external(os.path.join(DATA_DIR, "btcusdt_funding.csv"), "fundingTime")
    if fund is not None:
        fund["fundingRate"] = pd.to_numeric(fund["fundingRate"], errors="coerce")
        out = pd.merge_asof(out, fund[["fundingTime", "fundingRate"]], left_on="time", right_on="fundingTime", direction="backward")
        out["funding_age_h"] = (out["time"] - out["fundingTime"]).dt.total_seconds() / 3600
        out["funding_rate"] = out["fundingRate"].where(out["funding_age_h"] <= 12)
    else:
        out["funding_rate"] = np.nan
    return out


def max_loss(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return int(best)


def metric(trades):
    n = int(len(trades))
    if n == 0:
        return {"trades": 0, "wins": 0, "wr": 0.0, "pnl_5u": 0.0, "max_loss": 0, "min_block_wr": None}
    wins = int(trades["win"].sum())
    wr = wins / n * 100
    pnl = wins * STAKE * PAYOUT - (n - wins) * STAKE
    chunks = np.array_split(np.arange(n), min(10, n))
    block_wrs = []
    for chunk in chunks:
        part = trades.iloc[chunk]
        block_wrs.append(float(part["win"].mean() * 100))
    times = pd.to_datetime(trades["time"], utc=True)
    day_counts = times.dt.date.value_counts()
    return {
        "trades": n,
        "wins": wins,
        "wr": round(wr, 2),
        "edge": round(wr - BREAKEVEN_WR, 2),
        "pnl_5u": round(pnl, 2),
        "max_loss": max_loss(trades["win"].tolist()),
        "min_block_wr": round(min(block_wrs), 2) if block_wrs else None,
        "max_day": int(day_counts.max()) if len(day_counts) else 0,
        "p90_day": round(float(day_counts.quantile(0.90)), 2) if len(day_counts) else 0.0,
        "start": str(times.min()) if len(times) else None,
        "end": str(times.max()) if len(times) else None,
    }


def base_trades(bars, lookback_min, tail_pct, gap_min=30, horizon_min=10):
    lookback = int(round(lookback_min / 2))
    horizon = int(round(horizon_min / 2))
    gap = int(round(gap_min / 2))
    close = bars["close"].astype(float).to_numpy()
    ret = pd.Series(np.log(close / np.roll(close, 1)))
    ret.iloc[0] = 0.0
    mu = ret.rolling(lookback).mean().to_numpy(copy=True)
    sigma = ret.rolling(lookback).std().to_numpy(copy=True)
    sigma[sigma == 0] = np.nan
    p_up = norm.cdf(np.sqrt(horizon) * (mu / sigma))
    target = 1.0 - tail_pct
    raw_up = (1.0 - p_up) >= target
    raw_down = p_up >= target
    valid = np.ones(len(bars), dtype=bool)
    valid[:lookback] = False
    valid[-horizon:] = False
    rows = []
    next_allowed = 0
    for i in range(len(bars)):
        if i < next_allowed or not valid[i]:
            continue
        direction = None
        if raw_up[i]:
            direction = "UP"
            win = close[i + horizon] > close[i]
        elif raw_down[i]:
            direction = "DOWN"
            win = close[i + horizon] < close[i]
        if direction is None:
            continue
        row = bars.iloc[i].to_dict()
        row.update({"direction": direction, "win": bool(win), "p_up": float(p_up[i])})
        rows.append(row)
        next_allowed = i + gap
    return pd.DataFrame(rows)


def apply_filter(trades, filter_name):
    if trades.empty:
        return trades
    t = trades.copy()
    direction = t["direction"].astype(str)
    if filter_name == "none":
        mask = np.ones(len(t), dtype=bool)
    elif filter_name == "low_volume":
        mask = t["vr60_rank"].fillna(0.5) <= 0.50
    elif filter_name == "not_high_volume":
        mask = t["vr60_rank"].fillna(0.5) <= 0.80
    elif filter_name == "not_high_range":
        mask = t["range_rank"].fillna(0.5) <= 0.80
    elif filter_name == "low_volume_not_high_range":
        mask = (t["vr60_rank"].fillna(0.5) <= 0.50) & (t["range_rank"].fillna(0.5) <= 0.80)
    elif filter_name == "taker_align":
        tr = t["taker_ratio"]
        mask = tr.notna() & (((direction == "UP") & (tr >= 1.05)) | ((direction == "DOWN") & (tr <= 0.95)))
    elif filter_name == "taker_not_counter":
        tr = t["taker_ratio"]
        mask = tr.notna() & ~(((direction == "UP") & (tr < 0.85)) | ((direction == "DOWN") & (tr > 1.15)))
    elif filter_name == "ls_contrarian":
        ls = t["ls_ratio"]
        mask = ls.notna() & (((direction == "UP") & (ls <= 0.70)) | ((direction == "DOWN") & (ls >= 1.30)))
    elif filter_name == "funding_contrarian":
        fr = t["funding_rate"]
        mask = fr.notna() & (((direction == "UP") & (fr <= -0.00002)) | ((direction == "DOWN") & (fr >= 0.00002)))
    elif filter_name == "flow_not_counter_lowvol":
        tr = t["taker_ratio"]
        mask = (
            tr.notna()
            &
            ~(((direction == "UP") & (tr < 0.85)) | ((direction == "DOWN") & (tr > 1.15)))
            & (t["vr60_rank"].fillna(0.5) <= 0.80)
        )
    else:
        raise ValueError(filter_name)
    return t[mask.fillna(False) if hasattr(mask, "fillna") else mask].reset_index(drop=True)


def run_dataset(file_name):
    df1 = load_1m(file_name)
    bars = merge_external(aggregate_2m(df1))
    lookbacks = [40, 50, 60, 75, 90]
    tails = [0.15, 0.18, 0.20, 0.22, 0.25]
    filters = [
        "none",
        "low_volume",
        "not_high_volume",
        "not_high_range",
        "low_volume_not_high_range",
        "taker_align",
        "taker_not_counter",
        "ls_contrarian",
        "funding_contrarian",
        "flow_not_counter_lowvol",
    ]
    rows = []
    for lookback in lookbacks:
        for tail in tails:
            base = base_trades(bars, lookback, tail)
            for filt in filters:
                filtered = apply_filter(base, filt)
                rows.append({
                    "dataset": file_name,
                    "lookback": lookback,
                    "tail_pct": tail,
                    "filter": filt,
                    **metric(filtered),
                })
    return {
        "file": file_name,
        "rows_1m": int(len(df1)),
        "start": str(df1["open_time"].iloc[0]),
        "end": str(df1["open_time"].iloc[-1]),
        "results": rows,
    }


def top(rows, min_trades=120, max_loss=None, limit=20):
    selected = [r for r in rows if r["trades"] >= min_trades]
    if max_loss is not None:
        selected = [r for r in selected if r["max_loss"] <= max_loss]
    selected.sort(key=lambda r: (r["pnl_5u"], r["wr"], -r["max_loss"]), reverse=True)
    return selected[:limit]


def top_wr(rows, min_trades=100, max_loss=None, limit=20):
    selected = [r for r in rows if r["trades"] >= min_trades]
    if max_loss is not None:
        selected = [r for r in selected if r["max_loss"] <= max_loss]
    selected.sort(key=lambda r: (r["wr"], r["pnl_5u"], -r["max_loss"]), reverse=True)
    return selected[:limit]


def main():
    datasets = ["btcusdt_1m.csv"]
    if os.path.exists(os.path.join(DATA_DIR, "btcusdt_1m_180d.csv")):
        datasets.append("btcusdt_1m_180d.csv")
    payload = {
        "method": {
            "type": "poc_2m_gap30_parameter_and_filter_search",
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Taker/long-short filters only apply where external data is available.",
        },
        "datasets": [],
    }
    for name in datasets:
        ds = run_dataset(name)
        rows = ds["results"]
        ds["top_pnl_low_streak"] = top(rows, min_trades=120, max_loss=5)
        ds["top_wr_usable"] = top_wr(rows, min_trades=100, max_loss=5)
        payload["datasets"].append(ds)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"Wrote {OUT_FILE}")
    for ds in payload["datasets"]:
        print(f"\n{ds['file']} {ds['start']} -> {ds['end']}")
        print("TOP PNL LOW STREAK")
        for r in ds["top_pnl_low_streak"][:10]:
            print(f"lb={r['lookback']:>2} tail={r['tail_pct']:.2f} {r['filter']:24s} n={r['trades']:4d} wr={r['wr']:5.2f}% pnl={r['pnl_5u']:7.2f} maxL={r['max_loss']} minBlk={r['min_block_wr']} maxDay={r['max_day']}")
        print("TOP WR USABLE")
        for r in ds["top_wr_usable"][:10]:
            print(f"lb={r['lookback']:>2} tail={r['tail_pct']:.2f} {r['filter']:24s} n={r['trades']:4d} wr={r['wr']:5.2f}% pnl={r['pnl_5u']:7.2f} maxL={r['max_loss']} minBlk={r['min_block_wr']} maxDay={r['max_day']}")


if __name__ == "__main__":
    main()
