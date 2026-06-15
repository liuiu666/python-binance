import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "tmp" / "server_second_latest" / "btcusdt_1s_trades.csv"
OUT = ROOT / "tmp" / "second_3600_23_analysis.json"


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


def build_candidates(bars, lookback=3600, horizon=600, tail_pct=0.23):
    close = bars["close"].to_numpy(dtype=float)
    times = bars.index
    lr = np.diff(np.log(close), prepend=np.nan)
    lr_series = pd.Series(lr)
    mu = lr_series.rolling(lookback, min_periods=60).mean().to_numpy()
    sigma = lr_series.rolling(lookback, min_periods=60).std(ddof=1).to_numpy()
    poc = 1.0 - tail_pct
    rows = []
    for i in range(lookback + 1, len(close) - horizon):
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
        entry = close[i]
        settle = close[i + horizon]
        won = settle > entry if signal == "UP" else settle < entry
        rows.append(
            {
                "idx": i,
                "time": times[i].isoformat(),
                "signal": signal,
                "p_up": round(float(p_up), 6),
                "entry": float(entry),
                "settle": float(settle),
                "won": bool(won),
            }
        )
    return rows


def with_gap(candidates, gap_sec):
    out = []
    last_idx = -10**12
    for row in candidates:
        if row["idx"] - last_idx < gap_sec:
            continue
        out.append(row)
        last_idx = row["idx"]
    return out


def streaks(candidates):
    if not candidates:
        return []
    groups = []
    cur = [candidates[0]]
    for row in candidates[1:]:
        prev = cur[-1]
        if row["signal"] == prev["signal"] and row["idx"] - prev["idx"] <= 180:
            cur.append(row)
        else:
            groups.append(cur)
            cur = [row]
    groups.append(cur)
    return groups


def summarize(seq):
    trades = len(seq)
    wins = sum(1 for x in seq if x["won"])
    return {
        "trades": trades,
        "winRate": round(100.0 * wins / trades, 2) if trades else None,
        "wins": wins,
        "losses": trades - wins,
    }


def main():
    bars = load_bars()
    candidates = build_candidates(bars)
    s180 = streaks(candidates)
    payload = {
        "sampleHours": round((bars.index[-1] - bars.index[0]).total_seconds() / 3600.0, 2),
        "rawCandidates": summarize(candidates),
        "gap600": summarize(with_gap(candidates, 600)),
        "gap900": summarize(with_gap(candidates, 900)),
        "gap1800": summarize(with_gap(candidates, 1800)),
        "candidateBurstCount": len(s180),
        "candidateBursts": [
            {
                "signal": burst[0]["signal"],
                "count": len(burst),
                "start": burst[0]["time"],
                "end": burst[-1]["time"],
                "wonCount": sum(1 for x in burst if x["won"]),
                "lossCount": sum(1 for x in burst if not x["won"]),
                "firstPup": burst[0]["p_up"],
                "lastPup": burst[-1]["p_up"],
            }
            for burst in sorted(s180, key=len, reverse=True)[:20]
        ],
        "gap600Signals": with_gap(candidates, 600),
        "gap900Signals": with_gap(candidates, 900),
        "gap1800Signals": with_gap(candidates, 1800),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
