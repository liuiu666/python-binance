import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "tmp" / "server_second_latest" / "btcusdt_1s_trades.csv"
OUT = ROOT / "tmp" / "research_second_chip_high_freq.json"


def load_1s():
    df = pd.read_csv(CSV)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "taker_buy_volume", "taker_sell_volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
    bars = df.groupby(df["timestamp"].dt.floor("s"), as_index=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        buy=("taker_buy_volume", "sum"),
        sell=("taker_sell_volume", "sum"),
    )
    idx = pd.date_range(bars.index.min(), bars.index.max(), freq="s", tz="UTC")
    bars = bars.reindex(idx)
    bars["close"] = bars["close"].ffill()
    bars["open"] = bars["open"].fillna(bars["close"])
    bars["high"] = bars["high"].fillna(bars["close"])
    bars["low"] = bars["low"].fillna(bars["close"])
    for col in ["volume", "buy", "sell"]:
        bars[col] = bars[col].fillna(0.0)
    return bars.dropna(subset=["close"])


def max_loss_streak(wins):
    cur = best = 0
    for won in wins:
        if won:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def summarize(rows, sample_hours):
    n = len(rows)
    wins = sum(1 for r in rows if r["won"])
    return {
        "trades": n,
        "wins": int(wins),
        "losses": int(n - wins),
        "winRate": round(100 * wins / n, 2) if n else None,
        "maxLoss": max_loss_streak([r["won"] for r in rows]),
        "tradesPerDay": round(n / max(sample_hours / 24, 1e-9), 2),
        "firstSignal": rows[0]["time"] if rows else None,
        "lastSignal": rows[-1]["time"] if rows else None,
        "sampleSignals": rows[-8:],
    }


def build_poc_features(close, volume, lookback, bin_size, target_share):
    bins = np.rint(close / bin_size).astype(int)
    offset = int(bins.min())
    size = int(bins.max() - offset + 1)
    counts = np.zeros(size)
    vols = np.zeros(size)
    out = {k: np.full(len(close), np.nan) for k in ["lo", "hi", "share", "volShare", "poc", "width"]}
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
        poc = int(np.argmax(counts))
        lo = hi = poc
        z = counts[poc]
        while z / max(total, 1e-12) < target_share:
            left = counts[lo - 1] if lo > 0 else -1
            right = counts[hi + 1] if hi + 1 < size else -1
            if left < 0 and right < 0:
                break
            if right > left:
                hi += 1
                z += counts[hi]
            else:
                lo -= 1
                z += counts[lo]
        sl = slice(lo, hi + 1)
        out["lo"][i] = (lo + offset) * bin_size
        out["hi"][i] = (hi + offset) * bin_size
        out["share"][i] = counts[sl].sum() / max(total, 1e-12)
        out["volShare"][i] = vols[sl].sum() / max(total_vol, 1e-12) if total_vol > 0 else 0
        out["poc"][i] = (poc + offset) * bin_size
        out["width"][i] = hi - lo + 1
    return out


def raw_candidates(bars, lookback, target_share, bin_size, break_pct, horizon):
    close = bars["close"].to_numpy(float)
    volume = bars["volume"].to_numpy(float)
    buy = bars["buy"].to_numpy(float)
    sell = bars["sell"].to_numpy(float)
    times = bars.index
    feat = build_poc_features(close, volume, lookback, bin_size, target_share)
    vol_ma = pd.Series(volume).rolling(300, min_periods=60).mean().to_numpy()
    flow = pd.Series(buy - sell).rolling(300, min_periods=60).sum().to_numpy()
    rows = []
    prev_state = "unknown"
    for i in range(len(close) - horizon):
        lo, hi = feat["lo"][i], feat["hi"][i]
        if not np.isfinite(lo) or not np.isfinite(hi):
            continue
        upper = hi * (1 + break_pct)
        lower = lo * (1 - break_pct)
        state = "inside"
        if close[i] > upper:
            state = "above"
        elif close[i] < lower:
            state = "below"
        signal = breakout = None
        if state == "above" and prev_state != "above":
            breakout = "UP"
            signal = "DOWN"
            dist_pct = close[i] / max(hi, 1e-12) - 1
        elif state == "below" and prev_state != "below":
            breakout = "DOWN"
            signal = "UP"
            dist_pct = lo / max(close[i], 1e-12) - 1
        prev_state = state
        if signal is None:
            continue
        entry = close[i]
        settle = close[i + horizon]
        won = settle > entry if signal == "UP" else settle < entry
        rows.append(
            {
                "idx": i,
                "time": times[i].isoformat(),
                "hour": int(times[i].hour),
                "breakout": breakout,
                "signal": signal,
                "entry": round(float(entry), 2),
                "settle": round(float(settle), 2),
                "poc": round(float(feat["poc"][i]), 2),
                "zoneLow": round(float(lo), 2),
                "zoneHigh": round(float(hi), 2),
                "zoneShare": float(feat["share"][i]),
                "zoneVolShare": float(feat["volShare"][i]),
                "width": int(feat["width"][i]),
                "distPct": float(dist_pct),
                "volRatio": float(volume[i] / max(vol_ma[i], 1e-12)) if np.isfinite(vol_ma[i]) else 0.0,
                "flow300": float(flow[i]) if np.isfinite(flow[i]) else 0.0,
                "won": bool(won),
            }
        )
    return rows


def apply_gap(rows, gap):
    out = []
    last = -10**12
    for r in rows:
        if r["idx"] - last < gap:
            continue
        out.append(r)
        last = r["idx"]
    return out


def main():
    bars = load_1s()
    sample_hours = (bars.index[-1] - bars.index[0]).total_seconds() / 3600
    results = []
    detail = {}
    for lookback in [1800, 3600, 7200]:
        if len(bars) < lookback + 600:
            continue
        for target in [0.07, 0.2, 0.35, 0.5]:
            for bin_size in [20, 50, 100]:
                for break_pct in [0.0015, 0.0023, 0.003, 0.004]:
                    base = raw_candidates(bars, lookback, target, bin_size, break_pct, 600)
                    filters = [
                        ("none", lambda r: True),
                        ("width<=3", lambda r: r["width"] <= 3),
                        ("width<=5", lambda r: r["width"] <= 5),
                        ("zoneVolShare>=0.45", lambda r: r["zoneVolShare"] >= 0.45),
                        ("volRatio>=1.2", lambda r: r["volRatio"] >= 1.2),
                        ("breakout_UP_only", lambda r: r["breakout"] == "UP"),
                        ("breakout_DOWN_only", lambda r: r["breakout"] == "DOWN"),
                        ("flow_reversal", lambda r: (r["signal"] == "UP" and r["flow300"] < 0) or (r["signal"] == "DOWN" and r["flow300"] > 0)),
                    ]
                    for filter_name, fn in filters:
                        filtered = [r for r in base if fn(r)]
                        for gap in [300, 600, 900, 1800]:
                            rows = apply_gap(filtered, gap)
                            if len(rows) < 3:
                                continue
                            s = summarize(rows, sample_hours)
                            score = (s["winRate"] or 0) + min(s["tradesPerDay"], 30) * 0.25 - max(0, s["maxLoss"] - 2) * 2
                            item = {
                                "lookbackSec": lookback,
                                "targetShare": target,
                                "binSize": bin_size,
                                "breakPct": break_pct,
                                "breakPctDisplay": f"{break_pct * 100:.2f}%",
                                "gapSec": gap,
                                "filter": filter_name,
                                "score": round(score, 3),
                                **{k: v for k, v in s.items() if k != "sampleSignals"},
                            }
                            key = f"{lookback}_{target}_{bin_size}_{break_pct}_{gap}_{filter_name}"
                            results.append({**item, "key": key})
                            detail[key] = rows
    ranked = sorted(results, key=lambda r: (r["score"], r["winRate"], r["tradesPerDay"]), reverse=True)
    high_freq = sorted([r for r in results if r["tradesPerDay"] >= 8], key=lambda r: (r["winRate"], r["tradesPerDay"]), reverse=True)
    top = ranked[:50]
    payload = {
        "source": str(CSV),
        "sampleHours": round(sample_hours, 2),
        "start": bars.index[0].isoformat(),
        "end": bars.index[-1].isoformat(),
        "warning": "Only about 20h of second-level data is available locally; results are candidates, not stable production proof.",
        "topScore": top,
        "topHighFrequencyAtLeast8PerDay": high_freq[:50],
        "topDetails": {r["key"]: detail[r["key"]][-10:] for r in top[:10]},
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
