import pandas as pd
import ta

class MarketAnalyzer:
    def __init__(self):
        pass

    def calculate_indicators(self, df):
        """
        计算技术指标
        :param df: 包含 K 线数据的 DataFrame
        :return: 包含指标的 DataFrame
        """
        if df is None or df.empty:
            return None
        
        # 复制一份以免修改原始数据
        data = df.copy()
        
        # 计算移动平均线 (MA)
        data['MA5'] = ta.trend.sma_indicator(data['收盘价'], window=5)
        data['MA20'] = ta.trend.sma_indicator(data['收盘价'], window=20)
        
        # 计算 RSI
        data['RSI'] = ta.momentum.rsi(data['收盘价'], window=14)
        
        # 计算 MACD
        macd = ta.trend.MACD(data['收盘价'])
        data['MACD'] = macd.macd()
        data['MACD_SIGNAL'] = macd.macd_signal()
        data['MACD_HIST'] = macd.macd_diff()
        
        # 计算 ATR (用于止损和衡量波动)
        data['ATR'] = ta.volatility.average_true_range(data['最高价'], data['最低价'], data['收盘价'], window=14)
        
        # 计算成交量均线 (用于判断放量)
        data['VOL_MA20'] = ta.trend.sma_indicator(data['成交量'], window=20)

        # 计算布林带 (Bollinger Bands)
        boll = ta.volatility.BollingerBands(close=data['收盘价'], window=20, window_dev=2)
        data['BOLL_UPPER'] = boll.bollinger_hband()
        data['BOLL_LOWER'] = boll.bollinger_lband()
        data['BOLL_MID'] = boll.bollinger_mavg()
        
        return data

    def check_breakout_strategy(self, df, lookback=20, vol_multiplier=1.5):
        """
        量价突破策略 (专为波动币设计)
        逻辑: 
        1. 价格突破过去 N 周期高点/低点 (Price Action)
        2. 成交量明显放大 (过滤假突破)
        :return: (Signal, Info_Dict)
        """
        if df is None or len(df) < lookback:
            return None, {}
            
        curr = df.iloc[-1]
        
        # 获取过去 N 根 K 线的最高价/最低价 (不包含当前 K 线)
        past_window = df.iloc[-lookback-1:-1]
        recent_high = past_window['最高价'].max()
        recent_low = past_window['最低价'].min()
        
        # 信号判断
        signal = None
        reason = ""
        
        # 1. 检查是否放量
        is_high_volume = curr['成交量'] > (curr['VOL_MA20'] * vol_multiplier)
        
        # 2. 检查突破
        # 向上突破：收盘价 > 过去高点 且 放量
        if curr['收盘价'] > recent_high:
            if is_high_volume:
                signal = 'BUY'
                reason = f"突破{lookback}周期高点({recent_high:.4f}) + 放量({curr['成交量']:.0f} > {curr['VOL_MA20']:.0f})"
            else:
                reason = "价格突破但成交量不足 (疑似假突破)"
                
        # 向下突破：收盘价 < 过去低点 且 放量
        elif curr['收盘价'] < recent_low:
            if is_high_volume:
                signal = 'SELL'
                reason = f"跌破{lookback}周期低点({recent_low:.4f}) + 放量"
            else:
                reason = "价格跌破但成交量不足"
        
        # 计算建议止损位 (基于 ATR)
        atr = curr['ATR']
        stop_loss = 0
        take_profit = 0
        
        if signal == 'BUY':
            stop_loss = curr['收盘价'] - (2 * atr) # 2倍 ATR 止损
            take_profit = curr['收盘价'] + (3 * atr) # 3倍 ATR 止盈 (盈亏比 1.5)
        elif signal == 'SELL':
            stop_loss = curr['收盘价'] + (2 * atr)
            take_profit = curr['收盘价'] - (3 * atr)
            
        return signal, {
            'reason': reason,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'current_price': curr['收盘价'],
            'atr': atr
        }

    def check_signal(self, df):
        """
        检查交易信号 (简单示例：MA 金叉/死叉)
        :param df: 包含指标的 DataFrame
        :return: 'BUY', 'SELL' 或 None
        """
        if df is None or len(df) < 2:
            return None
            
        # 获取最后两行数据
        last_row = df.iloc[-1]
        prev_row = df.iloc[-2]
        
        # 简单的 MA 策略
        # 金叉：短期 MA 上穿长期 MA
        if prev_row['MA5'] <= prev_row['MA20'] and last_row['MA5'] > last_row['MA20']:
            return 'BUY'
            
        # 死叉：短期 MA 下穿长期 MA
        if prev_row['MA5'] >= prev_row['MA20'] and last_row['MA5'] < last_row['MA20']:
            return 'SELL'
            
        return None

    def analyze_money_flow(self, flow_data):
        """
        分析资金流向
        :param flow_data: get_money_flow 返回的字典
        :return: 分析结果字符串
        """
        if not flow_data:
            return "无资金流向数据"
            
        net_inflow = flow_data['净流入量']
        ratio = flow_data['买卖比']
        
        analysis = f"周期 [{flow_data['周期']}] 分析:\n"
        analysis += f"- 主动买入: {flow_data['主动买入量']:.2f}\n"
        analysis += f"- 主动卖出: {flow_data['主动卖出量']:.2f}\n"
        analysis += f"- 净流入: {net_inflow:.2f}\n"
        analysis += f"- 买卖比: {ratio:.4f}\n"
        
        if net_inflow > 0 and ratio > 1.1:
            analysis += "=> 结论: 资金明显净流入，多头情绪占优"
        elif net_inflow < 0 and ratio < 0.9:
            analysis += "=> 结论: 资金明显净流出，空头情绪占优"
        else:
            analysis += "=> 结论: 资金博弈激烈，方向不明确"
            
        return analysis

    def scan_volatile_coins(self, tickers, min_volume=10000000, top_n=10):
        """
        筛选波动最大的币种
        :param tickers: 所有交易对的 24h 数据
        :param min_volume: 最小成交额 (USDT)，默认 1000万，过滤掉流动性太差的
        :param top_n: 返回前 N 个
        :return: 筛选后的 DataFrame
        """
        if not tickers:
            return None
            
        import pandas as pd
        
        # 转换为 DataFrame
        df = pd.DataFrame(tickers)
        
        # 筛选 USDT 合约
        df = df[df['symbol'].str.endswith('USDT')]
        
        # 转换数据类型
        numeric_cols = ['lastPrice', 'priceChangePercent', 'highPrice', 'lowPrice', 'quoteVolume']
        df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric)
        
        # 计算振幅 (最高 - 最低) / 最低
        # 注意：priceChangePercent 是涨跌幅，不是振幅。振幅更能体现波动性。
        df['amplitude'] = (df['highPrice'] - df['lowPrice']) / df['lowPrice'] * 100
        
        # 过滤流动性 (成交额 > min_volume)
        df = df[df['quoteVolume'] > min_volume]
        
        # 排除一些非主流或不想交易的币 (可选，例如 USDC)
        df = df[~df['symbol'].str.contains('USDC')]
        
        # 按振幅排序 (降序)
        df_sorted = df.sort_values(by='amplitude', ascending=False)
        
        # 选取前 N 个
        result = df_sorted.head(top_n)[['symbol', 'lastPrice', 'priceChangePercent', 'amplitude', 'quoteVolume']]
        
        # 重命名列以便展示
        result.columns = ['交易对', '当前价', '24h涨跌幅%', '24h振幅%', '24h成交额']
        
        return result


