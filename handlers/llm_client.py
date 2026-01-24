import os
import json
from openai import OpenAI

class LLMClient:
    def __init__(self, config_path='config.json'):
        self.config = self._load_config(config_path)
        
        # 优先读取环境变量，其次读取 config.json
        api_key = os.getenv('LLM_API_KEY') or self.config.get('llm', {}).get('api_key')
        base_url = os.getenv('LLM_BASE_URL') or self.config.get('llm', {}).get('base_url')
        self.model = os.getenv('LLM_MODEL') or self.config.get('llm', {}).get('model', 'gpt-4o')
        
        if not api_key or api_key == "YOUR_API_KEY_HERE":
            print(">>> [Warning] LLM API Key 未配置，AI 决策功能将不可用。")
            self.client = None
        else:
            self.client = OpenAI(api_key=api_key, base_url=base_url)

    def _load_config(self, path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except:
            return {}

    def get_trading_advice(self, market_data):
        """
        发送市场数据给 LLM，获取交易建议
        :param market_data: 字典格式的市场数据
        :return: (signal, reason)
        """
        if not self.client:
            return None, "LLM Client 未初始化"

        prompt = self._build_prompt(market_data)
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一个专业的加密货币趋势与资金面分析师，目标是显著提升胜率并降低无效交易。决策时必须综合资金流向、10分钟成交量变化、合约持仓(OI)、资金费率与大周期趋势；若关键维度缺失或相互矛盾，直接给出 HOLD 并降低信心。请只输出 JSON 格式，包含字段: signal (BUY/SELL/HOLD), reason (简短理由), confidence (0-100), stop_loss (止损价), take_profit (止盈价), support_level (支撑位), resistance_level (压力位)。reason 必须同时提及资金流向与10分钟成交量的变化结论。请根据提供的 K 线数据精确识别当前的支撑位和压力位。"},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            
            # 兼容性处理：防止 LLM 返回列表而非字典
            if isinstance(result, list):
                if len(result) > 0:
                    result = result[0]
                else:
                    return None, "LLM 返回了空列表"
            
            if not isinstance(result, dict):
                return None, f"LLM 返回格式错误: {type(result)}"

            # 返回完整结果以便上层获取 confidence 等信息
            return result, None
            
        except Exception as e:
            print(f"LLM 调用失败: {str(e)}")
            return None, f"调用错误: {str(e)}"

    def get_position_audit(self, position_data, market_data):
        """
        请求 AI 评估当前持仓
        :param position_data: 持仓信息
        :param market_data: 市场信息
        :return: (action, reason) action: HOLD or CLOSE
        """
        if not self.client:
            return None, "LLM Client 未初始化"
        
        # 格式化资金流向
        flow_text = "暂无数据"
        if market_data.get('money_flow'):
            mf = market_data['money_flow']
            flow_text = f"周期: {mf.get('周期')}, 主动买入: {mf.get('主动买入量'):.2f}, 主动卖出: {mf.get('主动卖出量'):.2f}, 净流入: {mf.get('净流入量'):.2f}, 买卖比: {mf.get('买卖比'):.4f}"

        open_orders = market_data.get('open_orders', [])
        open_orders_desc = "无挂单"
        if open_orders:
            descs = []
            for o in open_orders:
                otype = o.get('type')
                oprice = o.get('stopPrice') or o.get('price')
                oside = o.get('side')
                descs.append(f"{otype} ({oside}) @ {oprice}")
            open_orders_desc = ", ".join(descs)
            
        prompt = f"""
        请评估当前持仓是否应该继续持有 ({position_data.get('symbol')}):
        
        [当前持仓]
        - 方向: {position_data.get('side')}
        - 持仓量: {position_data.get('amount')}
        - 开仓均价: {position_data.get('entry_price')}
        - 未实现盈亏: {position_data.get('unrealized_pnl')} USDT
        - 杠杆: {position_data.get('leverage')}x
        - 当前挂单: {open_orders_desc}
        
        [订单簿深度 (挂单墙)]
        **请重点分析此数据，判断是否有主力压盘或托单**
        - 买单墙 (支撑): {market_data.get('bid_wall', '无显著买单')}
        - 卖单墙 (压力): {market_data.get('ask_wall', '无显著卖单')}
        - 买一价: {market_data.get('bid_price')} (量: {market_data.get('bid_qty')})
        - 卖一价: {market_data.get('ask_price')} (量: {market_data.get('ask_qty')})
        
        [关键资金数据 (核心依据)]
        - 资金流向 (5m): {flow_text}
        - CMF指标: {market_data.get('cmf', 0):.4f} (Chaikin Money Flow)
        - 净流入MA5: {market_data.get('net_flow_ma', 0):.2f}
        
        [市场技术面]
        - 当前价: {market_data.get('current_price')}
        - RSI (14): {market_data.get('rsi')}
        - MA趋势: {market_data.get('ma_status')}
        - ATR: {market_data.get('atr')}
        
        [决策规则]
        1. **严禁单纯依赖 RSI 超买超卖平仓**。在强趋势中，RSI > 70 或 < 30 是常态，除非伴随资金大幅撤退，否则不要平仓。
        2. **核心看资金**：
           - 做多时：只有当资金明显流出 (净流出且 CMF < 0) 或 价格有效跌破 MA20 或 **跌破关键支撑位** 时，才建议 CLOSE。
           - 做空时：只有当资金明显流入 (净流入且 CMF > 0) 或 价格有效站上 MA20 或 **突破关键压力位** 时，才建议 CLOSE。
        3. 如果资金流向与持仓方向一致（例如做多且资金净流入），请坚定 HOLD，哪怕有浮亏。
        4. **加减仓判断 (Pyramiding/Scaling)**:
           - **ADD (加仓)**: 当趋势极强 (CMF>0.1, 净流入>0, 成交量放大) 且 浮盈 > 2% 且 当前价格突破关键阻力位/均线发散向上时。
           - **REDUCE (减仓)**: 当趋势受阻但未完全反转 (例如遇到强阻力位滞涨, 资金流出但均线未破) 且 浮盈 > 5% 时，建议减仓锁定利润。
        5. **检查挂单合理性 (动态调整)**：
           - **关键**: 如果当前没有止损单 (open_orders 中无 STOP 类订单)，**必须**建议设置止损 (SET_SL)，价格建议参考 ATR 或 **K线支撑压力位**。
           - 如果建议 HOLD 且趋势强劲，检查是否需要取消止盈 (CANCEL_TP) 或 上移止盈 (MOVE_TP)。
           - 如果建议 HOLD 但风险增加，检查是否需要上移/下移止损 (MOVE_SL)，参考 **近期K线形成的支撑/压力位**。
           - 如果当前没有止盈且趋势变弱，建议设置止盈 (SET_TP)，参考 **上方压力位/下方支撑位**。
           - **盈利时止损必须上移到盈利区间**：做多止损不得低于开仓价，做空止损不得高于开仓价；若给出 MOVE_SL/SET_SL，必须满足该条件。
        
        请输出 JSON: 
        - action (HOLD / CLOSE / ADD / REDUCE)
        - reason (简短理由)
        - confidence (0-100)
        - adjustment (可选对象):
            - type: "CANCEL_TP" | "MOVE_SL" | "MOVE_TP" | "SET_SL" | "SET_TP" | "NONE"
            - value: 数值 (新的价格)
            - reason: 调整理由
        """
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一个专业的风险控制专家。负责监控持仓风险，决定是继续持有还是平仓止损/止盈。"},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            
            if isinstance(result, list) and len(result) > 0:
                result = result[0]
                
            if not isinstance(result, dict):
                return None, "Format Error"
                
            return result, None
            
        except Exception as e:
            print(f"LLM Audit Failed: {e}")
            return None, str(e)

    def _build_prompt(self, data):
        # 格式化 K 线序列
        kline_text = ""
        if 'recent_klines' in data and data['recent_klines']:
            kline_text = "\n".join(data['recent_klines'])
        
        # 格式化资金流向
        flow_text = "暂无数据"
        if data.get('money_flow'):
            mf = data['money_flow']
            flow_text = f"周期: {mf.get('周期')}, 主动买入: {mf.get('主动买入量'):.2f}, 主动卖出: {mf.get('主动卖出量'):.2f}, 净流入: {mf.get('净流入量'):.2f}, 买卖比: {mf.get('买卖比'):.4f}"

        return f"""
        请分析以下加密货币数据 ({data.get('symbol')}):
        
        [关键资金流向 (核心驱动力)]
        **这是最重要的判断依据，请优先参考**
        {flow_text}
        (如果净流入为正且买卖比 > 1.1，说明资金在抢筹; 反之说明资金出逃)

        [关键合约数据]
        1. 资金费率: {data.get('funding_rate', '未知')} 
           (注意: 正值代表多头付钱给空头; 若 > 0.05% 则持仓成本极高; 若 < -0.05% 可能是空头拥挤)
        2. 持仓量 (OI): {data.get('open_interest', 0)}

        [10分钟成交量与价格动量]
        - 近10m成交量: {data.get('volume_10m', 0)}
        - 前10m成交量: {data.get('volume_10m_prev', 0)}
        - 近30m均值(折算10m): {data.get('volume_10m_avg', 0)}
        - 10m量能变化: {data.get('volume_10m_change_pct', 0):.2f}% (倍数: {data.get('volume_10m_ratio', 0):.2f})
        - 10m价格变动: {data.get('price_10m_change_pct', 0):.2f}%
        
        [最近 60 根 1m K线价格走势 (从旧到新)]
        (请基于此数据识别支撑位 support_level 和 压力位 resistance_level)
        {kline_text}
        
        [当前技术指标 (辅助参考)]
        1. 趋势指标:
           - MA5: {data.get('ma5')}
           - MA20: {data.get('ma20')} ({data.get('ma_status')})
           - MACD: {data.get('macd')} (Hist: {data.get('macd_hist')})
           - 布林带: 上轨 {data.get('boll_upper')}, 中轨 {data.get('boll_mid')}, 下轨 {data.get('boll_lower')}
           
        2. 震荡指标:
           - RSI (14): {data.get('rsi')}
           - ATR (波动率): {data.get('atr')}
        
        3. 市场概况:
           - 当前价: {data.get('current_price')}
           - 24h 涨跌: {data.get('change_pct')}%

        [大周期趋势参考]
        - MA5: {data.get('larger_timeframe_trend', {}).get('ma5')}
        - MA20: {data.get('larger_timeframe_trend', {}).get('ma20')}
        - RSI: {data.get('larger_timeframe_trend', {}).get('rsi')}
        - 趋势: {data.get('larger_timeframe_trend', {}).get('trend')}
           
        [分析任务]
        用户目标是**快速翻倍**，偏好**高波动、高增长**的机会。
        请**重资金流向和成交量，轻滞后指标**，并遵循多因子一致性。
        1. 只有当以下条件中至少满足 3 条才给出 BUY/SELL，否则输出 HOLD：
           - 资金流向与方向一致且强度明确（净流入/净流出明显，买卖比偏离 1）
           - 10m 成交量明显放大或高于近30m均值
           - OI 与价格同向增量（上涨+OI上升 或 下跌+OI上升）
           - 大周期趋势与信号方向一致
           - 10m 价格动量与资金方向一致
        2. 如果资金流出但指标显示超卖，不要轻易抄底（可能是阴跌）。
        3. 只要资金和量能支持，不要在意 RSI 超买（可能是主升浪）。
        
        [硬性风控规则]
        1. 手续费成本约为 0.1% (双边)。如果预期利润 < 0.2%，请直接观望 (HOLD)。
        2. 如果价格上涨但 OI (持仓量) 下跌，视为“空头平仓导致的诱多”，请谨慎。
        3. 如果资金费率极高 (>0.05%) 且趋势不明确，请避免做多。
        4. 如果 10m 成交量没有放大且价格动量不足，避免给出高信心信号。
        
        [输出要求]
        请只输出 JSON 格式，包含字段: 
        - signal (BUY/SELL/HOLD)
        - confidence (0-100)
        - stop_loss (建议止损价格，数值类型)
        - take_profit (建议止盈价格，数值类型)
        - reason (简短理由，必须同时包含资金流向与10分钟成交量变化结论)
        
        [止盈止损建议]
        - 请根据 ATR 或 支撑压力位 给出具体的止盈止损价格。
        - 针对小市值/高波动币种，建议设置较宽的止损 (如 3倍 ATR) 以防被震仓。
        - 止盈应至少是止损距离的 2.0 倍 (追求高盈亏比)。
        """
