"""
10分钟二元期权回测 — 基于泊松异常信号
用K线数据回测：异常放量后10分钟价格方向预测胜率

用法: python scripts/backtest_binary.py
"""

import sys
import csv
import math
import numpy as np
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = PROJECT_ROOT / "data" / "klines_BTCUSDT_1m_7d.csv"


def poisson_p_value(k: int, lam: float) -> float:
    """计算泊松上侧p-value: P(X >= k)"""
    if k <= lam:
        return 1.0
    # 大lambda用正态近似
    if lam > 50:
        z = (k - 0.5 - lam) / math.sqrt(lam)
        return 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))
    # 小lambda直接累加
    try:
        cdf = 0.0
        term = math.exp(-lam)
        cdf += term
        for i in range(1, k):
            term = term * lam / i
            cdf += term
        return max(1.0 - cdf, 0.0)
    except:
        return 0.0


def load_klines(filepath: Path):
    """加载K线CSV数据"""
    rows = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "open_time": row["open_time"],
                "close_price": float(row["close_price"]),
                "open_price": float(row["open_price"]),
                "high_price": float(row["high_price"]),
                "low_price": float(row["low_price"]),
                "volume": float(row["volume"]),
                "trades_count": int(row["trades_count"]),
            })
    return rows


def run_backtest(klines, window_size=60, lookahead=10):
    """
    回测逻辑:
    1. 滚动窗口估计lambda
    2. 检测泊松异常 (p < 0.01)
    3. 统计异常后10分钟价格方向
    """
    trades_counts = [k["trades_count"] for k in klines]

    # 按异常等级分类统计
    results = {
        "anomaly": {"total": 0, "up": 0, "down": 0, "flat": 0, "profits": []},
        "extreme": {"total": 0, "up": 0, "down": 0, "flat": 0, "profits": []},
        "watch": {"total": 0, "up": 0, "down": 0, "flat": 0, "profits": []},
    }

    # 统计全部K线的基准胜率（随机猜）
    baseline_up = 0
    baseline_down = 0
    baseline_flat = 0

    # 按小时统计异常胜率
    hourly_stats = defaultdict(lambda: {"total": 0, "win": 0})

    # 异常方向一致性：异常方向 vs 未来10分钟方向
    direction_consistent = {"anomaly": 0, "extreme": 0}

    print(f"正在回测 {len(klines)} 条K线数据...")
    print(f"窗口大小: {window_size}, 前瞻: {lookahead}分钟")
    print()

    for i in range(window_size, len(klines) - lookahead):
        current = klines[i]
        future = klines[i + lookahead]

        # 计算未来10分钟价格变动
        price_change = future["close_price"] - current["close_price"]
        price_pct = price_change / current["close_price"] * 100
        # 方向：涨跌幅超过0.01%才算有方向
        if price_pct > 0.01:
            direction = "up"
        elif price_pct < -0.01:
            direction = "down"
        else:
            direction = "flat"

        # 基准统计
        if direction == "up":
            baseline_up += 1
        elif direction == "down":
            baseline_down += 1
        else:
            baseline_flat += 1

        # 计算滚动窗口lambda
        window = trades_counts[i - window_size:i]
        lam = np.mean(window)
        if lam < 1:
            continue

        # 泊松检测
        p_value = poisson_p_value(current["trades_count"], lam)
        z_score = (current["trades_count"] - lam) / math.sqrt(max(lam, 1))

        # 当前K线方向
        candle_direction = "up" if current["close_price"] > current["open_price"] else "down"

        # 按小时统计
        hour_str = current["open_time"][11:13] if len(current["open_time"]) > 13 else "??"

        if p_value < 0.001:
            level = "extreme"
        elif p_value < 0.01:
            level = "anomaly"
        elif p_value < 0.05:
            level = "watch"
        else:
            continue

        results[level]["total"] += 1
        results[level][direction] += 1

        # 收益：猜对+1，猜错-1（二元期权）
        # 策略：跟随异常方向买入
        if candle_direction == direction:
            results[level]["profits"].append(1)
            hourly_stats[hour_str]["total"] += 1
            hourly_stats[hour_str]["win"] += 1
            if level in direction_consistent:
                direction_consistent[level] += 1
        elif direction != "flat":
            results[level]["profits"].append(-1)
            hourly_stats[hour_str]["total"] += 1
        else:
            results[level]["profits"].append(0)

    total_baseline = baseline_up + baseline_down + baseline_flat
    if total_baseline > 0:
        print("=" * 60)
        print("📊 基准胜率（纯随机猜测）")
        print("=" * 60)
        print(f"  总K线数: {total_baseline}")
        print(f"  上涨: {baseline_up} ({baseline_up/total_baseline*100:.1f}%)")
        print(f"  下跌: {baseline_down} ({baseline_down/total_baseline*100:.1f}%)")
        print(f"  持平: {baseline_flat} ({baseline_flat/total_baseline*100:.1f}%)")
        print(f"  随机猜涨胜率: {baseline_up/total_baseline*100:.1f}%")
        print()

    for level_name, label in [("watch", "⚠️ 关注 (p<0.05)"), ("anomaly", "🚨 异常 (p<0.01)"), ("extreme", "🔥 极端 (p<0.001)")]:
        r = results[level_name]
        if r["total"] == 0:
            continue

        total_with_dir = r["up"] + r["down"]
        win_rate = (r["up"] / total_with_dir * 100) if total_with_dir > 0 else 0

        # 跟随异常方向的胜率
        follow_win = direction_consistent.get(level_name, 0)
        follow_total = r["total"]
        follow_rate = (follow_win / follow_total * 100) if follow_total > 0 else 0

        # 累计收益
        cum_profit = sum(r["profits"])

        print("=" * 60)
        print(f"  {label}")
        print("=" * 60)
        print(f"  触发次数: {r['total']}")
        print(f"  未来10分钟上涨: {r['up']} ({r['up']/r['total']*100:.1f}%)")
        print(f"  未来10分钟下跌: {r['down']} ({r['down']/r['total']*100:.1f}%)")
        print(f"  未来10分钟持平: {r['flat']} ({r['flat']/r['total']*100:.1f}%)")
        print(f"  ─────────────────────────────")
        print(f"  看涨胜率: {r['up']/total_with_dir*100:.1f}% (基准 {baseline_up/total_baseline*100:.1f}%)")
        print(f"  跟随异常方向胜率: {follow_rate:.1f}%")
        print(f"  累计收益(跟方向): {cum_profit:+d} (每笔±1)")
        if len(r["profits"]) > 0:
            win_count = sum(1 for p in r["profits"] if p > 0)
            loss_count = sum(1 for p in r["profits"] if p < 0)
            print(f"  胜/负: {win_count} / {loss_count}")
        print()

    # 按小时统计
    print("=" * 60)
    print("⏰ 各时段异常信号胜率")
    print("=" * 60)
    for hour in sorted(hourly_stats.keys()):
        s = hourly_stats[hour]
        if s["total"] >= 3:
            rate = s["win"] / s["total"] * 100
            bar = "█" * int(rate / 5) + "░" * (20 - int(rate / 5))
            print(f"  {hour}:00  {rate:5.1f}% ({s['win']:2d}/{s['total']:2d}) {bar}")
    print()

    # 综合结论
    print("=" * 60)
    print("📋 综合结论")
    print("=" * 60)
    for level_name in ["anomaly", "extreme"]:
        r = results[level_name]
        if r["total"] == 0:
            continue
        total_with_dir = r["up"] + r["down"]
        up_rate = r["up"] / total_with_dir * 100 if total_with_dir > 0 else 0
        follow_win = direction_consistent.get(level_name, 0)
        follow_rate = follow_win / r["total"] * 100 if r["total"] > 0 else 0
        baseline = baseline_up / total_baseline * 100

        print(f"\n  {level_name.upper()}:")
        if follow_rate > 55:
            print(f"  ✅ 跟随异常方向胜率 {follow_rate:.1f}% > 55%，可能有效")
        elif follow_rate > 50:
            print(f"  ⚠️ 跟随异常方向胜率 {follow_rate:.1f}%，略高于50%，证据不足")
        else:
            print(f"  ❌ 跟随异常方向胜率 {follow_rate:.1f}%，低于50%")

        if up_rate > baseline + 5:
            print(f"  ✅ 异常后看涨胜率 {up_rate:.1f}% 显著高于基准 {baseline:.1f}%")
        elif abs(up_rate - baseline) <= 5:
            print(f"  ⚠️ 异常后看涨胜率 {up_rate:.1f}%，接近基准 {baseline:.1f}%")
        else:
            print(f"  📉 异常后看涨胜率 {up_rate:.1f}%，低于基准")


def main():
    if not DATA_FILE.exists():
        print(f"❌ 数据文件不存在: {DATA_FILE}")
        print("请先运行: python scripts/export_data.py")
        return

    klines = load_klines(DATA_FILE)
    print(f"已加载 {len(klines)} 条K线数据")
    print()

    run_backtest(klines, window_size=60, lookahead=10)


if __name__ == "__main__":
    main()
