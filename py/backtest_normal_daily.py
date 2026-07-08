"""
backtest_normal_daily.py
========================
用现有秒级数据做每日盈亏回测，分析胜率稳定性问题。

重点：
  - 每天的胜率、PnL、交易次数
  - 多参数组合对比
  - 胜率稳定性分析（日内方差、最大连败）
  - 过滤器对稳定性的影响（flow imbalance / volatility）
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from second_backtest.data import load_second_bars

CSV_PATH = ROOT / "tmp" / "current_live_pull_20260623_214203" / "data" / "btcusdt_1s_trades.csv"
OUT_PATH  = ROOT / "tmp" / "normal_daily_backtest.json"
HORIZON   = 600   # 10分钟
COOLDOWN  = 600   # 信号冷却
# 4U赢/-5U亏 赔率
WIN_PAY   = 4
LOSS_PAY  = -5
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY))  # 55.56%


# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────
def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def max_drawdown_seq(results: list[bool]) -> int:
    """最大连败数"""
    cur = best = 0
    for won in results:
        cur = 0 if won else cur + 1
        best = max(best, cur)
    return best


def trade_metrics(trades: list[dict], label: str = "") -> dict:
    n = len(trades)
    if n == 0:
        return {"label": label, "trades": 0}
    wins = sum(1 for t in trades if t["won"])
    wr = wins / n
    pnl = wins * WIN_PAY + (n - wins) * LOSS_PAY
    mdd = max_drawdown_seq([t["won"] for t in trades])
    return {
        "label": label,
        "trades": n,
        "wins": wins,
        "win_rate_pct": round(wr * 100, 2),
        "pnl": int(pnl),
        "max_loss_streak": mdd,
        "edge_vs_breakeven_pct": round((wr - BREAKEVEN_WR) * 100, 2),
        "profitable": bool(wr >= BREAKEVEN_WR),
    }


# ─────────────────────────────────────────────────────────────
# 核心回测：逐笔生成交易记录
# ─────────────────────────────────────────────────────────────
def run_backtest(
    bars: pd.DataFrame,
    lookback: int,
    tail_pct: float,
    flow_min: float = 0.0,     # 最小 taker flow imbalance 过滤
    vol_rank_max: float = 1.0, # 最大成交量排名（1.0=不过滤）
    sigma_min_bps: float = 0.0,# 最小波动率（bps）过滤
    sigma_max_bps: float = 9999.0,
) -> list[dict]:
    """返回每笔交易记录列表。"""
    close    = bars["close"].to_numpy(float)
    buy_qty  = bars["buy_qty"].to_numpy(float)
    sell_qty = bars["sell_qty"].to_numpy(float)
    volume   = bars["volume"].to_numpy(float)
    times    = bars.index if hasattr(bars.index, 'tz') else pd.to_datetime(bars.index, utc=True)

    lr = np.diff(np.log(close), prepend=np.nan)
    series = pd.Series(lr, index=times)
    min_p  = max(60, lookback // 4)
    mu_arr = series.rolling(lookback, min_periods=min_p).mean().to_numpy(float)
    sg_arr = series.rolling(lookback, min_periods=min_p).std(ddof=1).to_numpy(float)

    # 成交量排名（滚动分位数）
    vol_s    = pd.Series(volume, index=times)
    vol_roll = vol_s.rolling(lookback, min_periods=min_p)

    trades: list[dict] = []
    last_signal = -COOLDOWN

    for i in range(lookback, len(close) - HORIZON):
        if i - last_signal < COOLDOWN:
            continue
        mu, sg = mu_arr[i], sg_arr[i]
        if not (np.isfinite(mu) and np.isfinite(sg) and sg > 1e-12):
            continue

        z    = HORIZON * mu / (math.sqrt(HORIZON) * sg)
        p_up = normal_cdf(z)

        threshold_hi = 1.0 - tail_pct
        if p_up >= threshold_hi:
            signal = "DOWN"
        elif p_up <= tail_pct:
            signal = "UP"
        else:
            continue

        # ── 可选过滤器 ──
        # 1. Taker flow imbalance（60s）
        if flow_min > 0.0:
            b60 = float(np.nansum(buy_qty[max(0, i-59):i+1]))
            s60 = float(np.nansum(sell_qty[max(0, i-59):i+1]))
            flow = (b60 - s60) / max(b60 + s60, 1e-12)
            side = 1.0 if signal == "UP" else -1.0
            if side * flow < flow_min:
                continue

        # 2. 波动率过滤（sigma_10min in bps）
        if sigma_min_bps > 0.0 or sigma_max_bps < 9999.0:
            sigma_10m_bps = math.sqrt(HORIZON) * sg * 10000.0
            if not (sigma_min_bps <= sigma_10m_bps <= sigma_max_bps):
                continue

        # 3. 成交量排名（60s vs lookback窗口）
        if vol_rank_max < 1.0:
            ref = vol_roll.iloc[i] if hasattr(vol_roll, 'iloc') else None
            try:
                window_vol = vol_s.iloc[max(0, i - lookback):i + 1]
                cur_vol    = float(np.nansum(volume[max(0, i-59):i+1]))
                rank       = float((window_vol <= cur_vol).mean())
                if rank > vol_rank_max:
                    continue
            except Exception:
                pass

        entry  = float(close[i])
        settle = float(close[i + HORIZON])
        won    = (settle > entry) if signal == "UP" else (settle < entry)

        trades.append({
            "time":    times[i].isoformat() if hasattr(times[i], 'isoformat') else str(times[i]),
            "date":    str(times[i])[:10],
            "signal":  signal,
            "p_up":    round(p_up, 4),
            "z":       round(z, 4),
            "entry":   round(entry, 2),
            "settle":  round(settle, 2),
            "won":     bool(won),
            "pnl":     WIN_PAY if won else LOSS_PAY,
        })
        last_signal = i

    return trades


# ─────────────────────────────────────────────────────────────
# 每日拆分分析
# ─────────────────────────────────────────────────────────────
def daily_breakdown(trades: list[dict]) -> list[dict]:
    by_day: dict[str, list] = {}
    for t in trades:
        by_day.setdefault(t["date"], []).append(t)

    rows = []
    cum_pnl = 0
    for date in sorted(by_day):
        day_trades = by_day[date]
        m = trade_metrics(day_trades, date)
        cum_pnl += m.get("pnl", 0)
        m["cum_pnl"] = cum_pnl
        rows.append(m)
    return rows


def stability_stats(daily: list[dict]) -> dict:
    """胜率稳定性统计。"""
    wrs = [d["win_rate_pct"] for d in daily if d.get("trades", 0) >= 3]
    if not wrs:
        return {"days_with_ge3_trades": 0}
    return {
        "days_analyzed": len(wrs),
        "win_rate_mean": round(float(np.mean(wrs)), 2),
        "win_rate_std":  round(float(np.std(wrs, ddof=1)), 2),
        "win_rate_min":  round(float(np.min(wrs)), 2),
        "win_rate_max":  round(float(np.max(wrs)), 2),
        "days_above_breakeven": int(sum(1 for w in wrs if w >= BREAKEVEN_WR * 100)),
        "days_below_50pct":     int(sum(1 for w in wrs if w < 50.0)),
        "profitable_day_rate_pct": round(sum(1 for w in wrs if w >= BREAKEVEN_WR * 100) / len(wrs) * 100, 1),
    }


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def main() -> None:
    print("📊 加载秒级数据...")
    bars = load_second_bars(CSV_PATH)
    print(f"   {len(bars):,} 行  {bars.index.min().date()} ~ {bars.index.max().date()}")

    # ── 测试配置组合 ──
    configs: list[dict[str, Any]] = [
        # 基准组（无过滤）
        dict(tag="4200_020_nofilter",     lookback=4200, tail_pct=0.20),
        dict(tag="3600_020_nofilter",     lookback=3600, tail_pct=0.20),
        dict(tag="5400_025_nofilter",     lookback=5400, tail_pct=0.25),
        dict(tag="2700_018_nofilter",     lookback=2700, tail_pct=0.18),
        # 加 flow imbalance 过滤
        dict(tag="4200_020_flow005",      lookback=4200, tail_pct=0.20, flow_min=0.05),
        dict(tag="4200_020_flow010",      lookback=4200, tail_pct=0.20, flow_min=0.10),
        dict(tag="3600_020_flow005",      lookback=3600, tail_pct=0.20, flow_min=0.05),
        # 加波动率过滤（只在适中波动率时入场）
        dict(tag="4200_020_sig8_25",      lookback=4200, tail_pct=0.20, sigma_min_bps=8.0,  sigma_max_bps=25.0),
        dict(tag="4200_020_sig10_30",     lookback=4200, tail_pct=0.20, sigma_min_bps=10.0, sigma_max_bps=30.0),
        # 组合过滤
        dict(tag="4200_020_flow005_sig8_25", lookback=4200, tail_pct=0.20, flow_min=0.05, sigma_min_bps=8.0, sigma_max_bps=25.0),
        dict(tag="3600_020_flow005_sig8_25", lookback=3600, tail_pct=0.20, flow_min=0.05, sigma_min_bps=8.0, sigma_max_bps=25.0),
    ]

    results = []
    print(f"\n{'tag':35} {'trades':>7} {'wr%':>7} {'pnl':>7} {'mdd':>5} {'days_ok':>8} {'wr_std':>7}")
    print("-" * 80)

    for cfg in configs:
        tag      = cfg["tag"]
        trades   = run_backtest(
            bars,
            lookback      = cfg["lookback"],
            tail_pct      = cfg["tail_pct"],
            flow_min      = cfg.get("flow_min", 0.0),
            vol_rank_max  = cfg.get("vol_rank_max", 1.0),
            sigma_min_bps = cfg.get("sigma_min_bps", 0.0),
            sigma_max_bps = cfg.get("sigma_max_bps", 9999.0),
        )
        overall  = trade_metrics(trades, tag)
        daily    = daily_breakdown(trades)
        stab     = stability_stats(daily)

        flag = "✅" if overall.get("profitable") else "❌"
        print(
            f"  {flag} {tag:33} {overall.get('trades',0):>6} "
            f"{overall.get('win_rate_pct',0):>7.1f}% "
            f"{overall.get('pnl',0):>+7}U "
            f"{overall.get('max_loss_streak',0):>4}  "
            f"{stab.get('days_above_breakeven','?'):>6}/{stab.get('days_analyzed','?')}  "
            f"{stab.get('win_rate_std','?'):>7}"
        )

        results.append({
            "config": cfg,
            "overall": overall,
            "daily": daily,
            "stability": stab,
            "trades_sample": trades[:5],
        })

    # ── 最优配置详细每日 ──
    best_tag = "4200_020_nofilter"
    best = next((r for r in results if r["config"]["tag"] == best_tag), results[0])

    print(f"\n\n📅 每日盈亏明细 [{best_tag}]")
    print(f"{'日期':12} {'笔数':>5} {'胜率%':>7} {'日PnL':>7} {'累计PnL':>8} {'连败':>5}  {'备注':}")
    print("-" * 60)
    for d in best["daily"]:
        flag = "✅" if d.get("profitable") else ("⚠️ " if d.get("win_rate_pct", 0) >= 50 else "❌")
        print(
            f"  {d['label']:10} {d.get('trades',0):>5} "
            f"{d.get('win_rate_pct',0):>7.1f}% "
            f"{d.get('pnl',0):>+7}U "
            f"{d.get('cum_pnl',0):>+8}U "
            f"{d.get('max_loss_streak',0):>4}  {flag}"
        )

    print(f"\n📌 胜率稳定性分析 [{best_tag}]")
    stab = best["stability"]
    print(f"  日均胜率: {stab.get('win_rate_mean')}%  ±{stab.get('win_rate_std')}%")
    print(f"  最差日: {stab.get('win_rate_min')}%  最好日: {stab.get('win_rate_max')}%")
    print(f"  达到盈亏平衡的天数: {stab.get('days_above_breakeven')}/{stab.get('days_analyzed')}")

    print("\n\n🔧 胜率稳定性对比（有无过滤器）")
    print(f"{'tag':35} {'trades':>7} {'wr%':>7} {'wr_std':>8} {'days_ok':>8}")
    for r in results:
        stab = r["stability"]
        print(
            f"  {r['config']['tag']:33} "
            f"{r['overall'].get('trades',0):>6} "
            f"{r['overall'].get('win_rate_pct',0):>7.1f}% "
            f"{stab.get('win_rate_std','?'):>8}  "
            f"{stab.get('days_above_breakeven','?')}/{stab.get('days_analyzed','?')}"
        )

    # ── 写输出 ──
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "data_range": {
            "start": str(bars.index.min()),
            "end":   str(bars.index.max()),
            "rows":  int(len(bars)),
        },
        "breakeven_wr_pct": round(BREAKEVEN_WR * 100, 2),
        "results": [
            {k: v for k, v in r.items() if k != "trades_sample"}
            for r in results
        ],
    }
    OUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8"
    )
    print(f"\n📄 完整报告: {OUT_PATH}")


if __name__ == "__main__":
    main()
