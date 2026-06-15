import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "tmp" / "server_second_latest" / "btcusdt_1s_trades.csv"
OUT = ROOT / "tmp" / "chip_zone_accumulation_10m_analysis.json"


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
        "sampleSignals": rows[-10:],
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


def choose_zone(counts, vols, bin_id, offset, bin_size, target_share, min_bin_share, method):
    total_count = counts.sum()
    total_vol = vols.sum()
    if total_count <= 0:
        return None
    shares = counts / max(total_count, 1e-12)
    cur = bin_id - offset

    if method == "single_bin_threshold":
        mask = shares >= target_share
        regions = contiguous_regions(mask)
        if not regions:
            return None
        containing = [r for r in regions if r[0] <= cur <= r[1]]
        lo, hi = max(containing or regions, key=lambda r: counts[r[0] : r[1] + 1].sum())
    elif method == "top_accumulated_contiguous":
        seed = int(np.argmax(counts))
        lo = hi = seed
        zone_count = counts[seed]
        while zone_count / max(total_count, 1e-12) < target_share:
            left_count = counts[lo - 1] if lo > 0 else -1
            right_count = counts[hi + 1] if hi + 1 < len(counts) else -1
            if left_count < 0 and right_count < 0:
                break
            if right_count > left_count:
                hi += 1
                zone_count += counts[hi]
            else:
                lo -= 1
                zone_count += counts[lo]
        if zone_count / max(total_count, 1e-12) < target_share:
            return None
    elif method == "around_current_accumulated":
        lo = hi = cur
        if lo < 0 or hi >= len(counts):
            return None
        zone_count = counts[cur]
        while zone_count / max(total_count, 1e-12) < target_share:
            left_count = counts[lo - 1] if lo > 0 else -1
            right_count = counts[hi + 1] if hi + 1 < len(counts) else -1
            if left_count < 0 and right_count < 0:
                break
            if right_count > left_count:
                hi += 1
                zone_count += counts[hi]
            else:
                lo -= 1
                zone_count += counts[lo]
        if zone_count / max(total_count, 1e-12) < target_share:
            return None
    else:
        raise ValueError(method)

    if min_bin_share > 0 and np.max(shares[lo : hi + 1]) < min_bin_share:
        return None
    sl = slice(lo, hi + 1)
    zone_count = counts[sl].sum()
    zone_vol = vols[sl].sum()
    return {
        "low": (lo + offset) * bin_size,
        "high": (hi + offset) * bin_size,
        "timeShare": zone_count / max(total_count, 1e-12),
        "volumeShare": zone_vol / max(total_vol, 1e-12) if total_vol > 0 else 0.0,
        "widthBins": hi - lo + 1,
        "poc": (int(np.argmax(counts[sl])) + lo + offset) * bin_size,
        "maxBinShare": float(np.max(shares[sl])),
    }


def build_features(close, volume, lookback, bin_size, target_share, min_bin_share, method):
    bin_ids = np.rint(close / bin_size).astype(int)
    offset = int(bin_ids.min())
    m = int(bin_ids.max() - offset + 1)
    counts = np.zeros(m, dtype=float)
    vols = np.zeros(m, dtype=float)
    out = {k: np.full(len(close), np.nan) for k in ["low", "high", "timeShare", "volumeShare", "widthBins", "poc", "maxBinShare"]}

    for i, b0 in enumerate(bin_ids):
        b = b0 - offset
        counts[b] += 1
        vols[b] += volume[i]
        if i >= lookback:
            old = bin_ids[i - lookback] - offset
            counts[old] -= 1
            vols[old] -= volume[i - lookback]
        if i < lookback:
            continue
        zone = choose_zone(counts, vols, b0, offset, bin_size, target_share, min_bin_share, method)
        if not zone:
            continue
        for k, v in zone.items():
            out[k][i] = v
    return out


def backtest(close, times, features, horizon, gap, bin_size, break_bins, mode):
    rows = []
    last_idx = -10**12
    prev_state = "unknown"
    for i in range(len(close) - horizon):
        lo = features["low"][i]
        hi = features["high"][i]
        if not np.isfinite(lo) or not np.isfinite(hi):
            continue
        upper = hi + break_bins * bin_size
        lower = lo - break_bins * bin_size
        state = "inside"
        if close[i] > upper:
            state = "above"
        elif close[i] < lower:
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
                "signal": signal,
                "breakout": breakout,
                "entry": round(float(entry), 2),
                "settle": round(float(settle), 2),
                "zoneLow": round(float(lo), 2),
                "zoneHigh": round(float(hi), 2),
                "poc": round(float(features["poc"][i]), 2),
                "zoneTimeShare": round(float(features["timeShare"][i]), 4),
                "maxBinShare": round(float(features["maxBinShare"][i]), 4),
                "zoneVolumeShare": round(float(features["volumeShare"][i]), 4),
                "zoneWidthBins": int(features["widthBins"][i]),
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
    lookback = 3600
    horizon = 600

    results = []
    grid = {
        "single_bin_threshold": {
            "binSize": [20, 50],
            "targetShare": [0.05, 0.07, 0.1],
            "minBinShare": [0.0],
        },
        "top_accumulated_contiguous": {
            "binSize": [20, 50, 100],
            "targetShare": [0.07, 0.2, 0.35, 0.5],
            "minBinShare": [0.0, 0.03, 0.05],
        },
        "around_current_accumulated": {
            "binSize": [20, 50, 100],
            "targetShare": [0.07, 0.2, 0.35, 0.5],
            "minBinShare": [0.0, 0.03, 0.05],
        },
    }
    for method, params in grid.items():
        for bin_size in params["binSize"]:
            for target_share in params["targetShare"]:
                for min_bin_share in params["minBinShare"]:
                    features = build_features(close, volume, lookback, bin_size, target_share, min_bin_share, method)
                    for break_bins in [0, 1, 2, 3]:
                        for gap in [600, 900, 1800]:
                            for mode in ["continue", "revert"]:
                                rows = backtest(close, times, features, horizon, gap, bin_size, break_bins, mode)
                                results.append(
                                    {
                                        "method": method,
                                        "lookbackSec": lookback,
                                        "horizonSec": horizon,
                                        "binSize": bin_size,
                                        "targetShare": target_share,
                                        "minBinShare": min_bin_share,
                                        "breakBins": break_bins,
                                        "gapSec": gap,
                                        "mode": mode,
                                        **summarize(rows, sample_hours),
                                    }
                                )

    eligible = [r for r in results if r["trades"] >= 5]
    ranked = sorted(eligible, key=lambda r: ((r["winRate"] or 0), min(r["tradesPerDay"], 12), -r["maxLoss"]), reverse=True)
    balanced = sorted(
        eligible,
        key=lambda r: (r["winRate"] or 0) + min(r["tradesPerDay"], 12) * 0.25 - max(0, r["maxLoss"] - 1) * 6,
        reverse=True,
    )
    payload = {
        "source": str(CSV),
        "sampleHours": round(sample_hours, 2),
        "start": times[0].isoformat(),
        "end": times[-1].isoformat(),
        "methodNotes": {
            "single_bin_threshold": "price bins whose individual time share reaches targetShare; this matches the previous strict 7% test",
            "top_accumulated_contiguous": "start at the POC/time-max price bin, expand to adjacent bins until the whole zone reaches targetShare",
            "around_current_accumulated": "start at current price bin, expand to adjacent bins until the whole zone reaches targetShare",
        },
        "topByWinRate": ranked[:40],
        "topBalanced": balanced[:40],
        "allResults": results,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ["sampleHours", "start", "end", "methodNotes"]}, ensure_ascii=False, indent=2))
    print(json.dumps({"topByWinRate": ranked[:10], "topBalanced": balanced[:10]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
