import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "tmp" / "server_second_latest" / "btcusdt_1s_trades.csv"
OUT = ROOT / "tmp" / "second_param_backtest_latest.json"


def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def max_loss_streak(results):
    cur = 0
    best = 0
    for won in results:
        if won:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def load_bars():
    df = pd.read_csv(CSV)
    ts_col = "timestamp" if "timestamp" in df.columns else "ts"
    price_col = "close" if "close" in df.columns else "price"
    df[ts_col] = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
    for col in [price_col, "volume", "taker_buy_volume", "taker_sell_volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df = df.dropna(subset=[ts_col]).sort_values(ts_col)
    bars = pd.DataFrame(
        {
            "time": df[ts_col].dt.floor("s"),
            "close": df[price_col],
            "volume": df.get("volume", 0.0),
            "buy_qty": df.get("taker_buy_volume", df.get("volume", 0.0) * 0.5),
            "sell_qty": df.get("taker_sell_volume", df.get("volume", 0.0) * 0.5),
        }
    )
    bars = bars.groupby("time", as_index=True).agg(
        close=("close", "last"),
        volume=("volume", "sum"),
        buy_qty=("buy_qty", "sum"),
        sell_qty=("sell_qty", "sum"),
    )
    idx = pd.date_range(bars.index.min(), bars.index.max(), freq="s", tz="UTC")
    bars = bars.reindex(idx)
    bars["close"] = bars["close"].ffill()
    for col in ["volume", "buy_qty", "sell_qty"]:
        bars[col] = bars[col].fillna(0.0)
    bars = bars.dropna(subset=["close"])
    return bars


def filter_allows(filter_name, signal, vol60, buy60, sell60, vol_rank):
    if filter_name in ("none", "", "off", "false"):
        return True
    flow_ratio = buy60 / max(sell60, 1e-12)
    if filter_name == "vol_high":
        return vol_rank >= 0.6 if np.isfinite(vol_rank) else False
    if filter_name == "vol_not_high":
        return (not np.isfinite(vol_rank)) or vol_rank <= 0.8
    if filter_name in ("flow_align", "flow_strong_align", "flow_align_vol_not_high"):
        up_min = 1.2 if filter_name == "flow_strong_align" else 1.05
        down_max = 0.8 if filter_name == "flow_strong_align" else 0.95
        flow_ok = flow_ratio >= up_min if signal == "UP" else flow_ratio <= down_max
        vol_ok = True
        if filter_name == "flow_align_vol_not_high":
            vol_ok = (not np.isfinite(vol_rank)) or vol_rank <= 0.8
        return bool(flow_ok and vol_ok)
    return False


def build_context(bars, lookbacks):
    close = bars["close"].to_numpy(dtype=float)
    volume = bars["volume"].to_numpy(dtype=float)
    buy = bars["buy_qty"].to_numpy(dtype=float)
    sell = bars["sell_qty"].to_numpy(dtype=float)
    times = bars.index

    logp = np.log(close)
    lr = np.diff(logp, prepend=np.nan)
    lr_series = pd.Series(lr)

    vol60 = pd.Series(volume).rolling(60, min_periods=1).sum().to_numpy()
    buy60 = pd.Series(buy).rolling(60, min_periods=1).sum().to_numpy()
    sell60 = pd.Series(sell).rolling(60, min_periods=1).sum().to_numpy()
    stats = {}
    ranks = {}
    for lookback in sorted(set(lookbacks)):
        stats[lookback] = (
            lr_series.rolling(lookback, min_periods=60).mean().to_numpy(),
            lr_series.rolling(lookback, min_periods=60).std(ddof=1).to_numpy(),
        )
        rank_window = max(lookback, 1800)
        if rank_window not in ranks:
            ranks[rank_window] = pd.Series(vol60).rolling(rank_window, min_periods=30).apply(
                lambda x: float((x <= x[-1]).mean()), raw=True
            ).to_numpy()
    return {
        "close": close,
        "times": times,
        "vol60": vol60,
        "buy60": buy60,
        "sell60": sell60,
        "stats": stats,
        "ranks": ranks,
    }


def backtest(ctx, lookback, horizon, gap, tail_pct, filter_name):
    close = ctx["close"]
    times = ctx["times"]
    vol60 = ctx["vol60"]
    buy60 = ctx["buy60"]
    sell60 = ctx["sell60"]
    mu, sigma = ctx["stats"][lookback]
    vol_rank = ctx["ranks"][max(lookback, 1800)]

    poc = 1.0 - tail_pct
    last_entry = -10**12
    wins = []
    signals = []
    start = lookback + 1
    end = len(close) - horizon
    for i in range(start, end):
        if i - last_entry < gap:
            continue
        if not np.isfinite(mu[i]) or not np.isfinite(sigma[i]) or sigma[i] < 1e-12:
            continue
        z = horizon * mu[i] / (math.sqrt(horizon) * sigma[i])
        p_up = normal_cdf(float(z))
        signal = None
        if p_up >= poc:
            signal = "DOWN"
        elif p_up <= tail_pct:
            signal = "UP"
        if signal is None:
            continue
        if not filter_allows(filter_name, signal, vol60[i], buy60[i], sell60[i], vol_rank[i]):
            continue
        entry = close[i]
        settle = close[i + horizon]
        won = settle > entry if signal == "UP" else settle < entry
        wins.append(bool(won))
        signals.append(
            {
                "time": times[i].isoformat(),
                "signal": signal,
                "entry": float(entry),
                "settle": float(settle),
                "p_up": round(float(p_up), 4),
                "won": bool(won),
            }
        )
        last_entry = i

    trades = len(wins)
    hours = (times[-1] - times[0]).total_seconds() / 3600.0
    return {
        "lookbackSec": lookback,
        "horizonSec": horizon,
        "gapSec": gap,
        "tailPct": tail_pct,
        "secondFilter": filter_name,
        "trades": trades,
        "winRate": round(100.0 * sum(wins) / trades, 2) if trades else None,
        "tradesPerDay": round(trades / max(hours / 24.0, 1e-9), 2),
        "maxLoss": max_loss_streak(wins),
        "firstSignal": signals[0]["time"] if signals else None,
        "lastSignal": signals[-1]["time"] if signals else None,
        "sampleSignals": signals[-5:],
    }


def main():
    bars = load_bars()
    sample_hours = (bars.index[-1] - bars.index[0]).total_seconds() / 3600.0
    lookbacks = [600, 900, 1200, 1800, 2400, 3600]
    tails = [0.18, 0.2, 0.23, 0.25, 0.27, 0.3]
    gaps = [600, 900, 1800]
    filters = ["none", "vol_not_high", "flow_align", "flow_strong_align", "flow_align_vol_not_high"]
    ctx = build_context(bars, lookbacks)
    results = []
    for lookback in lookbacks:
        for tail in tails:
            for gap in gaps:
                for filt in filters:
                    results.append(backtest(ctx, lookback, 600, gap, tail, filt))

    ranked = sorted(
        results,
        key=lambda r: (
            r["trades"] >= 3,
            r["winRate"] if r["winRate"] is not None else -1,
            r["trades"],
            -r["maxLoss"],
        ),
        reverse=True,
    )
    current = backtest(ctx, 1800, 600, 600, 0.2, "none")
    payload = {
        "source": str(CSV),
        "rows": int(len(bars)),
        "start": bars.index[0].isoformat(),
        "end": bars.index[-1].isoformat(),
        "sampleHours": round(sample_hours, 2),
        "currentLive": current,
        "topByWinRateMin3Trades": [r for r in ranked if r["trades"] >= 3][:20],
        "topBalanced": sorted(
            [r for r in results if r["trades"] >= 3],
            key=lambda r: ((r["winRate"] or 0) - max(0, r["maxLoss"] - 2) * 3 + min(r["tradesPerDay"], 12) * 0.35),
            reverse=True,
        )[:20],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
