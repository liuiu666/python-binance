import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "data" / "btcusdt_1m_180d.csv"
OUT = ROOT / "tmp" / "local_1m_history_backtest.json"


def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


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


def daily_stats(rows):
    if not rows:
        return {"activeDays": 0, "avgPerCalendarDay": 0, "avgPerActiveDay": 0, "maxDay": 0, "p90Day": 0}
    days = pd.Series([pd.Timestamp(r["time"]).date().isoformat() for r in rows])
    counts = days.value_counts()
    first = pd.Timestamp(rows[0]["time"]).date()
    last = pd.Timestamp(rows[-1]["time"]).date()
    calendar_days = max((last - first).days + 1, 1)
    return {
        "activeDays": int(len(counts)),
        "avgPerCalendarDay": round(len(rows) / calendar_days, 2),
        "avgPerActiveDay": round(float(counts.mean()), 2),
        "maxDay": int(counts.max()),
        "p90Day": round(float(counts.quantile(0.9)), 2),
    }


def block_stats(rows, blocks=10):
    if not rows:
        return {"blocks": [], "minBlockWr": None, "positiveBlocks": 0}
    out = []
    n = len(rows)
    for i in range(blocks):
        lo = int(i * n / blocks)
        hi = int((i + 1) * n / blocks)
        part = rows[lo:hi]
        if not part:
            continue
        wins = sum(1 for r in part if r["won"])
        wr = round(100.0 * wins / len(part), 2)
        out.append({"block": i + 1, "trades": len(part), "wins": wins, "losses": len(part) - wins, "wr": wr})
    return {
        "blocks": out,
        "minBlockWr": min((b["wr"] for b in out), default=None),
        "positiveBlocks": sum(1 for b in out if b["wr"] >= 54.05),
    }


def summarize(rows, sample_days):
    wins = [r["won"] for r in rows]
    n = len(rows)
    w = sum(wins)
    return {
        "trades": n,
        "wins": int(w),
        "losses": int(n - w),
        "winRate": round(100.0 * w / n, 2) if n else None,
        "edgeOver85PayoutBreakeven": round((100.0 * w / n) - 54.05, 2) if n else None,
        "maxLoss": max_loss_streak(wins),
        "tradesPerDay": round(n / max(sample_days, 1e-9), 2),
        "firstSignal": rows[0]["time"] if rows else None,
        "lastSignal": rows[-1]["time"] if rows else None,
        "daily": daily_stats(rows),
        "blocks": block_stats(rows),
        "sampleSignals": rows[-8:],
    }


def load_1m():
    df = pd.read_csv(CSV)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True, errors="coerce")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open_time", "close"]).sort_values("open_time")
    df = df.drop_duplicates(subset=["open_time"], keep="last").set_index("open_time")
    idx = pd.date_range(df.index.min(), df.index.max(), freq="min", tz="UTC")
    df = df.reindex(idx)
    df["close"] = df["close"].ffill()
    df["open"] = df["open"].fillna(df["close"])
    df["high"] = df["high"].fillna(df["close"])
    df["low"] = df["low"].fillna(df["close"])
    df["volume"] = df["volume"].fillna(0.0)
    return df.dropna(subset=["close"])


def with_gap(candidates, gap):
    rows = []
    last = -10**12
    for row in candidates:
        if row["idx"] - last < gap:
            continue
        rows.append(row)
        last = row["idx"]
    return rows


def backtest_normal(df, lookback, horizon, tail_pct, gap):
    close = df["close"].to_numpy(float)
    times = df.index
    lr = np.diff(np.log(close), prepend=np.nan)
    lr_series = pd.Series(lr)
    mu = lr_series.rolling(lookback, min_periods=max(10, lookback // 2)).mean().to_numpy()
    sigma = lr_series.rolling(lookback, min_periods=max(10, lookback // 2)).std(ddof=1).to_numpy()
    candidates = []
    for i in range(lookback + 1, len(close) - horizon):
        if not np.isfinite(mu[i]) or not np.isfinite(sigma[i]) or sigma[i] < 1e-12:
            continue
        p_up = normal_cdf(horizon * mu[i] / (math.sqrt(horizon) * sigma[i]))
        signal = None
        if p_up >= 1.0 - tail_pct:
            signal = "DOWN"
        elif p_up <= tail_pct:
            signal = "UP"
        if not signal:
            continue
        entry = close[i]
        settle = close[i + horizon]
        won = settle > entry if signal == "UP" else settle < entry
        candidates.append(
            {
                "idx": i,
                "time": times[i].isoformat(),
                "signal": signal,
                "pUp": round(float(p_up), 6),
                "entry": round(float(entry), 2),
                "settle": round(float(settle), 2),
                "won": bool(won),
            }
        )
    return with_gap(candidates, gap)


def build_poc_features(close, volume, lookback, bin_size, target_share):
    bins = np.rint(close / bin_size).astype(int)
    offset = int(bins.min())
    size = int(bins.max() - offset + 1)
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
        poc = int(np.argmax(counts))
        lo = hi = poc
        zone_count = counts[poc]
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
        features["volShare"][i] = vols[sl].sum() / max(total_vol, 1e-12) if total_vol > 0 else 0.0
        features["poc"][i] = (poc + offset) * bin_size
        features["width"][i] = hi - lo + 1
    return features


def backtest_chip(df, lookback, horizon, bin_size, target_share, break_pct, gap):
    close = df["close"].to_numpy(float)
    volume = df["volume"].to_numpy(float)
    times = df.index
    features = build_poc_features(close, volume, lookback, bin_size, target_share)
    rows = []
    last = -10**12
    prev_state = "unknown"
    for i in range(len(close) - horizon):
        lo = features["lo"][i]
        hi = features["hi"][i]
        if not np.isfinite(lo) or not np.isfinite(hi):
            continue
        upper = hi * (1.0 + break_pct)
        lower = lo * (1.0 - break_pct)
        state = "inside"
        if close[i] > upper:
            state = "above"
        elif close[i] < lower:
            state = "below"
        signal = None
        breakout = None
        if state == "above" and prev_state != "above":
            breakout = "UP"
            signal = "DOWN"
        elif state == "below" and prev_state != "below":
            breakout = "DOWN"
            signal = "UP"
        prev_state = state
        if signal is None or i - last < gap:
            continue
        entry = close[i]
        settle = close[i + horizon]
        won = settle > entry if signal == "UP" else settle < entry
        rows.append(
            {
                "idx": i,
                "time": times[i].isoformat(),
                "breakout": breakout,
                "signal": signal,
                "entry": round(float(entry), 2),
                "settle": round(float(settle), 2),
                "poc": round(float(features["poc"][i]), 2),
                "zoneLow": round(float(lo), 2),
                "zoneHigh": round(float(hi), 2),
                "zoneShare": round(float(features["share"][i]), 4),
                "zoneVolShare": round(float(features["volShare"][i]), 4),
                "won": bool(won),
            }
        )
        last = i
    return rows


def main():
    df = load_1m()
    sample_days = (df.index[-1] - df.index[0]).total_seconds() / 86400.0

    normal_results = []
    for lookback in [30, 60, 120, 240]:
        for tail_pct in [0.15, 0.2, 0.23, 0.27, 0.3]:
            for gap in [10, 15, 30, 60]:
                rows = backtest_normal(df, lookback, 10, tail_pct, gap)
                normal_results.append(
                    {
                        "type": "1m_normal",
                        "lookbackMin": lookback,
                        "horizonMin": 10,
                        "tailPct": tail_pct,
                        "gapMin": gap,
                        **summarize(rows, sample_days),
                    }
                )

    chip_results = []
    for lookback in [60, 120]:
        for target_share in [0.35, 0.5]:
            for break_pct in [0.002, 0.0023, 0.003]:
                for gap in [15, 30, 60]:
                    rows = backtest_chip(df, lookback, 10, 50, target_share, break_pct, gap)
                    chip_results.append(
                        {
                            "type": "1m_chip_poc",
                            "lookbackMin": lookback,
                            "horizonMin": 10,
                            "binSize": 50,
                            "targetShare": target_share,
                            "breakPct": break_pct,
                            "breakPctDisplay": f"{break_pct * 100:.2f}%",
                            "gapMin": gap,
                            **summarize(rows, sample_days),
                        }
                    )

    def rank(rows):
        eligible = [r for r in rows if r["trades"] >= 50]
        return sorted(
            eligible,
            key=lambda r: (
                r["winRate"] or 0,
                r["blocks"]["positiveBlocks"],
                -(r["blocks"]["minBlockWr"] or 0 < 50),
                -r["maxLoss"],
                -abs(r["tradesPerDay"] - 3),
            ),
            reverse=True,
        )

    payload = {
        "source": str(CSV),
        "rows1m": int(len(df)),
        "sampleDays": round(sample_days, 2),
        "start": df.index[0].isoformat(),
        "end": df.index[-1].isoformat(),
        "note": "Causal rolling features only; future close is used only for 10m settlement labels.",
        "topNormal": rank(normal_results)[:30],
        "topChip": rank(chip_results)[:30],
        "allNormal": normal_results,
        "allChip": chip_results,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ["rows1m", "sampleDays", "start", "end", "topNormal", "topChip"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
