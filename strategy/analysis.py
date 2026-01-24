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
            
        # 数据长度检查：如果数据量太少，无法计算指标，直接返回 None
        # ATR 需要 14，MA200 需要 200 (虽然 MA 可以是 NaN，但太少的数据计算无意义且可能导致库报错)
        if len(df) < 50:
            return None
        
        # 复制一份以免修改原始数据
        data = df.copy()
        
        # 计算移动平均线 (MA)
        data['MA5'] = ta.trend.sma_indicator(data['收盘价'], window=5)
        data['MA20'] = ta.trend.sma_indicator(data['收盘价'], window=20)
        data['MA50'] = ta.trend.sma_indicator(data['收盘价'], window=50)
        data['MA200'] = ta.trend.sma_indicator(data['收盘价'], window=200)
        
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
        
        # 计算 ADX (用于判断趋势强度)
        adx = ta.trend.ADXIndicator(data['最高价'], data['最低价'], data['收盘价'], window=14)
        data['ADX'] = adx.adx()
        data['ADX_POS'] = adx.adx_pos()
        data['ADX_NEG'] = adx.adx_neg()

        # --- 资金流向分析 (新增) ---
        if '主动买入成交量' in data.columns:
            # 1. 资金净流入 (Taker Buy - Taker Sell)
            # 总成交量 = 主动买入 + 主动卖出
            # 主动卖出 = 总成交量 - 主动买入
            # 净买入 = 主动买入 - 主动卖出 = 2 * 主动买入 - 总成交量
            data['Net_Volume'] = 2 * data['主动买入成交量'] - data['成交量']
            data['Net_Flow_MA5'] = ta.trend.sma_indicator(data['Net_Volume'], window=5)
            
            # 2. 资金流入比例 (主动买入 / 总成交量)
            data['Buy_Ratio'] = data['主动买入成交量'] / data['成交量']
        
        # 3. MFI (资金流量指标)
        mfi = ta.volume.MFIIndicator(high=data['最高价'], low=data['最低价'], close=data['收盘价'], volume=data['成交量'], window=14)
        data['MFI'] = mfi.money_flow_index()
        
        # 4. CMF (蔡金资金流向)
        cmf = ta.volume.ChaikinMoneyFlowIndicator(high=data['最高价'], low=data['最低价'], close=data['收盘价'], volume=data['成交量'], window=20)
        data['CMF'] = cmf.chaikin_money_flow()

        return data

    def get_trend_bias(self, df):
        if df is None or len(df) < 50:
            return None
        last = df.iloc[-1]
        prev = df.iloc[-2]
        ma20 = float(last.get('MA20', 0) or 0)
        ma50 = float(last.get('MA50', 0) or 0)
        ma200 = float(last.get('MA200', 0) or 0)
        ma20_prev = float(prev.get('MA20', 0) or 0)
        price = float(last.get('收盘价', 0) or 0)
        if price <= 0:
            return None
            
        # [优化] 放宽趋势判断：不再死板要求 MA50 > MA200
        # 只要 MA20 > MA50 (短期金叉) 或者 价格强势站上 MA50，就允许做多
        # 这样对于刚启动的行情也能捕捉到
        if (ma20 > ma50 and ma50 > 0) or (price > ma50 and ma50 > 0):
            return 'BUY_ONLY'
        if (ma20 < ma50 and ma50 > 0) or (price < ma50 and ma50 > 0):
            return 'SELL_ONLY'
            
        # 极简模式：如果均线粘合，直接看价格和 MA20 关系
        if price > ma20:
            return 'BUY_ONLY'
        if price < ma20:
            return 'SELL_ONLY'
            
        return None

    def suggest_leverage(self, df):
        """
        根据 ATR 波动率动态建议杠杆
        :param df: K线数据 (建议使用 1m 数据)
        :return: 建议的杠杆倍数 (int)
        """
        if df is None or len(df) < 20:
            return 10
        
        last = df.iloc[-1]
        price = float(last.get('收盘价', 0) or 0)
        atr = float(last.get('ATR', 0) or 0)
        
        if price <= 0:
            return 10
        
        # 计算波动率百分比 (ATR / Price)
        atr_pct = (atr / price) * 100
        
        # 动态杠杆逻辑 (激进模式 - Plus)
        # 波动率极高 (>0.5%) -> 10x
        # 波动率高 (>0.3%)   -> 20x
        # 波动率中等 (>0.1%) -> 35x
        # 波动率低 (<=0.1%)  -> 50x
        
        if atr_pct > 0.5:
            return 10
        elif atr_pct > 0.3:
            return 20
        elif atr_pct > 0.1:
            return 35
        else:
            return 50

    def check_trend_following(self, df, trend_bias=None, volume_ratio=0.8, check_money_flow=True):
        if df is None or len(df) < 50:
            return None, {}
        last = df.iloc[-1]
        prev = df.iloc[-2]
        ma20 = float(last.get('MA20', 0) or 0)
        ma50 = float(last.get('MA50', 0) or 0)
        price = float(last.get('收盘价', 0) or 0)
        atr = float(last.get('ATR', 0) or 0)
        vol = float(last.get('成交量', 0) or 0)
        vol_ma20 = float(last.get('VOL_MA20', 0) or 0)
        adx = float(last.get('ADX', 0) or 0)
        
        # 资金流向数据
        net_flow = float(last.get('Net_Flow_MA5', 0) or 0)
        cmf = float(last.get('CMF', 0) or 0)
        mfi = float(last.get('MFI', 50) or 50)
        
        if price <= 0:
            return None, {}
            
        # [优化1] ADX 过滤：如果 ADX < 20，说明是震荡行情，不建议趋势操作
        if adx < 20:
            return None, {}

        # [放宽] 不再强制要求放量，只要不极度缩量即可
        # if vol_ma20 > 0 and vol < vol_ma20 * volume_ratio:
        #    return None, {}

        # [优化] 趋势判定放宽
        # 不再要求 ma50 > ma200，只要短期 ma20 > ma50 或价格强势
        buy_trend = ma20 > ma50 or (price > ma20 and price > ma50)
        sell_trend = ma20 < ma50 or (price < ma20 and price < ma50)
        
        signal = None
        reason = ""
        
        if trend_bias == 'BUY_ONLY' or trend_bias is None:
            # 只要价格站稳 MA20 即可尝试做多
            if buy_trend and price > ma20:
                signal = 'BUY'
                reason = "价格站上 MA20，短期趋势看多"
                
        if signal is None and (trend_bias == 'SELL_ONLY' or trend_bias is None):
            if sell_trend and price < ma20:
                signal = 'SELL'
                reason = "价格跌破 MA20，短期趋势看空"
        
        # [资金综合分析]
        if signal and check_money_flow:
            if signal == 'BUY':
                # 稍微放宽资金流要求：只要不是严重流出 (CMF < -0.1) 即可
                if cmf < -0.1:
                    signal = None
                    reason = f"趋势虽好但资金严重流出 (CMF: {cmf:.2f})"
                # [优化2] MFI 过滤：避免超买 (MFI > 85)
                elif mfi > 85:
                    signal = None
                    reason = f"MFI 超买 ({mfi:.1f})，谨防回调"
                else:
                    reason += f" (资金面 CMF: {cmf:.2f}, ADX: {adx:.1f})"
                    
            elif signal == 'SELL':
                if cmf > 0.1:
                    signal = None
                    reason = f"趋势虽差但资金严重流入 (CMF: {cmf:.2f})"
                # [优化2] MFI 过滤：避免超卖 (MFI < 15)
                elif mfi < 15:
                    signal = None
                    reason = f"MFI 超卖 ({mfi:.1f})，谨防反弹"
                else:
                    reason += f" (资金面 CMF: {cmf:.2f}, ADX: {adx:.1f})"

        if not signal:
            return None, {}
        
        stop_loss = 0
        take_profit = 0
        
        # [优化] 动态调整止损价格，放宽止损以适应高波动
        if atr > 0:
            atr_multiplier_sl = 3.0 # 从 2.2 放宽到 3.0
            atr_multiplier_tp = 5.0 # 保持盈亏比
            
            if signal == 'BUY':
                stop_loss = price - (atr_multiplier_sl * atr)
                take_profit = price + (atr_multiplier_tp * atr)
            else:
                stop_loss = price + (atr_multiplier_sl * atr)
                take_profit = price - (atr_multiplier_tp * atr)
        else:
            # 兜底百分比也放宽
            if signal == 'BUY':
                stop_loss = price * 0.96 # 放宽到 4%
                take_profit = price * 1.08
            else:
                stop_loss = price * 1.04
                take_profit = price * 0.92
                
        return signal, {
            'reason': reason,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'current_price': price,
            'atr': atr
        }

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
