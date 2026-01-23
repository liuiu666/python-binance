from handlers.llm_client import LLMClient
from handlers.binance_client import BinanceClient
import pandas as pd
import time

class AIStrategy:
    def __init__(self):
        self.llm = LLMClient()
        self.client = BinanceClient()

    def analyze(self, df, symbol):
        """
        使用 AI 分析市场数据并生成信号
        :param df: 包含技术指标的 DataFrame
        :param symbol: 交易对名称
        :return: (signal, info)
                 signal: 'BUY', 'SELL', or None
                 info: dict 包含 'reason', 'current_price' 等
        """
        if df is None or len(df) < 20:
            return None, {}

        # 1. 获取最新数据
        current = df.iloc[-1]
        prev = df.iloc[-2]
        
        # 2.5 提取最近 K 线序列 (用于 AI 识别形态)
        # 取最近 12 根 K 线
        recent_klines = []
        subset = df.tail(12)
        for idx, row in subset.iterrows():
            k_str = f"Time: {row.name}, O: {row['开盘价']}, H: {row['最高价']}, L: {row['最低价']}, C: {row['收盘价']}, V: {row['成交量']}"
            recent_klines.append(k_str)

        # 2.6 获取合约数据 (资金费率 & 持仓量 & 资金流向)
        funding_rate = self.client.get_funding_rate(symbol)
        open_interest = self.client.get_open_interest(symbol)
        money_flow = self.client.get_money_flow(symbol, period='15m') # 获取15分钟级别的资金流向

        # 3. 构建数据包
        market_data = {
            "symbol": symbol,
            "funding_rate": funding_rate,
            "open_interest": open_interest,
            "money_flow": money_flow, # 新增
            "current_price": current['收盘价'],
            "change_pct": (current['收盘价'] - current['开盘价']) / current['开盘价'] * 100,
            "atr": current.get('ATR', 0),
            "rsi": current.get('RSI', 50),
            "ma5": current.get('MA5', 0),
            "ma20": current.get('MA20', 0),
            "macd": current.get('MACD', 0),
            "macd_hist": current.get('MACD_HIST', 0),
            "boll_upper": current.get('BOLL_UPPER', 0),
            "boll_mid": current.get('BOLL_MID', 0),
            "boll_lower": current.get('BOLL_LOWER', 0),
            "ma_status": "Bullish" if current.get('MA5', 0) > current.get('MA20', 0) else "Bearish",
            "recent_klines": recent_klines
        }

        print(f">>> [AI Strategy] 正在请求 AI 分析 {symbol} ...")
        
        # 4. 调用 LLM
        llm_result, error = self.llm.get_trading_advice(market_data)
        
        signal_raw = None
        reason = "AI 分析失败"
        confidence = 0
        
        if llm_result:
             signal_raw = llm_result.get('signal')
             reason = llm_result.get('reason', '无理由')
             confidence = llm_result.get('confidence', 0)
        elif error:
             reason = error
        
        # 5. 解析信号
        signal = None
        if signal_raw:
            signal_raw = str(signal_raw).upper()
            if signal_raw in ['BUY', 'SELL']:
                # [新增] 信心分数过滤 (避免频繁操作)
                if confidence >= 75:
                    signal = signal_raw
                else:
                    reason = f"信心不足 ({confidence} < 75)，忽略 {signal_raw} 信号"
                    signal_raw = None # 标记为 None
            
        info = {
            "reason": reason,
            "confidence": confidence,
            "current_price": current['收盘价'],
            "atr": current.get('ATR', 0)
        }
        
        # 6. 计算止盈止损 (使用 LLM 返回的建议值，如果 LLM 未返回则使用默认 ATR 逻辑兜底)
        if signal:
            atr = current.get('ATR', 0)
            price = current['收盘价']
            
            # 优先使用 LLM 建议的止盈止损
            llm_sl = llm_result.get('stop_loss')
            llm_tp = llm_result.get('take_profit')
            
            if llm_sl and llm_tp:
                info['stop_loss'] = float(llm_sl)
                info['take_profit'] = float(llm_tp)
                print(f"   [AI 建议] 使用 AI 提供的风控点位: SL {llm_sl}, TP {llm_tp}")
            elif atr > 0:
                # [兜底] 针对小币种/高波动币种，扩大止盈止损范围
                if signal == 'BUY':
                    # 止损 3.0 ATR (原 2.0)，止盈 5.0 ATR (原 3.0)
                    info['stop_loss'] = price - (3.0 * atr)
                    info['take_profit'] = price + (5.0 * atr)
                elif signal == 'SELL':
                    info['stop_loss'] = price + (3.0 * atr)
                    info['take_profit'] = price - (5.0 * atr)
                print(f"   [AI 未提供点位] 使用 ATR 兜底策略: SL {info['stop_loss']:.4f}, TP {info['take_profit']:.4f}")
            else:
                # [兜底] 如果没有 ATR，使用固定百分比 (针对小币种扩大波动容忍)
                # 止损 3%，止盈 6%
                if signal == 'BUY':
                    info['stop_loss'] = price * 0.97
                    info['take_profit'] = price * 1.06
                elif signal == 'SELL':
                    info['stop_loss'] = price * 1.03
                    info['take_profit'] = price * 0.94
                print(f"   [AI 未提供点位且无 ATR] 使用固定百分比兜底: SL {info['stop_loss']:.4f}, TP {info['take_profit']:.4f}")
        
        print("-" * 50)
        print(f"   [AI 分析结果] {symbol}")
        print(f"   方向: {signal if signal else 'HOLD/WAIT'}")
        print(f"   置信度: {info.get('confidence', 0)}%")
        print(f"   分析理由: {reason}")
        
        if signal and 'stop_loss' in info:
            print(f"   建议风控: SL {info['stop_loss']:.4f}, TP {info['take_profit']:.4f}")
        print("-" * 50)

        return signal, info

    def audit_position(self, position, df):
        """
        评估当前持仓
        :param position: 持仓字典
        :param df: K线数据
        :return: (action, info) action: 'HOLD' or 'CLOSE'
        """
        if df is None or len(df) < 5:
            return 'HOLD', {}
            
        # [新增] 最小持仓时间保护 (15分钟)
        # 避免开仓后因短期波动立即平仓
        current_ts = int(time.time() * 1000)
        last_update = position.get('update_time', 0)
        
        # 如果获取不到 update_time (比如旧代码兼容)，则默认不保护
        if last_update > 0:
            holding_ms = current_ts - last_update
            # 15分钟 = 900000 毫秒
            if holding_ms < 900000:
                print(f">>> [持仓保护] {position['symbol']} 持仓时间不足 15 分钟 ({holding_ms/1000/60:.1f} min)，跳过 AI 评估")
                return 'HOLD', {"reason": "持仓保护期", "confidence": 100}

        current = df.iloc[-1]
        
        market_data = {
            "current_price": current['收盘价'],
            "atr": current.get('ATR', 0),
            "rsi": current.get('RSI', 50),
            "ma_status": "Bullish" if current.get('MA5', 0) > current.get('MA20', 0) else "Bearish",
        }
        
        print(f">>> [AI Strategy] 正在评估持仓 {position['symbol']} ...")
        
        llm_result, error = self.llm.get_position_audit(position, market_data)
        
        action = 'HOLD'
        reason = "AI 评估失败"
        confidence = 0
        
        if llm_result:
            action_raw = str(llm_result.get('action')).upper()
            confidence = llm_result.get('confidence', 0)
            
            if action_raw in ['HOLD', 'CLOSE']:
                # [新增] 平仓也需要一定信心，否则保持持有 (让利润奔跑)
                if action_raw == 'CLOSE' and confidence < 60:
                     action = 'HOLD'
                     reason = f"AI 建议平仓但信心不足 ({confidence} < 60) -> 保持持有"
                else:
                    action = action_raw
                    reason = llm_result.get('reason', '无理由')
            else:
                reason = llm_result.get('reason', '无理由')
            
        print("-" * 50)
        print(f"   [持仓评估] {position['symbol']}")
        print(f"   建议操作: {action} (信心: {confidence})")
        print(f"   评估理由: {reason}")
        print("-" * 50)
        
        # 将 confidence 放入 info 字典返回
        info = {
            "reason": reason,
            "confidence": confidence
        }
        
        return action, info
