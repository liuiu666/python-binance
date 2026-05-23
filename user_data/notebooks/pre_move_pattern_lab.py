"""前置规律实验：找出真正能预测后续 10 分钟方向的 1m 特征。

对每根 1m K 线计算 ~15 个候选特征，按极端值切片，统计 10 分钟后：
  - 涨概率 P(fwd_ret > 0)
  - 跌概率 P(fwd_ret < 0)
  - 震荡概率 P(|fwd_ret| < 阈值)
  - 1.8x 二元期权 WR 与 Wilson LB

输出每个特征的最强极端 decile，按 Wilson LB 排序，揭示"涨前、跌前长什么样"。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures-extended.feather")
HORIZON = 10
WILSON_Z = 1.96
RANGE_THRESH = 0.0005   # |fwd_ret| < 0.05% 视为震荡


def wilson_lb(wins: int, n: int, z: float = WILSON_Z) -> float:
    if n == 0:
        return float("nan")
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().sort_values("date").reset_index(drop=True)
    c, h, l, o, v = out["close"], out["high"], out["low"], out["open"], out["volume"]

    # 收益 / 动量
    out["ret_1m"] = c.pct_change(1)
    out["ret_3m"] = c.pct_change(3)
    out["ret_5m"] = c.pct_change(5)
    out["ret_10m"] = c.pct_change(10)
    out["ret_30m"] = c.pct_change(30)

    # 偏离（多个 EMA span）
    rng = h - l
    atr = rng.rolling(120).mean()
    for span in [30, 60, 120, 240]:
        ema = c.ewm(span=span, adjust=False).mean()
        out[f"ema{span}_dev"] = (c - ema) / atr

    # 偏离加速度（ema_dev 5 分钟变化）
    out["dev120_accel"] = out["ema120_dev"] - out["ema120_dev"].shift(5)

    # 波动
    logret = np.log(c).diff()
    rv60 = logret.rolling(60).std()
    rv_med = rv60.rolling(60 * 24).median()
    rv_std = rv60.rolling(60 * 24).std()
    out["rv_z"] = (rv60 - rv_med) / rv_std
    out["rv60"] = rv60

    # 成交量
    vmed = v.rolling(40).median()
    vstd = v.rolling(40).std()
    out["vol_z"] = (v - vmed) / vstd

    # K 线形态
    body = (c - o).abs()
    out["body_ratio"] = body / rng.replace(0, np.nan)
    out["upper_wick"] = (h - np.maximum(o, c)) / rng.replace(0, np.nan)
    out["lower_wick"] = (np.minimum(o, c) - l) / rng.replace(0, np.nan)

    # 连续涨跌
    up = (c > c.shift(1)).astype(int)
    out["streak_up3"] = up.rolling(3).sum()  # 0~3
    out["streak_up5"] = up.rolling(5).sum()

    # 突破：当前收盘 vs 最近 30 分钟最高/最低
    out["breakout_hi30"] = (c - c.rolling(30).max().shift(1)) / atr
    out["breakout_lo30"] = (c - c.rolling(30).min().shift(1)) / atr

    # taker 主动方向（如有）
    if "taker_buy_quote" in out.columns and "quote_volume" in out.columns:
        qv = out["quote_volume"].replace(0, np.nan)
        out["taker_imb"] = 2 * out["taker_buy_quote"] / qv - 1   # -1 全卖, +1 全买
        # 5 分钟主动方向累积
        net = out["taker_buy_quote"] - (qv - out["taker_buy_quote"])
        out["net_taker_5m"] = net.rolling(5).sum() / qv.rolling(5).sum()

    # 大单密度
    if "num_trades" in out.columns and "quote_volume" in out.columns:
        out["trade_size"] = out["quote_volume"] / out["num_trades"].replace(0, np.nan)
        out["trade_size_z"] = (out["trade_size"] - out["trade_size"].rolling(60).median()) / \
                              out["trade_size"].rolling(60).std()

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
    print("加载扩展 1m 数据...")
    df = build_features(pd.read_feather(DATA))
    print(f"样本: {len(df)} 行  时间: {df['date'].iloc[0]} -> {df['date'].iloc[-1]}\n")

    feats = [
        "ret_1m", "ret_3m", "ret_5m", "ret_10m", "ret_30m",
        "ema30_dev", "ema60_dev", "ema120_dev", "ema240_dev",
        "dev120_accel",
        "rv_z", "vol_z",
        "body_ratio", "upper_wick", "lower_wick",
        "streak_up3", "streak_up5",
        "breakout_hi30", "breakout_lo30",
    ]
    if "taker_imb" in df.columns:
        feats += ["taker_imb", "net_taker_5m", "trade_size_z"]

    all_rows = []
    for f in feats:
        if f not in df.columns:
            continue
        d = stats_by_decile(df, f, n_bins=10)
        if not d.empty:
            all_rows.append(d)
    if not all_rows:
        print("没有可用特征")
        return

    res = pd.concat(all_rows, ignore_index=True)

    # ---------- 总体方向偏好 (基线) ----------
    overall_up = (df["fwd_ret"] > 0).mean()
    overall_rng = (df["fwd_ret"].abs() < RANGE_THRESH).mean()
    print(f"=== 全样本基线 ===")
    print(f"P(10m 涨) = {overall_up:.4f}")
    print(f"P(10m 跌) = {1 - overall_up - 0:.4f}")
    print(f"P(10m 震荡 |ret|<5bp) = {overall_rng:.4f}\n")

    # ---------- 每个特征：最强 CALL 极端 + 最强 PUT 极端 ----------
    print(f"{'特征':<18} {'Bin':>3} {'区间':<22} {'n':>5} {'P_涨':>6} {'P_跌':>6} {'P_震':>6} {'CALL_LB':>8} {'PUT_LB':>8}")
    print("-" * 120)

    summary = []
    for feat, g in res.groupby("feat", sort=False):
        # 极端低 bin
        lo = g.iloc[0]
        # 极端高 bin
        hi = g.iloc[-1]
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

    # ---------- TOP 5 最强极端规律 ----------
    sm = pd.DataFrame(summary).sort_values("best_LB", ascending=False).head(10)
    print("=" * 110)
    print("【TOP 10 最强单特征极端规律】(1.8x 二元期权盈亏平衡 = 55.56%)")
    print("=" * 110)
    print(f"{'排名':>3} {'特征':<18} {'方向':<5} {'方向类型':<5} {'n':>5} {'WR':>7} {'Wilson_LB':>10} {'P_震荡':>7}")
    for i, r in sm.iterrows():
        wr = r["P_up"] if r["side"] == "CALL" else r["P_down"]
        flag = "✓盈利" if r["best_LB"] >= 0.5556 else " "
        print(f"{i + 1:>3}. {r['feat']:<18} {r['tag']:<5} {r['side']:<5} {r['n']:>5} "
              f"{wr:>6.4f}  {r['best_LB']:>9.4f}  {r['P_range']:>6.3f}  {flag}")


if __name__ == "__main__":
    main()
