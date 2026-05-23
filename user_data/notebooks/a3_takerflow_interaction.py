"""A3 ∩ taker-flow interaction study.

Within the A3 trigger zone (|ema120_dev| in top decile, mean-revert side),
slice by taker order-flow features and see whether secondary filtering
pushes WR above the ~58% OHLCV ceiling.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures-extended.feather")
HORIZON = 10
WILSON_Z = 1.96

# A3 trigger thresholds (production)
EMA_DEV_THRESH = 1.4   # |ema120_dev| >= 1.4 ATR
VOL_Z_MAX = 2.0        # vol_z40 < 2.0 (filter out blow-offs)
RV_Z_MAX = 1.5         # realized-vol z-score


def wilson_lb(wins: int, n: int, z: float = WILSON_Z) -> float:
    if n == 0:
        return float("nan")
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def load() -> pd.DataFrame:
    df = pd.read_feather(DATA).sort_values("date").reset_index(drop=True)
    df["fwd_ret"] = df["close"].shift(-HORIZON) / df["close"] - 1
    return df


def build(df: pd.DataFrame) -> pd.DataFrame:
    f = df.copy()
    ema120 = df["close"].ewm(span=120, adjust=False).mean()
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(60).mean()
    f["ema120_dev"] = (df["close"] - ema120) / atr
    f["vol_z40"] = (df["volume"] - df["volume"].rolling(40).mean()) / df["volume"].rolling(40).std()
    rv = df["close"].pct_change().rolling(30).std()
    f["rv_z"] = (rv - rv.rolling(200).mean()) / rv.rolling(200).std()

    # taker flow
    net_quote = df["taker_buy_quote"] - (df["quote_volume"] - df["taker_buy_quote"])
    f["net_quote_sum10"] = net_quote.rolling(10).sum()
    f["net_quote_sum10_z"] = ((f["net_quote_sum10"] - f["net_quote_sum10"].rolling(200).mean())
                              / f["net_quote_sum10"].rolling(200).std())
    tbr = df["taker_buy_base"] / df["volume"].replace(0, np.nan)
    f["taker_imb_ema20"] = (2 * tbr - 1).ewm(span=20, adjust=False).mean()
    f["taker_imb_sum30"] = (2 * tbr - 1).rolling(30).sum()
    return f


def report_slice(name: str, mask: pd.Series, df: pd.DataFrame, side: str) -> None:
    sub = df[mask].dropna(subset=["fwd_ret"])
    n = len(sub)
    if n == 0:
        print(f"{name:<60} n=0")
        return
    if side == "PUT":
        wins = int((sub["fwd_ret"] < 0).sum())
    else:
        wins = int((sub["fwd_ret"] > 0).sum())
    wr = wins / n
    lb = wilson_lb(wins, n)
    # PnL with 5U stake, 1.8x payout (win +4U, loss -5U)
    pnl = wins * 4.0 - (n - wins) * 5.0
    daily = n / (len(df) / (24 * 60))
    print(f"{name:<60} n={n:>6}  WR={wr:.4f}  LB={lb:.4f}  PnL(5U)={pnl:+.0f}U  ~{daily:.2f}/day")


def main() -> None:
    df = build(load())
    print(f"loaded {len(df):,} rows")

    # A3 base trigger: extreme deviation, calm vol
    base_filter = (df["vol_z40"] < VOL_Z_MAX) & (df["rv_z"] < RV_Z_MAX)
    put_base = (df["ema120_dev"] >= EMA_DEV_THRESH) & base_filter
    call_base = (df["ema120_dev"] <= -EMA_DEV_THRESH) & base_filter

    print("\n=== Baseline A3 ===")
    report_slice("A3 PUT  (ema_dev>=1.4)", put_base, df, "PUT")
    report_slice("A3 CALL (ema_dev<=-1.4)", call_base, df, "CALL")
    report_slice("A3 BOTH", put_base | call_base, df, "BOTH-special")  # handled below

    # Special handling for combined: count side correctly
    both = df[put_base | call_base].dropna(subset=["fwd_ret"]).copy()
    both["win"] = np.where(both["ema120_dev"] >= EMA_DEV_THRESH,
                           (both["fwd_ret"] < 0).astype(int),
                           (both["fwd_ret"] > 0).astype(int))
    n = len(both); wins = int(both["win"].sum())
    print(f"A3 BOTH (corrected)                                          n={n:>6}  "
          f"WR={wins/n:.4f}  LB={wilson_lb(wins,n):.4f}  PnL(5U)={wins*4 - (n-wins)*5:+.0f}U")

    # Taker-flow secondary filter on PUT side
    print("\n=== A3 PUT ∩ taker-flow secondary filter ===")
    for col in ["net_quote_sum10_z", "taker_imb_ema20", "taker_imb_sum30", "net_quote_sum10"]:
        f = df[col]
        for q in [0.0, 0.3, 0.5, 0.7, 0.9]:
            thresh = f[put_base].quantile(q) if q > 0 else -np.inf
            mask = put_base & (f >= thresh)
            report_slice(f"PUT  {col} >= q{q:.1f} ({thresh:.3f})", mask, df, "PUT")
        print()

    print("=== A3 CALL ∩ taker-flow secondary filter ===")
    for col in ["net_quote_sum10_z", "taker_imb_ema20", "taker_imb_sum30", "net_quote_sum10"]:
        f = df[col]
        for q in [0.0, 0.1, 0.3, 0.5, 0.7]:
            thresh = f[call_base].quantile(q) if q < 1 else np.inf
            mask = call_base & (f <= thresh) if q < 1 else call_base
            report_slice(f"CALL {col} <= q{q:.1f} ({thresh:.3f})", mask, df, "CALL")
        print()


if __name__ == "__main__":
    main()
