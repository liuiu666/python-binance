import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "data" / "btcusdt_1m_180d.csv"
OUT = ROOT / "tmp" / "research_1m_chip_high_freq.json"


def max_loss_streak(wins):
    cur = best = 0
    for won in wins:
        if won:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def summarize(rows, sample_days):
    n = len(rows)
    wins = sum(1 for r in rows if r["won"])
    days = pd.Series([pd.Timestamp(r["time"]).date().isoformat() for r in rows]) if rows else pd.Series(dtype=str)
    counts = days.value_counts() if rows else pd.Series(dtype=int)
    return {
        "trades": n,
        "wins": int(wins),
        "losses": int(n - wins),
        "winRate": round(100 * wins / n, 2) if n else None,
        "edgeOver85PayoutBreakeven": round(100 * wins / n - 54.05, 2) if n else None,
        "maxLoss": max_loss_streak([r["won"] for r in rows]),
        "tradesPerDay": round(n / sample_days, 2),
        "maxDay": int(counts.max()) if len(counts) else 0,
        "p90Day": round(float(counts.quantile(0.9)), 2) if len(counts) else 0,
        "firstSignal": rows[0]["time"] if rows else None,
        "lastSignal": rows[-1]["time"] if rows else None,
    }


def block_min_wr(rows, blocks=10):
    if not rows:
        return None
    wrs = []
    for i in range(blocks):
        lo = int(i * len(rows) / blocks)
        hi = int((i + 1) * len(rows) / blocks)
        part = rows[lo:hi]
        if not part:
            continue
        wrs.append(100 * sum(1 for r in part if r["won"]) / len(part))
    return round(min(wrs), 2) if wrs else None


def load_1m():
    df = pd.read_csv(CSV)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True, errors="coerce")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open_time", "close"]).sort_values("open_time")
    df = df.drop_duplicates("open_time", keep="last").set_index("open_time")
    idx = pd.date_range(df.index.min(), df.index.max(), freq="min", tz="UTC")
    df = df.reindex(idx)
    df["close"] = df["close"].ffill()
    df["open"] = df["open"].fillna(df["close"])
    df["high"] = df["high"].fillna(df["close"])
    df["low"] = df["low"].fillna(df["close"])
    df["volume"] = df["volume"].fillna(0)
    return df.dropna(subset=["close"])


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


def candidates(df, lookback, target_share, break_pct):
    close = df["close"].to_numpy(float)
    volume = df["volume"].to_numpy(float)
    times = df.index
    feat = build_poc_features(close, volume, lookback, 50, target_share)
    ret30 = pd.Series(close).pct_change(30).to_numpy()
    ret60 = pd.Series(close).pct_change(60).to_numpy()
    vol_ma = pd.Series(volume).rolling(60, min_periods=10).mean().to_numpy()
    rng = ((df["high"] - df["low"]) / df["close"]).to_numpy(float)
    rng_ma = pd.Series(rng).rolling(60, min_periods=10).mean().to_numpy()
    out = []
    prev_state = "unknown"
    horizon = 10
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
        out.append(
            {
                "idx": i,
                "time": times[i].isoformat(),
                "hour": int(times[i].hour),
                "signal": signal,
                "breakout": breakout,
                "entry": round(float(entry), 2),
                "settle": round(float(settle), 2),
                "won": bool(won),
                "distPct": float(dist_pct),
                "zoneShare": float(feat["share"][i]),
                "zoneVolShare": float(feat["volShare"][i]),
                "width": int(feat["width"][i]),
                "ret30": float(ret30[i]) if np.isfinite(ret30[i]) else 0.0,
                "ret60": float(ret60[i]) if np.isfinite(ret60[i]) else 0.0,
                "volRatio": float(volume[i] / max(vol_ma[i], 1e-12)) if np.isfinite(vol_ma[i]) else 0.0,
                "rangeRatio": float(rng[i] / max(rng_ma[i], 1e-12)) if np.isfinite(rng_ma[i]) else 0.0,
            }
        )
    return out


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
    df = load_1m()
    sample_days = (df.index[-1] - df.index[0]).total_seconds() / 86400
    results = []
    for lookback in [60, 120]:
        for target in [0.35, 0.5]:
            for break_pct in [0.002, 0.0023, 0.003, 0.004, 0.005]:
                base = candidates(df, lookback, target, break_pct)
                filters = []
                for min_dist in [break_pct, break_pct * 1.25, break_pct * 1.5]:
                    filters.append((f"dist>={min_dist:.4f}", lambda r, md=min_dist: r["distPct"] >= md))
                filters += [
                    ("width<=3", lambda r: r["width"] <= 3),
                    ("width<=5", lambda r: r["width"] <= 5),
                    ("zoneVolShare>=0.45", lambda r: r["zoneVolShare"] >= 0.45),
                    ("volRatio>=1.2", lambda r: r["volRatio"] >= 1.2),
                    ("rangeRatio>=1.2", lambda r: r["rangeRatio"] >= 1.2),
                    ("breakout_UP_only", lambda r: r["breakout"] == "UP"),
                    ("breakout_DOWN_only", lambda r: r["breakout"] == "DOWN"),
                    ("against_30m_trend", lambda r: (r["signal"] == "UP" and r["ret30"] < 0) or (r["signal"] == "DOWN" and r["ret30"] > 0)),
                    ("with_30m_reversal", lambda r: (r["signal"] == "UP" and r["ret30"] < -0.001) or (r["signal"] == "DOWN" and r["ret30"] > 0.001)),
                    ("active_hours_0_8_12_16_20", lambda r: r["hour"] in {0, 1, 2, 7, 8, 12, 13, 14, 15, 16, 20, 21}),
                ]
                filter_sets = [("none", lambda r: True)]
                filter_sets += filters
                for a_name, a_fn in filters:
                    for b_name, b_fn in filters:
                        if a_name >= b_name:
                            continue
                        filter_sets.append((a_name + "+" + b_name, lambda r, af=a_fn, bf=b_fn: af(r) and bf(r)))
                for filter_name, fn in filter_sets:
                    filtered = [r for r in base if fn(r)]
                    for gap in [5, 10, 15, 30, 60]:
                        rows = apply_gap(filtered, gap)
                        if len(rows) < 100:
                            continue
                        s = summarize(rows, sample_days)
                        s["minBlockWr"] = block_min_wr(rows)
                        score = (s["winRate"] or 0) + min(s["tradesPerDay"], 20) * 0.8 - max(0, s["maxLoss"] - 8) * 1.2
                        results.append(
                            {
                                "lookbackMin": lookback,
                                "targetShare": target,
                                "breakPct": break_pct,
                                "breakPctDisplay": f"{break_pct * 100:.2f}%",
                                "gapMin": gap,
                                "filter": filter_name,
                                "score": round(score, 3),
                                **s,
                            }
                        )

    ranked = sorted(results, key=lambda r: (r["score"], r["winRate"], r["tradesPerDay"]), reverse=True)
    high_wr = sorted([r for r in results if r["tradesPerDay"] >= 3], key=lambda r: (r["winRate"], r["tradesPerDay"]), reverse=True)
    high_freq = sorted([r for r in results if r["winRate"] >= 56.5], key=lambda r: (r["tradesPerDay"], r["winRate"]), reverse=True)
    payload = {
        "source": str(CSV),
        "sampleDays": round(sample_days, 2),
        "start": df.index[0].isoformat(),
        "end": df.index[-1].isoformat(),
        "goal": "maximize both win rate and trade count for 1m POC/chip-zone reversal",
        "topScore": ranked[:50],
        "topHighWinRateWithAtLeast3PerDay": high_wr[:50],
        "topHighFrequencyWithWinRateAtLeast56_5": high_freq[:50],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ["sampleDays", "start", "end", "topScore", "topHighWinRateWithAtLeast3PerDay", "topHighFrequencyWithWinRateAtLeast56_5"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
