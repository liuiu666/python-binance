import pandas as pd
import numpy as np
import ta

def analyze_1m_data(file_path='WETUSDT_1m.csv'):
    print(f"正在分析数据文件: {file_path} ...")
    
    # 1. 加载数据
    try:
        df = pd.read_csv(file_path)
    except FileNotFoundError:
        print("错误: 找不到数据文件，请先运行 download_data.py")
        return

    # 转换时间
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    
    # 2. 计算基础指标
    # ATR (14) - 衡量波动幅度
    df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
    
    # RSI (14) - 衡量超买超卖
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    
    # Bollinger Bands (20, 2) - 衡量价格位置
    indicator_bb = ta.volatility.BollingerBands(close=df['close'], window=20, window_dev=2)
    df['bb_upper'] = indicator_bb.bollinger_hband()
    df['bb_lower'] = indicator_bb.bollinger_lband()
    df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_upper'] # 布林带宽度
    
    # EMA (20) - 短期趋势
    df['ema20'] = ta.trend.EMAIndicator(close=df['close'], window=20).ema_indicator()
    
    # ADX (14) - 趋势强度
    df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()

    # KDJ (9, 3) - 随机指标
    kdj = ta.momentum.StochasticOscillator(df['high'], df['low'], df['close'], window=14, smooth_window=3)
    df['k'] = kdj.stoch()
    df['d'] = kdj.stoch_signal()

    # 3. 统计分析报告
    print("\n" + "="*20 + " 1分钟短线数据分析报告 " + "="*20)
    print(f"数据时间范围: {df['timestamp'].iloc[0]} 至 {df['timestamp'].iloc[-1]}")
    print(f"总 K 线数: {len(df)}")

    # --- 波动率分析 ---
    avg_price = df['close'].mean()
    avg_atr = df['atr'].mean()
    avg_range_pct = ((df['high'] - df['low']) / df['open']).mean() * 100
    
    print(f"\n[1. 波动率特征]")
    print(f"平均价格: {avg_price:.4f}")
    print(f"平均 ATR (14): {avg_atr:.6f} (约 {avg_atr/avg_price*100:.3f}%)")
    print(f"单根 K 线平均振幅: {avg_range_pct:.3f}%")
    print(f"建议最小止损距离: > {avg_atr:.6f} (1 ATR)")
    print(f"建议剥头皮目标: {avg_atr * 1.5:.6f} - {avg_atr * 3:.6f}")

    # --- 趋势 vs 震荡 ---
    trend_bars = len(df[df['adx'] > 25])
    range_bars = len(df[df['adx'] <= 25])
    trend_ratio = trend_bars / len(df) * 100
    
    print(f"\n[2. 市场状态分布]")
    print(f"趋势状态 (ADX > 25): {trend_ratio:.1f}%")
    print(f"震荡状态 (ADX <= 25): {100 - trend_ratio:.1f}%")
    if trend_ratio > 40:
        print(">> 结论: 市场趋势性较强，适合顺势策略 (Trend Following)")
    else:
        print(">> 结论: 市场震荡为主，适合均值回归策略 (Mean Reversion)")

    # --- RSI 反转有效性测试 ---
    # 定义反转信号: RSI > 70 做空, RSI < 30 做多
    # 统计信号出现后 5根 K 线内的最大盈利
    
    # 寻找 RSI 超买点
    overbought = df[df['rsi'] > 70].index
    oversold = df[df['rsi'] < 30].index
    
    print(f"\n[3. RSI 极值反转测试 (持有 5分钟)]")
    
    if len(overbought) > 0:
        max_drops = []
        for idx in overbought:
            if idx + 5 < len(df):
                entry_price = df.loc[idx, 'close']
                min_price_next_5 = df.loc[idx+1:idx+5, 'low'].min()
                max_drop_pct = (entry_price - min_price_next_5) / entry_price * 100
                max_drops.append(max_drop_pct)
        avg_drop = np.mean(max_drops) if max_drops else 0
        print(f"RSI > 70 (超买) 出现次数: {len(overbought)}")
        print(f"-> 后5根K线平均最大回调: {avg_drop:.3f}%")
    
    if len(oversold) > 0:
        max_rises = []
        for idx in oversold:
            if idx + 5 < len(df):
                entry_price = df.loc[idx, 'close']
                max_price_next_5 = df.loc[idx+1:idx+5, 'high'].max()
                max_rise_pct = (max_price_next_5 - entry_price) / entry_price * 100
                max_rises.append(max_rise_pct)
        avg_rise = np.mean(max_rises) if max_rises else 0
        print(f"RSI < 30 (超卖) 出现次数: {len(oversold)}")
        print(f"-> 后5根K线平均最大反弹: {avg_rise:.3f}%")

    # --- 均线回归测试 ---
    # 统计价格偏离 EMA20 超过 0.5% 后的回归概率
    df['bias'] = (df['close'] - df['ema20']) / df['ema20']
    high_bias = df[df['bias'] > 0.005].index # 正乖离 > 0.5%
    low_bias = df[df['bias'] < -0.005].index # 负乖离 < -0.5%
    
    print(f"\n[4. 均线乖离回归测试 (乖离率 > 0.5%)]")
    print(f"正乖离 (>0.5%) 次数: {len(high_bias)}")
    print(f"负乖离 (<-0.5%) 次数: {len(low_bias)}")
    
    # 简单统计：乖离后 10 根 K 线内是否回到 EMA20
    revert_count = 0
    total_bias = list(high_bias) + list(low_bias)
    for idx in total_bias:
        if idx + 10 < len(df):
            # 检查未来 10 根是否有穿过 EMA20
            # 这里简化为检查价格是否触及当时的 EMA20 价格 (近似)
            target_price = df.loc[idx, 'ema20']
            future_prices = df.loc[idx+1:idx+10]
            if (df.loc[idx, 'close'] > target_price and future_prices['low'].min() <= target_price) or \
               (df.loc[idx, 'close'] < target_price and future_prices['high'].max() >= target_price):
                revert_count += 1
                
    if len(total_bias) > 0:
        revert_rate = revert_count / len(total_bias) * 100
        print(f"10分钟内回归均线概率: {revert_rate:.1f}%")
        if revert_rate > 70:
            print(">> 结论: 乖离率过大时回归概率极高，适合做反转")
    
    print("="*60)

if __name__ == "__main__":
    analyze_1m_data()
