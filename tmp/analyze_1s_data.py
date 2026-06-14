"""Analyze downloaded second-level BTCUSDT data for strategy optimization insights."""
import pandas as pd
import numpy as np
import os

CSV = os.path.join(os.path.dirname(__file__), "server_1s_trades.csv")

def main():
    df = pd.read_csv(CSV)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for c in ["open","high","low","close","volume","quote_volume","trades",
              "taker_buy_volume","taker_sell_volume","taker_buy_sell_ratio"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("timestamp").reset_index(drop=True)

    print(f"{'='*60}")
    print(f"秒级数据分析报告")
    print(f"{'='*60}")
    print(f"总行数: {len(df)}")
    print(f"时间范围: {df['timestamp'].iloc[0]} ~ {df['timestamp'].iloc[-1]}")
    span = df['timestamp'].iloc[-1] - df['timestamp'].iloc[0]
    print(f"时间跨度: {span.total_seconds()/60:.1f} 分钟")
    print(f"价格范围: {df['close'].min():.1f} ~ {df['close'].max():.1f}")
    print(f"价格变动: {(df['close'].iloc[-1] - df['close'].iloc[0]):.1f} ({(df['close'].iloc[-1]/df['close'].iloc[0]-1)*100:.3f}%)")

    # 1. Data quality - gaps
    print(f"\n{'='*60}")
    print("1. 数据完整性")
    print(f"{'='*60}")
    ts_diff = df["timestamp"].diff().dt.total_seconds()
    gaps = ts_diff[ts_diff > 2]
    print(f"  预期每秒1行, 实际中位间隔: {ts_diff.median():.1f}s, 平均: {ts_diff.mean():.2f}s")
    print(f"  >2秒的间隔: {len(gaps)} 个")
    if len(gaps) > 0:
        print(f"  最大间隔: {ts_diff.max():.1f}s")
        for idx in gaps.index[:10]:
            print(f"    gap at {df['timestamp'].iloc[idx]}: {ts_diff.iloc[idx]:.0f}s")

    # 2. Per-second trade activity
    print(f"\n{'='*60}")
    print("2. 每秒交易活跃度")
    print(f"{'='*60}")
    print(f"  每秒平均成交笔数: {df['trades'].mean():.1f}")
    print(f"  每秒平均成交量(BTC): {df['volume'].mean():.3f}")
    print(f"  每秒平均成交额(USDT): {df['quote_volume'].mean():,.0f}")
    print(f"  零成交秒数: {(df['trades']==0).sum()} ({(df['trades']==0).mean()*100:.1f}%)")
    print(f"  单笔成交>10的秒数: {(df['trades']>=10).sum()} ({(df['trades']>=10).mean()*100:.1f}%)")
    print(f"  单笔成交>50的秒数(大单): {(df['trades']>=50).sum()}")

    # 3. Taker buy/sell ratio distribution
    print(f"\n{'='*60}")
    print("3. Taker 买卖比分布 (关键: 当前策略过滤条件)")
    print(f"{'='*60}")
    ratio = df["taker_buy_sell_ratio"]
    ratio_valid = ratio[(ratio > 0) & (ratio < 900)]
    print(f"  有效行数: {len(ratio_valid)} / {len(ratio)}")
    print(f"  ratio=999(纯买)的比例: {(ratio>=999).sum()} ({(ratio>=999).mean()*100:.1f}%)")
    print(f"  ratio=0(纯卖)的比例: {(ratio<=0).sum()} ({(ratio<=0).mean()*100:.1f}%)")
    print(f"\n  有效ratio统计:")
    print(f"    均值: {ratio_valid.mean():.3f}")
    print(f"    中位数: {ratio_valid.median():.3f}")
    print(f"    标准差: {ratio_valid.std():.3f}")
    print(f"    25%: {ratio_valid.quantile(0.25):.3f}")
    print(f"    75%: {ratio_valid.quantile(0.75):.3f}")

    # Current filter thresholds analysis
    thresholds = [
        ("当前TAKER align_down=0.95", 0.95),
        ("当前TAKER align_up=1.05", 1.05),
        ("TAKER_27 align_down=0.95", 0.95),
        ("TAKER_27 align_up=1.05", 1.05),
        ("放宽 align_down=0.90", 0.90),
        ("放宽 align_up=1.10", 1.10),
        ("not_counter_down=1.15", 1.15),
        ("not_counter_up=0.85", 0.85),
    ]
    print(f"\n  各阈值通过比例:")
    print(f"    ratio >= 1.05 (UP align): {(ratio_valid>=1.05).mean()*100:.1f}%")
    print(f"    ratio <= 0.95 (DOWN align): {(ratio_valid<=0.95).mean()*100:.1f}%")
    print(f"    ratio >= 1.10 (宽松UP): {(ratio_valid>=1.10).mean()*100:.1f}%")
    print(f"    ratio <= 0.90 (宽松DOWN): {(ratio_valid<=0.90).mean()*100:.1f}%")
    print(f"    ratio >= 0.85 (not_counter UP): {(ratio_valid>=0.85).mean()*100:.1f}%")
    print(f"    ratio <= 1.15 (not_counter DOWN): {(ratio_valid<=1.15).mean()*100:.1f}%")

    # 4. Aggregate to 2m bars and run POC Normal analysis
    print(f"\n{'='*60}")
    print("4. 聚合到2分钟K线 + POC Normal 信号模拟")
    print(f"{'='*60}")
    df["period_2m"] = df["timestamp"].dt.floor("2min")
    agg2m = df.groupby("period_2m").agg(
        open=("open", "first"),
        close=("close", "last"),
        high=("high", "max"),
        low=("low", "min"),
        volume=("volume", "sum"),
        trades=("trades", "sum"),
        taker_buy_vol=("taker_buy_volume", "sum"),
        taker_sell_vol=("taker_sell_volume", "sum"),
    ).reset_index()
    agg2m["taker_ratio"] = np.where(
        agg2m["taker_sell_vol"] > 0,
        agg2m["taker_buy_vol"] / agg2m["taker_sell_vol"],
        999.0
    )
    print(f"  2分钟K线数: {len(agg2m)}")
    print(f"  平均每根K线成交量: {agg2m['volume'].mean():.2f} BTC")

    # POC Normal simulation at different tail_pcts
    close_2m = agg2m["close"].values
    lr_2m = np.log(close_2m[1:] / close_2m[:-1])
    lr_2m = lr_2m[np.isfinite(lr_2m)]
    print(f"  2分钟对数收益率: 均值={lr_2m.mean():.6f}, 标准差={lr_2m.std():.6f}")

    window = 30  # 60min / 2min = 30 bars
    horizon = 5  # 10min / 2min = 5 bars
    tail_pcts = [0.15, 0.18, 0.20, 0.22, 0.23, 0.25, 0.27, 0.30, 0.35]

    print(f"\n  POC Normal 信号统计 (window=60min, horizon=10min):")
    print(f"  {'tail_pct':>8} | {'UP信号':>6} | {'DOWN信号':>6} | {'总信号':>6} | {'每bar频率':>10}")
    print(f"  {'-'*8}-+-{'-'*6}-+-{'-'*6}-+-{'-'*6}-+-{'-'*10}")

    for tp in tail_pcts:
        poc_thresh = 1.0 - tp
        signals_up = 0
        signals_dn = 0
        for i in range(window, len(lr_2m)):
            w = lr_2m[i-window:i]
            if len(w) < 20:
                continue
            mu = np.mean(w)
            sigma = np.std(w, ddof=1)
            if sigma < 1e-10:
                continue
            from scipy.stats import norm as scipy_norm
            z = (horizon * mu) / (np.sqrt(horizon) * sigma)
            p_up = scipy_norm.cdf(z)
            if p_up >= poc_thresh:
                signals_dn += 1
            elif p_up <= tp:
                signals_up += 1
        total = signals_up + signals_dn
        n_bars = len(lr_2m) - window
        freq = total / n_bars if n_bars > 0 else 0
        print(f"  {tp:>8.2f} | {signals_up:>6d} | {signals_dn:>6d} | {total:>6d} | {freq:>10.3f}")

    # 5. Taker filter impact at 2m level
    print(f"\n{'='*60}")
    print("5. Taker 过滤对信号的影响 (tail_pct=0.20)")
    print(f"{'='*60}")
    tp = 0.20
    poc_thresh = 0.80
    signals = []
    for i in range(window, len(lr_2m)):
        w = lr_2m[i-window:i]
        if len(w) < 20:
            continue
        mu = np.mean(w)
        sigma = np.std(w, ddof=1)
        if sigma < 1e-10:
            continue
        from scipy.stats import norm as scipy_norm
        z = (horizon * mu) / (np.sqrt(horizon) * sigma)
        p_up = scipy_norm.cdf(z)
        sig = None
        if p_up >= poc_thresh:
            sig = "DOWN"
        elif p_up <= tp:
            sig = "UP"
        if sig:
            tr = agg2m["taker_ratio"].iloc[i] if i < len(agg2m) else None
            signals.append({"signal": sig, "taker_ratio": tr, "p_up": p_up})

    if signals:
        sdf = pd.DataFrame(signals)
        print(f"  总信号数: {len(sdf)}")
        print(f"    UP: {(sdf['signal']=='UP').sum()}, DOWN: {(sdf['signal']=='DOWN').sum()}")
        print(f"\n  Taker align过滤效果:")
        up_sigs = sdf[sdf['signal'] == 'UP']
        dn_sigs = sdf[sdf['signal'] == 'DOWN']
        for thresh_name, up_t, dn_t in [
            ("align 1.05/0.95", 1.05, 0.95),
            ("宽松 1.10/0.90", 1.10, 0.90),
            ("更宽 1.15/0.85", 1.15, 0.85),
            ("not_counter 1.15/0.85", 1.15, 0.85),
        ]:
            up_pass = (up_sigs['taker_ratio'] >= up_t).sum() if len(up_sigs) > 0 else 0
            dn_pass = (dn_sigs['taker_ratio'] <= dn_t).sum() if len(dn_sigs) > 0 else 0
            total_pass = up_pass + dn_pass
            print(f"    {thresh_name:>25s}: UP通过={up_pass}/{len(up_sigs)}, DOWN通过={dn_pass}/{len(dn_sigs)}, 合计={total_pass}/{len(sdf)} ({total_pass/len(sdf)*100:.0f}%)")

    # 6. Second-level microstructure insights
    print(f"\n{'='*60}")
    print("6. 秒级微观结构洞察")
    print(f"{'='*60}")
    # Large single-second moves
    df["ret_1s"] = df["close"].pct_change()
    big_moves = df[df["ret_1s"].abs() > 0.001]  # >0.1% in 1 second
    print(f"  >0.1%秒级波动: {len(big_moves)} 次 ({len(big_moves)/len(df)*100:.2f}%)")
    for _, row in big_moves.head(10).iterrows():
        print(f"    {row['timestamp']} close={row['close']:.1f} ret={row['ret_1s']*100:.3f}% vol={row['volume']:.3f}BTC trades={int(row['trades'])}")

    # Consecutive directional pressure
    df["direction"] = np.where(df["taker_buy_volume"] > df["taker_sell_volume"], 1, -1)
    print(f"\n  连续同方向分析:")
    streaks = []
    current_dir = df["direction"].iloc[0]
    streak_start = 0
    for i in range(1, len(df)):
        if df["direction"].iloc[i] != current_dir:
            streaks.append({"dir": current_dir, "len": i - streak_start, "start": df["timestamp"].iloc[streak_start]})
            current_dir = df["direction"].iloc[i]
            streak_start = i
    streaks.append({"dir": current_dir, "len": len(df) - streak_start, "start": df["timestamp"].iloc[streak_start]})
    sdf = pd.DataFrame(streaks)
    print(f"    总连续段数: {len(sdf)}")
    print(f"    平均连续秒数: {sdf['len'].mean():.1f}")
    print(f"    最长连续: {sdf['len'].max()} 秒 (方向={'买' if sdf.loc[sdf['len'].idxmax(), 'dir']==1 else '卖'})")
    for threshold in [10, 20, 30, 60]:
        n = (sdf['len'] >= threshold).sum()
        print(f"    >=  {threshold}秒连续: {n} 次")

    print(f"\n{'='*60}")
    print("分析完成!")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
