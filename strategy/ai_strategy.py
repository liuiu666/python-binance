from handlers.llm_client import LLMClient
from handlers.binance_client import BinanceClient
import time

class AIStrategy:
    def __init__(self, client=None):
        self.llm = LLMClient()
        self.client = client or BinanceClient()

    def analyze(self, df, symbol, trend_bias=None, df_larger=None):
        """
        使用大模型分析市场数据并生成信号
        :param df: 包含技术指标的 DataFrame (1m)
        :param symbol: 交易对名称
        :param trend_bias: 趋势偏向
        :param df_larger: 更大周期的 DataFrame (如 5m 或 15m)
        :return: (signal, info)
        """
        if df is None or len(df) < 20:
            return None, {}

        # 1. 获取最新数据 (1m)
        current = df.iloc[-1]
        
        # 获取大周期数据 (5m/15m) 作为参考
        larger_info = {}
        if df_larger is not None and not df_larger.empty:
            curr_large = df_larger.iloc[-1]
            larger_info = {
                "ma5": float(curr_large.get('MA5', 0) or 0),
                "ma20": float(curr_large.get('MA20', 0) or 0),
                "rsi": float(curr_large.get('RSI', 50) or 50),
                "trend": "看多" if float(curr_large.get('MA5', 0) or 0) > float(curr_large.get('MA20', 0) or 0) else "看空"
            }

        price = float(current['收盘价'])
        atr = float(current.get('ATR', 0) or 0)
        atr_pct = (atr / price) if price > 0 else 0
        rsi = float(current.get('RSI', 50) or 50)
        ma5 = float(current.get('MA5', 0) or 0)
        ma20 = float(current.get('MA20', 0) or 0)

        if price <= 0:
            return None, {}

        if atr > 0 and atr_pct < 0.003:
            return None, {
                "reason": f"波动不足（波动率占比 {atr_pct*100:.2f}%），跳过",
                "confidence": 0,
                "current_price": price,
                "atr": atr
            }

        if ma5 > 0 and ma20 > 0:
            ma_gap = abs(ma5 - ma20) / price
            if ma_gap < 0.0006:
                return None, {
                    "reason": "趋势不清晰，跳过",
                    "confidence": 0,
                    "current_price": price,
                    "atr": atr
                }
        
        # 2.5 提取最近 K 线序列（用于形态识别）
        recent_klines = []
        # [优化] 增加 K 线数量到 60 (约1小时数据)，以便 AI 识别更有效的支撑压力位
        subset = df.tail(60)
        for idx, row in subset.iterrows():
            k_str = f"时间: {row.name}, 开: {row['开盘价']}, 高: {row['最高价']}, 低: {row['最低价']}, 收: {row['收盘价']}, 量: {row['成交量']}"
            recent_klines.append(k_str)

        vol_10m = 0
        vol_prev_10m = 0
        vol_10m_avg = 0
        price_10m_change_pct = 0
        if len(df) >= 10:
            vol_10m = float(df.tail(10)['成交量'].sum())
            base_price_10m = float(df.iloc[-10]['收盘价'] or 0)
            if base_price_10m > 0:
                price_10m_change_pct = (price - base_price_10m) / base_price_10m * 100
        if len(df) >= 20:
            vol_prev_10m = float(df.iloc[-20:-10]['成交量'].sum())
        if len(df) >= 30:
            vol_10m_avg = float(df.tail(30)['成交量'].sum() / 3.0)

        # 2.6 获取合约数据（资金费率、持仓量、资金流向）
        funding_rate = self.client.get_funding_rate(symbol)
        open_interest = self.client.get_open_interest(symbol)
        money_flow = self.client.get_money_flow(symbol, period='5m')

        try:
            if money_flow:
                buy_vol = float(money_flow.get('主动买入量', 0) or 0)
                sell_vol = float(money_flow.get('主动卖出量', 0) or 0)
                net_inflow = float(money_flow.get('净流入量', 0) or 0)
                buy_sell_ratio = float(money_flow.get('买卖比', 1) or 1)
                total = buy_vol + sell_vol
                if total > 0:
                    if abs(buy_sell_ratio - 1) < 0.03 and abs(net_inflow) < (total * 0.02):
                        return None, {
                            "reason": "资金流向不明确，跳过",
                            "confidence": 0,
                            "current_price": price,
                            "atr": atr
                        }
        except Exception:
            pass

        try:
            fr_val = float(funding_rate or 0)
            fr_desc = "正常"
            if fr_val > 0.001: fr_desc = "费率偏高(多头拥挤)"
            if fr_val < -0.001: fr_desc = "费率偏低(空头拥挤)"

            if abs(fr_val) > 0.001 and ma5 > 0 and ma20 > 0:
                ma_gap = abs(ma5 - ma20) / price
                if ma_gap < 0.001:
                    return None, {
                        "reason": "资金费率偏极端且趋势不清晰，跳过",
                        "confidence": 0,
                        "current_price": price,
                        "atr": atr
                    }
        except Exception:
            fr_val = 0
            fr_desc = "未知"
            
        # 提取资金流指标
        cmf = float(current.get('CMF', 0) or 0)
        net_flow_ma = float(current.get('Net_Flow_MA5', 0) or 0)

        # 3. 构建数据包
        vol_ratio = 0
        if vol_prev_10m > 0:
            vol_ratio = vol_10m / vol_prev_10m
        vol_change_pct = 0
        if vol_prev_10m > 0:
            vol_change_pct = (vol_10m - vol_prev_10m) / vol_prev_10m * 100

        # 2.7 获取买卖盘口深度 (Wall)
        book_ticker = self.client.get_book_tickers(symbol)
        bid_wall = "无显著买单"
        ask_wall = "无显著卖单"
        bid_qty = 0
        ask_qty = 0
        bid_price = 0
        ask_price = 0
        
        if book_ticker:
            bid_qty = float(book_ticker.get('bidQty', 0))
            ask_qty = float(book_ticker.get('askQty', 0))
            bid_price = float(book_ticker.get('bidPrice', 0))
            ask_price = float(book_ticker.get('askPrice', 0))
            
            # 简单的墙判定：如果买一量是卖一量的 5 倍以上
            if ask_qty > 0 and bid_qty > 5 * ask_qty and bid_qty * bid_price > 50000:
                bid_wall = f"强支撑 (量: {bid_qty:.0f}, 额: {bid_qty*bid_price/10000:.1f}万)"
            if bid_qty > 0 and ask_qty > 5 * bid_qty and ask_qty * ask_price > 50000:
                ask_wall = f"强压盘 (量: {ask_qty:.0f}, 额: {ask_qty*ask_price/10000:.1f}万)"

        market_data = {
            "symbol": symbol,
            "funding_rate": f"{fr_val:.6f} ({fr_desc})",
            "open_interest": open_interest,
            "money_flow": money_flow,
            "bid_wall": bid_wall,
            "ask_wall": ask_wall,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "bid_price": bid_price,
            "ask_price": ask_price,
            "volume_10m": vol_10m,
            "volume_10m_prev": vol_prev_10m,
            "volume_10m_avg": vol_10m_avg,
            "volume_10m_ratio": vol_ratio,
            "volume_10m_change_pct": vol_change_pct,
            "price_10m_change_pct": price_10m_change_pct,
            "cmf": cmf,
            "net_flow_ma": net_flow_ma,
            "current_price": price,
            "change_pct": (current['收盘价'] - current['开盘价']) / current['开盘价'] * 100,
            "atr": atr,
            "rsi": rsi,
            "ma5": ma5,
            "ma20": ma20,
            "macd": current.get('MACD', 0),
            "macd_hist": current.get('MACD_HIST', 0),
            "boll_upper": current.get('BOLL_UPPER', 0),
            "boll_mid": current.get('BOLL_MID', 0),
            "boll_lower": current.get('BOLL_LOWER', 0),
            "ma_status": "看多" if current.get('MA5', 0) > current.get('MA20', 0) else "看空",
            "larger_timeframe_trend": larger_info,
            "recent_klines": recent_klines
        }

        def _dynamic_levels(sig, p, a):
            if p <= 0:
                return None, None
            if a and a > 0:
                atr_pct = a / p
                if atr_pct < 0.004:
                    sl_mult = 2.0
                elif atr_pct < 0.012:
                    sl_mult = 2.6
                elif atr_pct < 0.025:
                    sl_mult = 3.2
                else:
                    sl_mult = 3.8
                tp_mult = max(sl_mult * 1.8, 4.0)
                if sig == 'BUY':
                    return p - (sl_mult * a), p + (tp_mult * a)
                return p + (sl_mult * a), p - (tp_mult * a)
            sl_pct = 0.02
            tp_pct = 0.035
            if sig == 'BUY':
                return p * (1 - sl_pct), p * (1 + tp_pct)
            return p * (1 + sl_pct), p * (1 - tp_pct)

        def _sanitize_levels(sig, p, a, sl, tp):
            if not sl or not tp or p <= 0:
                return None, None
            try:
                sl = float(sl)
                tp = float(tp)
            except Exception:
                return None, None
            if sig == 'BUY' and not (sl < p < tp):
                return None, None
            if sig == 'SELL' and not (tp < p < sl):
                return None, None
            sl_dist = abs(p - sl)
            tp_dist = abs(tp - p)
            if a and a > 0:
                min_sl = max(1.6 * a, p * 0.004)
                max_sl = max(5.5 * a, p * 0.06)
            else:
                min_sl = p * 0.005
                max_sl = p * 0.08
            if sl_dist < min_sl or sl_dist > max_sl:
                return None, None
            if tp_dist < sl_dist * 1.3:
                return None, None
            return sl, tp

        print(f">>>【智能策略】正在请求分析 {symbol} ...")
        
        # 4. 调用大模型
        llm_result, error = self.llm.get_trading_advice(market_data)
        
        signal_raw = None
        reason = "智能分析失败"
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
                if confidence >= 75:
                    signal = signal_raw
                else:
                    zh = "做多" if signal_raw == 'BUY' else "做空"
                    reason = f"信心不足（{confidence} < 75），忽略信号：{zh}"
                    signal_raw = None

        if signal and trend_bias in ['BUY_ONLY', 'SELL_ONLY']:
            if trend_bias == 'BUY_ONLY' and signal != 'BUY':
                reason = "1小时趋势过滤：只做多，忽略做空信号"
                signal = None
                confidence = 0
            elif trend_bias == 'SELL_ONLY' and signal != 'SELL':
                reason = "1小时趋势过滤：只做空，忽略做多信号"
                signal = None
                confidence = 0
            
        info = {
            "reason": reason,
            "confidence": confidence,
            "current_price": price,
            "atr": atr
        }
        
        # 6. 计算止盈止损 (使用 LLM 返回的建议值，如果 LLM 未返回则使用默认 ATR 逻辑兜底)
        if signal:
            atr = current.get('ATR', 0)
            price = current['收盘价']
            
            # 优先使用建议的止盈止损
            llm_sl = llm_result.get('stop_loss') if llm_result else None
            llm_tp = llm_result.get('take_profit') if llm_result else None
            sl, tp = _sanitize_levels(signal, price, atr, llm_sl, llm_tp)
            if sl and tp:
                info['stop_loss'] = float(sl)
                info['take_profit'] = float(tp)
                print(f"   【建议】使用模型提供的风控点位: 止损 {sl}, 止盈 {tp}")
            else:
                sl, tp = _dynamic_levels(signal, price, atr)
                if sl and tp:
                    info['stop_loss'] = float(sl)
                    info['take_profit'] = float(tp)
                    print(f"   【建议】使用动态风控点位: 止损 {sl:.4f}, 止盈 {tp:.4f}")
        
        print("-" * 50)
        print(f"   【分析结果】{symbol}")
        direction = "观望"
        if signal == 'BUY':
            direction = "做多"
        elif signal == 'SELL':
            direction = "做空"
        print(f"   方向: {direction}")
        print(f"   信心: {info.get('confidence', 0)}%")
        print(f"   理由: {reason}")
        
        if signal and 'stop_loss' in info:
            print(f"   风控: 止损 {info['stop_loss']:.4f}, 止盈 {info['take_profit']:.4f}")
        print("-" * 50)

        return signal, info

    def audit_position(self, position, df, open_orders=None):
        """
        评估当前持仓
        :param position: 持仓字典
        :param df: K线数据
        :param open_orders: 当前挂单列表
        :return: (action, info) action: 'HOLD' 或 'CLOSE'
        """
        if df is None or len(df) < 5:
            return 'HOLD', {}
            
        current_ts = int(time.time() * 1000)
        last_update = position.get('update_time', 0)
        
        if last_update > 0:
            holding_ms = current_ts - last_update
            if holding_ms < 60000:
                return 'HOLD', {"reason": "持仓保护期（1分钟）", "confidence": 100}

        current = df.iloc[-1]
        
        money_flow = self.client.get_money_flow(position['symbol'], period='5m')
        
        # 提取资金流指标
        cmf = float(current.get('CMF', 0) or 0)
        net_flow_ma = float(current.get('Net_Flow_MA5', 0) or 0)

        market_data = {
            "current_price": current['收盘价'],
            "atr": current.get('ATR', 0),
            "rsi": current.get('RSI', 50),
            "cmf": cmf,
            "net_flow_ma": net_flow_ma,
            "ma_status": "看多" if current.get('MA5', 0) > current.get('MA20', 0) else "看空",
            "money_flow": money_flow,
            "open_orders": open_orders  # [新增] 传入挂单信息
        }
        
        print(f">>>【智能策略】正在评估持仓 {position['symbol']} ...")
        
        if open_orders:
            print(f"   [挂单信息] 正在结合当前 {len(open_orders)} 个挂单进行分析...")
        
        llm_result, error = self.llm.get_position_audit(position, market_data)
        
        action = 'HOLD'
        reason = "智能评估失败"
        confidence = 0
        
        if llm_result:
            action_raw = str(llm_result.get('action')).upper()
            confidence = llm_result.get('confidence', 0)
            
            if action_raw in ['HOLD', 'CLOSE']:
                if action_raw == 'CLOSE':
                    can_close = False
                    
                    if confidence >= 85:
                        can_close = True
                        reason = f"{llm_result.get('reason')}（模型极度确信）"
                    
                    # 综合资金流分析 (Net Inflow, CMF)
                    elif money_flow or cmf != 0:
                        net_inflow = money_flow.get('净流入量', 0) if money_flow else 0
                        side = position['side']
                        
                        # 判断资金流向是否支持平仓
                        # 做多时：资金流出 (Net < 0 or CMF < 0) 支持平仓
                        # 做空时：资金流入 (Net > 0 or CMF > 0) 支持平仓
                        
                        is_outflow = net_inflow < 0 or cmf < -0.05
                        is_inflow = net_inflow > 0 or cmf > 0.05
                        
                        if side == 'BUY' and is_outflow:
                            can_close = True
                            reason = f"{llm_result.get('reason')}（且资金流出验证 CMF:{cmf:.2f}）"
                        elif side == 'SELL' and is_inflow:
                            can_close = True
                            reason = f"{llm_result.get('reason')}（且资金流入验证 CMF:{cmf:.2f}）"
                        else:
                            action = 'HOLD'
                            reason = f"模型建议平仓（信心 {confidence}），但资金面未撤退 (CMF:{cmf:.2f}) -> 驳回平仓"
                    
                    else:
                        if confidence >= 75:
                             can_close = True
                        else:
                             action = 'HOLD'
                             reason = f"模型建议平仓但信心不足（{confidence} < 75）-> 保持持有"
                    
                    if can_close:
                        action = 'CLOSE'
                else:
                    action = 'HOLD'
                    reason = llm_result.get('reason', '无理由')
            else:
                reason = llm_result.get('reason', '无理由')
            
        print("-" * 50)
        print(f"   [持仓评估] {position['symbol']}")
        zh_action = "持有"
        if action == 'CLOSE':
            zh_action = "平仓"
        print(f"   建议操作: {zh_action}（信心: {confidence}）")
        print(f"   评估理由: {reason}")
        print("-" * 50)
        
        # 将 confidence 放入 info 字典返回
        info = {
            "reason": reason,
            "confidence": confidence,
            "adjust_suggestion": llm_result.get('adjust_suggestion'), # 保留旧字段兼容
            "adjustment": llm_result.get('adjustment'), # [新增] 结构化调整建议
            "stop_loss": llm_result.get('suggested_stop_loss') # [新增] 提取建议止损价
        }
        
        return action, info
