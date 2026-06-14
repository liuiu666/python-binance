"""用 79 分钟秒级数据回测 POC Normal 策略，测试不同参数组合的信号频率和胜率。"""
import pandas as pd
import numpy as np
from scipy.stats import norm as scipy_norm
import os, itertools

CSV = os.path.join(os.path.dirname(__file__), "server_1s_trades.csv")

def load():
    df = pd.read_csv(CSV)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for c in ["open","high","low","close","volume","quote_volume","trades",
              "taker_buy_volume","taker_sell_volume","taker_buy_sell_ratio"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("timestamp").reset_index(drop=True)

def agg_bars(df, seconds):
    """把秒级数据聚合为 N 秒 K 线"""
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

def run_poc_normal(bars, bar_sec, window_min, horizon_min, tail_pct, mode="reversal"):
    """
    POC Normal 回测。
    返回信号列表，每个包含 signal, p_up, taker_ratio, win(是否预测正确)
    """
    close = bars["close"].values
    lr = np.log(close[1:] / close[:-1])
    lr = lr[np.isfinite(lr)]

    window_bars = max(2, int(window_min * 60 / bar_sec))
    horizon_bars = max(1, int(horizon_min * 60 / bar_sec))
    poc_thresh = 1.0 - tail_pct

    signals = []
    for i in range(window_bars, len(lr) - horizon_bars):
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
        if mode == "reversal":
            if p_up >= poc_thresh:
                sig = "DOWN"
            elif p_up <= tail_pct:
                sig = "UP"

        if sig is None:
            continue

        # 判断是否预测正确: 比较 horizon 后的价格
        future_close = close[i + horizon_bars] if (i + horizon_bars) < len(close) else None
        current_close = close[i]
        if future_close is None:
            continue
        actual_dir = "UP" if future_close > current_close else "DOWN"
        win = (sig == actual_dir)
        tr = float(bars["taker_ratio"].iloc[i]) if i < len(bars) else 999.0

        signals.append({
            "idx": i,
            "signal": sig,
            "p_up": p_up,
            "win": win,
            "taker_ratio": tr,
            "entry_price": current_close,
            "exit_price": future_close,
            "ret_pct": (future_close / current_close - 1) * 100,
        })

    return pd.DataFrame(signals) if signals else pd.DataFrame()

def test_taker_filter(sdf, filter_mode, up_thresh, dn_thresh):
    """测试 taker 过滤后的信号"""
    if sdf.empty:
        return 0, 0, 0
    if filter_mode == "none":
        mask = pd.Series([True] * len(sdf))
    elif filter_mode == "align":
        mask = np.where(
            sdf["signal"] == "UP",
            sdf["taker_ratio"] >= up_thresh,
            sdf["taker_ratio"] <= dn_thresh
        )
    elif filter_mode == "not_counter":
        mask = np.where(
            sdf["signal"] == "UP",
            sdf["taker_ratio"] >= up_thresh,
            sdf["taker_ratio"] <= dn_thresh
        )
    else:
        mask = pd.Series([True] * len(sdf))

    passed = sdf[mask]
    if passed.empty:
        return 0, 0, 0
    n = len(passed)
    wins = passed["win"].sum()
    wr = wins / n * 100 if n > 0 else 0
    return n, wins, wr

def main():
    df = load()
    total_sec = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).total_seconds()
    print(f"数据: {len(df)} 行, {total_sec/60:.1f} 分钟")
    print(f"价格: {df['close'].min():.1f} ~ {df['close'].max():.1f}")
    print()

    # 测试不同聚合级别
    bar_configs = [
        (5, "5秒K线"),
        (10, "10秒K线"),
        (15, "15秒K线"),
        (30, "30秒K线"),
        (60, "1分钟K线"),
    ]

    # 参数网格
    tail_pcts = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
    window_mins = [1, 2, 5, 10]
    horizon_mins_list = [1, 2, 5, 10]

    print(f"{'='*90}")
    print(f"参数网格扫描: bar_size × tail_pct × window × horizon")
    print(f"{'='*90}")
    print(f"{'bar':>6} | {'tail':>5} | {'win_min':>7} | {'h_min':>5} | {'信号':>4} | {'胜':>4} | {'胜率':>6} | {'信号/min':>8}")
    print(f"{'-'*6}-+-{'-'*5}-+-{'-'*7}-+-{'-'*5}-+-{'-'*4}-+-{'-'*4}-+-{'-'*6}-+-{'-'*8}")

    results = []
    for bar_sec, bar_name in bar_configs:
        bars = agg_bars(df, bar_sec)
        for w_min in window_mins:
            for h_min in horizon_mins_list:
                # 确保数据足够
                window_bars = int(w_min * 60 / bar_sec)
                horizon_bars = int(h_min * 60 / bar_sec)
                if window_bars + horizon_bars >= len(bars) - 1:
                    continue
                for tp in tail_pcts:
                    sdf = run_poc_normal(bars, bar_sec, w_min, h_min, tp)
                    if sdf.empty:
                        continue
                    n = len(sdf)
                    wins = int(sdf["win"].sum())
                    wr = wins / n * 100
                    rate = n / (total_sec / 60)

                    # taker align 过滤
                    n_a, w_a, wr_a = test_taker_filter(sdf, "align", 1.05, 0.95)
                    # taker not_counter
                    n_nc, w_nc, wr_nc = test_taker_filter(sdf, "not_counter", 0.85, 1.15)

                    results.append({
                        "bar_sec": bar_sec, "bar_name": bar_name,
                        "tail_pct": tp, "window_min": w_min, "horizon_min": h_min,
                        "signals": n, "wins": wins, "wr": wr, "rate": rate,
                        "taker_align_n": n_a, "taker_align_wr": wr_a,
                        "taker_nc_n": n_nc, "taker_nc_wr": wr_nc,
                    })

                    print(f"{bar_name:>6} | {tp:>5.2f} | {w_min:>7} | {h_min:>5} | {n:>4} | {wins:>4} | {wr:>5.1f}% | {rate:>7.2f}")

    # Taker 过滤对比
    print(f"\n{'='*90}")
    print(f"Taker 过滤对比 (仅显示有信号的配置)")
    print(f"{'='*90}")
    print(f"{'bar':>6} | {'tail':>5} | {'win':>4} | {'hor':>4} | {'无过滤':>12} | {'align 1.05':>12} | {'not_counter':>12}")
    print(f"{'-'*6}-+-{'-'*5}-+-{'-'*4}-+-{'-'*4}-+-{'-'*12}-+-{'-'*12}-+-{'-'*12}")
    for r in results:
        if r["signals"] == 0:
            continue
        no_filter = f"{r['signals']:>4} {r['wr']:>5.1f}%"
        align = f"{r['taker_align_n']:>4} {r['taker_align_wr']:>5.1f}%"
        nc = f"{r['taker_nc_n']:>4} {r['taker_nc_wr']:>5.1f}%"
        print(f"{r['bar_name']:>6} | {r['tail_pct']:>5.2f} | {r['window_min']:>4} | {r['horizon_min']:>4} | {no_filter:>12} | {align:>12} | {nc:>12}")

    # 按胜率排序的 top 配置
    print(f"\n{'='*90}")
    print(f"胜率 Top 20 (信号数 >= 2)")
    print(f"{'='*90}")
    top = sorted([r for r in results if r["signals"] >= 2], key=lambda x: -x["wr"])[:20]
    for i, r in enumerate(top):
        print(f"  {i+1:>2}. {r['bar_name']:>6} tail={r['tail_pct']:.2f} win={r['window_min']}min hor={r['horizon_min']}min "
              f"→ {r['signals']}信号 胜率={r['wr']:.1f}% ({r['wins']}/{r['signals']}) "
              f"信号率={r['rate']:.2f}/min "
              f"align={r['taker_align_n']}({r['taker_align_wr']:.0f}%) "
              f"nc={r['taker_nc_n']}({r['taker_nc_wr']:.0f}%)")

    # 按信号频率排序
    print(f"\n{'='*90}")
    print(f"信号频率 Top 20 (胜率 >= 50%)")
    print(f"{'='*90}")
    top_rate = sorted([r for r in results if r["wr"] >= 50], key=lambda x: -x["rate"])[:20]
    for i, r in enumerate(top_rate):
        print(f"  {i+1:>2}. {r['bar_name']:>6} tail={r['tail_pct']:.2f} win={r['window_min']}min hor={r['horizon_min']}min "
              f"→ {r['signals']}信号 胜率={r['wr']:.1f}% 信号率={r['rate']:.2f}/min")

    print(f"\n完成! 共测试 {len(results)} 种参数组合")

if __name__ == "__main__":
    main()
