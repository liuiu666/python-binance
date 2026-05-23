"""阈值松紧权衡实验：在不同 quantile / 静态 ATR 阈值下统计 10 分钟 WR / 频率 / PnL。

用于回答："偏离 -3.69 / -2.94 ATR 这种'差一点'的样本，把它们也吃了能赚吗？"
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures-extended.feather")
HORIZON = 10
WILSON_Z = 1.96


def wilson_lb(wins: int, n: int, z: float = WILSON_Z) -> float:
    if n == 0:
        return float("nan")
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def main() -> None:
    df = pd.read_feather(DATA).sort_values("date").reset_index(drop=True)
    df["fwd_ret"] = df["close"].shift(-HORIZON) / df["close"] - 1

    rng = df["high"] - df["low"]
    atr = rng.rolling(120).mean()
    ema = df["close"].ewm(span=120, adjust=False).mean()
    df["ev"] = (df["close"] - ema) / atr

    # 现有 vol_z / rv_z 过滤
    vmed = df["volume"].rolling(40).median()
    vstd = df["volume"].rolling(40).std()
    df["vol_z"] = (df["volume"] - vmed) / vstd
    logret = np.log(df["close"]).diff()
    rv60 = logret.rolling(60).std()
    rv_med = rv60.rolling(60 * 24).median()
    rv_std = rv60.rolling(60 * 24).std()
    df["rv_z"] = (rv60 - rv_med) / rv_std

    # 14 天滚动分位
    df["q_lo_05"] = df["ev"].rolling(60 * 24 * 14).quantile(0.05)
    df["q_lo_10"] = df["ev"].rolling(60 * 24 * 14).quantile(0.10)
    df["q_lo_15"] = df["ev"].rolling(60 * 24 * 14).quantile(0.15)
    df["q_lo_20"] = df["ev"].rolling(60 * 24 * 14).quantile(0.20)
    df["q_lo_25"] = df["ev"].rolling(60 * 24 * 14).quantile(0.25)
    df["q_hi_75"] = df["ev"].rolling(60 * 24 * 14).quantile(0.75)
    df["q_hi_80"] = df["ev"].rolling(60 * 24 * 14).quantile(0.80)
    df["q_hi_85"] = df["ev"].rolling(60 * 24 * 14).quantile(0.85)
    df["q_hi_90"] = df["ev"].rolling(60 * 24 * 14).quantile(0.90)
    df["q_hi_95"] = df["ev"].rolling(60 * 24 * 14).quantile(0.95)

    base = (df["vol_z"] < 1.0) & (df["rv_z"].abs() < 1.0) & df["ev"].notna() & df["fwd_ret"].notna()
    days = (df["date"].iloc[-1] - df["date"].iloc[0]).total_seconds() / 86400

    print(f"样本范围: {df['date'].iloc[0]} -> {df['date'].iloc[-1]}  ({days:.0f} 天)\n")
    print(f"{'方案':<35} {'触发数':>6} {'频率/天':>7} {'WR':>7} {'Wilson_LB':>10} {'PnL(5U)':>10}")
    print("-" * 90)

    schemes = [
        ("CALL q_lo<=0.05 (现状更严)", "ev <= q_lo_05", "fwd_ret > 0"),
        ("CALL q_lo<=0.10 (生产)", "ev <= q_lo_10", "fwd_ret > 0"),
        ("CALL q_lo<=0.15", "ev <= q_lo_15", "fwd_ret > 0"),
        ("CALL q_lo<=0.20 (放宽)", "ev <= q_lo_20", "fwd_ret > 0"),
        ("CALL q_lo<=0.25 (大放宽)", "ev <= q_lo_25", "fwd_ret > 0"),
        ("PUT  q_hi>=0.95 (现状更严)", "ev >= q_hi_95", "fwd_ret < 0"),
        ("PUT  q_hi>=0.90 (生产)", "ev >= q_hi_90", "fwd_ret < 0"),
        ("PUT  q_hi>=0.85", "ev >= q_hi_85", "fwd_ret < 0"),
        ("PUT  q_hi>=0.80 (放宽)", "ev >= q_hi_80", "fwd_ret < 0"),
        ("PUT  q_hi>=0.75 (大放宽)", "ev >= q_hi_75", "fwd_ret < 0"),
    ]

    for name, sig_expr, win_expr in schemes:
        sig = base & df.eval(sig_expr)
        sub = df[sig]
        n = len(sub)
        if n == 0:
            continue
        wins = int(sub.eval(win_expr).sum())
        wr = wins / n
        lb = wilson_lb(wins, n)
        pnl = wins * 4.0 - (n - wins) * 5.0   # 1.8x payout, 5U stake
        per_day = n / days
        print(f"{name:<35} {n:>6}  {per_day:>6.1f}  {wr:>6.4f}  {lb:>9.4f}  {pnl:>+9.0f}U")

    # 静态 ATR 阈值（不用 quantile）
    print("\n静态 ATR 阈值（不分高低波动期）：")
    print(f"{'方案':<35} {'触发数':>6} {'频率/天':>7} {'WR':>7} {'Wilson_LB':>10} {'PnL(5U)':>10}")
    print("-" * 90)
    for thresh in [3.0, 3.5, 4.0, 4.5, 5.0]:
        for side, sig_expr, win_expr in [
            ("CALL", f"ev <= -{thresh}", "fwd_ret > 0"),
            ("PUT",  f"ev >= {thresh}", "fwd_ret < 0"),
        ]:
            sig = base & df.eval(sig_expr)
            sub = df[sig]
            n = len(sub)
            if n == 0:
                continue
            wins = int(sub.eval(win_expr).sum())
            wr = wins / n
            lb = wilson_lb(wins, n)
            pnl = wins * 4.0 - (n - wins) * 5.0
            per_day = n / days
            label = f"{side} ev{'<=' if side=='CALL' else '>='}{'-' if side=='CALL' else '+'}{thresh}"
            print(f"{label:<35} {n:>6}  {per_day:>6.1f}  {wr:>6.4f}  {lb:>9.4f}  {pnl:>+9.0f}U")


if __name__ == "__main__":
    main()
