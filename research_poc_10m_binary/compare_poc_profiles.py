"""Compare clean POC-normal reversal profiles on 1m and aggregated 2m bars.

This script is intentionally separate from the exploratory research scripts so
the production candidate can be retested with one consistent metric format.
"""
import json
import os
import glob

import numpy as np
import pandas as pd
from scipy.stats import norm


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_FILE = os.path.join(DATA_DIR, "poc_profile_comparison.json")

PAYOUT = 0.85
STAKE = 5.0
BREAKEVEN_WR = 100 / (1 + PAYOUT)


PROFILES = [
    {
        "name": "safe_1m_sma60_tail20_cd10",
        "source_minutes": 1,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "note": "Current recommended production profile.",
    },
    {
        "name": "sniper_1m_sma60_tail15_cd10",
        "source_minutes": 1,
        "lookback_minutes": 60,
        "tail_pct": 0.15,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "note": "Higher win-rate, fewer trades.",
    },
    {
        "name": "aggressive_1m_sma60_tail20_cd5",
        "source_minutes": 1,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 5,
        "note": "Higher turnover; allows overlapping 10m exposure.",
    },
    {
        "name": "safe_2m_sma60_tail20_cd10",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "note": "Same 60-minute lookback after aggregating to 2m bars.",
    },
    {
        "name": "safe_2m_cap6",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "daily_trade_cap": 6,
        "note": "Safe 2m with a maximum of 6 trades per UTC day.",
    },
    {
        "name": "safe_2m_cap4",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "daily_trade_cap": 4,
        "note": "Safe 2m with a maximum of 4 trades per UTC day.",
    },
    {
        "name": "safe_2m_lossstop2",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "daily_loss_stop": 2,
        "note": "Safe 2m stops for the UTC day after 2 selected losses.",
    },
    {
        "name": "safe_2m_cap6_lossstop2",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "daily_trade_cap": 6,
        "daily_loss_stop": 2,
        "note": "Safe 2m with daily cap 6 and day stop after 2 selected losses.",
    },
    {
        "name": "safe_2m_gap20",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "min_gap_minutes": 20,
        "note": "Safe 2m with at least 20 minutes between selected trades.",
    },
    {
        "name": "safe_2m_gap30",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "min_gap_minutes": 30,
        "note": "Safe 2m with at least 30 minutes between selected trades.",
    },
    {
        "name": "safe_2m_gap30_ml58",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "min_gap_minutes": 30,
        "confirm_threshold": 0.58,
        "note": "Safe 2m gap30 plus rolling OOS 2m model agreement at 58%.",
    },
    {
        "name": "safe_2m_gap30_ml62",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "min_gap_minutes": 30,
        "confirm_threshold": 0.62,
        "note": "Safe 2m gap30 plus rolling OOS 2m model agreement at 62%.",
    },
    {
        "name": "sniper_2m_sma60_tail15_cd10",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.15,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "note": "2m aggregated high win-rate candidate.",
    },
    {
        "name": "aggressive_2m_sma60_tail20_cd6",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 6,
        "note": "2m counterpart to short cooldown; rounded to 3 bars.",
    },
    {
        "name": "confirm_2m_sma60_tail20_cd10_ml58",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "confirm_threshold": 0.58,
        "note": "2m POC signal must agree with rolling OOS 2m model at 58%.",
    },
    {
        "name": "confirm_2m_sma60_tail20_cd10_ml62",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "confirm_threshold": 0.62,
        "note": "2m POC signal must agree with rolling OOS 2m model at 62%.",
    },
    {
        "name": "confirm_2m_sma60_tail20_cd10_ml65",
        "source_minutes": 2,
        "lookback_minutes": 60,
        "tail_pct": 0.20,
        "horizon_minutes": 10,
        "cooldown_minutes": 10,
        "confirm_threshold": 0.65,
        "note": "2m POC signal must agree with rolling OOS 2m model at 65%.",
    },
]


def load_1m(file_name):
    path = os.path.join(DATA_DIR, file_name)
    df = pd.read_csv(path, parse_dates=["open_time"])
    needed = ["open_time", "open", "high", "low", "close", "volume"]
    for col in needed[1:]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return (
        df.dropna(subset=["open_time", "close"])
        .drop_duplicates("open_time")
        .sort_values("open_time")
        .reset_index(drop=True)
    )


def aggregate_bars(df, minutes):
    if minutes == 1:
        out = df[["open_time", "close"]].copy()
        out["bar_minutes"] = 1
        return out

    d = df.copy()
    d["period"] = pd.to_datetime(d["open_time"], utc=True).dt.floor(f"{minutes}min")
    agg = d.groupby("period", as_index=False).agg(close=("close", "last"))
    agg = agg.rename(columns={"period": "open_time"})
    agg["bar_minutes"] = minutes
    return agg.dropna().reset_index(drop=True)


def load_confirmation_cache():
    paths = sorted(
        glob.glob(os.path.join(DATA_DIR, "cache", "walkforward_BTC_2m_10min*.npz")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not paths:
        return None, None
    data = np.load(paths[0], allow_pickle=True)
    frame = pd.DataFrame({
        "open_time": pd.to_datetime(data["time"].astype(str), utc=True),
        "prob_up": data["avg"].astype(float),
    })
    frame = frame.drop_duplicates("open_time").set_index("open_time").sort_index()
    return paths[0], frame


def max_loss_streak(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return int(best)


def block_metrics(trades, blocks=10):
    if trades.empty:
        return {"active_blocks": 0, "positive_blocks": 0, "min_block_wr": None}
    out = []
    indices = np.array_split(np.arange(len(trades)), min(blocks, len(trades)))
    for idx, chunk in enumerate(indices, start=1):
        part = trades.iloc[chunk]
        wins = int(part["win"].sum())
        total = int(len(part))
        wr = wins / total * 100 if total else 0.0
        out.append({
            "slice": f"block_{idx:02d}",
            "trades": total,
            "wins": wins,
            "wr": round(wr, 2),
            "pnl_5u": round(wins * STAKE * PAYOUT - (total - wins) * STAKE, 2),
            "max_loss": max_loss_streak(part["win"].tolist()),
        })
    positive = [b for b in out if b["pnl_5u"] > 0]
    return {
        "active_blocks": len(out),
        "positive_blocks": len(positive),
        "min_block_wr": round(min(b["wr"] for b in out), 2) if out else None,
        "blocks": out,
    }


def daily_distribution(trades):
    if trades.empty:
        return {
            "span_days": 0,
            "active_days": 0,
            "zero_days": 0,
            "avg_all_days": 0.0,
            "avg_active_days": 0.0,
            "median_active": 0.0,
            "p90_active": 0.0,
            "max_day": 0,
        }
    times = pd.to_datetime(trades["time"], utc=True)
    counts = times.dt.date.value_counts().sort_index()
    all_days = pd.date_range(min(counts.index), max(counts.index), freq="D").date
    counts = counts.reindex(all_days, fill_value=0)
    active = counts[counts > 0]
    return {
        "span_days": int(len(counts)),
        "active_days": int(len(active)),
        "zero_days": int((counts == 0).sum()),
        "avg_all_days": round(float(counts.mean()), 2),
        "avg_active_days": round(float(active.mean()), 2) if len(active) else 0.0,
        "median_active": round(float(active.median()), 2) if len(active) else 0.0,
        "p90_active": round(float(active.quantile(0.90)), 2) if len(active) else 0.0,
        "max_day": int(counts.max()) if len(counts) else 0,
    }


def run_profile(df1m, profile, confirmation=None):
    bar_min = int(profile["source_minutes"])
    df = aggregate_bars(df1m, bar_min)
    close = df["close"].astype(float).to_numpy()
    n = len(df)
    horizon = int(round(profile["horizon_minutes"] / bar_min))
    lookback = int(round(profile["lookback_minutes"] / bar_min))
    cooldown = max(1, int(round(profile["cooldown_minutes"] / bar_min)))

    log_returns = np.zeros(n)
    log_returns[1:] = np.log(close[1:] / close[:-1])
    ret = pd.Series(log_returns)
    mu = ret.rolling(lookback).mean().to_numpy(copy=True)
    sigma = ret.rolling(lookback).std().to_numpy(copy=True)
    sigma[sigma == 0] = np.nan

    z = np.sqrt(horizon) * (mu / sigma)
    p_up = norm.cdf(z)
    target_p = 1.0 - float(profile["tail_pct"])

    raw_call = (1.0 - p_up) >= target_p
    raw_put = p_up >= target_p
    valid = np.ones(n, dtype=bool)
    valid[:lookback] = False
    valid[-horizon:] = False

    actual_up = np.zeros(n, dtype=bool)
    actual_up[:-horizon] = close[horizon:] > close[:-horizon]
    actual_down = np.zeros(n, dtype=bool)
    actual_down[:-horizon] = close[horizon:] < close[:-horizon]

    rows = []
    next_allowed = 0
    min_gap = max(cooldown, int(round(float(profile.get("min_gap_minutes", profile["cooldown_minutes"])) / bar_min)))
    daily_trade_cap = profile.get("daily_trade_cap")
    daily_loss_stop = profile.get("daily_loss_stop")
    daily_counts = {}
    daily_losses = {}
    for i in range(n):
        if not valid[i] or i < next_allowed:
            continue
        ts = pd.to_datetime(df.loc[i, "open_time"], utc=True)
        day_key = ts.date().isoformat()
        if daily_trade_cap is not None and daily_counts.get(day_key, 0) >= int(daily_trade_cap):
            continue
        if daily_loss_stop is not None and daily_losses.get(day_key, 0) >= int(daily_loss_stop):
            continue
        direction = None
        win = None
        if raw_call[i]:
            direction = "UP"
            win = bool(actual_up[i])
        elif raw_put[i]:
            direction = "DOWN"
            win = bool(actual_down[i])
        if direction is None:
            continue
        confirm_threshold = profile.get("confirm_threshold")
        confirm_prob = None
        if confirm_threshold is not None:
            if confirmation is None:
                continue
            if ts not in confirmation.index:
                continue
            confirm_prob = float(confirmation.loc[ts, "prob_up"])
            if direction == "UP" and confirm_prob < float(confirm_threshold):
                continue
            if direction == "DOWN" and confirm_prob > 1.0 - float(confirm_threshold):
                continue
        rows.append({
            "time": df.loc[i, "open_time"],
            "direction": direction,
            "win": win,
            "p_up": float(p_up[i]),
            "confirm_prob": confirm_prob,
        })
        daily_counts[day_key] = daily_counts.get(day_key, 0) + 1
        if not win:
            daily_losses[day_key] = daily_losses.get(day_key, 0) + 1
        next_allowed = i + min_gap

    trades = pd.DataFrame(rows)
    total = int(len(trades))
    wins = int(trades["win"].sum()) if total else 0
    losses = total - wins
    pnl = wins * STAKE * PAYOUT - losses * STAKE
    wr = wins / total * 100 if total else 0.0
    days = max(
        1e-9,
        (pd.to_datetime(df["open_time"].iloc[-1], utc=True) - pd.to_datetime(df["open_time"].iloc[0], utc=True)).total_seconds() / 86400,
    )

    return {
        "name": profile["name"],
        "source_minutes": bar_min,
        "lookback_bars": lookback,
        "lookback_minutes": profile["lookback_minutes"],
        "tail_pct": profile["tail_pct"],
        "threshold": round(target_p, 4),
        "confirm_threshold": profile.get("confirm_threshold"),
        "horizon_bars": horizon,
        "horizon_minutes": profile["horizon_minutes"],
        "cooldown_bars": cooldown,
        "cooldown_minutes": profile["cooldown_minutes"],
        "min_gap_minutes": profile.get("min_gap_minutes", profile["cooldown_minutes"]),
        "daily_trade_cap": daily_trade_cap,
        "daily_loss_stop": daily_loss_stop,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "wr": round(wr, 2),
        "edge_over_breakeven": round(wr - BREAKEVEN_WR, 2),
        "pnl_5u": round(pnl, 2),
        "max_loss": max_loss_streak(trades["win"].tolist()) if total else 0,
        "trades_per_day": round(total / days, 2),
        "block_summary": block_metrics(trades),
        "daily_distribution": daily_distribution(trades),
        "note": profile["note"],
    }


def run_dataset(file_name, confirmation=None):
    df = load_1m(file_name)
    results = [run_profile(df, p, confirmation=confirmation) for p in PROFILES]
    results.sort(key=lambda r: (r["pnl_5u"], r["wr"]), reverse=True)
    return {
        "file": file_name,
        "rows_1m": int(len(df)),
        "start": str(df["open_time"].iloc[0]),
        "end": str(df["open_time"].iloc[-1]),
        "results": results,
    }


def main():
    datasets = ["btcusdt_1m.csv"]
    if os.path.exists(os.path.join(DATA_DIR, "btcusdt_1m_180d.csv")):
        datasets.append("btcusdt_1m_180d.csv")

    confirm_path, confirmation = load_confirmation_cache()
    report = {
        "method": {
            "type": "poc_normal_reversal_profile_comparison",
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "All signals use causal rolling mean/std; future prices are used only for settlement labels.",
            "confirmation_cache": confirm_path,
        },
        "datasets": [run_dataset(name, confirmation=confirmation) for name in datasets],
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    print(f"Wrote {OUT_FILE}")
    for dataset in report["datasets"]:
        print(f"\n{dataset['file']} | {dataset['start']} -> {dataset['end']} | rows={dataset['rows_1m']}")
        print(f"{'name':36s} {'trades':>7s} {'wr':>7s} {'pnl':>9s} {'maxL':>5s} {'minBlk':>7s}")
        for r in dataset["results"]:
            min_block = r["block_summary"].get("min_block_wr")
            min_block_str = "n/a" if min_block is None else f"{min_block:.2f}"
            daily = r["daily_distribution"]
            print(
                f"{r['name']:36s} {r['trades']:7d} {r['wr']:6.2f}% {r['pnl_5u']:9.2f} "
                f"{r['max_loss']:5d} {min_block_str:>7s} maxDay={daily['max_day']:2d} p90Day={daily['p90_active']:4.1f}"
            )


if __name__ == "__main__":
    main()
