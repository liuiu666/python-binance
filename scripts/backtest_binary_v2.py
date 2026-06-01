"""
10分钟二元期权回测 — 多信号组合策略
在泊松异常基础上叠加：价格动量、成交量变化、连续异常等信号

用法: python scripts/backtest_binary_v2.py
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
    if k <= lam:
        return 1.0
    if lam > 50:
        z = (k - 0.5 - lam) / math.sqrt(lam)
        return 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))
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


def calc_rsi(prices, period=14):
    """计算RSI"""
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.convolve(gains, np.ones(period)/period, mode='full')[:len(gains)]
    avg_loss = np.convolve(losses, np.ones(period)/period, mode='full')[:len(losses)]
    # 简单移动平均
    avg_gain = np.cumsum(gains) / np.arange(1, len(gains)+1)
    avg_loss = np.cumsum(losses) / np.arange(1, len(losses)+1)
    rs = np.where(avg_loss > 0, avg_gain / avg_loss, 100)
    rsi = 100 - (100 / (1 + rs))
    # 填充第一个元素
    return np.concatenate([[50], rsi])


def run_backtest(klines, window_size=60, lookahead=10):
    trades_counts = np.array([k["trades_count"] for k in klines])
    close_prices = np.array([k["close_price"] for k in klines])
    volumes = np.array([k["volume"] for k in klines])

    # 预计算特征
    rsi = calc_rsi(close_prices, 14)

    # 策略列表
    strategies = {}

    # ====== 策略1: 泊松异常 + 放量阳线/阴线 ======
    strat = {"total": 0, "win": 0, "profits": [], "trades": []}
    for i in range(window_size, len(klines) - lookahead):
        window = trades_counts[i - window_size:i]
        lam = np.mean(window)
        if lam < 1:
            continue
        p = poisson_p_value(trades_counts[i], lam)
        if p >= 0.01:
            continue

        # 当前K线是阳线还是阴线
        candle_up = klines[i]["close_price"] > klines[i]["open_price"]
        # 预测方向：跟随阳线看涨，阴线看跌
        pred = "up" if candle_up else "down"

        future_close = close_prices[i + lookahead]
        current_close = close_prices[i]
        actual = "up" if future_close > current_close else ("down" if future_close < current_close else "flat")

        strat["total"] += 1
        win = (pred == actual)
        if win:
            strat["win"] += 1
            strat["profits"].append(1)
        elif actual != "flat":
            strat["profits"].append(-1)
        else:
            strat["profits"].append(0)
        strat["trades"].append({"time": klines[i]["open_time"], "pred": pred, "actual": actual, "p": p})

    strategies["泊松异常+K线方向"] = strat

    # ====== 策略2: 泊松异常 + 连续2根同方向 ======
    strat = {"total": 0, "win": 0, "profits": [], "trades": []}
    for i in range(window_size + 1, len(klines) - lookahead):
        window = trades_counts[i - window_size:i]
        lam = np.mean(window)
        if lam < 1:
            continue
        p = poisson_p_value(trades_counts[i], lam)
        if p >= 0.01:
            continue

        # 当前和前一根都是同方向
        curr_up = klines[i]["close_price"] > klines[i]["open_price"]
        prev_up = klines[i-1]["close_price"] > klines[i-1]["open_price"]
        if curr_up != prev_up:
            continue

        pred = "up" if curr_up else "down"
        future_close = close_prices[i + lookahead]
        current_close = close_prices[i]
        actual = "up" if future_close > current_close else ("down" if future_close < current_close else "flat")

        strat["total"] += 1
        win = (pred == actual)
        if win:
            strat["win"] += 1
            strat["profits"].append(1)
        elif actual != "flat":
            strat["profits"].append(-1)
        else:
            strat["profits"].append(0)

    strategies["泊松异常+连续同向"] = strat

    # ====== 策略3: 泊松异常 + RSI超买超卖反转 ======
    strat = {"total": 0, "win": 0, "profits": [], "trades": []}
    for i in range(window_size, len(klines) - lookahead):
        window = trades_counts[i - window_size:i]
        lam = np.mean(window)
        if lam < 1:
            continue
        p = poisson_p_value(trades_counts[i], lam)
        if p >= 0.01:
            continue

        current_rsi = rsi[i]
        # RSI>70 超买 → 预测下跌; RSI<30 超卖 → 预测上涨
        if current_rsi > 70:
            pred = "down"
        elif current_rsi < 30:
            pred = "up"
        else:
            continue

        future_close = close_prices[i + lookahead]
        current_close = close_prices[i]
        actual = "up" if future_close > current_close else ("down" if future_close < current_close else "flat")

        strat["total"] += 1
        win = (pred == actual)
        if win:
            strat["win"] += 1
            strat["profits"].append(1)
        elif actual != "flat":
            strat["profits"].append(-1)
        else:
            strat["profits"].append(0)

    strategies["泊松异常+RSI反转"] = strat

    # ====== 策略4: 泊松异常 + 成交量放大>3倍 ======
    strat = {"total": 0, "win": 0, "profits": [], "trades": []}
    for i in range(window_size, len(klines) - lookahead):
        window = trades_counts[i - window_size:i]
        lam = np.mean(window)
        if lam < 1:
            continue
        p = poisson_p_value(trades_counts[i], lam)
        if p >= 0.01:
            continue

        # 成交量放大3倍以上
        vol_avg = np.mean(volumes[i - window_size:i])
        if vol_avg < 1 or volumes[i] < vol_avg * 3:
            continue

        candle_up = klines[i]["close_price"] > klines[i]["open_price"]
        pred = "up" if candle_up else "down"

        future_close = close_prices[i + lookahead]
        current_close = close_prices[i]
        actual = "up" if future_close > current_close else ("down" if future_close < current_close else "flat")

        strat["total"] += 1
        win = (pred == actual)
        if win:
            strat["win"] += 1
            strat["profits"].append(1)
        elif actual != "flat":
            strat["profits"].append(-1)
        else:
            strat["profits"].append(0)

    strategies["泊松异常+放量3倍+方向"] = strat

    # ====== 策略5: 泊松异常 + 价格突破前高/前低 ======
    strat = {"total": 0, "win": 0, "profits": [], "trades": []}
    for i in range(window_size + 10, len(klines) - lookahead):
        window = trades_counts[i - window_size:i]
        lam = np.mean(window)
        if lam < 1:
            continue
        p = poisson_p_value(trades_counts[i], lam)
        if p >= 0.05:
            continue

        # 前10根K线最高/最低
        recent_high = max(close_prices[i-10:i])
        recent_low = min(close_prices[i-10:i])
        current_close = close_prices[i]

        if current_close > recent_high:
            pred = "up"  # 突破前高看涨
        elif current_close < recent_low:
            pred = "down"  # 跌破前低看跌
        else:
            continue

        future_close = close_prices[i + lookahead]
        actual = "up" if future_close > current_close else ("down" if future_close < current_close else "flat")

        strat["total"] += 1
        win = (pred == actual)
        if win:
            strat["win"] += 1
            strat["profits"].append(1)
        elif actual != "flat":
            strat["profits"].append(-1)
        else:
            strat["profits"].append(0)

    strategies["泊松异常+突破前高/低"] = strat

    # ====== 策略6: 纯动量（对照） ======
    strat = {"total": 0, "win": 0, "profits": [], "trades": []}
    for i in range(5, len(klines) - lookahead):
        # 最近5根K线方向
        changes = close_prices[i] - close_prices[i-5]
        if abs(changes) / close_prices[i] < 0.001:
            continue
        pred = "up" if changes > 0 else "down"

        future_close = close_prices[i + lookahead]
        current_close = close_prices[i]
        actual = "up" if future_close > current_close else ("down" if future_close < current_close else "flat")

        strat["total"] += 1
        win = (pred == actual)
        if win:
            strat["win"] += 1
            strat["profits"].append(1)
        elif actual != "flat":
            strat["profits"].append(-1)
        else:
            strat["profits"].append(0)

    strategies["纯5分钟动量(对照)"] = strat

    # ====== 策略7: 泊松异常 + 放量 + 连续同向 ======
    strat = {"total": 0, "win": 0, "profits": [], "trades": []}
    for i in range(window_size + 1, len(klines) - lookahead):
        window = trades_counts[i - window_size:i]
        lam = np.mean(window)
        if lam < 1:
            continue
        p = poisson_p_value(trades_counts[i], lam)
        if p >= 0.01:
            continue

        # 放量2倍
        vol_avg = np.mean(volumes[i - window_size:i])
        if vol_avg < 1 or volumes[i] < vol_avg * 2:
            continue

        # 连续同向
        curr_up = klines[i]["close_price"] > klines[i]["open_price"]
        prev_up = klines[i-1]["close_price"] > klines[i-1]["open_price"]
        if curr_up != prev_up:
            continue

        pred = "up" if curr_up else "down"
        future_close = close_prices[i + lookahead]
        current_close = close_prices[i]
        actual = "up" if future_close > current_close else ("down" if future_close < current_close else "flat")

        strat["total"] += 1
        win = (pred == actual)
        if win:
            strat["win"] += 1
            strat["profits"].append(1)
        elif actual != "flat":
            strat["profits"].append(-1)
        else:
            strat["profits"].append(0)

    strategies["泊松异常+放量2倍+连续同向"] = strat

    # ====== 输出结果 ======
    print("=" * 70)
    print("📊 10分钟二元期权 — 多策略回测对比")
    print("=" * 70)
    print(f"数据: {len(klines)} 条K线, 窗口{window_size}, 前瞻{lookahead}分钟")
    print()

    # 按胜率排序
    strat_list = []
    for name, s in strategies.items():
        if s["total"] > 0:
            wr = s["win"] / s["total"] * 100
            profit = sum(s["profits"])
            strat_list.append((name, s["total"], wr, profit, len([p for p in s["profits"] if p > 0]), len([p for p in s["profits"] if p < 0])))

    strat_list.sort(key=lambda x: x[2], reverse=True)

    print(f"{'策略':<25} {'次数':>6} {'胜率':>8} {'收益':>8} {'胜/负':>10}")
    print("-" * 70)
    for name, total, wr, profit, wins, losses in strat_list:
        emoji = "✅" if wr > 55 else ("⚠️" if wr > 50 else "❌")
        print(f"{emoji} {name:<23} {total:>6} {wr:>7.1f}% {profit:>+7d} {wins:>4}/{losses:<4}")

    print()

    # 详细分析最佳策略
    best_name = strat_list[0][0]
    best = strategies[best_name]
    print("=" * 70)
    print(f"🏆 最佳策略详情: {best_name}")
    print("=" * 70)

    # 计算连续亏损
    max_consec_loss = 0
    current_loss_streak = 0
    for p in best["profits"]:
        if p < 0:
            current_loss_streak += 1
            max_consec_loss = max(max_consec_loss, current_loss_streak)
        else:
            current_loss_streak = 0

    # 计算最大回撤
    cum = np.cumsum(best["profits"])
    running_max = np.maximum.accumulate(cum)
    max_drawdown = int(np.max(running_max - cum))

    total_with_result = best["total"]
    print(f"  总交易: {total_with_result}")
    print(f"  胜率: {best['win']/total_with_result*100:.1f}%")
    print(f"  累计收益: {sum(best['profits']):+d}")
    print(f"  最大连续亏损: {max_consec_loss} 次")
    print(f"  最大回撤: {max_drawdown}")

    # 如果二元期权赔率80%，计算期望收益
    if total_with_result > 0:
        wr = best["win"] / total_with_result
        ev = wr * 0.80 - (1 - wr) * 1.0
        print(f"  期望值(80%赔率): {ev*100:+.2f}% 每笔")
        ev85 = wr * 0.85 - (1 - wr) * 1.0
        print(f"  期望值(85%赔率): {ev85*100:+.2f}% 每笔")
        ev90 = wr * 0.90 - (1 - wr) * 1.0
        print(f"  期望值(90%赔率): {ev90*100:+.2f}% 每笔")


def main():
    if not DATA_FILE.exists():
        print(f"❌ 数据文件不存在: {DATA_FILE}")
        return

    klines = load_klines(DATA_FILE)
    print(f"已加载 {len(klines)} 条K线数据\n")
    run_backtest(klines)


if __name__ == "__main__":
    main()
