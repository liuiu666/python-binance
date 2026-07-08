"""
analyze_loss_cause.py
======================
深度分析亏损原因：
  1. 亏损 vs 盈利的市场状态对比（趋势、波动率、时间）
  2. 亏损是否聚集（连败结构）
  3. 趋势行情 vs 震荡行情下的胜率差异
  4. 入场时 z_score 分布 vs 实际结果
  5. 针对各原因的过滤方案测试
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from second_backtest.data import load_second_bars

CSV_PATH = ROOT / "tmp" / "current_live_pull_20260623_214203" / "data" / "btcusdt_1s_trades.csv"
OUT_PATH  = ROOT / "tmp" / "loss_cause_analysis.json"
HORIZON   = 600
COOLDOWN  = 600
WIN_PAY, LOSS_PAY = 4, -5
BREAKEVEN = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY))

LOOKBACK  = 4200
TAIL_PCT  = 0.20

def normal_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def collect_trades_with_context(bars: pd.DataFrame) -> list[dict]:
    """回测并附带丰富的市场上下文信息。"""
    close    = bars["close"].to_numpy(float)
    high     = bars["high"].to_numpy(float)
    low      = bars["low"].to_numpy(float)
    buy_qty  = bars["buy_qty"].to_numpy(float)
    sell_qty = bars["sell_qty"].to_numpy(float)
    volume   = bars["volume"].to_numpy(float)
    times    = bars.index

    lr     = np.diff(np.log(close), prepend=np.nan)
    series = pd.Series(lr, index=times)
    min_p  = max(60, LOOKBACK // 4)
    mu_arr = series.rolling(LOOKBACK, min_periods=min_p).mean().to_numpy(float)
    sg_arr = series.rolling(LOOKBACK, min_periods=min_p).std(ddof=1).to_numpy(float)

    # 预计算各种上下文指标
    close_s = pd.Series(close, index=times)

    # 趋势：不同窗口的斜率
    slope300  = (close_s / close_s.shift(300)  - 1).to_numpy(float) * 10000   # bps
    slope1800 = (close_s / close_s.shift(1800) - 1).to_numpy(float) * 10000
    slope3600 = (close_s / close_s.shift(3600) - 1).to_numpy(float) * 10000

    # 波动率：rolling ATR-like
    abs_lr_s = pd.Series(np.abs(lr), index=times)
    atr60    = abs_lr_s.rolling(60,   min_periods=10).mean().to_numpy(float) * 10000  # bps/sec
    atr600   = abs_lr_s.rolling(600,  min_periods=60).mean().to_numpy(float) * 10000
    atr3600  = abs_lr_s.rolling(3600, min_periods=300).mean().to_numpy(float) * 10000

    # Taker flow
    buy_s  = pd.Series(buy_qty,  index=times)
    sell_s = pd.Series(sell_qty, index=times)
    flow60  = ((buy_s - sell_s) / (buy_s + sell_s + 1e-12)).rolling(60,  min_periods=10).mean().to_numpy(float)
    flow300 = ((buy_s - sell_s) / (buy_s + sell_s + 1e-12)).rolling(300, min_periods=30).mean().to_numpy(float)

    # 价格在区间内的位置（均值回归程度）
    roll_max = close_s.rolling(LOOKBACK, min_periods=min_p).max().to_numpy(float)
    roll_min = close_s.rolling(LOOKBACK, min_periods=min_p).min().to_numpy(float)
    roll_mean = close_s.rolling(LOOKBACK, min_periods=min_p).mean().to_numpy(float)

    trades = []
    last_signal = -COOLDOWN

    for i in range(LOOKBACK, len(close) - HORIZON):
        if i - last_signal < COOLDOWN:
            continue
        mu, sg = mu_arr[i], sg_arr[i]
        if not (np.isfinite(mu) and np.isfinite(sg) and sg > 1e-12):
            continue

        z    = HORIZON * mu / (math.sqrt(HORIZON) * sg)
        p_up = normal_cdf(z)

        if p_up >= 1.0 - TAIL_PCT:
            signal = "DOWN"
        elif p_up <= TAIL_PCT:
            signal = "UP"
        else:
            continue

        entry  = float(close[i])
        settle = float(close[i + HORIZON])
        won    = (settle > entry) if signal == "UP" else (settle < entry)
        side   = 1.0 if signal == "UP" else -1.0

        # 10分钟内的价格路径
        path = close[i:i + HORIZON + 1]
        max_favor  = side * (np.max(path if signal == "UP" else -path) - entry) / entry * 10000
        max_advers = side * (np.min(path if signal == "UP" else -path) - entry) / entry * 10000

        # 是否曾经领先过（至少 +5bps）
        ever_ahead = bool(max_favor >= 5.0)

        # 时间上下文
        ts = times[i]
        hour_utc = ts.hour if hasattr(ts, 'hour') else int(str(ts)[11:13])

        # range位置（0=底部，1=顶部）
        rng = float(roll_max[i] - roll_min[i])
        range_pos = float((entry - roll_min[i]) / rng) if rng > 0.01 else 0.5

        # 趋势强度（绝对值越大越趋势）
        trend_300  = float(slope300[i])  if np.isfinite(slope300[i])  else 0.0
        trend_1800 = float(slope1800[i]) if np.isfinite(slope1800[i]) else 0.0
        trend_3600 = float(slope3600[i]) if np.isfinite(slope3600[i]) else 0.0

        # aligned: 信号方向是否与短期趋势一致
        aligned_trend = side * trend_300 > 0  # 顺势
        counter_trend = side * trend_300 < 0  # 逆势（均值回归应该是counter）

        trades.append({
            "idx":         i,
            "time":        str(ts)[:19],
            "date":        str(ts)[:10],
            "hour_utc":    hour_utc,
            "signal":      signal,
            "won":         bool(won),
            "pnl":         WIN_PAY if won else LOSS_PAY,
            "z":           round(float(z), 4),
            "p_up":        round(float(p_up), 4),
            "entry":       round(entry, 2),
            "settle":      round(settle, 2),
            "ret_bps":     round((settle - entry) / entry * 10000, 2),
            # 路径
            "max_favor_bps":  round(float(max_favor), 2),
            "max_advers_bps": round(float(max_advers), 2),
            "ever_ahead":     ever_ahead,
            # 波动率
            "atr60_bps":   round(float(atr60[i])  if np.isfinite(atr60[i])  else 0, 4),
            "atr600_bps":  round(float(atr600[i]) if np.isfinite(atr600[i]) else 0, 4),
            "atr3600_bps": round(float(atr3600[i])if np.isfinite(atr3600[i])else 0, 4),
            "vol_ratio":   round(float(atr60[i]/atr3600[i]) if np.isfinite(atr3600[i]) and atr3600[i] > 0 else 1.0, 3),
            # sigma
            "sigma_10m_bps": round(math.sqrt(HORIZON) * float(sg) * 10000, 2),
            # 趋势
            "slope300_bps":  round(trend_300,  2),
            "slope1800_bps": round(trend_1800, 2),
            "slope3600_bps": round(trend_3600, 2),
            "aligned_300":   bool(aligned_trend),
            "counter_300":   bool(counter_trend),
            # flow
            "flow60":  round(float(flow60[i])  if np.isfinite(flow60[i])  else 0, 4),
            "flow300": round(float(flow300[i]) if np.isfinite(flow300[i]) else 0, 4),
            # 区间位置
            "range_pos":   round(range_pos, 3),
            "range_bps":   round(rng / entry * 10000, 2) if entry > 0 else 0,
        })
        last_signal = i

    return trades


def compare_win_loss(trades: list[dict], field: str, label: str = None) -> dict:
    wins  = [t[field] for t in trades if t["won"]  and np.isfinite(t[field])]
    losses= [t[field] for t in trades if not t["won"] and np.isfinite(t[field])]
    if not wins or not losses:
        return {}
    return {
        "field": field,
        "label": label or field,
        "win_mean":  round(float(np.mean(wins)), 4),
        "loss_mean": round(float(np.mean(losses)), 4),
        "diff":      round(float(np.mean(wins) - np.mean(losses)), 4),
        "win_std":   round(float(np.std(wins)), 4),
        "loss_std":  round(float(np.std(losses)), 4),
    }


def bucket_winrate(trades: list[dict], field: str, bins: list[float]) -> list[dict]:
    rows = []
    vals = np.array([t[field] for t in trades])
    wons = np.array([t["won"] for t in trades])
    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (vals >= lo) & (vals < hi)
        n = int(mask.sum())
        if n == 0:
            rows.append({"range": f"[{lo},{hi})", "n": 0})
            continue
        wr = float(wons[mask].mean())
        rows.append({
            "range": f"[{lo:.1f},{hi:.1f})",
            "n": n,
            "win_rate_pct": round(wr * 100, 1),
            "above_be": bool(wr >= BREAKEVEN),
        })
    return rows


def main():
    print("📊 加载数据...")
    bars = load_second_bars(CSV_PATH)
    print(f"   {len(bars):,} 行  {bars.index.min().date()} ~ {bars.index.max().date()}")

    print("🔍 生成交易记录（含上下文）...")
    trades = collect_trades_with_context(bars)
    wins   = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    n, nw, nl = len(trades), len(wins), len(losses)
    wr = nw / n if n else 0

    print(f"\n总交易: {n}  胜:{nw}({wr*100:.1f}%)  败:{nl}")

    # ─── 1. 亏损交易的核心特征对比 ───
    print("\n" + "="*60)
    print("📌 1. 盈利 vs 亏损交易特征对比")
    print("="*60)
    fields = [
        ("vol_ratio",    "波动率比（60s/3600s，>1=当前高波）"),
        ("sigma_10m_bps","10分钟预测sigma (bps)"),
        ("slope300_bps", "300s趋势斜率 (bps)"),
        ("slope1800_bps","1800s趋势斜率 (bps)"),
        ("flow60",       "60s Taker Flow"),
        ("flow300",      "300s Taker Flow"),
        ("range_pos",    "区间位置(0=底,1=顶)"),
        ("range_bps",    "价格区间宽度 (bps)"),
        ("z",            "z_score (信号强度)"),
    ]
    comparisons = []
    for field, label in fields:
        c = compare_win_loss(trades, field, label)
        if c:
            diff_dir = "↑胜" if c["diff"] > 0 else "↓败"
            print(f"  {label:30} 胜={c['win_mean']:>8.3f}  败={c['loss_mean']:>8.3f}  差={c['diff']:>+8.3f} {diff_dir}")
            comparisons.append(c)

    # ─── 2. 亏损聚集性（连败分析）───
    print("\n" + "="*60)
    print("📌 2. 亏损是否聚集（连败结构）")
    results_seq = [t["won"] for t in trades]
    # 实际连败 vs 随机期望
    streaks = []
    cur = 0
    for w in results_seq:
        cur = 0 if w else cur + 1
        if cur > 0:
            streaks.append(cur)
    max_streak = max(streaks) if streaks else 0
    # 随机期望最大连败（模拟）
    rng = np.random.default_rng(42)
    sim_max = []
    for _ in range(1000):
        sim = rng.random(n) > wr
        cur2 = streak2 = 0
        for w in sim:
            cur2 = 0 if not w else cur2 + 1
            streak2 = max(streak2, cur2)
        sim_max.append(streak2)
    expected_max = float(np.mean(sim_max))
    print(f"  实际最大连败: {max_streak}  随机期望连败: {expected_max:.1f}")
    print(f"  亏损后下一笔胜率: ", end="")
    after_loss_wins = sum(1 for i in range(1, len(trades)) if not trades[i-1]["won"] and trades[i]["won"])
    after_loss_total = sum(1 for i in range(1, len(trades)) if not trades[i-1]["won"])
    if after_loss_total:
        print(f"{after_loss_wins/after_loss_total*100:.1f}% ({after_loss_wins}/{after_loss_total})")
    else:
        print("N/A")

    # ─── 3. 趋势行情 vs 震荡行情 ───
    print("\n" + "="*60)
    print("📌 3. 趋势行情 vs 震荡行情 胜率")
    strong_trend = [t for t in trades if abs(t["slope1800_bps"]) > 20]
    weak_trend   = [t for t in trades if abs(t["slope1800_bps"]) <= 20]
    counter_t    = [t for t in trades if t["counter_300"]]
    aligned_t    = [t for t in trades if t["aligned_300"]]

    def wr_str(lst):
        if not lst: return "N/A"
        w = sum(t["won"] for t in lst)
        return f"{w/len(lst)*100:.1f}% ({w}/{len(lst)})"

    print(f"  强趋势(slope1800>20bps): {wr_str(strong_trend)}  {'✅' if strong_trend and sum(t['won'] for t in strong_trend)/len(strong_trend) >= BREAKEVEN else '❌'}")
    print(f"  弱趋势(slope1800<=20bps):{wr_str(weak_trend)}  {'✅' if weak_trend and sum(t['won'] for t in weak_trend)/len(weak_trend) >= BREAKEVEN else '❌'}")
    print(f"  逆势信号(counter_300):   {wr_str(counter_t)}  {'✅' if counter_t and sum(t['won'] for t in counter_t)/len(counter_t) >= BREAKEVEN else '❌'}")
    print(f"  顺势信号(aligned_300):   {wr_str(aligned_t)}  {'✅' if aligned_t and sum(t['won'] for t in aligned_t)/len(aligned_t) >= BREAKEVEN else '❌'}")

    # ─── 4. 波动率分桶胜率 ───
    print("\n" + "="*60)
    print("📌 4. 波动率分桶胜率（vol_ratio = 当前60s/历史3600s）")
    vol_buckets = bucket_winrate(trades, "vol_ratio", [0, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 5.0])
    for b in vol_buckets:
        if b["n"] == 0: continue
        flag = "✅" if b.get("above_be") else "❌"
        print(f"  {flag} vol_ratio {b['range']:15} n={b['n']:>3}  胜率={b['win_rate_pct']:>5.1f}%")

    print("\n📌 4b. sigma_10m 分桶胜率")
    sig_buckets = bucket_winrate(trades, "sigma_10m_bps", [0, 5, 8, 12, 18, 25, 35, 100])
    for b in sig_buckets:
        if b["n"] == 0: continue
        flag = "✅" if b.get("above_be") else "❌"
        print(f"  {flag} sigma_10m {b['range']:15} n={b['n']:>3}  胜率={b['win_rate_pct']:>5.1f}%")

    # ─── 5. 时间分布 ───
    print("\n" + "="*60)
    print("📌 5. UTC时段胜率（每4小时）")
    hour_buckets = bucket_winrate(trades, "hour_utc", [0, 4, 8, 12, 16, 20, 24])
    for b in hour_buckets:
        if b["n"] == 0: continue
        flag = "✅" if b.get("above_be") else "❌"
        print(f"  {flag} UTC {b['range']:12} n={b['n']:>3}  胜率={b['win_rate_pct']:>5.1f}%")

    # ─── 6. 过滤方案效果测试 ───
    print("\n" + "="*60)
    print("📌 6. 基于亏损原因的过滤方案测试")
    filters = [
        ("无过滤（基准）",         lambda t: True),
        ("排除高波(vol_ratio>1.5)", lambda t: t["vol_ratio"] <= 1.5),
        ("排除强趋势(|sl1800|>30)", lambda t: abs(t["slope1800_bps"]) <= 30),
        ("只要逆势信号",            lambda t: t["counter_300"]),
        ("sigma 8~25 bps",          lambda t: 8 <= t["sigma_10m_bps"] <= 25),
        ("flow逆势(>0.03)",         lambda t: t["flow60"] * (-1 if t["signal"]=="UP" else 1) < 0.03),
        ("组合：sigma8_25+逆势",    lambda t: 8 <= t["sigma_10m_bps"] <= 25 and t["counter_300"]),
        ("组合：sigma8_25+低波",    lambda t: 8 <= t["sigma_10m_bps"] <= 25 and t["vol_ratio"] <= 1.5),
        ("组合：逆势+低波",         lambda t: t["counter_300"] and t["vol_ratio"] <= 1.5),
        ("三重：sigma+逆势+低波",   lambda t: 8 <= t["sigma_10m_bps"] <= 25 and t["counter_300"] and t["vol_ratio"] <= 1.5),
    ]
    print(f"  {'过滤条件':32} {'笔数':>6} {'胜率':>8} {'PnL':>7}  结论")
    for label, fn in filters:
        subset = [t for t in trades if fn(t)]
        n2 = len(subset)
        if n2 == 0:
            print(f"  {label:32} {'0':>6} {'N/A':>8} {'N/A':>7}")
            continue
        w2 = sum(t["won"] for t in subset)
        wr2 = w2 / n2
        pnl2 = w2 * WIN_PAY + (n2 - w2) * LOSS_PAY
        flag = "✅盈利" if wr2 >= BREAKEVEN else "❌亏损"
        print(f"  {label:32} {n2:>6} {wr2*100:>7.1f}% {pnl2:>+7}U  {flag}")

    # ─── 7. 亏损交易案例（最具代表性）───
    print("\n" + "="*60)
    print("📌 7. 典型亏损案例（vol_ratio最高的亏损）")
    loss_sorted = sorted(losses, key=lambda t: t["vol_ratio"], reverse=True)
    for t in loss_sorted[:5]:
        print(f"  {t['time']}  {t['signal']:4}  z={t['z']:>6.3f}  "
              f"vol_ratio={t['vol_ratio']:>5.2f}  sigma={t['sigma_10m_bps']:>5.1f}bps  "
              f"slope1800={t['slope1800_bps']:>+7.1f}bps  "
              f"ret={t['ret_bps']:>+6.1f}bps")

    # ─── 写报告 ───
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "config": {"lookback": LOOKBACK, "tail_pct": TAIL_PCT},
        "summary": {"trades": n, "wins": nw, "losses": nl, "win_rate_pct": round(wr*100, 2)},
        "feature_comparison": comparisons,
        "streak_analysis": {
            "max_actual_streak": max_streak,
            "expected_random_streak": round(expected_max, 1),
            "after_loss_win_rate_pct": round(after_loss_wins/after_loss_total*100, 1) if after_loss_total else None,
        },
        "trend_bucket": {
            "strong_trend": wr_str(strong_trend),
            "weak_trend": wr_str(weak_trend),
            "counter_300": wr_str(counter_t),
            "aligned_300": wr_str(aligned_t),
        },
        "vol_ratio_buckets": vol_buckets,
        "sigma_buckets": sig_buckets,
        "hour_buckets": hour_buckets,
        "filter_tests": [
            {"label": lbl, "trades": len([t for t in trades if fn(t)]),
             "win_rate_pct": round(sum(t["won"] for t in trades if fn(t)) / max(len([t for t in trades if fn(t)]),1)*100, 1)}
            for lbl, fn in filters
        ],
        "all_trades": trades,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n📄 完整报告: {OUT_PATH}")


if __name__ == "__main__":
    main()
