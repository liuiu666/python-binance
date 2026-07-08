"""
research_normal_10min_binary.py
================================
用秒级数据对 BTC 10分钟二元期权做系统性正态分布检验。

分析维度：
  1. 收益率正态性检验（Shapiro-Wilk / Jarque-Bera / KS）
  2. 滚动均值/方差随时间分布
  3. 尾部胖尾程度（峰度、偏度）
  4. 不同 lookback 窗口下 tail_pct 的回测统计
  5. Z-score 边界击穿后的 10min 实际胜率（逐区间）
  6. 正态假设下 p_up 预测准确性校准图
输出：tmp/normal_10min_binary_research.json
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from second_backtest.data import load_second_bars

# ──────────────────────────────────────────────
# 路径配置
# ──────────────────────────────────────────────
DEFAULT_CSV = ROOT / "tmp" / "current_live_pull_20260623_214203" / "data" / "btcusdt_1s_trades.csv"
DEFAULT_OUT = ROOT / "tmp" / "normal_10min_binary_research.json"
HORIZON_SEC = 600          # 10分钟期权到期
SIGNAL_COOLDOWN = 600      # 信号冷却（不重叠）


# ──────────────────────────────────────────────
# 1. 基础正态性检验
# ──────────────────────────────────────────────
def normality_tests(lr: np.ndarray) -> dict:
    """对对数收益率做多项正态性检验。"""
    lr = lr[np.isfinite(lr)]
    n = len(lr)
    result: dict = {
        "n": n,
        "mean": round(float(np.mean(lr)), 10),
        "std": round(float(np.std(lr, ddof=1)), 10),
        "skewness": round(float(stats.skew(lr)), 6),
        "kurtosis_excess": round(float(stats.kurtosis(lr)), 6),   # 超额峰度，正态=0
        "annualized_vol_pct": round(float(np.std(lr, ddof=1) * math.sqrt(86400) * 100), 4),
    }

    # Jarque-Bera
    jb_stat, jb_p = stats.jarque_bera(lr)
    result["jarque_bera"] = {
        "statistic": round(float(jb_stat), 4),
        "p_value": round(float(jb_p), 6),
        "reject_normal_at_5pct": bool(jb_p < 0.05),
    }

    # KS test vs normal
    ks_stat, ks_p = stats.kstest(lr, "norm", args=(float(np.mean(lr)), float(np.std(lr, ddof=1))))
    result["ks_test"] = {
        "statistic": round(float(ks_stat), 6),
        "p_value": round(float(ks_p), 6),
        "reject_normal_at_5pct": bool(ks_p < 0.05),
    }

    # Shapiro-Wilk（样本过大时用子集）
    sample = lr if n <= 5000 else lr[np.random.default_rng(42).choice(n, 5000, replace=False)]
    sw_stat, sw_p = stats.shapiro(sample)
    result["shapiro_wilk"] = {
        "statistic": round(float(sw_stat), 6),
        "p_value": round(float(sw_p), 6),
        "reject_normal_at_5pct": bool(sw_p < 0.05),
        "sample_size_used": len(sample),
    }

    # 分位数比较（正态 vs 实际）
    q_theory = stats.norm.ppf([0.01, 0.05, 0.10, 0.25, 0.75, 0.90, 0.95, 0.99],
                               loc=float(np.mean(lr)), scale=float(np.std(lr, ddof=1)))
    q_actual = np.quantile(lr, [0.01, 0.05, 0.10, 0.25, 0.75, 0.90, 0.95, 0.99])
    result["quantile_comparison"] = {
        "labels": ["q1", "q5", "q10", "q25", "q75", "q90", "q95", "q99"],
        "theoretical_normal": [round(float(v), 10) for v in q_theory],
        "actual": [round(float(v), 10) for v in q_actual],
        "ratio_actual_theory": [
            round(float(a / t), 4) if abs(t) > 1e-15 else None
            for a, t in zip(q_actual, q_theory)
        ],
    }
    return result


# ──────────────────────────────────────────────
# 2. 多窗口正态参数稳定性
# ──────────────────────────────────────────────
def rolling_normal_stability(bars: pd.DataFrame, lookbacks: list[int]) -> dict:
    """不同 lookback 下滚动 μ/σ 的统计分布。"""
    lr = np.diff(np.log(bars["close"].to_numpy(float)), prepend=np.nan)
    series = pd.Series(lr, index=bars.index)
    out = {}
    for lb in lookbacks:
        mu = series.rolling(lb, min_periods=max(60, lb // 4)).mean().dropna().to_numpy(float)
        sigma = series.rolling(lb, min_periods=max(60, lb // 4)).std(ddof=1).dropna().to_numpy(float)
        mu_bps = mu * 10000.0
        # 预测10分钟涨跌概率
        z_horizon = HORIZON_SEC * mu / np.maximum(math.sqrt(HORIZON_SEC) * sigma, 1e-12)
        p_up = 0.5 * (1.0 + np.array([math.erf(z / math.sqrt(2.0)) for z in z_horizon]))
        out[str(lb)] = {
            "lookback_sec": lb,
            "mu_mean_bps": round(float(np.mean(mu_bps)), 8),
            "mu_std_bps": round(float(np.std(mu_bps, ddof=1)), 8),
            "sigma_mean_bps": round(float(np.mean(sigma * 10000.0)), 6),
            "sigma_std_bps": round(float(np.std(sigma * 10000.0, ddof=1)), 6),
            "z_horizon_mean": round(float(np.mean(z_horizon)), 6),
            "z_horizon_std": round(float(np.std(z_horizon, ddof=1)), 6),
            "p_up_q05": round(float(np.quantile(p_up, 0.05)), 4),
            "p_up_q25": round(float(np.quantile(p_up, 0.25)), 4),
            "p_up_q50": round(float(np.quantile(p_up, 0.50)), 4),
            "p_up_q75": round(float(np.quantile(p_up, 0.75)), 4),
            "p_up_q95": round(float(np.quantile(p_up, 0.95)), 4),
            "p_up_extreme_pct": round(float(np.mean((p_up >= 0.80) | (p_up <= 0.20)) * 100), 4),
            "n_samples": int(len(mu)),
        }
    return out


# ──────────────────────────────────────────────
# 3. Z-score 区间胜率分布（核心信号分析）
# ──────────────────────────────────────────────
def zscore_bucket_winrate(bars: pd.DataFrame, lookback: int) -> dict:
    """
    计算各 z_horizon 区间内，10分钟实际胜率。
    z_horizon = sqrt(600) * mu / sigma  （10分钟漂移 z 分数）
    """
    close = bars["close"].to_numpy(float)
    lr = np.diff(np.log(close), prepend=np.nan)
    series = pd.Series(lr, index=bars.index)
    min_p = max(60, lookback // 4)
    mu = series.rolling(lookback, min_periods=min_p).mean().to_numpy(float)
    sigma = series.rolling(lookback, min_periods=min_p).std(ddof=1).to_numpy(float)

    buckets = {
        "z_le_-1.5": [],
        "z_-1.5_-1.0": [],
        "z_-1.0_-0.5": [],
        "z_-0.5_0": [],
        "z_0_0.5": [],
        "z_0.5_1.0": [],
        "z_1.0_1.5": [],
        "z_ge_1.5": [],
    }

    last_signal = -SIGNAL_COOLDOWN
    for i in range(lookback, len(close) - HORIZON_SEC):
        if not np.isfinite(mu[i]) or not np.isfinite(sigma[i]) or sigma[i] < 1e-12:
            continue
        if i - last_signal < SIGNAL_COOLDOWN:
            continue
        z = float(HORIZON_SEC * mu[i] / (math.sqrt(HORIZON_SEC) * sigma[i]))
        p_up = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

        entry = close[i]
        settle = close[i + HORIZON_SEC]
        # 信号方向：p_up >= 0.5 预测UP，否则预测DOWN
        predicted_up = p_up >= 0.5
        actual_up = settle > entry
        won = (predicted_up and actual_up) or (not predicted_up and not actual_up)

        record = {"p_up": p_up, "won": won, "z": z}

        if z <= -1.5:
            buckets["z_le_-1.5"].append(record)
        elif z <= -1.0:
            buckets["z_-1.5_-1.0"].append(record)
        elif z <= -0.5:
            buckets["z_-1.0_-0.5"].append(record)
        elif z < 0:
            buckets["z_-0.5_0"].append(record)
        elif z < 0.5:
            buckets["z_0_0.5"].append(record)
        elif z < 1.0:
            buckets["z_0.5_1.0"].append(record)
        elif z < 1.5:
            buckets["z_1.0_1.5"].append(record)
        else:
            buckets["z_ge_1.5"].append(record)

        last_signal = i

    result = {}
    for bname, records in buckets.items():
        n = len(records)
        if n == 0:
            result[bname] = {"n": 0}
            continue
        wins = sum(r["won"] for r in records)
        p_ups = [r["p_up"] for r in records]
        result[bname] = {
            "n": n,
            "win_rate_pct": round(wins / n * 100, 2),
            "mean_p_up": round(float(np.mean(p_ups)), 4),
            "theory_win_pct": round(float(np.mean([max(p, 1 - p) for p in p_ups]) * 100), 2),
            "calibration_error": round(float(wins / n - np.mean([max(p, 1 - p) for p in p_ups])) * 100, 2),
        }
    return {"lookback_sec": lookback, "buckets": result}


# ──────────────────────────────────────────────
# 4. tail_pct 参数扫描（二元期权胜率回测）
# ──────────────────────────────────────────────
def tail_pct_sweep(bars: pd.DataFrame) -> list[dict]:
    """
    对不同 lookback × tail_pct 组合，回测10分钟二元期权胜率。
    逻辑：当 p_up >= (1-tail_pct) 时做 DOWN，当 p_up <= tail_pct 时做 UP
    （均值回归型策略）
    """
    close = bars["close"].to_numpy(float)
    lr = np.diff(np.log(close), prepend=np.nan)
    series = pd.Series(lr, index=bars.index)
    start_ts = bars.index.min()
    end_ts = bars.index.max()
    days = max((end_ts - start_ts).total_seconds() / 86400.0, 1e-6)

    results = []
    lookbacks = [1800, 2700, 3600, 4200, 5400, 7200]
    tail_pcts = [0.15, 0.18, 0.20, 0.22, 0.25, 0.28, 0.30]

    for lb in lookbacks:
        min_p = max(60, lb // 4)
        mu = series.rolling(lb, min_periods=min_p).mean().to_numpy(float)
        sigma = series.rolling(lb, min_periods=min_p).std(ddof=1).to_numpy(float)

        for tp in tail_pcts:
            threshold_hi = 1.0 - tp
            trades, wins = 0, 0
            last_signal = -SIGNAL_COOLDOWN
            for i in range(lb, len(close) - HORIZON_SEC):
                if not np.isfinite(mu[i]) or not np.isfinite(sigma[i]) or sigma[i] < 1e-12:
                    continue
                if i - last_signal < SIGNAL_COOLDOWN:
                    continue
                z = float(HORIZON_SEC * mu[i] / (math.sqrt(HORIZON_SEC) * sigma[i]))
                p_up = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
                if p_up >= threshold_hi:
                    signal = "DOWN"
                elif p_up <= tp:
                    signal = "UP"
                else:
                    continue
                entry = close[i]
                settle = close[i + HORIZON_SEC]
                won = (settle > entry if signal == "UP" else settle < entry)
                trades += 1
                wins += int(won)
                last_signal = i
            if trades == 0:
                continue
            win_rate = wins / trades
            # 期望收益：赔率 4:5（胜+4U 负-5U）
            pnl = wins * 4 + (trades - wins) * (-5)
            results.append({
                "lookback_sec": lb,
                "tail_pct": tp,
                "trades": trades,
                "win_rate_pct": round(win_rate * 100, 2),
                "pnl_4u_win_5u_loss": int(pnl),
                "trades_per_day": round(trades / days, 2),
                "edge_vs_50pct": round((win_rate - 0.5) * 100, 2),
                "required_wr_for_breakeven_pct": round(5 / (4 + 5) * 100, 2),  # 55.56%
                "above_breakeven": bool(win_rate >= 5 / 9),
            })

    # 按胜率排序
    results.sort(key=lambda x: (x["win_rate_pct"], x["pnl_4u_win_5u_loss"]), reverse=True)
    return results


# ──────────────────────────────────────────────
# 5. 10分钟收益率自身的正态检验
# ──────────────────────────────────────────────
def horizon_return_normality(bars: pd.DataFrame) -> dict:
    """直接检验每10分钟的实际收益率是否正态。"""
    close = bars["close"].to_numpy(float)
    # 每600秒取一个非重叠收益率
    step = HORIZON_SEC
    n_samples = (len(close) - HORIZON_SEC) // step
    returns_10m = []
    for i in range(0, n_samples * step, step):
        if i + HORIZON_SEC < len(close):
            r = math.log(close[i + HORIZON_SEC] / close[i])
            returns_10m.append(r)
    arr = np.array(returns_10m, dtype=float)
    arr = arr[np.isfinite(arr)]
    arr_bps = arr * 10000.0
    return {
        "n_samples": len(arr),
        "step_sec": step,
        "description": "非重叠10分钟对数收益率 bps",
        "mean_bps": round(float(np.mean(arr_bps)), 4),
        "std_bps": round(float(np.std(arr_bps, ddof=1)), 4),
        "skewness": round(float(stats.skew(arr)), 6),
        "kurtosis_excess": round(float(stats.kurtosis(arr)), 6),
        "up_pct": round(float(np.mean(arr > 0) * 100), 2),
        "down_pct": round(float(np.mean(arr < 0) * 100), 2),
        "normality_tests": normality_tests(arr),
    }


# ──────────────────────────────────────────────
# 6. p_up 校准分析（预测 vs 实际）
# ──────────────────────────────────────────────
def pup_calibration(bars: pd.DataFrame, lookback: int = 3600) -> dict:
    """
    将 p_up 分成 10 个桶，检验正态模型预测与实际胜率的校准程度。
    """
    close = bars["close"].to_numpy(float)
    lr = np.diff(np.log(close), prepend=np.nan)
    series = pd.Series(lr, index=bars.index)
    min_p = max(60, lookback // 4)
    mu = series.rolling(lookback, min_periods=min_p).mean().to_numpy(float)
    sigma = series.rolling(lookback, min_periods=min_p).std(ddof=1).to_numpy(float)

    records = []
    last_signal = -SIGNAL_COOLDOWN
    for i in range(lookback, len(close) - HORIZON_SEC):
        if not np.isfinite(mu[i]) or not np.isfinite(sigma[i]) or sigma[i] < 1e-12:
            continue
        if i - last_signal < SIGNAL_COOLDOWN:
            continue
        z = float(HORIZON_SEC * mu[i] / (math.sqrt(HORIZON_SEC) * sigma[i]))
        p_up = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        entry = close[i]
        settle = close[i + HORIZON_SEC]
        actual_up = settle > entry
        records.append({"p_up": p_up, "actual_up": actual_up})
        last_signal = i

    # 按 p_up 分 10 桶
    bins = np.linspace(0.0, 1.0, 11)
    calib_buckets = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        subset = [r for r in records if lo <= r["p_up"] < hi]
        n = len(subset)
        if n == 0:
            calib_buckets.append({
                "p_up_range": [round(lo, 2), round(hi, 2)],
                "n": 0,
                "predicted_p_up": round((lo + hi) / 2, 3),
                "actual_up_rate": None,
                "calibration_error": None,
            })
        else:
            pred = (lo + hi) / 2
            act = sum(r["actual_up"] for r in subset) / n
            calib_buckets.append({
                "p_up_range": [round(lo, 2), round(hi, 2)],
                "n": n,
                "predicted_p_up": round(pred, 3),
                "actual_up_rate": round(act, 4),
                "calibration_error": round(act - pred, 4),
            })
    return {
        "lookback_sec": lookback,
        "total_samples": len(records),
        "buckets": calib_buckets,
    }


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────
def main() -> None:
    print("📊 加载秒级数据...")
    bars = load_second_bars(DEFAULT_CSV)
    print(f"   行数: {len(bars):,}  时间: {bars.index.min()} ~ {bars.index.max()}")

    close = bars["close"].to_numpy(float)
    lr = np.diff(np.log(close), prepend=np.nan)
    lr_clean = lr[np.isfinite(lr)]

    print("✅ 1/6 基础正态性检验...")
    base_normality = normality_tests(lr_clean)

    print("✅ 2/6 多窗口滚动正态稳定性...")
    stability = rolling_normal_stability(bars, [600, 1800, 2700, 3600, 5400, 7200])

    print("✅ 3/6 Z-score 区间胜率（lookback=3600s）...")
    zscore_3600 = zscore_bucket_winrate(bars, 3600)

    print("✅ 4/6 tail_pct 参数扫描（完整回测）...")
    sweep = tail_pct_sweep(bars)

    print("✅ 5/6 10分钟收益率正态性检验...")
    horizon_norm = horizon_return_normality(bars)

    print("✅ 6/6 p_up 校准分析（lookback=3600s）...")
    calib = pup_calibration(bars, lookback=3600)

    # ── 汇总最优参数 ──
    top_configs = [r for r in sweep if r["above_breakeven"]][:10]
    best = sweep[0] if sweep else {}

    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "data_sample": {
            "start": str(bars.index.min()),
            "end": str(bars.index.max()),
            "rows": int(len(bars)),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600, 2),
            "horizon_sec": HORIZON_SEC,
        },
        # 1. 秒级收益率是否正态
        "base_normality_1s_logreturns": base_normality,
        # 2. 滚动窗口稳定性
        "rolling_normal_stability": stability,
        # 3. Z 区间胜率
        "zscore_bucket_winrate_lb3600": zscore_3600,
        # 4. 参数扫描
        "tail_pct_sweep_all": sweep,
        "tail_pct_sweep_above_breakeven": top_configs,
        "best_config": best,
        # 5. 10min 收益率正态性
        "horizon_10m_return_normality": horizon_norm,
        # 6. 校准
        "pup_calibration_lb3600": calib,
    }

    out = Path(DEFAULT_OUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ── 控制台摘要 ──
    print("\n" + "=" * 60)
    print("📌 秒级收益率正态性检验")
    print(f"   Jarque-Bera p={base_normality['jarque_bera']['p_value']:.6f}  拒绝正态: {base_normality['jarque_bera']['reject_normal_at_5pct']}")
    print(f"   偏度={base_normality['skewness']:.4f}  超额峰度={base_normality['kurtosis_excess']:.4f}（正态应=0）")
    print(f"   年化波动={base_normality['annualized_vol_pct']:.2f}%")

    print("\n📌 10分钟收益率正态性")
    hn = horizon_norm["normality_tests"]
    print(f"   样本数={horizon_norm['n_samples']}  上涨比例={horizon_norm['up_pct']}%")
    print(f"   Jarque-Bera p={hn['jarque_bera']['p_value']:.6f}  拒绝正态: {hn['jarque_bera']['reject_normal_at_5pct']}")
    print(f"   偏度={horizon_norm['skewness']:.4f}  超额峰度={horizon_norm['kurtosis_excess']:.4f}")

    print("\n📌 tail_pct 扫描 TOP10（需要胜率>55.56%才盈利）")
    header = f"{'lookback':>8} {'tail_pct':>8} {'trades':>7} {'win%':>7} {'pnl':>7} {'tpd':>6}"
    print(header)
    for r in sweep[:10]:
        flag = "✅" if r["above_breakeven"] else "❌"
        print(f"  {flag} {r['lookback_sec']:>6}s  {r['tail_pct']:>7.2f}  "
              f"{r['trades']:>6}  {r['win_rate_pct']:>6.2f}%  "
              f"{r['pnl_4u_win_5u_loss']:>7}U  {r['trades_per_day']:>5.1f}/d")

    print("\n📌 Z-score 区间实际胜率（lb=3600s）")
    for bname, bd in zscore_3600["buckets"].items():
        if bd.get("n", 0) == 0:
            continue
        flag = "✅" if bd["win_rate_pct"] >= 55.56 else "⚠️ " if bd["win_rate_pct"] >= 50 else "❌"
        print(f"  {flag} [{bname:>16}] n={bd['n']:>4}  实际胜率={bd['win_rate_pct']:>5.1f}%  "
              f"理论={bd['theory_win_pct']:>5.1f}%  误差={bd['calibration_error']:>+.1f}%")

    print(f"\n📄 完整报告: {out}")


if __name__ == "__main__":
    main()
