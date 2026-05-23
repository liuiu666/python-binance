"""不同到期时间（5/10/15/20/30 分钟）下 A4 多期共振信号的胜率对比。

回答：30 分钟二元期权用 A4 同样的规则会更好/更差/差不多吗？
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures-extended.feather")
HORIZONS = [5, 10, 15, 20, 30]   # 分钟
WILSON_Z = 1.96
SPANS = [30, 60, 120, 240]
QUANTILE_WIN = 60 * 24 * 14


def wilson_lb(wins: int, n: int, z: float = WILSON_Z) -> float:
    if n == 0:
        return float("nan")
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def main() -> None:
    print("加载数据并计算公共特征...")
    df = pd.read_feather(DATA).sort_values("date").reset_index(drop=True)
    c, h, l = df["close"], df["high"], df["low"]
    rng = h - l
    atr = rng.rolling(120).mean()
    for s in SPANS:
        ema = c.ewm(span=s, adjust=False).mean()
        df[f"d{s}"] = (c - ema) / atr
        df[f"q{s}"] = df[f"d{s}"].rolling(QUANTILE_WIN, min_periods=QUANTILE_WIN // 2).rank(pct=True)

    logret = np.log(c).diff()
    rv60 = logret.rolling(60).std()
    df["rv_z"] = (rv60 - rv60.rolling(60 * 24).median()) / rv60.rolling(60 * 24).std()
    vmed = df["volume"].rolling(40).median()
    vstd = df["volume"].rolling(40).std()
    df["vol_z"] = (df["volume"] - vmed) / vstd
    base_filter = (df["vol_z"] > 1.0) & (df["rv_z"].abs() < 1.0)
    days = (df["date"].iloc[-1] - df["date"].iloc[0]).total_seconds() / 86400

    # 4 类 A4 信号（与生产 runner 一致）
    hq_lo_count = sum((df[f"q{s}"] <= 0.05).astype(int) for s in SPANS)
    norm_lo_count = sum((df[f"q{s}"] <= 0.10).astype(int) for s in SPANS)
    hq_hi_count = sum((df[f"q{s}"] >= 0.95).astype(int) for s in SPANS)
    norm_hi_count = sum((df[f"q{s}"] >= 0.90).astype(int) for s in SPANS)

    sig_hq_call = base_filter & (hq_lo_count >= 4)
    sig_hq_put  = base_filter & (hq_hi_count >= 2)
    sig_norm_call = base_filter & (norm_lo_count >= 4) & ~sig_hq_call
    sig_norm_put  = base_filter & (norm_hi_count >= 3) & ~sig_hq_put

    print(f"样本: {len(df):,}  天数 {days:.0f}\n")
    print("【A4 信号在不同到期时间的表现对比】(1.8x 二元期权盈亏平衡 = 55.56%)")
    print("=" * 110)

    for H in HORIZONS:
        df["fwd"] = c.shift(-H) / c - 1
        # PnL 用 5U 仓位计算（高质和普通用同一仓位以便公平对比）
        rows = []
        for sig_name, sig, side in [
            ("HQ CALL",   sig_hq_call,   "call"),
            ("HQ PUT",    sig_hq_put,    "put"),
            ("NORM CALL", sig_norm_call, "call"),
            ("NORM PUT",  sig_norm_put,  "put"),
        ]:
            mask = sig & df["fwd"].notna()
            sub = df[mask]
            n = len(sub)
            if n == 0:
                continue
            wins = int((sub["fwd"] > 0).sum() if side == "call" else (sub["fwd"] < 0).sum())
            wr = wins / n
            lb = wilson_lb(wins, n)
            pnl = wins * 4.0 - (n - wins) * 5.0
            rows.append({"sig": sig_name, "n": n, "per_day": n / days,
                         "wr": wr, "lb": lb, "pnl_5U": pnl})

        # 总和
        total_n = sum(r["n"] for r in rows)
        total_pnl = sum(r["pnl_5U"] for r in rows)
        total_per_day = total_n / days

        print(f"\n--- 到期 = {H} 分钟 ---")
        print(f"{'信号':<12} {'n':>6} {'频/天':>7} {'WR':>7} {'Wilson_LB':>10} {'年PnL(5U)':>12}")
        for r in rows:
            flag = "✓" if r["lb"] >= 0.5556 else " "
            print(f"{r['sig']:<12} {r['n']:>6} {r['per_day']:>6.1f}  {r['wr']:>6.4f}  "
                  f"{r['lb']:>9.4f}  {r['pnl_5U']:>+11.0f}U  {flag}")
        print(f"{'合计':<12} {total_n:>6} {total_per_day:>6.1f}  {'':>7} {'':>10} {total_pnl:>+11.0f}U")

    # 横向汇总：同一规则不同到期下的 LB 排序
    print("\n" + "=" * 110)
    print("【横向对比表】每个信号 × 每个到期时间的 Wilson LB / 年 PnL")
    print("=" * 110)
    print(f"\n{'信号':<12}", end="")
    for H in HORIZONS:
        print(f" {H:>3}m_LB     PnL", end="")
    print()
    print("-" * 110)

    # 重算结构化
    summary = {}
    for sig_name, sig, side in [
        ("HQ CALL",   sig_hq_call,   "call"),
        ("HQ PUT",    sig_hq_put,    "put"),
        ("NORM CALL", sig_norm_call, "call"),
        ("NORM PUT",  sig_norm_put,  "put"),
    ]:
        summary[sig_name] = {}
        for H in HORIZONS:
            df["fwd"] = c.shift(-H) / c - 1
            mask = sig & df["fwd"].notna()
            sub = df[mask]
            n = len(sub)
            wins = int((sub["fwd"] > 0).sum() if side == "call" else (sub["fwd"] < 0).sum())
            lb = wilson_lb(wins, n) if n > 0 else float("nan")
            pnl = wins * 4.0 - (n - wins) * 5.0
            summary[sig_name][H] = (lb, pnl)

    for sig_name in summary:
        print(f"{sig_name:<12}", end="")
        for H in HORIZONS:
            lb, pnl = summary[sig_name][H]
            print(f"  {lb*100:5.2f}% {pnl:+7.0f}", end="")
        print()


if __name__ == "__main__":
    main()
