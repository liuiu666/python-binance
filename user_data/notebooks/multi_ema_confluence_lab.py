"""多期限 EMA 共振实验：要求 30/60/120/240 分钟 EMA 偏离同时到达极端才触发。

假设：单一 EMA 偏离 LB 最高 ~54.76%；如果不同时间尺度的 EMA 都说"价格离均值远了"，
信号置信度应该提升。代价是触发频率大幅下降。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures-extended.feather")
HORIZON = 10
WILSON_Z = 1.96
SPANS = [30, 60, 120, 240]
QUANTILE_WIN = 60 * 24 * 14   # 14 天滚动分位


def wilson_lb(wins: int, n: int, z: float = WILSON_Z) -> float:
    if n == 0:
        return float("nan")
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def main() -> None:
    print("加载数据并计算多期限 EMA 偏离...")
    df = pd.read_feather(DATA).sort_values("date").reset_index(drop=True)
    c, h, l = df["close"], df["high"], df["low"]
    rng = h - l
    atr = rng.rolling(120).mean()

    # 计算各期 EMA 偏离 + 14 天滚动分位
    for s in SPANS:
        ema = c.ewm(span=s, adjust=False).mean()
        df[f"d{s}"] = (c - ema) / atr

    # 每根 K 线对每个 EMA：当前偏离在过去 14 天的百分位
    # 用 rolling.rank 做近似（pct=True 给出当前值的分位）
    for s in SPANS:
        df[f"q{s}"] = df[f"d{s}"].rolling(QUANTILE_WIN, min_periods=QUANTILE_WIN // 2).rank(pct=True)

    # 简单波动 / 量过滤（沿用 A3 生产）
    logret = np.log(c).diff()
    rv60 = logret.rolling(60).std()
    rv_med = rv60.rolling(60 * 24).median()
    rv_std = rv60.rolling(60 * 24).std()
    df["rv_z"] = (rv60 - rv_med) / rv_std
    vmed = df["volume"].rolling(40).median()
    vstd = df["volume"].rolling(40).std()
    df["vol_z"] = (df["volume"] - vmed) / vstd

    df["fwd_ret"] = c.shift(-HORIZON) / c - 1
    # 与生产 A3 一致：要求 vol_z > 1.0 (放量)，|rv_z| < 1.0 (波动正常)
    base_filter = (df["vol_z"] > 1.0) & (df["rv_z"].abs() < 1.0) & df["fwd_ret"].notna()
    days = (df["date"].iloc[-1] - df["date"].iloc[0]).total_seconds() / 86400

    print(f"样本: {len(df):,}  天数 {days:.0f}  基线触发数（仅 vol_z/rv_z 过滤): {int(base_filter.sum()):,}\n")

    # ---------- 实验：要求 K 个 EMA 同时在低/高分位 ----------
    print("规则：在过去 14 天分位中，要求 K 个 EMA 偏离同时 ≤ q_lo 或 ≥ q_hi 才触发\n")

    schemes = []
    for q_lo, q_hi in [(0.10, 0.90), (0.05, 0.95), (0.15, 0.85), (0.20, 0.80)]:
        for k_min in [1, 2, 3, 4]:
            for side in ["CALL", "PUT"]:
                if side == "CALL":
                    flags = [(df[f"q{s}"] <= q_lo).fillna(False) for s in SPANS]
                    win = df["fwd_ret"] > 0
                else:
                    flags = [(df[f"q{s}"] >= q_hi).fillna(False) for s in SPANS]
                    win = df["fwd_ret"] < 0
                # 至少 K 个 EMA 同时极端
                count = sum(f.astype(int) for f in flags)
                trig = base_filter & (count >= k_min)
                n = int(trig.sum())
                if n < 30:
                    continue
                wins = int((trig & win).sum())
                wr = wins / n
                lb = wilson_lb(wins, n)
                pnl = wins * 4.0 - (n - wins) * 5.0
                per_day = n / days
                schemes.append({
                    "side": side, "q_lo": q_lo, "q_hi": q_hi,
                    "k_min": k_min, "n": n, "per_day": per_day,
                    "wr": wr, "lb": lb, "pnl": pnl,
                })

    res = pd.DataFrame(schemes)

    # 打印分组：CALL 和 PUT 分开
    for side in ["CALL", "PUT"]:
        print(f"=== {side} 侧（CALL=fwd>0 / PUT=fwd<0）===")
        print(f"{'q_lo/hi':>8} {'K个EMA':>7} {'触发数':>7} {'频/天':>7} {'WR':>7} {'Wilson_LB':>10} {'年PnL(5U)':>11} 评价")
        sub = res[res["side"] == side].sort_values(["k_min", "q_lo"])
        for _, r in sub.iterrows():
            q_str = f"{r['q_lo']:.2f}" if side == "CALL" else f"{r['q_hi']:.2f}"
            flag = "✓盈利" if r["lb"] >= 0.5556 else ("●进了" if r["wr"] >= 0.5556 else "")
            print(f"{q_str:>8} {int(r['k_min']):>7} {int(r['n']):>7} {r['per_day']:>6.1f}  "
                  f"{r['wr']:>6.4f}  {r['lb']:>9.4f}  {r['pnl']:>+10.0f}U  {flag}")
        print()

    # 找 LB 最高且年 PnL > 0 的方案
    print("=" * 95)
    print("【LB 排序 TOP 10（要求 PnL > 0）】(1.8x 二元期权盈亏平衡 = 55.56%)")
    print("=" * 95)
    profit = res[res["pnl"] > 0].sort_values("lb", ascending=False).head(10)
    print(f"{'方向':<5} {'分位':<10} {'K个':<5} {'触发数':>7} {'频/天':>7} {'WR':>7} {'Wilson_LB':>10} {'年PnL':>10}")
    for _, r in profit.iterrows():
        q_str = f"q≤{r['q_lo']:.2f}" if r["side"] == "CALL" else f"q≥{r['q_hi']:.2f}"
        print(f"{r['side']:<5} {q_str:<10} {int(r['k_min']):<5} {int(r['n']):>7} "
              f"{r['per_day']:>6.1f}  {r['wr']:>6.4f}  {r['lb']:>9.4f}  {r['pnl']:>+9.0f}U")


if __name__ == "__main__":
    main()
