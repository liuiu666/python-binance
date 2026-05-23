"""Feature lab using extended futures data (taker buy volume / trades).

Goal: see whether order-flow proxies (taker buy ratio, trade-size, quote
imbalance) push 10-minute binary-option win-rates meaningfully above the
~58% OHLCV ceiling found in earlier research.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures-extended.feather")
HORIZON = 10  # minutes -> 10m binary expiry
WILSON_Z = 1.96


def wilson_lb(wins: int, n: int, z: float = WILSON_Z) -> float:
    if n == 0:
        return float("nan")
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def load() -> pd.DataFrame:
    df = pd.read_feather(DATA)
    df = df.sort_values("date").reset_index(drop=True)
    df["ret1"] = df["close"].pct_change()
    df["fwd_ret"] = df["close"].shift(-HORIZON) / df["close"] - 1
    df["y_call"] = (df["fwd_ret"] > 0).astype(int)
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=df.index)

    # Existing OHLCV anchors (for reference)
    ema120 = df["close"].ewm(span=120, adjust=False).mean()
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(60).mean()
    f["ema120_dev"] = (df["close"] - ema120) / atr
    f["vol_z40"] = (df["volume"] - df["volume"].rolling(40).mean()) / df["volume"].rolling(40).std()

    # Taker buy ratio: 0=all sells, 1=all buys
    tbr = df["taker_buy_base"] / df["volume"].replace(0, np.nan)
    f["taker_ratio"] = tbr
    f["taker_ratio_z40"] = (tbr - tbr.rolling(40).mean()) / tbr.rolling(40).std()
    f["taker_imbalance"] = (2 * tbr - 1)  # signed [-1, 1]
    f["taker_imb_ema20"] = f["taker_imbalance"].ewm(span=20, adjust=False).mean()
    f["taker_imb_sum10"] = f["taker_imbalance"].rolling(10).sum()
    f["taker_imb_sum30"] = f["taker_imbalance"].rolling(30).sum()

    # Quote imbalance vs total quote (price-weighted)
    qbr = df["taker_buy_quote"] / df["quote_volume"].replace(0, np.nan)
    f["quote_imb"] = 2 * qbr - 1
    f["quote_imb_sum10"] = f["quote_imb"].rolling(10).sum()

    # Trade-size: average notional per trade (informational flow)
    avg_notional = df["quote_volume"] / df["num_trades"].replace(0, np.nan)
    f["avg_notional_z40"] = (avg_notional - avg_notional.rolling(40).mean()) / avg_notional.rolling(40).std()
    f["num_trades_z40"] = (df["num_trades"] - df["num_trades"].rolling(40).mean()) / df["num_trades"].rolling(40).std()

    # Net taker dollar flow z-score
    net_quote = df["taker_buy_quote"] - (df["quote_volume"] - df["taker_buy_quote"])
    f["net_quote_z40"] = (net_quote - net_quote.rolling(40).mean()) / net_quote.rolling(40).std()
    f["net_quote_sum10"] = net_quote.rolling(10).sum()
    f["net_quote_sum10_z"] = (f["net_quote_sum10"] - f["net_quote_sum10"].rolling(200).mean()) / f["net_quote_sum10"].rolling(200).std()

    return f


def decile_table(feat: pd.Series, y: pd.Series, name: str) -> pd.DataFrame:
    mask = feat.notna() & y.notna()
    f = feat[mask]
    yy = y[mask]
    q = pd.qcut(f, 10, labels=False, duplicates="drop")
    rows = []
    for d in sorted(q.dropna().unique()):
        sel = q == d
        n = int(sel.sum())
        wr = float(yy[sel].mean())
        wins = int(yy[sel].sum())
        # CALL side WR == wr; PUT side WR == 1 - wr
        call_lb = wilson_lb(wins, n)
        put_lb = wilson_lb(n - wins, n)
        rows.append({
            "feature": name,
            "decile": int(d),
            "n": n,
            "call_wr": wr,
            "call_lb": call_lb,
            "put_wr": 1 - wr,
            "put_lb": put_lb,
            "best_lb": max(call_lb, put_lb),
        })
    return pd.DataFrame(rows)


def main() -> None:
    df = load()
    print(f"loaded {len(df):,} rows  range={df['date'].iloc[0]} -> {df['date'].iloc[-1]}")
    feats = build_features(df)
    y = df["y_call"]

    summary = []
    for col in feats.columns:
        tbl = decile_table(feats[col], y, col)
        if tbl.empty:
            continue
        best = tbl.sort_values("best_lb", ascending=False).iloc[0]
        summary.append({
            "feature": col,
            "best_decile": int(best["decile"]),
            "side": "CALL" if best["call_lb"] >= best["put_lb"] else "PUT",
            "n": int(best["n"]),
            "wr": best["call_wr"] if best["call_lb"] >= best["put_lb"] else best["put_wr"],
            "wilson_lb": best["best_lb"],
        })

    summary_df = pd.DataFrame(summary).sort_values("wilson_lb", ascending=False)
    pd.set_option("display.max_rows", 200)
    pd.set_option("display.width", 160)
    print("\n=== Best decile per feature (sorted by Wilson LB) ===")
    print(summary_df.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # Drill-down: top 5 features full decile table
    top5 = summary_df.head(5)["feature"].tolist()
    for feat in top5:
        print(f"\n--- decile detail: {feat} ---")
        print(decile_table(feats[feat], y, feat).to_string(index=False, float_format=lambda x: f"{x:.4f}"))


if __name__ == "__main__":
    main()
