import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "tmp" / "server_second_latest" / "btcusdt_1s_trades.csv"
OUT = ROOT / "tmp" / "chip_zone_focused_7pct.json"


def load_bars():
    df = pd.read_csv(CSV)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
    bars = df.groupby(df["timestamp"].dt.floor("s"), as_index=True).agg(
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    idx = pd.date_range(bars.index.min(), bars.index.max(), freq="s", tz="UTC")
    bars = bars.reindex(idx)
    bars["close"] = bars["close"].ffill()
    bars["volume"] = bars["volume"].fillna(0.0)
    return bars.dropna(subset=["close"])


def max_loss_streak(wins):
    cur = best = 0
    for w in wins:
        if w:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def summary(rows, sample_hours):
    wins = [r["won"] for r in rows]
    n = len(wins)
    return {
        "trades": n,
        "winRate": round(sum(wins) * 100 / n, 2) if n else None,
        "wins": int(sum(wins)),
        "losses": n - int(sum(wins)),
        "maxLoss": max_loss_streak(wins),
        "tradesPerDay": round(n / max(sample_hours / 24, 1e-9), 2),
        "firstSignal": rows[0]["time"] if rows else None,
        "lastSignal": rows[-1]["time"] if rows else None,
        "signals": rows,
    }


def build_zone_at_poc(close, volume, lookback, bin_size, target_share):
    bins = np.rint(close / bin_size).astype(int)
    offset = bins.min()
    size = bins.max() - offset + 1
    counts = np.zeros(size, dtype=float)
    vols = np.zeros(size, dtype=float)
    features = {k: np.full(len(close), np.nan) for k in ["lo", "hi", "share", "volShare", "poc", "width"]}

    for i, b0 in enumerate(bins):
        b = b0 - offset
        counts[b] += 1
        vols[b] += volume[i]
        if i >= lookback:
            old = bins[i - lookback] - offset
            counts[old] -= 1
            vols[old] -= volume[i - lookback]
        if i < lookback:
            continue

        total = counts.sum()
        total_vol = vols.sum()
        poc_idx = int(np.argmax(counts))
        lo = hi = poc_idx
        zone_count = counts[poc_idx]
        while zone_count / max(total, 1e-12) < target_share:
            left = counts[lo - 1] if lo > 0 else -1
            right = counts[hi + 1] if hi + 1 < size else -1
            if left < 0 and right < 0:
                break
            if right > left:
                hi += 1
                zone_count += counts[hi]
            else:
                lo -= 1
                zone_count += counts[lo]

        sl = slice(lo, hi + 1)
        features["lo"][i] = (lo + offset) * bin_size
        features["hi"][i] = (hi + offset) * bin_size
        features["share"][i] = counts[sl].sum() / max(total, 1e-12)
        features["volShare"][i] = vols[sl].sum() / max(total_vol, 1e-12) if total_vol > 0 else 0
        features["poc"][i] = (poc_idx + offset) * bin_size
        features["width"][i] = hi - lo + 1
    return features


def backtest(close, times, f, bin_size, horizon, gap, break_bins, mode):
    rows = []
    last = -10**12
    prev_state = "unknown"
    for i in range(len(close) - horizon):
        lo, hi = f["lo"][i], f["hi"][i]
        if not np.isfinite(lo):
            continue
        lower = lo - break_bins * bin_size
        upper = hi + break_bins * bin_size
        state = "inside"
        if close[i] > upper:
            state = "above"
        elif close[i] < lower:
            state = "below"

        breakout = signal = None
        if state == "above" and prev_state != "above":
            breakout = "UP"
            signal = "UP" if mode == "continue" else "DOWN"
        elif state == "below" and prev_state != "below":
            breakout = "DOWN"
            signal = "DOWN" if mode == "continue" else "UP"
        prev_state = state
        if signal is None or i - last < gap:
            continue
        entry = close[i]
        settle = close[i + horizon]
        won = settle > entry if signal == "UP" else settle < entry
        rows.append(
            {
                "time": times[i].isoformat(),
                "breakout": breakout,
                "signal": signal,
                "entry": round(float(entry), 2),
                "settle": round(float(settle), 2),
                "zoneLow": round(float(lo), 2),
                "zoneHigh": round(float(hi), 2),
                "poc": round(float(f["poc"][i]), 2),
                "zoneTimeShare": round(float(f["share"][i]), 4),
                "zoneVolumeShare": round(float(f["volShare"][i]), 4),
                "zoneWidthBins": int(f["width"][i]),
                "won": bool(won),
            }
        )
        last = i
    return rows


def main():
    bars = load_bars()
    close = bars["close"].to_numpy(float)
    volume = bars["volume"].to_numpy(float)
    times = bars.index
    sample_hours = (times[-1] - times[0]).total_seconds() / 3600
    lookback = 3600
    horizon = 600
    bin_size = 50
    target_share = 0.07
    features = build_zone_at_poc(close, volume, lookback, bin_size, target_share)

    results = []
    for break_bins in [0, 1, 2, 3, 4]:
        for gap in [600, 900, 1800]:
            for mode in ["continue", "revert"]:
                rows = backtest(close, times, features, bin_size, horizon, gap, break_bins, mode)
                results.append(
                    {
                        "lookbackSec": lookback,
                        "horizonSec": horizon,
                        "binSize": bin_size,
                        "targetShare": target_share,
                        "zoneDefinition": "POC price bin expands to adjacent bins until cumulative time share >= 7%",
                        "breakBins": break_bins,
                        "breakDistanceUsdt": break_bins * bin_size,
                        "gapSec": gap,
                        "mode": mode,
                        **summary(rows, sample_hours),
                    }
                )
    ranked = sorted([r for r in results if r["trades"] >= 3], key=lambda r: ((r["winRate"] or 0), r["trades"], -r["maxLoss"]), reverse=True)
    payload = {
        "source": str(CSV),
        "sampleHours": round(sample_hours, 2),
        "start": times[0].isoformat(),
        "end": times[-1].isoformat(),
        "top": ranked,
        "allResults": results,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"sampleHours": payload["sampleHours"], "start": payload["start"], "end": payload["end"], "top10": ranked[:10]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
