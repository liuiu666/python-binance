"""前置规律实验 (2m 版)：把 1m K 线聚合成 2m，看降噪后的预测力变化。

10 分钟期权 = 5 根 2m K 线。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures-extended.feather")
HORIZON_BARS = 5            # 5 根 2m = 10 分钟
WILSON_Z = 1.96
RANGE_THRESH = 0.0005


def wilson_lb(wins: int, n: int, z: float = WILSON_Z) -> float:
    if n == 0:
        return float("nan")
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def aggregate_2m(df1: pd.DataFrame) -> pd.DataFrame:
    df1 = df1.sort_values("date").reset_index(drop=True)
    df1["date"] = pd.to_datetime(df1["date"], utc=True)
    df1 = df1.set_index("date")
    agg = {
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum",
    }
    if "quote_volume" in df1.columns:
        agg["quote_volume"] = "sum"
    if "num_trades" in df1.columns:
        agg["num_trades"] = "sum"
    if "taker_buy_base" in df1.columns:
        agg["taker_buy_base"] = "sum"
    if "taker_buy_quote" in df1.columns:
        agg["taker_buy_quote"] = "sum"
    df2 = df1.resample("2T", label="left", closed="left").agg(agg).dropna(subset=["open"])
    df2 = df2.reset_index()
    return df2


def build_features(out: pd.DataFrame) -> pd.DataFrame:
    c, h, l, o, v = out["close"], out["high"], out["low"], out["open"], out["volume"]

    # 收益（以 2m 为单位，所以 ret_5m -> 这里就是 ret_2bars）
    for k in [1, 2, 3, 5, 15]:
        out[f"ret_{k * 2}m"] = c.pct_change(k)

    rng = h - l
    atr = rng.rolling(60).mean()    # 60 根 2m = 2 小时
    for span in [15, 30, 60, 120]:  # 30/60/120/240 分钟
        ema = c.ewm(span=span, adjust=False).mean()
        out[f"ema{span * 2}m_dev"] = (c - ema) / atr

    out["dev120m_accel"] = out["ema120m_dev"] - out["ema120m_dev"].shift(3)

    logret = np.log(c).diff()
    rv30 = logret.rolling(30).std()
    rv_med = rv30.rolling(720).median()
    rv_std = rv30.rolling(720).std()
    out["rv_z"] = (rv30 - rv_med) / rv_std

    vmed = v.rolling(20).median()
    vstd = v.rolling(20).std()
    out["vol_z"] = (v - vmed) / vstd

    body = (c - o).abs()
    out["body_ratio"] = body / rng.replace(0, np.nan)
    out["upper_wick"] = (h - np.maximum(o, c)) / rng.replace(0, np.nan)
    out["lower_wick"] = (np.minimum(o, c) - l) / rng.replace(0, np.nan)

    up = (c > c.shift(1)).astype(int)
    out["streak_up3"] = up.rolling(3).sum()
    out["streak_up5"] = up.rolling(5).sum()

    out["breakout_hi15"] = (c - c.rolling(15).max().shift(1)) / atr
    out["breakout_lo15"] = (c - c.rolling(15).min().shift(1)) / atr

    if "taker_buy_quote" in out.columns and "quote_volume" in out.columns:
        qv = out["quote_volume"].replace(0, np.nan)
        out["taker_imb"] = 2 * out["taker_buy_quote"] / qv - 1
        net = out["taker_buy_quote"] - (qv - out["taker_buy_quote"])
        out["net_taker_3bar"] = net.rolling(3).sum() / qv.rolling(3).sum()

    if "num_trades" in out.columns and "quote_volume" in out.columns:
        out["trade_size"] = out["quote_volume"] / out["num_trades"].replace(0, np.nan)
        out["trade_size_z"] = (out["trade_size"] - out["trade_size"].rolling(30).median()) / \
                              out["trade_size"].rolling(30).std()

    out["fwd_ret"] = c.shift(-HORIZON_BARS) / c - 1
    return out


def stats_by_decile(df: pd.DataFrame, feat: str, n_bins: int = 10) -> pd.DataFrame:
    sub = df[[feat, "fwd_ret"]].dropna()
    if len(sub) < n_bins * 50:
        return pd.DataFrame()
    sub = sub.copy()
    sub["bin"] = pd.qcut(sub[feat], n_bins, labels=False, duplicates="drop")
    rows = []
    for b in range(int(sub["bin"].max()) + 1):
        s = sub[sub["bin"] == b]
        n = len(s)
        if n == 0:
            continue
        p_up = (s["fwd_ret"] > 0).mean()
        p_dn = (s["fwd_ret"] < 0).mean()
        p_rng = (s["fwd_ret"].abs() < RANGE_THRESH).mean()
        wins_up = int((s["fwd_ret"] > 0).sum())
        wins_dn = int((s["fwd_ret"] < 0).sum())
        rows.append({
            "feat": feat, "bin": b, "n": n,
            "feat_lo": s[feat].min(), "feat_hi": s[feat].max(),
            "P_up": p_up, "P_down": p_dn, "P_range": p_rng,
            "WR_call": p_up, "LB_call": wilson_lb(wins_up, n),
            "WR_put": p_dn, "LB_put": wilson_lb(wins_dn, n),
        })
    return pd.DataFrame(rows)


def main() -> None:
    print("加载 1m 数据并聚合到 2m...")
    df1 = pd.read_feather(DATA)
    df = aggregate_2m(df1)
    df = build_features(df)
    print(f"2m 样本: {len(df)} 行  时间: {df['date'].iloc[0]} -> {df['date'].iloc[-1]}\n")

    overall_up = (df["fwd_ret"] > 0).mean()
    overall_rng = (df["fwd_ret"].abs() < RANGE_THRESH).mean()
    print(f"=== 全样本基线 (2m) ===")
    print(f"P(10m 涨) = {overall_up:.4f}")
    print(f"P(10m 跌) = {1 - overall_up:.4f}")
    print(f"P(10m 震荡 |ret|<5bp) = {overall_rng:.4f}\n")

    feats = [c for c in [
        "ret_2m", "ret_4m", "ret_6m", "ret_10m", "ret_30m",
        "ema30m_dev", "ema60m_dev", "ema120m_dev", "ema240m_dev",
        "dev120m_accel",
        "rv_z", "vol_z",
        "body_ratio", "upper_wick", "lower_wick",
        "streak_up3", "streak_up5",
        "breakout_hi15", "breakout_lo15",
        "taker_imb", "net_taker_3bar", "trade_size_z",
    ] if c in df.columns]

    all_rows = []
    for f in feats:
        d = stats_by_decile(df, f, n_bins=10)
        if not d.empty:
            all_rows.append(d)
    res = pd.concat(all_rows, ignore_index=True)

    print(f"{'特征':<18} {'Bin':>3} {'区间':<22} {'n':>5} {'P_涨':>6} {'P_跌':>6} {'P_震':>6} {'CALL_LB':>8} {'PUT_LB':>8}")
    print("-" * 110)
    summary = []
    for feat, g in res.groupby("feat", sort=False):
        lo, hi = g.iloc[0], g.iloc[-1]
        for tag, row in [("低", lo), ("高", hi)]:
            rng_str = f"[{row['feat_lo']:+.4f},{row['feat_hi']:+.4f}]"
            print(f"{feat + '(' + tag + ')':<18} {int(row['bin']):>3} {rng_str:<22} "
                  f"{int(row['n']):>5} {row['P_up']:>5.3f} {row['P_down']:>5.3f} {row['P_range']:>5.3f} "
                  f"{row['LB_call']:>7.4f} {row['LB_put']:>7.4f}")
            summary.append({
                "feat": feat, "tag": tag,
                "best_LB": max(row["LB_call"], row["LB_put"]),
                "side": "CALL" if row["LB_call"] >= row["LB_put"] else "PUT",
                "P_up": row["P_up"], "P_down": row["P_down"], "P_range": row["P_range"],
                "n": int(row["n"]),
            })
        print()

    sm = pd.DataFrame(summary).sort_values("best_LB", ascending=False).head(12)
    print("=" * 100)
    print("【2m TOP 12 最强单特征极端规律】(1.8x 二元期权盈亏平衡 = 55.56%)")
    print("=" * 100)
    print(f"{'排名':>3} {'特征':<18} {'极端':<5} {'方向':<5} {'n':>5} {'WR':>7} {'Wilson_LB':>10} {'P_震荡':>7} 评价")
    for i, r in sm.iterrows():
        wr = r["P_up"] if r["side"] == "CALL" else r["P_down"]
        flag = "✓盈利" if r["best_LB"] >= 0.5556 else ""
        print(f"{i + 1:>3}. {r['feat']:<18} {r['tag']:<5} {r['side']:<5} {r['n']:>5} "
              f"{wr:>6.4f}  {r['best_LB']:>9.4f}  {r['P_range']:>6.3f}  {flag}")


if __name__ == "__main__":
    main()
