import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "tmp" / "server_second_latest" / "btcusdt_1s_trades.csv"
OUT = ROOT / "tmp" / "second_volume_poc_analysis.json"


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
    return bars.dropna(subset=["close"])


def max_loss_streak(wins):
    cur = 0
    best = 0
    for won in wins:
        if won:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def summarize(rows, sample_hours):
    wins = [r["won"] for r in rows]
    trades = len(wins)
    return {
        "trades": trades,
        "winRate": round(100.0 * sum(wins) / trades, 2) if trades else None,
        "tradesPerDay": round(trades / max(sample_hours / 24.0, 1e-9), 2),
        "maxLoss": max_loss_streak(wins),
        "firstSignal": rows[0]["time"] if rows else None,
        "lastSignal": rows[-1]["time"] if rows else None,
        "sampleSignals": rows[-5:],
    }


def build_poc_series(close, volume, lookback, bin_size):
    n = len(close)
    poc = np.full(n, np.nan)
    sigma = np.full(n, np.nan)
    total_volume = np.full(n, np.nan)
    poc_volume = np.full(n, np.nan)
    for i in range(lookback, n):
        prices = close[i - lookback : i]
        vols = volume[i - lookback : i]
        good = np.isfinite(prices)
        prices = prices[good]
        vols = vols[good]
        if len(prices) < 60:
            continue
        vol_sum = float(np.nansum(vols))
        weights = vols if vol_sum > 1e-12 else np.ones_like(prices)
        bins = np.round(prices / bin_size) * bin_size
        uniq, inv = np.unique(bins, return_inverse=True)
        bin_vol = np.bincount(inv, weights=weights)
        j = int(np.argmax(bin_vol))
        center = float(uniq[j])
        var = float(np.average((prices - center) ** 2, weights=weights))
        sig = math.sqrt(max(var, 0.0))
        if sig < 1e-9:
            continue
        poc[i] = center
        sigma[i] = sig
        total_volume[i] = vol_sum
        poc_volume[i] = float(bin_vol[j])
    return poc, sigma, total_volume, poc_volume


def backtest(close, times, poc, sigma, total_volume, poc_volume, horizon, gap, z_threshold, entry_mode):
    rows = []
    last_idx = -10**12
    in_up_extreme = False
    in_down_extreme = False
    for i in range(len(close) - horizon):
        if not np.isfinite(poc[i]) or not np.isfinite(sigma[i]) or sigma[i] < 1e-9:
            continue
        z = (close[i] - poc[i]) / sigma[i]
        signal = None
        if z >= z_threshold:
            signal = "DOWN"
        elif z <= -z_threshold:
            signal = "UP"

        if entry_mode == "first_enter":
            if signal == "DOWN":
                if in_down_extreme:
                    continue
                in_down_extreme = True
                in_up_extreme = False
            elif signal == "UP":
                if in_up_extreme:
                    continue
                in_up_extreme = True
                in_down_extreme = False
            else:
                in_up_extreme = False
                in_down_extreme = False
                continue
        elif signal is None:
            continue

        if i - last_idx < gap:
            continue
        entry = close[i]
        settle = close[i + horizon]
        won = settle > entry if signal == "UP" else settle < entry
        rows.append(
            {
                "time": times[i].isoformat(),
                "signal": signal,
                "entry": round(float(entry), 2),
                "settle": round(float(settle), 2),
                "poc": round(float(poc[i]), 2),
                "z": round(float(z), 4),
                "pocVolShare": round(float(poc_volume[i] / total_volume[i]), 4) if total_volume[i] and total_volume[i] > 0 else None,
                "won": bool(won),
            }
        )
        last_idx = i
    return rows


def main():
    bars = load_bars()
    close = bars["close"].to_numpy(dtype=float)
    volume = bars["volume"].to_numpy(dtype=float)
    times = bars.index
    sample_hours = (times[-1] - times[0]).total_seconds() / 3600.0
    horizon = 600

    lookbacks = [3600]
    bin_sizes = [10, 20, 50]
    thresholds = [1.0, 1.25, 1.5, 1.75]
    gaps = [600, 1800]
    entry_modes = ["gap", "first_enter"]

    results = []
    for lookback in lookbacks:
        for bin_size in bin_sizes:
            poc, sigma, total_volume, poc_volume = build_poc_series(close, volume, lookback, bin_size)
            for threshold in thresholds:
                for gap in gaps:
                    for entry_mode in entry_modes:
                        rows = backtest(close, times, poc, sigma, total_volume, poc_volume, horizon, gap, threshold, entry_mode)
                        results.append(
                            {
                                "lookbackSec": lookback,
                                "horizonSec": horizon,
                                "binSize": bin_size,
                                "zThreshold": threshold,
                                "gapSec": gap,
                                "entryMode": entry_mode,
                                **summarize(rows, sample_hours),
                            }
                        )

    ranked = sorted(
        [r for r in results if r["trades"] >= 3],
        key=lambda r: (
            r["winRate"] or 0,
            min(r["tradesPerDay"], 20),
            -r["maxLoss"],
        ),
        reverse=True,
    )
    balanced = sorted(
        [r for r in results if r["trades"] >= 3],
        key=lambda r: (
            (r["winRate"] or 0)
            + min(r["tradesPerDay"], 12) * 0.35
            - max(0, r["maxLoss"] - 1) * 4
            - (0 if r["trades"] >= 5 else 3)
        ),
        reverse=True,
    )
    payload = {
        "source": str(CSV),
        "sampleHours": round(sample_hours, 2),
        "start": times[0].isoformat(),
        "end": times[-1].isoformat(),
        "method": "volume_poc_centered_normal",
        "definition": {
            "center": "price bin with max volume in lookback window",
            "sigma": "volume-weighted price standard deviation around POC",
            "signal": "reversal: price z >= threshold => DOWN, z <= -threshold => UP",
        },
        "topByWinRate": ranked[:20],
        "topBalanced": balanced[:20],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
