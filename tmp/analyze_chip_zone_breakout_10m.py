import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "tmp" / "server_second_latest" / "btcusdt_1s_trades.csv"
OUT = ROOT / "tmp" / "chip_zone_breakout_10m_analysis.json"


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
        "wins": int(sum(wins)),
        "losses": int(trades - sum(wins)),
        "maxLoss": max_loss_streak(wins),
        "tradesPerDay": round(trades / max(sample_hours / 24.0, 1e-9), 2),
        "firstSignal": rows[0]["time"] if rows else None,
        "lastSignal": rows[-1]["time"] if rows else None,
        "sampleSignals": rows[-8:],
    }


def contiguous_regions(mask):
    regions = []
    start = None
    for i, ok in enumerate(mask):
        if ok and start is None:
            start = i
        elif not ok and start is not None:
            regions.append((start, i - 1))
            start = None
    if start is not None:
        regions.append((start, len(mask) - 1))
    return regions


def build_chip_zone_features(close, volume, buy_qty, sell_qty, lookback, bin_size, time_threshold):
    bin_id = np.rint(close / bin_size).astype(int)
    offset = int(bin_id.min())
    m = int(bin_id.max() - offset + 1)
    counts = np.zeros(m, dtype=float)
    vols = np.zeros(m, dtype=float)
    buys = np.zeros(m, dtype=float)
    sells = np.zeros(m, dtype=float)

    n = len(close)
    zone_low = np.full(n, np.nan)
    zone_high = np.full(n, np.nan)
    zone_time_share = np.full(n, np.nan)
    zone_volume_share = np.full(n, np.nan)
    zone_width_bins = np.full(n, np.nan)
    zone_flow_ratio = np.full(n, np.nan)

    for i in range(n):
        b = bin_id[i] - offset
        counts[b] += 1
        vols[b] += volume[i]
        buys[b] += buy_qty[i]
        sells[b] += sell_qty[i]
        if i >= lookback:
            old = bin_id[i - lookback] - offset
            counts[old] -= 1
            vols[old] -= volume[i - lookback]
            buys[old] -= buy_qty[i - lookback]
            sells[old] -= sell_qty[i - lookback]
        if i < lookback:
            continue

        total_count = counts.sum()
        total_vol = vols.sum()
        if total_count <= 0:
            continue
        time_share = counts / max(total_count, 1e-12)
        mask = time_share >= time_threshold
        regions = contiguous_regions(mask)
        if not regions:
            continue

        cur = bin_id[i] - offset
        containing = [r for r in regions if r[0] <= cur <= r[1]]
        if containing:
            lo, hi = max(containing, key=lambda r: counts[r[0] : r[1] + 1].sum())
        else:
            lo, hi = max(regions, key=lambda r: counts[r[0] : r[1] + 1].sum())

        sl = slice(lo, hi + 1)
        zone_low[i] = (lo + offset) * bin_size
        zone_high[i] = (hi + offset) * bin_size
        zone_time_share[i] = counts[sl].sum() / max(total_count, 1e-12)
        zone_volume_share[i] = vols[sl].sum() / max(total_vol, 1e-12) if total_vol > 0 else 0.0
        zone_width_bins[i] = hi - lo + 1
        zone_flow_ratio[i] = buys[sl].sum() / max(sells[sl].sum(), 1e-12)

    return {
        "zoneLow": zone_low,
        "zoneHigh": zone_high,
        "zoneTimeShare": zone_time_share,
        "zoneVolumeShare": zone_volume_share,
        "zoneWidthBins": zone_width_bins,
        "zoneFlowRatio": zone_flow_ratio,
    }


def backtest_breakouts(close, times, features, horizon, gap, bin_size, break_bins, mode, min_zone_time_share):
    rows = []
    last_idx = -10**12
    prev_state = "unknown"
    for i in range(len(close) - horizon):
        lo = features["zoneLow"][i]
        hi = features["zoneHigh"][i]
        zts = features["zoneTimeShare"][i]
        if not np.isfinite(lo) or not np.isfinite(hi) or not np.isfinite(zts):
            continue
        if zts < min_zone_time_share:
            continue

        price = close[i]
        upper = hi + break_bins * bin_size
        lower = lo - break_bins * bin_size
        state = "inside"
        if price > upper:
            state = "above"
        elif price < lower:
            state = "below"

        signal = None
        breakout = None
        if state == "above" and prev_state != "above":
            breakout = "UP"
            signal = "UP" if mode == "continue" else "DOWN"
        elif state == "below" and prev_state != "below":
            breakout = "DOWN"
            signal = "DOWN" if mode == "continue" else "UP"
        prev_state = state
        if signal is None or i - last_idx < gap:
            continue

        entry = close[i]
        settle = close[i + horizon]
        won = settle > entry if signal == "UP" else settle < entry
        rows.append(
            {
                "time": times[i].isoformat(),
                "mode": mode,
                "breakout": breakout,
                "signal": signal,
                "entry": round(float(entry), 2),
                "settle": round(float(settle), 2),
                "zoneLow": round(float(lo), 2),
                "zoneHigh": round(float(hi), 2),
                "zoneTimeShare": round(float(zts), 4),
                "zoneVolumeShare": round(float(features["zoneVolumeShare"][i]), 4),
                "zoneWidthBins": int(features["zoneWidthBins"][i]) if np.isfinite(features["zoneWidthBins"][i]) else None,
                "zoneFlowRatio": round(float(features["zoneFlowRatio"][i]), 4) if np.isfinite(features["zoneFlowRatio"][i]) else None,
                "won": bool(won),
            }
        )
        last_idx = i
    return rows


def main():
    bars = load_bars()
    close = bars["close"].to_numpy(dtype=float)
    volume = bars["volume"].to_numpy(dtype=float)
    buy_qty = bars["buy_qty"].to_numpy(dtype=float)
    sell_qty = bars["sell_qty"].to_numpy(dtype=float)
    times = bars.index
    sample_hours = (times[-1] - times[0]).total_seconds() / 3600.0

    lookback = 3600
    horizon = 600
    results = []
    for bin_size in [10, 20, 50]:
        for time_threshold in [0.03, 0.05, 0.07, 0.1]:
            features = build_chip_zone_features(close, volume, buy_qty, sell_qty, lookback, bin_size, time_threshold)
            for break_bins in [0, 1, 2]:
                for gap in [600, 900, 1800]:
                    for mode in ["continue", "revert"]:
                        rows = backtest_breakouts(
                            close,
                            times,
                            features,
                            horizon,
                            gap,
                            bin_size,
                            break_bins,
                            mode,
                            min_zone_time_share=time_threshold,
                        )
                        results.append(
                            {
                                "lookbackSec": lookback,
                                "horizonSec": horizon,
                                "binSize": bin_size,
                                "timeThreshold": time_threshold,
                                "breakBins": break_bins,
                                "gapSec": gap,
                                "mode": mode,
                                **summarize(rows, sample_hours),
                            }
                        )

    ranked = sorted(
        [r for r in results if r["trades"] >= 3],
        key=lambda r: ((r["winRate"] or 0), min(r["tradesPerDay"], 12), -r["maxLoss"]),
        reverse=True,
    )
    balanced = sorted(
        [r for r in results if r["trades"] >= 5],
        key=lambda r: (r["winRate"] or 0) + min(r["tradesPerDay"], 12) * 0.3 - max(0, r["maxLoss"] - 1) * 4,
        reverse=True,
    )
    payload = {
        "source": str(CSV),
        "sampleHours": round(sample_hours, 2),
        "start": times[0].isoformat(),
        "end": times[-1].isoformat(),
        "method": "chip_zone_time_at_price_breakout",
        "definition": {
            "axis": "x=time, y=price, chip=time/volume accumulated by price bin",
            "chipZone": "contiguous price bins where each bin has at least N% of prior window time",
            "breakout": "price leaves chipZone upward/downward",
            "continue": "trade breakout direction for 10m binary",
            "revert": "trade opposite breakout direction for 10m binary",
        },
        "topByWinRate": ranked[:30],
        "topBalanced": balanced[:30],
        "allResults": results,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
