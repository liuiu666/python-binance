"""
10分钟二元期权回测 — 主动买卖力量分析
利用 taker_buy_volume 分析主动买入/卖出力量，预测短期方向

用法: python scripts/backtest_taker.py
"""

import sys
import csv
import math
import numpy as np
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = PROJECT_ROOT / "data" / "klines_BTCUSDT_1m_7d.csv"


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
                "taker_buy_volume": float(row.get("taker_buy_volume", 0)),
            })
    return rows


def run_backtest(klines, lookahead=10):
    close_prices = np.array([k["close_price"] for k in klines])
    volumes = np.array([k["volume"] for k in klines])
    taker_buys = np.array([k["taker_buy_volume"] for k in klines])

    # 计算主动买入比例 (taker buy ratio)
    # TBR = taker_buy_volume / volume
    # TBR > 0.5 说明主动买入多于卖出，买方力量强
    # TBR < 0.5 说明主动卖出多于买入，卖方力量强
    with np.errstate(divide='ignore', invalid='ignore'):
        tbr = np.where(volumes > 0, taker_buys / volumes, 0.5)

    # 计算主动卖出量
    taker_sells = volumes - taker_buys

    print("=" * 70)
    print("📊 10分钟二元期权 — 主动买卖力量 (Taker Flow) 分析")
    print("=" * 70)
    print(f"数据: {len(klines)} 条K线, 前瞻{lookahead}分钟\n")

    # 先看TBR的基本统计
    valid_tbr = tbr[(volumes > 0)]
    print("── Taker Buy Ratio 基本统计 ──")
    print(f"  均值: {np.mean(valid_tbr):.4f}")
    print(f"  中位数: {np.median(valid_tbr):.4f}")
    print(f"  标准差: {np.std(valid_tbr):.4f}")
    print(f"  TBR>0.5占比: {np.mean(valid_tbr > 0.5)*100:.1f}%")
    print(f"  TBR>0.6占比: {np.mean(valid_tbr > 0.6)*100:.1f}%")
    print(f"  TBR<0.4占比: {np.mean(valid_tbr < 0.4)*100:.1f}%")
    print()

    strategies = {}

    # ====== 策略1: TBR极端 (>0.65 看涨, <0.35 看跌) ======
    for threshold_name, up_thresh, down_thresh in [
        ("TBR>0.6/<0.4", 0.6, 0.4),
        ("TBR>0.65/<0.35", 0.65, 0.35),
        ("TBR>0.7/<0.3", 0.7, 0.3),
        ("TBR>0.75/<0.25", 0.75, 0.25),
    ]:
        strat = {"total": 0, "win": 0, "profits": [], "long_win": 0, "long_total": 0,
                 "short_win": 0, "short_total": 0}
        for i in range(1, len(klines) - lookahead):
            if volumes[i] < 1:
                continue
            ratio = tbr[i]

            if ratio > up_thresh:
                pred = "up"
            elif ratio < down_thresh:
                pred = "down"
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

            # 分别统计做多做空
            if pred == "up":
                strat["long_total"] += 1
                if actual == "up":
                    strat["long_win"] += 1
            else:
                strat["short_total"] += 1
                if actual == "down":
                    strat["short_win"] += 1

        strategies[f"{threshold_name} 跟随方向"] = strat

    # ====== 策略2: TBR变化率（动量）======
    # TBR从低变高 → 买方力量增强 → 看涨
    # TBR从高变低 → 卖方力量增强 → 看跌
    for lookback_name, lookback in [("5根", 5), ("10根", 10), ("20根", 20)]:
        strat = {"total": 0, "win": 0, "profits": [], "long_win": 0, "long_total": 0,
                 "short_win": 0, "short_total": 0}
        for i in range(lookback, len(klines) - lookahead):
            if volumes[i] < 1 or volumes[i - lookback] < 1:
                continue
            tbr_change = tbr[i] - tbr[i - lookback]

            if abs(tbr_change) < 0.05:
                continue

            pred = "up" if tbr_change > 0 else "down"
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

            if pred == "up":
                strat["long_total"] += 1
                if actual == "up":
                    strat["long_win"] += 1
            else:
                strat["short_total"] += 1
                if actual == "down":
                    strat["short_win"] += 1

        strategies[f"TBR动量({lookback_name})"] = strat

    # ====== 策略3: TBR极端 + 放量 ======
    strat = {"total": 0, "win": 0, "profits": [], "long_win": 0, "long_total": 0,
             "short_win": 0, "short_total": 0}
    for i in range(60, len(klines) - lookahead):
        if volumes[i] < 1:
            continue
        vol_avg = np.mean(volumes[i-60:i])
        if vol_avg < 1:
            continue
        vol_ratio = volumes[i] / vol_avg

        # 放量2倍 + TBR极端
        if vol_ratio < 2:
            continue
        ratio = tbr[i]
        if ratio > 0.6:
            pred = "up"
        elif ratio < 0.4:
            pred = "down"
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

        if pred == "up":
            strat["long_total"] += 1
            if actual == "up":
                strat["long_win"] += 1
        else:
            strat["short_total"] += 1
            if actual == "down":
                strat["short_win"] += 1

    strategies["TBR极端+放量2倍"] = strat

    # ====== 策略4: 累计TBR（多根K线累计买卖力量）======
    for cum_name, cum_len in [("10根累计", 10), ("20根累计", 20), ("30根累计", 30)]:
        strat = {"total": 0, "win": 0, "profits": [], "long_win": 0, "long_total": 0,
                 "short_win": 0, "short_total": 0}
        for i in range(cum_len, len(klines) - lookahead):
            window_buy = np.sum(taker_buys[i-cum_len:i])
            window_sell = np.sum(taker_sells[i-cum_len:i])
            total = window_buy + window_sell
            if total < 1:
                continue
            cum_tbr = window_buy / total

            if cum_tbr > 0.55:
                pred = "up"
            elif cum_tbr < 0.45:
                pred = "down"
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

            if pred == "up":
                strat["long_total"] += 1
                if actual == "up":
                    strat["long_win"] += 1
            else:
                strat["short_total"] += 1
                if actual == "down":
                    strat["short_win"] += 1

        strategies[f"累计TBR({cum_name})"] = strat

    # ====== 策略5: TBR反转（极端TBR回归）======
    strat = {"total": 0, "win": 0, "profits": [], "long_win": 0, "long_total": 0,
             "short_win": 0, "short_total": 0}
    for i in range(5, len(klines) - lookahead):
        if volumes[i] < 1:
            continue
        # 前5根TBR极端高 → 预测回归（看跌）
        # 前5根TBR极端低 → 预测回归（看涨）
        prev_tbr = np.mean(tbr[i-5:i])
        if prev_tbr > 0.7:
            pred = "down"  # 买方力量过度，预期回落
        elif prev_tbr < 0.3:
            pred = "up"  # 卖方力量过度，预期反弹
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

        if pred == "up":
            strat["long_total"] += 1
            if actual == "up":
                strat["long_win"] += 1
        else:
            strat["short_total"] += 1
            if actual == "down":
                strat["short_win"] += 1

    strategies["TBR反转(极端回归)"] = strat

    # ====== 输出 ======
    print(f"{'策略':<28} {'次数':>6} {'胜率':>8} {'收益':>8} {'做多胜率':>10} {'做空胜率':>10}")
    print("-" * 80)

    strat_list = []
    for name, s in strategies.items():
        if s["total"] > 0:
            wr = s["win"] / s["total"] * 100
            profit = sum(s["profits"])
            long_wr = s["long_win"] / s["long_total"] * 100 if s["long_total"] > 0 else 0
            short_wr = s["short_win"] / s["short_total"] * 100 if s["short_total"] > 0 else 0
            strat_list.append((name, s["total"], wr, profit, long_wr, short_wr, s))

    strat_list.sort(key=lambda x: x[2], reverse=True)

    for name, total, wr, profit, long_wr, short_wr, s in strat_list:
        emoji = "✅" if wr > 55 else ("⚠️" if wr > 50 else "❌")
        print(f"{emoji} {name:<26} {total:>6} {wr:>7.1f}% {profit:>+7d}  {long_wr:>7.1f}%({s['long_total']:>3})  {short_wr:>7.1f}%({s['short_total']:>3})")

    print()

    # 最佳策略详情
    best_name, best_total, best_wr, _, _, _, best_s = strat_list[0]
    print("=" * 70)
    print(f"🏆 最佳策略详情: {best_name}")
    print("=" * 70)

    cum = np.cumsum(best_s["profits"])
    running_max = np.maximum.accumulate(cum)
    max_drawdown = int(np.max(running_max - cum))
    max_consec_loss = 0
    streak = 0
    for p in best_s["profits"]:
        if p < 0:
            streak += 1
            max_consec_loss = max(max_consec_loss, streak)
        else:
            streak = 0

    print(f"  总交易: {best_total}")
    print(f"  胜率: {best_wr:.1f}%")
    print(f"  累计收益: {sum(best_s['profits']):+d}")
    print(f"  最大连续亏损: {max_consec_loss} 次")
    print(f"  最大回撤: {max_drawdown}")
    wr_decimal = best_s["win"] / best_total
    for payout in [0.80, 0.85, 0.90, 0.95]:
        ev = wr_decimal * payout - (1 - wr_decimal) * 1.0
        print(f"  期望值({int(payout*100)}%赔率): {ev*100:+.2f}% 每笔")


def main():
    if not DATA_FILE.exists():
        print(f"❌ 数据文件不存在: {DATA_FILE}")
        return

    klines = load_klines(DATA_FILE)
    print(f"已加载 {len(klines)} 条K线数据\n")
    run_backtest(klines)


if __name__ == "__main__":
    main()
