# 交易策略模块
from loguru import logger
import pandas as pd
import numpy as np
import config

class Strategy:
    def __init__(self):
        pass

    def calculate_indicators(self, df):
        # 1. 计算 ATR (config.ATR_PERIOD)
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        df['atr'] = true_range.rolling(config.ATR_PERIOD).mean()

        # 2. 计算 RSI (config.RSI_PERIOD)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        avg_gain = gain.rolling(window=config.RSI_PERIOD, min_periods=1).mean()
        avg_loss = loss.rolling(window=config.RSI_PERIOD, min_periods=1).mean()
        rs = avg_gain / avg_loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # 3. 计算布林带 (config.BB_PERIOD, config.BB_STD)
        df['bb_middle'] = df['close'].rolling(window=config.BB_PERIOD).mean()
        df['bb_std'] = df['close'].rolling(window=config.BB_PERIOD).std()
        df['bb_upper'] = df['bb_middle'] + (config.BB_STD * df['bb_std'])
        df['bb_lower'] = df['bb_middle'] - (config.BB_STD * df['bb_std'])

        # 4. 计算 EMA (双均线系统)
        df['ema_slow'] = df['close'].ewm(span=config.EMA_SLOW_PERIOD, adjust=False).mean()
        df['ema_fast'] = df['close'].ewm(span=config.EMA_FAST_PERIOD, adjust=False).mean()

        # 5. 计算 KDJ (9, 3, 3)
        low_min = df['low'].rolling(window=9).min()
        high_max = df['high'].rolling(window=9).max()
        rsv = (df['close'] - low_min) / (high_max - low_min) * 100
        # 初始化 K, D
        df['k'] = 50.0
        df['d'] = 50.0
        
        # 迭代计算 K, D, J
        k_values = [50.0]
        d_values = [50.0]
        
        for i in range(1, len(df)):
            curr_rsv = rsv.iloc[i] if not np.isnan(rsv.iloc[i]) else 50.0
            new_k = (2/3) * k_values[-1] + (1/3) * curr_rsv
            new_d = (2/3) * d_values[-1] + (1/3) * new_k
            k_values.append(new_k)
            d_values.append(new_d)
            
        df['k'] = k_values
        df['d'] = d_values
        df['j'] = 3 * df['k'] - 2 * df['d']

        # 6. 计算 MACD (12, 26, 9)
        exp12 = df['close'].ewm(span=12, adjust=False).mean()
        exp26 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp12 - exp26
        df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['hist'] = df['macd'] - df['signal']

        # 7. 计算成交量均线 (Volume MA 20)
        df['vol_ma'] = df['volume'].rolling(window=20).mean()

        # 8. 计算 ADX (14)
        plus_dm = df['high'].diff()
        minus_dm = df['low'].diff()
        plus_dm[plus_dm < 0] = 0
        minus_dm[minus_dm > 0] = 0
        
        tr1 = pd.DataFrame(high_low)
        tr2 = pd.DataFrame(high_close)
        tr3 = pd.DataFrame(low_close)
        frames = [tr1, tr2, tr3]
        tr = pd.concat(frames, axis=1, join='inner').max(axis=1)
        atr = tr.rolling(config.ADX_PERIOD).mean()
        
        plus_di = 100 * (plus_dm.ewm(alpha=1/config.ADX_PERIOD).mean() / atr)
        minus_di = 100 * (abs(minus_dm).ewm(alpha=1/config.ADX_PERIOD).mean() / atr)
        dx = (abs(plus_di - minus_di) / abs(plus_di + minus_di)) * 100
        df['adx'] = dx.rolling(config.ADX_PERIOD).mean()

        return df

    def check_signal(self, klines):
        if not klines or len(klines) < 200: # 需要更多数据计算 EMA200
            return 0, 0.0, 0.0, 50.0, 0.0, "未知" # 增加 ADX 和 Mode 返回
            
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
        prev = df.iloc[-2] # 前一根 K 线用于判断交叉
        
        # 获取核心数据
        current_price = curr['close']
        current_atr = curr['atr']
        current_rsi = curr['rsi']
        bb_middle = curr['bb_middle']
        bb_upper = curr['bb_upper']
        bb_lower = curr['bb_lower']
        ema_slow = curr['ema_slow']
        ema_fast = curr['ema_fast']
        k, d = curr['k'], curr['d']
        adx = curr['adx']
        
        # 市场状态判定
        market_mode = "趋势" if adx > config.ADX_THRESHOLD else "震荡"
        trend_dir = "多头" if ema_fast > ema_slow else "空头"
        
        logger.info(f"价格:{current_price} | RSI:{current_rsi:.1f} | ADX:{adx:.1f}({market_mode}) | 趋势:{trend_dir}")
        
        # 0. 波动率过滤
        if current_atr < (current_price * config.MIN_VOLATILITY):
            logger.info("波动率过低，不交易")
            return 0, current_atr, bb_middle, current_rsi, adx, market_mode

        # --- 策略分支 ---
        
        # 分支 A: 震荡市场 (ADX < 25) -> 布林带回归策略
        if market_mode == "震荡":
            # 震荡做多: 跌破下轨 + RSI超卖 (<30)
            if current_price < bb_lower and current_rsi < 30:
                logger.success(f"震荡触底 (RSI {current_rsi:.1f}) -> 逆势开多")
                return 1, current_atr, bb_middle, current_rsi, adx, market_mode
            
            # 震荡做空: 突破上轨 + RSI超买 (>70)
            if current_price > bb_upper and current_rsi > 70:
                logger.success(f"震荡触顶 (RSI {current_rsi:.1f}) -> 逆势开空")
                return -1, current_atr, bb_middle, current_rsi, adx, market_mode

        # 分支 B: 趋势市场 (ADX >= 25) -> 双均线顺势策略
        else:
            # 趋势做多: 多头排列 + KDJ金叉 + 并非严重超买
            if (ema_fast > ema_slow and 
                prev['k'] < prev['d'] and k > d and 
                current_rsi < 70):
                 logger.success(f"趋势金叉 (ADX {adx:.1f}) -> 顺势开多")
                 return 1, current_atr, bb_middle, current_rsi, adx, market_mode

            # 趋势做空: 空头排列 + KDJ死叉 + 并非严重超卖
            if (ema_fast < ema_slow and 
                prev['k'] > prev['d'] and k < d and 
                current_rsi > 30):
                logger.success(f"趋势死叉 (ADX {adx:.1f}) -> 顺势开空")
                return -1, current_atr, bb_middle, current_rsi, adx, market_mode
            
        return 0, current_atr, bb_middle, current_rsi, adx, market_mode

    def analyze_orderbook(self, depth):
        if not depth:
            return 0, 0.0

        bids = depth['bids']
        asks = depth['asks']

        # 计算买卖盘总量 (前 N 档)
        bid_vol = sum([float(x[1]) for x in bids])
        ask_vol = sum([float(x[1]) for x in asks])

        # 避免除以零
        if ask_vol == 0: ask_vol = 0.0001
        
        # 计算失衡比例
        imbalance_ratio = bid_vol / ask_vol
        
        logger.info(f"盘口分析 | 买量:{bid_vol:.2f} | 卖量:{ask_vol:.2f} | 多空比:{imbalance_ratio:.2f}")

        # 信号判断
        if imbalance_ratio > config.IMBALANCE_THRESHOLD:
            return 1, imbalance_ratio # 强买入信号
        elif imbalance_ratio < (1 / config.IMBALANCE_THRESHOLD):
            return -1, imbalance_ratio # 强卖出信号
            
        return 0, imbalance_ratio
