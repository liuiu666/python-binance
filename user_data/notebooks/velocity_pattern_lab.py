"""涨跌速率规律实验：前 N 分钟的"速度 / 加速度"对后续 10 分钟方向的预测力。

不同于普通 ret_Nm（累积涨跌），重点放在：
  - 连续跌 / 连续涨 的 K 数 + 累计跌幅
  - 速率 = ret_Nm / N（每分钟平均涨跌幅）
  - 加速度 = 本期速率 - 上期速率
  - 5 分钟内最大回撤 / 最大涨幅
  - 单根 K 线幅度（极端单根 K 线后是反转还是续跌？）
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures-extended.feather")
HORIZON = 10
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


def build_velocity_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().sort_values("date").reset_index(drop=True)
    c, h, l = out["close"], out["high"], out["low"]
    rng = h - l
    atr = rng.rolling(120).mean()

    # 1. 平均速率 (return per minute)
    for k in [3, 5, 10, 15, 30]:
        out[f"vel_{k}m"] = c.pct_change(k) / k * 10000   # bp/min

    # 2. 加速度：本 5 分钟速率 vs 上 5 分钟速率
    ret5_now = c.pct_change(5)
    ret5_prev = c.shift(5).pct_change(5)
    out["accel_5m"] = (ret5_now - ret5_prev) * 10000  # bp 差值

    ret10_now = c.pct_change(10)
    ret10_prev = c.shift(10).pct_change(10)
    out["accel_10m"] = (ret10_now - ret10_prev) * 10000

    # 3. 连续涨跌 K 数 + 累计幅度
    up = (c > c.shift(1)).astype(int)
    dn = (c < c.shift(1)).astype(int)

    def streak(series: pd.Series) -> pd.Series:
        # 当前向上 / 向下连续根数（同方向链长）
        chg = (series != series.shift()).cumsum()
        return series.groupby(chg).cumsum() * series

    out["up_streak"] = streak(up)   # >0: 当前是连阳K线根数
    out["down_streak"] = streak(dn)

    # 连跌期间累计跌幅
    bar_ret = c.pct_change()
    grp = (np.sign(bar_ret) != np.sign(bar_ret.shift())).cumsum()
    cum_in_streak = bar_ret.groupby(grp).cumsum()
    out["streak_cum_ret"] = cum_in_streak * 10000  # bp

    # 4. 最近 5 / 10 / 15 分钟内最大单根跌 / 涨幅
    for k in [5, 10, 15]:
        out[f"max_drop_{k}m"] = bar_ret.rolling(k).min() * 10000
        out[f"max_rise_{k}m"] = bar_ret.rolling(k).max() * 10000

    # 5. 单根 K 线幅度（ATR 标准化）
    out["bar_range_atr"] = rng / atr
    out["bar_ret_atr"] = (c - c.shift(1)) / atr

    # 6. 最近 5/10 分钟内"是否单边" = 净涨幅 / 累计绝对值
    abs_ret = bar_ret.abs()
    for k in [5, 10]:
        net = c.pct_change(k)
        gross = abs_ret.rolling(k).sum()
        out[f"trendiness_{k}m"] = net / gross.replace(0, np.nan)   # -1~+1, 越极端越单边

    # 7. 5 分钟范围 / ATR — 短期波动暴增
    rolling_range = (h.rolling(5).max() - l.rolling(5).min())
    out["range5m_atr"] = rolling_range / atr

    # 目标
    out["fwd_ret"] = c.shift(-HORIZON) / c - 1
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
            "mean_fwd_bp": s["fwd_ret"].mean() * 10000,
        })
    return pd.DataFrame(rows)


def main() -> None:
    print("加载并构造速率特征...")
    df = build_velocity_features(pd.read_feather(DATA))
    print(f"样本: {len(df)} 行  时间: {df['date'].iloc[0]} -> {df['date'].iloc[-1]}\n")

    feats = [
        "vel_3m", "vel_5m", "vel_10m", "vel_15m", "vel_30m",
        "accel_5m", "accel_10m",
        "up_streak", "down_streak", "streak_cum_ret",
        "max_drop_5m", "max_drop_10m", "max_drop_15m",
        "max_rise_5m", "max_rise_10m", "max_rise_15m",
        "bar_ret_atr", "bar_range_atr",
        "trendiness_5m", "trendiness_10m",
        "range5m_atr",
    ]

    rows = []
    for f in feats:
        if f not in df.columns:
            continue
        d = stats_by_decile(df, f, n_bins=10)
        if not d.empty:
            rows.append(d)
    res = pd.concat(rows, ignore_index=True)

    # ---------- 详细打印 ----------
    print(f"{'特征':<22} {'Bin':>3} {'区间(单位bp或ATR)':<22} {'n':>5} {'P_涨':>6} {'P_跌':>6} {'P_震':>6} "
          f"{'CALL_LB':>8} {'PUT_LB':>8} {'均fwd_bp':>9}")
    print("-" * 130)
    summary = []
    for feat, g in res.groupby("feat", sort=False):
        for tag, row in [("低", g.iloc[0]), ("高", g.iloc[-1])]:
            rng_str = f"[{row['feat_lo']:+.3f},{row['feat_hi']:+.3f}]"
            print(f"{feat + '(' + tag + ')':<22} {int(row['bin']):>3} {rng_str:<22} "
                  f"{int(row['n']):>5} {row['P_up']:>5.3f} {row['P_down']:>5.3f} {row['P_range']:>5.3f} "
                  f"{row['LB_call']:>7.4f} {row['LB_put']:>7.4f} {row['mean_fwd_bp']:>+8.2f}")
            summary.append({
                "feat": feat, "tag": tag,
                "best_LB": max(row["LB_call"], row["LB_put"]),
                "side": "CALL" if row["LB_call"] >= row["LB_put"] else "PUT",
                "P_up": row["P_up"], "P_down": row["P_down"], "P_range": row["P_range"],
                "n": int(row["n"]),
                "mean_fwd_bp": row["mean_fwd_bp"],
            })
        print()

    sm = pd.DataFrame(summary).sort_values("best_LB", ascending=False).head(15)
    print("=" * 110)
    print("【速率类特征 TOP 15】(1.8x 二元期权盈亏平衡 = 55.56%)")
    print("=" * 110)
    print(f"{'排名':>3} {'特征':<22} {'极端':<5} {'方向':<5} {'n':>5} {'WR':>7} {'Wilson_LB':>10} {'P_震荡':>7} {'fwd_bp':>8}")
    for i, r in sm.iterrows():
        wr = r["P_up"] if r["side"] == "CALL" else r["P_down"]
        flag = "✓盈利" if r["best_LB"] >= 0.5556 else ""
        print(f"{i + 1:>3}. {r['feat']:<22} {r['tag']:<5} {r['side']:<5} {r['n']:>5} "
              f"{wr:>6.4f}  {r['best_LB']:>9.4f}  {r['P_range']:>6.3f}  {r['mean_fwd_bp']:>+7.2f}  {flag}")


if __name__ == "__main__":
    main()
