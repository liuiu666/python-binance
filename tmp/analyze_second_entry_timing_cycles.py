import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "tmp" / "server_second_latest" / "btcusdt_1s_trades.csv"
OUT = ROOT / "tmp" / "second_entry_timing_cycles.json"


def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


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
        }
    )
    bars = bars.groupby("time", as_index=True).agg(close=("close", "last"), volume=("volume", "sum"))
    idx = pd.date_range(bars.index.min(), bars.index.max(), freq="s", tz="UTC")
    bars = bars.reindex(idx)
    bars["close"] = bars["close"].ffill()
    bars["volume"] = bars["volume"].fillna(0.0)
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


def summarize(rows, sample_hours=None):
    wins = [r["won"] for r in rows]
    trades = len(wins)
    out = {
        "trades": trades,
        "winRate": round(100.0 * sum(wins) / trades, 2) if trades else None,
        "wins": int(sum(wins)),
        "losses": int(trades - sum(wins)),
        "maxLoss": max_loss_streak(wins),
    }
    if sample_hours is not None:
        out["tradesPerDay"] = round(trades / max(sample_hours / 24.0, 1e-9), 2)
    return out


def build_candidates(bars, lookback=3600, horizon=600, tail_pct=0.23):
    close = bars["close"].to_numpy(dtype=float)
    times = bars.index
    lr = np.diff(np.log(close), prepend=np.nan)
    lr_series = pd.Series(lr)
    mu = lr_series.rolling(lookback, min_periods=60).mean().to_numpy()
    sigma = lr_series.rolling(lookback, min_periods=60).std(ddof=1).to_numpy()
    upper = 1.0 - tail_pct
    rows = []
    for i in range(lookback + 1, len(close) - horizon):
        if not np.isfinite(mu[i]) or not np.isfinite(sigma[i]) or sigma[i] < 1e-12:
            continue
        p_up = normal_cdf(float(horizon * mu[i] / (math.sqrt(horizon) * sigma[i])))
        signal = "DOWN" if p_up >= upper else "UP" if p_up <= tail_pct else None
        if not signal:
            continue
        entry = close[i]
        settle = close[i + horizon]
        won = settle > entry if signal == "UP" else settle < entry
        ts = times[i]
        rows.append(
            {
                "idx": i,
                "time": ts.isoformat(),
                "hourUtc": int(ts.hour),
                "minute": int(ts.minute),
                "second": int(ts.second),
                "minuteMod10": int(ts.minute % 10),
                "secondIn10m": int((ts.minute % 10) * 60 + ts.second),
                "secondBucket30": int(((ts.minute % 10) * 60 + ts.second) // 30),
                "signal": signal,
                "p_up": round(float(p_up), 6),
                "entry": round(float(entry), 2),
                "settle": round(float(settle), 2),
                "won": bool(won),
            }
        )
    return rows


def select_gap(rows, gap):
    out = []
    last = -10**12
    for row in rows:
        if row["idx"] - last < gap:
            continue
        out.append(row)
        last = row["idx"]
    return out


def group(rows, key):
    buckets = {}
    for row in rows:
        buckets.setdefault(str(row[key]), []).append(row)
    return {k: summarize(v) for k, v in sorted(buckets.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else kv[0])}


def test_allowed_buckets(rows, key, gap, min_trades=3):
    base = select_gap(rows, gap)
    bucket_stats = group(base, key)
    good_values = {
        k for k, stat in bucket_stats.items()
        if stat["trades"] >= min_trades and stat["winRate"] is not None and stat["winRate"] >= 60
    }
    filtered_source = [r for r in rows if str(r[key]) in good_values]
    return {
        "key": key,
        "gapSec": gap,
        "allowedValues": sorted(good_values, key=lambda x: int(x) if x.isdigit() else x),
        "base": summarize(base),
        "filtered": summarize(select_gap(filtered_source, gap)),
        "bucketStats": bucket_stats,
    }


def main():
    bars = load_bars()
    sample_hours = (bars.index[-1] - bars.index[0]).total_seconds() / 3600.0
    candidates = build_candidates(bars)
    payload = {
        "source": str(CSV),
        "sampleHours": round(sample_hours, 2),
        "start": bars.index[0].isoformat(),
        "end": bars.index[-1].isoformat(),
        "strategy": "SECOND 3600/23 horizon 600",
        "rawCandidates": summarize(candidates, sample_hours),
        "gap600": summarize(select_gap(candidates, 600), sample_hours),
        "gap900": summarize(select_gap(candidates, 900), sample_hours),
        "gap1800": summarize(select_gap(candidates, 1800), sample_hours),
        "groups": {
            "gap600_minuteMod10": group(select_gap(candidates, 600), "minuteMod10"),
            "gap600_hourUtc": group(select_gap(candidates, 600), "hourUtc"),
            "gap600_secondBucket30": group(select_gap(candidates, 600), "secondBucket30"),
            "gap1800_minuteMod10": group(select_gap(candidates, 1800), "minuteMod10"),
            "gap1800_hourUtc": group(select_gap(candidates, 1800), "hourUtc"),
        },
        "cycleFilters": [
            test_allowed_buckets(candidates, "minuteMod10", 600, min_trades=2),
            test_allowed_buckets(candidates, "hourUtc", 600, min_trades=2),
            test_allowed_buckets(candidates, "secondBucket30", 600, min_trades=2),
            test_allowed_buckets(candidates, "minuteMod10", 1800, min_trades=2),
            test_allowed_buckets(candidates, "hourUtc", 1800, min_trades=2),
        ],
        "gap600Signals": select_gap(candidates, 600),
        "gap1800Signals": select_gap(candidates, 1800),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
