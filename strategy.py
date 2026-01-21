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
        atr = tr.rolling(14).mean()
        
        plus_di = 100 * (plus_dm.ewm(alpha=1/14).mean() / atr)
        minus_di = 100 * (abs(minus_dm).ewm(alpha=1/14).mean() / atr)
        dx = (abs(plus_di - minus_di) / abs(plus_di + minus_di)) * 100
        df['adx'] = dx.rolling(14).mean()
        
        # 计算成交量均线 (Volume MA)
        df['vol_ma20'] = df['volume'].rolling(window=20).mean()
        
        return df

    def check_signal(self, df):
        if len(df) < 100:
            return 0, 0.0, 0.0, 50.0, 0.0, "数据不足"
        
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
        volume = curr['volume']
        vol_ma = curr['vol_ma20']
        
        # 市场状态判定
        trend_strength = adx
        is_trending = trend_strength > 20
        is_vol_valid = volume > vol_ma # 确认有成交量支持
        
        # 均线趋势定义
        trend_dir = "多头" if ema_fast > ema_slow else "空头"
        
        logger.info(f"价格:{current_price} | EMA20:{ema_fast:.4f} | RSI:{current_rsi:.1f} | ADX:{adx:.1f} | 趋势:{trend_dir}")
        
        # 0. 波动率过滤
        if current_atr < (current_price * config.MIN_VOLATILITY):
            logger.info("波动率过低，不交易")
            return 0, current_atr, bb_middle, current_rsi, adx, "低波"
        
        # --- 策略 V7: 5m 趋势回调策略 (只顺势，不逆势) ---
        
        # 如果没有趋势 (ADX < 20)，休息
        if not is_trending:
            logger.info(f"ADX过低 ({adx:.1f} < 20) -> 市场无序，休息观望")
            return 0, current_atr, bb_middle, current_rsi, adx, "震荡"

        # 1. 做多逻辑
        # 条件A: 长期多头 (EMA20 > EMA120)
        # 条件B: 价格回调到位 (KDJ金叉)
        # 条件C: 拒绝追高 (RSI < 60, 价格距离 EMA20 不超过 0.5%)
        # 条件D: 成交量确认 (Volume > 0.8 * MA20)
        # 条件E: 动能确认 (RSI > 45)
        if trend_dir == "多头":
            # 计算乖离率
            bias = (current_price - ema_fast) / ema_fast
            
            # 1. KDJ 金叉 (K 上穿 D)
            # 2. K < 60 (回调区域)
            # 3. 乖离率 < 0.5% (放宽至 0.5%)
            # 4. 成交量 > 0.8 * MA20
            if (prev['k'] < prev['d'] and k > d and k < 60 and bias < 0.005 and volume > 0.8 * vol_ma and current_rsi > 45):
                 logger.success(f"趋势多头 + 回调金叉 + 放量 (Bias {bias*100:.2f}%) -> 顺势低吸")
                 return 1, current_atr, bb_middle, current_rsi, adx, "趋势多"

        # 2. 做空逻辑
        # 条件A: 长期空头 (EMA20 < EMA120)
        # 条件B: 价格反弹到位
        # 条件C: 拒绝追低 (RSI > 40, 价格距离 EMA20 不超过 0.5%)
        # 条件D: 成交量确认
        # 条件E: 动能确认 (RSI < 55)
        elif trend_dir == "空头":
            bias = (ema_fast - current_price) / ema_fast
            
            # 1. KDJ 死叉 (K 下穿 D)
            # 2. K > 40 (反弹区域)
            # 3. 乖离率 < 0.5%
            # 4. 成交量 > 0.8 * MA20
            if (prev['k'] > prev['d'] and k < d and k > 40 and bias < 0.005 and volume > 0.8 * vol_ma and current_rsi < 55):
                logger.success(f"趋势空头 + 反弹死叉 + 放量 (Bias {bias*100:.2f}%) -> 顺势高空")
                return -1, current_atr, bb_middle, current_rsi, adx, "趋势空"
            
        return 0, current_atr, bb_middle, current_rsi, adx, "观察"

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
