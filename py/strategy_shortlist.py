"""
strategy_shortlist.py
======================
全参数扫描 + 严格入选标准，输出可入实盘的策略候选名单。

入选标准：
  1. 胜率 >= 58%（高于盈亏平衡线55.56% + 2.5%安全边际）
  2. 样本量 >= 20 笔
  3. 盈利天数比例 >= 60%
  4. 最大连败 <= 5
  5. 日胜率标准差 <= 25%（稳定性）
  6. 与其他入选策略信号重叠率 < 60%（独立性）
"""
from __future__ import annotations
import json, math, sys
from pathlib import Path
from itertools import product

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from second_backtest.data import load_second_bars

CSV_PATH = ROOT / "tmp" / "current_live_pull_20260623_214203" / "data" / "btcusdt_1s_trades.csv"
OUT_PATH  = ROOT / "tmp" / "strategy_shortlist.json"
HORIZON, COOLDOWN = 600, 600
WIN_PAY, LOSS_PAY = 4, -5
BREAKEVEN = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY))  # 55.56%

def normal_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

def max_streak(seq):
    cur = best = 0
    for w in seq:
        cur = 0 if w else cur + 1
        best = max(best, cur)
    return best

# ── 参数空间（精简版，快速运行）──────────────────────────────────
LOOKBACKS   = [2700, 3600, 4200, 5400]
TAIL_PCTS   = [0.18, 0.20, 0.22, 0.25]
SIGMA_MINS  = [0.0, 8.0]
SIGMA_MAXES = [9999, 25.0]
SLOPE_MAXES = [9999, 40.0]
SKIP_HOURS  = [None, frozenset(range(12, 16))]

def run_all(bars, lookback, tail_pct,
            sigma_min=0.0, sigma_max=9999.0,
            slope_max=9999.0, skip_hours=None):
    close    = bars["close"].to_numpy(float)
    buy_qty  = bars["buy_qty"].to_numpy(float)
    sell_qty = bars["sell_qty"].to_numpy(float)
    times    = bars.index
    vol      = bars["volume"].to_numpy(float)

    lr      = np.diff(np.log(close), prepend=np.nan)
    series  = pd.Series(lr, index=times)
    min_p   = max(60, lookback // 4)
    mu_arr  = series.rolling(lookback, min_periods=min_p).mean().to_numpy(float)
    sg_arr  = series.rolling(lookback, min_periods=min_p).std(ddof=1).to_numpy(float)

    close_s  = pd.Series(close, index=times)
    slope18  = (close_s / close_s.shift(1800) - 1).to_numpy(float) * 10000
    abs_lr_s = pd.Series(np.abs(lr), index=times)
    atr60    = abs_lr_s.rolling(60, min_periods=10).mean().to_numpy(float) * 10000
    atr3600  = abs_lr_s.rolling(3600, min_periods=300).mean().to_numpy(float) * 10000

    trades, idxs = [], []
    last = -COOLDOWN
    for i in range(lookback, len(close) - HORIZON):
        if i - last < COOLDOWN: continue
        mu, sg = mu_arr[i], sg_arr[i]
        if not (np.isfinite(mu) and np.isfinite(sg) and sg > 1e-12): continue

        # sigma 过滤
        sigma_10m = math.sqrt(HORIZON) * sg * 10000
        if not (sigma_min <= sigma_10m <= sigma_max): continue

        # 趋势过滤
        if slope_max < 9999 and np.isfinite(slope18[i]):
            if abs(slope18[i]) > slope_max: continue

        # 时段过滤
        if skip_hours:
            h = times[i].hour
            if h in skip_hours: continue

        z    = HORIZON * mu / (math.sqrt(HORIZON) * sg)
        p_up = normal_cdf(z)
        if p_up >= 1 - tail_pct:     signal = "DOWN"
        elif p_up <= tail_pct:        signal = "UP"
        else:                         continue

        entry, settle = float(close[i]), float(close[i + HORIZON])
        won = (settle > entry) if signal == "UP" else (settle < entry)
        trades.append({
            "date": str(times[i])[:10],
            "won": bool(won),
            "pnl": WIN_PAY if won else LOSS_PAY,
        })
        idxs.append(i)
        last = i
    return trades, idxs

def score(trades, idxs):
    n = len(trades)
    if n < 20: return None
    wins = sum(t["won"] for t in trades)
    wr   = wins / n
    if wr < 0.58: return None
    pnl  = wins * WIN_PAY + (n - wins) * LOSS_PAY
    mdd  = max_streak([t["won"] for t in trades])
    if mdd > 5: return None

    by_day = {}
    for t in trades:
        by_day.setdefault(t["date"], []).append(t["won"])
    days = sorted(by_day)
    day_wrs = [sum(v)/len(v)*100 for v in by_day.values() if len(v) >= 2]
    if not day_wrs: return None
    days_ok = sum(1 for w in day_wrs if w >= BREAKEVEN * 100)
    if days_ok / len(day_wrs) < 0.60: return None
    wr_std = float(np.std(day_wrs, ddof=1)) if len(day_wrs) > 1 else 99.0
    if wr_std > 25: return None

    days_total = len(by_day)
    return {
        "trades": n, "wins": wins,
        "win_rate_pct": round(wr * 100, 2),
        "pnl": int(pnl),
        "max_streak": mdd,
        "days_ok": days_ok,
        "days_total": days_total,
        "days_analyzed": len(day_wrs),
        "wr_std": round(wr_std, 2),
        "profitable_day_pct": round(days_ok / len(day_wrs) * 100, 1),
        "trades_per_day": round(n / max(days_total, 1), 1),
        "score": round(wr * 100 - wr_std * 0.3 + math.log(n) * 2, 2),
    }

def overlap_rate(idxs_a, idxs_b, tol=60):
    """两策略信号在 tol 秒内重合的比率（取较小策略的比率）。"""
    if not idxs_a or not idxs_b: return 0.0
    set_a, set_b = set(idxs_a), set(idxs_b)
    matches = sum(
        1 for i in set_a
        if any(abs(i - j) <= tol for j in range(i - tol, i + tol + 1) if j in set_b)
    )
    return matches / min(len(idxs_a), len(idxs_b))

def main():
    print("📊 加载数据...")
    bars = load_second_bars(CSV_PATH)
    print(f"   {len(bars):,} 行  {bars.index.min().date()} ~ {bars.index.max().date()}")

    print("🔍 全参数扫描中（请稍候）...")
    candidates = []
    total = len(LOOKBACKS) * len(TAIL_PCTS) * len(SIGMA_MINS) * len(SLOPE_MAXES) * len(SKIP_HOURS)
    done = 0

    for lb, tp, sm, sl, sh in product(LOOKBACKS, TAIL_PCTS, SIGMA_MINS, SLOPE_MAXES, SKIP_HOURS):
        # sigma_max 与 sigma_min 搭配
        for smax in SIGMA_MAXES:
            if sm > 0 and smax < sm: continue
            if sm == 0 and smax < 9999 and smax < 20: continue  # 避免无意义组合
            trades, idxs = run_all(bars, lb, tp,
                                   sigma_min=sm, sigma_max=smax,
                                   slope_max=sl, skip_hours=sh)
            s = score(trades, idxs)
            if s:
                tag = (f"lb{lb}_tp{int(tp*100)}"
                       + (f"_sm{int(sm)}" if sm > 0 else "")
                       + (f"_sx{int(smax)}" if smax < 9999 else "")
                       + (f"_sl{int(sl)}" if sl < 9999 else "")
                       + (f"_skip{len(sh) if sh else 0}h" if sh else ""))
                candidates.append({
                    "tag": tag,
                    "lookback": lb, "tail_pct": tp,
                    "sigma_min": sm, "sigma_max": smax,
                    "slope_max": sl,
                    "skip_hours": sorted(sh) if sh else [],
                    **s,
                    "_idxs": idxs,
                })
        done += 1

    print(f"   扫描完成，初筛候选: {len(candidates)}")

    # 按综合分数排序
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # ── 去重：基于信号重叠率，选出互相独立的策略 ──────────────────
    shortlist = []
    MAX_SHORTLIST = 5
    MAX_OVERLAP   = 0.50  # 重叠率阈值

    for c in candidates:
        if len(shortlist) >= MAX_SHORTLIST:
            break
        # 检查与已入选策略的重叠
        too_similar = False
        for s in shortlist:
            ol = overlap_rate(c["_idxs"], s["_idxs"])
            if ol > MAX_OVERLAP:
                too_similar = True
                break
        if not too_similar:
            shortlist.append(c)

    # ── 打印结果 ──────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"✅ 入选策略（共 {len(shortlist)} 个）")
    print(f"{'='*70}")

    for rank, s in enumerate(shortlist, 1):
        skip_str = f"跳过UTC{s['skip_hours']}" if s['skip_hours'] else "全时段"
        sigma_str = (f"sigma {s['sigma_min']}~{s['sigma_max']}bps"
                     if s['sigma_min'] > 0 or s['sigma_max'] < 9999 else "无sigma过滤")
        slope_str = f"|slope1800|≤{s['slope_max']}bps" if s['slope_max'] < 9999 else "无趋势过滤"
        print(f"\n  #{rank} {s['tag']}")
        print(f"       lookback={s['lookback']}s  tail_pct={s['tail_pct']}")
        print(f"       过滤: {sigma_str}  {slope_str}  {skip_str}")
        print(f"       总笔数={s['trades']}  胜率={s['win_rate_pct']}%  PnL={s['pnl']:+}U")
        print(f"       最大连败={s['max_streak']}  盈利天={s['days_ok']}/{s['days_analyzed']}({s['profitable_day_pct']}%)")
        print(f"       日胜率std={s['wr_std']}%  日均笔数={s['trades_per_day']}  综合分={s['score']}")

    # ── 初筛候选 TOP20 ─────────────────────────────────────────
    print(f"\n\n📊 初筛通过 TOP20（按综合分数）")
    print(f"{'#':>3} {'tag':45} {'wr%':>7} {'pnl':>6} {'mdd':>4} {'std':>6} {'tpd':>5} {'score':>7}")
    print("-" * 90)
    for i, c in enumerate(candidates[:20], 1):
        print(f"  {i:>2}. {c['tag']:43} {c['win_rate_pct']:>6.1f}% "
              f"{c['pnl']:>+6}U {c['max_streak']:>3} "
              f"{c['wr_std']:>6.1f}% {c['trades_per_day']:>5.1f} "
              f"{c['score']:>7.2f}")

    # ── 保存 ──────────────────────────────────────────────────
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "total_candidates": len(candidates),
        "shortlist_count": len(shortlist),
        "criteria": {
            "min_win_rate_pct": 58.0,
            "min_trades": 20,
            "min_profitable_day_pct": 60.0,
            "max_loss_streak": 5,
            "max_wr_std": 25.0,
            "max_overlap_rate": MAX_OVERLAP,
        },
        "shortlist": [{k: v for k, v in s.items() if k != "_idxs"} for s in shortlist],
        "top20_candidates": [{k: v for k, v in c.items() if k != "_idxs"} for c in candidates[:20]],
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n📄 完整报告: {OUT_PATH}")

if __name__ == "__main__":
    main()
