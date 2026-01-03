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

        return df

    def check_signal(self, klines):
        if not klines or len(klines) < 200: # 需要更多数据计算 EMA200
            return 0, 0.0, 0.0, 50.0 # 默认 RSI 50
            
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
        ema_slow = curr['ema_slow']
        ema_fast = curr['ema_fast']
        k, d, j = curr['k'], curr['d'], curr['j']
        prev_k, prev_d = prev['k'], prev['d']
        hist = curr['hist']
        volume = curr['volume']
        vol_ma = curr['vol_ma']
        
        # 状态描述 (双均线金叉=多头，死叉=空头)
        trend_status = "多头" if ema_fast > ema_slow else "空头"
        kdj_status = f"K:{k:.1f}/D:{d:.1f}"
        macd_status = f"动能:{hist:.4f}"
        vol_status = "放量" if volume > vol_ma else "缩量"
        
        logger.info(f"价格:{current_price} | RSI:{current_rsi:.1f} | ATR:{current_atr:.4f} | EMA(20/120):{ema_fast:.4f}/{ema_slow:.4f}")
        logger.info(f"KDJ:{kdj_status} | MACD:{macd_status} | 趋势:{trend_status} | 量能:{vol_status}")
        
        # --- 策略逻辑 (双EMA趋势 + KDJ共振 + MACD动能 + 成交量确认) ---
        
        # 0. 波动率过滤
        if current_atr < (current_price * config.MIN_VOLATILITY):
            logger.info("波动率过低，不交易")
            return 0, current_atr, bb_middle, current_rsi

        # 1. 开多信号 (趋势跟随优化版):
        # - 趋势向上 (快线 > 慢线)
        # - KDJ 金叉 (K 上穿 D)
        # - 去除 K < 50 限制 (强趋势回调不深)
        # - 去除成交量限制 (缓慢上涨可能无量)
        if (ema_fast > ema_slow and 
            prev_k < prev_d and k > d and # KDJ 金叉
            k < 80 and # 只要不是在极高位金叉
            current_rsi < 75): # RSI 只要没严重超买
             logger.success(f"触发做多信号: 趋势金叉 (K={k:.1f})")
             return 1, current_atr, bb_middle, current_rsi

        # 2. 开空信号 (趋势跟随优化版):
        # - 趋势向下 (快线 < 慢线)
        # - KDJ 死叉 (K 下穿 D)
        if (ema_fast < ema_slow and 
            prev_k > prev_d and k < d and # KDJ 死叉
            k > 20 and 
            current_rsi > 25):
            logger.success(f"触发做空信号: 趋势死叉 (K={k:.1f})")
            return -1, current_atr, bb_middle, current_rsi
            
        return 0, current_atr, bb_middle, current_rsi

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
