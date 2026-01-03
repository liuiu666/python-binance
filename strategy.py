# 交易策略模块
from loguru import logger
import pandas as pd
import numpy as np

class Strategy:
    def __init__(self):
        pass

    def calculate_indicators(self, df):
        # 1. 计算 ATR (10) - 用于 SuperTrend
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        df['atr'] = true_range.rolling(10).mean() # 使用周期 10

        # 2. 计算 SuperTrend (10, 3) - 趋势追踪核心
        # 基础上下轨
        hl2 = (df['high'] + df['low']) / 2
        df['basic_upper'] = hl2 + (3 * df['atr'])
        df['basic_lower'] = hl2 - (3 * df['atr'])
        
        # 初始化最终上下轨
        df['final_upper'] = df['basic_upper']
        df['final_lower'] = df['basic_lower']
        
        # 迭代计算 SuperTrend
        # 逻辑：如果收盘价在下轨之上，下轨只能上移不能下移；反之亦然
        # 先找到第一个有效的 ATR 索引
        first_valid_idx = df['atr'].first_valid_index()
        if first_valid_idx is None:
            return df
            
        start_idx = df.index.get_loc(first_valid_idx)
        
        for i in range(start_idx + 1, len(df)):
            # Upper Band Logic
            if df['basic_upper'].iloc[i] < df['final_upper'].iloc[i-1] or \
               df['close'].iloc[i-1] > df['final_upper'].iloc[i-1]:
                df.at[df.index[i], 'final_upper'] = df['basic_upper'].iloc[i]
            else:
                df.at[df.index[i], 'final_upper'] = df['final_upper'].iloc[i-1]
            
            # Lower Band Logic
            if df['basic_lower'].iloc[i] > df['final_lower'].iloc[i-1] or \
               df['close'].iloc[i-1] < df['final_lower'].iloc[i-1]:
                df.at[df.index[i], 'final_lower'] = df['basic_lower'].iloc[i]
            else:
                df.at[df.index[i], 'final_lower'] = df['final_lower'].iloc[i-1]

        # 确定 SuperTrend 方向 (True=多头, False=空头)
        df['trend_dir'] = True 
        for i in range(start_idx + 1, len(df)):
            if df['trend_dir'].iloc[i-1] == True and df['close'].iloc[i] < df['final_lower'].iloc[i]:
                df.at[df.index[i], 'trend_dir'] = False
            elif df['trend_dir'].iloc[i-1] == False and df['close'].iloc[i] > df['final_upper'].iloc[i]:
                df.at[df.index[i], 'trend_dir'] = True
            else:
                df.at[df.index[i], 'trend_dir'] = df['trend_dir'].iloc[i-1]
        
        # 赋值 SuperTrend 具体数值
        df['supertrend'] = np.where(df['trend_dir'], df['final_lower'], df['final_upper'])
        
        return df

    def check_signal(self, klines):
        if not klines or len(klines) < 60:
            return 0, 0.0, 0.0
            
        # 转换为 DataFrame
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])
        
        # 转换数据类型
        df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
        
        # 计算指标
        df = self.calculate_indicators(df)
        
        # 获取最新数据
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        
        # 获取核心数据
        current_atr = curr['atr']
        st_value = curr['supertrend']
        is_bullish = curr['trend_dir']
        
        status = "看跌 (价格在趋势线下方)"
        if is_bullish:
            status = "看涨 (价格在趋势线上方)"

        logger.info(f"价格:{curr['close']} | SuperTrend:{st_value:.5f} | ATR:{current_atr:.4f}")
        logger.info(f"状态: {status}")
        
        # --- 策略逻辑 (SuperTrend 趋势追踪) ---
        
        # 1. 趋势反转开多: 之前是空头，现在变多头 (价格上穿 SuperTrend)
        if prev['trend_dir'] == False and curr['trend_dir'] == True:
             return 1, current_atr, st_value

        # 2. 趋势反转开空: 之前是多头，现在变空头 (价格下穿 SuperTrend)
        if prev['trend_dir'] == True and curr['trend_dir'] == False:
            return -1, current_atr, st_value
            
        # 3. 趋势延续 (不发开仓信号，但返回当前趋势状态供持仓管理)
        # 如果持仓方向与 SuperTrend 不符，应该平仓 (由 main.py 处理)
        
        return 0, current_atr, st_value
