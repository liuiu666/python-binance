"""秒级 POC Normal 回测 v2 — 修复版
- 加上 cooldown 冷却间隔
- 加上 80% 赔率（10分钟二元期权）
- taker_ratio 用上一个已完成 bar
- 处理平盘（不算赢也不算输）
- 计算实际盈亏
"""
import pandas as pd
import numpy as np
from scipy.stats import norm as scipy_norm
import os

CSV = os.path.join(os.path.dirname(__file__), "server_1s_trades.csv")
PAYOUT = 0.80  # 10分钟二元期权赔率 80%
BREAKEVEN_WR = 1.0 / (1.0 + PAYOUT)  # 盈亏平衡胜率 = 55.56%

def load():
    df = pd.read_csv(CSV)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for c in ["open","high","low","close","volume","quote_volume","trades",
              "taker_buy_volume","taker_sell_volume","taker_buy_sell_ratio"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("timestamp").reset_index(drop=True)

def agg_bars(df, seconds):
    df = df.copy()
    df["period"] = df["timestamp"].dt.floor(f"{seconds}s")
    g = df.groupby("period").agg(
        time=("timestamp", "first"),
        open=("open", "first"),
        close=("close", "last"),
        high=("high", "max"),
        low=("low", "min"),
        volume=("volume", "sum"),
        trades=("trades", "sum"),
        taker_buy=("taker_buy_volume", "sum"),
        taker_sell=("taker_sell_volume", "sum"),
    ).reset_index(drop=True)
    g["taker_ratio"] = np.where(g["taker_sell"] > 0, g["taker_buy"] / g["taker_sell"], 999.0)
    return g

def run_backtest(bars, bar_sec, window_min, horizon_min, tail_pct, cooldown_min=0,
                 taker_mode="none", taker_up=1.05, taker_dn=0.95):
    """
    完整回测，返回详细结果。
    cooldown_min: 同方向信号冷却间隔（分钟）
    taker_mode: none / align / not_counter
    """
    close = bars["close"].values
    n_bars = len(close)
    lr = np.log(close[1:] / close[:-1])
    lr = lr[np.isfinite(lr)]

    window_bars = max(2, int(window_min * 60 / bar_sec))
    horizon_bars = max(1, int(horizon_min * 60 / bar_sec))
    cooldown_bars = max(0, int(cooldown_min * 60 / bar_sec))
    poc_thresh = 1.0 - tail_pct

    trades = []
    last_signal_bar = {"UP": -999999, "DOWN": -999999}

    for i in range(window_bars, len(lr) - horizon_bars):
        # 1. 生成信号（只用历史数据）
        w = lr[i - window_bars:i]
        if len(w) < max(10, window_bars // 2):
            continue
        mu = np.mean(w)
        sigma = np.std(w, ddof=1)
        if sigma < 1e-10:
            continue
        z = (horizon_bars * mu) / (np.sqrt(horizon_bars) * sigma)
        p_up = scipy_norm.cdf(z)

        sig = None
        if p_up >= poc_thresh:
            sig = "DOWN"
        elif p_up <= tail_pct:
            sig = "UP"
        if sig is None:
            continue

        # 2. Cooldown 检查
        if i - last_signal_bar[sig] < cooldown_bars:
            continue

        # 3. Taker 过滤（用上一个已完成 bar 的 taker ratio）
        prev_idx = max(0, i - 1)
        tr = float(bars["taker_ratio"].iloc[prev_idx]) if prev_idx < len(bars) else 999.0

        if taker_mode == "align":
            if sig == "UP" and tr < taker_up:
                continue
            if sig == "DOWN" and tr > taker_dn:
                continue
        elif taker_mode == "not_counter":
            if sig == "UP" and tr < taker_up:
                continue
            if sig == "DOWN" and tr > taker_dn:
                continue

        # 4. 评估结果
        future_close = close[i + horizon_bars]
        current_close = close[i]
        price_change = future_close - current_close

        if price_change > 0:
            actual = "UP"
        elif price_change < 0:
            actual = "DOWN"
        else:
            actual = "FLAT"

        if actual == "FLAT":
            pnl = 0.0  # 平盘退还
            outcome = "flat"
        elif sig == actual:
            pnl = PAYOUT   # 赢: +80%
            outcome = "win"
        else:
            pnl = -1.0     # 输: -100%
            outcome = "loss"

        last_signal_bar[sig] = i
        trades.append({
            "bar_idx": i,
            "signal": sig,
            "p_up": round(p_up, 6),
            "entry_price": current_close,
            "exit_price": future_close,
            "price_change": price_change,
            "outcome": outcome,
            "pnl": pnl,
            "taker_ratio": tr,
        })

    if not trades:
        return None

    df = pd.DataFrame(trades)
    total = len(df)
    wins = (df["outcome"] == "win").sum()
    losses = (df["outcome"] == "loss").sum()
    flats = (df["outcome"] == "flat").sum()
    decided = wins + losses
    wr = wins / decided * 100 if decided > 0 else 0
    total_pnl = df["pnl"].sum()
    max_dd = 0
    cum = 0
    for p in df["pnl"]:
        cum += p
        if cum < max_dd:
            max_dd = cum

    return {
        "trades": total,
        "wins": int(wins),
        "losses": int(losses),
        "flats": int(flats),
        "win_rate": wr,
        "total_pnl": total_pnl,  # 以 1U 为单位
        "max_drawdown": max_dd,
        "breakeven": BREAKEVEN_WR * 100,
        "profitable": wr > BREAKEVEN_WR * 100,
        "df": df,
    }


def main():
    df = load()
    total_sec = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).total_seconds()
    print(f"数据: {len(df)} 行, {total_sec/60:.1f} 分钟")
    print(f"价格: {df['close'].min():.1f} ~ {df['close'].max():.1f}")
    print(f"赔率: {PAYOUT*100:.0f}%, 盈亏平衡胜率: {BREAKEVEN_WR*100:.2f}%")
    print()

    bar_configs = [
        (5, "5s"),
        (10, "10s"),
        (30, "30s"),
        (60, "1m"),
    ]

    tail_pcts = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
    window_mins = [5, 10]
    horizon_mins_list = [5, 10]
    cooldown_mins = [0, 2, 5, 10]

    taker_configs = [
        ("none", 0, 0),
        ("align", 1.05, 0.95),
        ("not_counter", 0.85, 1.15),
    ]

    all_results = []

    print(f"{'='*110}")
    print(f"完整回测扫描: bar × tail × window × horizon × cooldown × taker")
    print(f"{'='*110}")
    header = (f"{'bar':>4} | {'tail':>5} | {'win':>4} | {'hor':>4} | {'cd':>3} | "
              f"{'taker':>11} | {'交易':>4} | {'胜':>4} | {'负':>4} | {'平':>3} | "
              f"{'胜率':>6} | {'PNL':>7} | {'最大回撤':>7} | {'盈利':>4}")
    print(header)
    print("-" * len(header))

    for bar_sec, bar_name in bar_configs:
        bars = agg_bars(df, bar_sec)
        for w_min in window_mins:
            for h_min in horizon_mins_list:
                window_bars = int(w_min * 60 / bar_sec)
                horizon_bars = int(h_min * 60 / bar_sec)
                if window_bars + horizon_bars >= len(bars) - 1:
                    continue
                for tp in tail_pcts:
                    for cd in cooldown_mins:
                        for taker_mode, taker_up, taker_dn in taker_configs:
                            res = run_backtest(bars, bar_sec, w_min, h_min, tp, cd, taker_mode, taker_up, taker_dn)
                            if res is None or res["trades"] == 0:
                                continue
                            taker_label = taker_mode if taker_mode == "none" else f"{taker_mode}({taker_up}/{taker_dn})"
                            profit_flag = "Y" if res["profitable"] else "N"
                            print(f"{bar_name:>4} | {tp:>5.2f} | {w_min:>4} | {h_min:>4} | {cd:>3} | "
                                  f"{taker_label:>11} | {res['trades']:>4} | {res['wins']:>4} | {res['losses']:>4} | {res['flats']:>3} | "
                                  f"{res['win_rate']:>5.1f}% | {res['total_pnl']:>+7.2f} | {res['max_drawdown']:>+7.2f} | {profit_flag:>4}")
                            all_results.append({
                                "bar": bar_name, "bar_sec": bar_sec,
                                "tail": tp, "win": w_min, "hor": h_min, "cd": cd,
                                "taker": taker_mode,
                                **{k: v for k, v in res.items() if k != "df"}
                            })

    # === 汇总排名 ===
    print(f"\n{'='*110}")
    print(f"综合排名: 按 PNL 排序（仅显示有交易的配置）")
    print(f"{'='*110}")
    ranked = sorted(all_results, key=lambda x: -x["total_pnl"])
    print(f"{'#':>3} | {'bar':>4} | {'tail':>5} | {'win':>4} | {'hor':>4} | {'cd':>3} | "
          f"{'taker':>11} | {'交易':>4} | {'胜率':>6} | {'PNL':>7} | {'最大回撤':>7}")
    print("-" * 95)
    for i, r in enumerate(ranked[:30]):
        print(f"{i+1:>3} | {r['bar']:>4} | {r['tail']:>5.2f} | {r['win']:>4} | {r['hor']:>4} | {r['cd']:>3} | "
              f"{r['taker']:>11} | {r['trades']:>4} | {r['win_rate']:>5.1f}% | {r['total_pnl']:>+7.2f} | {r['max_drawdown']:>+7.2f}")

    # === 盈利配置（胜率 > 55.56%）===
    print(f"\n{'='*110}")
    print(f"盈利配置（胜率 > {BREAKEVEN_WR*100:.1f}%，交易数 >= 3）")
    print(f"{'='*110}")
    profitable = [r for r in all_results if r["win_rate"] > BREAKEVEN_WR * 100 and r["trades"] >= 3]
    profitable.sort(key=lambda x: -x["total_pnl"])
    for i, r in enumerate(profitable[:20]):
        print(f"  {i+1:>2}. {r['bar']:>4} tail={r['tail']:.2f} win={r['win']}min hor={r['hor']}min cd={r['cd']}min "
              f"taker={r['taker']:>11} → {r['trades']}笔 胜率={r['win_rate']:.1f}% "
              f"PNL={r['total_pnl']:+.2f} 回撤={r['max_drawdown']:+.2f}")

    if not profitable:
        print("  (无)")

    print(f"\n完成! 共测试 {len(all_results)} 种有效配置")

if __name__ == "__main__":
    main()
