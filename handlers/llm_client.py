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
                    {"role": "system", "content": "你是一个专业的加密货币日内交易员。根据用户提供的技术指标和市场数据，判断未来的短期走势。请只输出 JSON 格式，包含字段: signal (BUY/SELL/HOLD), reason (简短理由), confidence (0-100)."},
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
            
        prompt = f"""
        请评估当前持仓是否应该继续持有 ({position_data.get('symbol')}):
        
        [当前持仓]
        - 方向: {position_data.get('side')}
        - 持仓量: {position_data.get('amount')}
        - 开仓均价: {position_data.get('entry_price')}
        - 未实现盈亏: {position_data.get('unrealized_pnl')} USDT
        - 杠杆: {position_data.get('leverage')}x
        
        [市场数据]
        - 当前价: {market_data.get('current_price')}
        - RSI (14): {market_data.get('rsi')}
        - MA5/MA20: {market_data.get('ma_status')}
        - ATR: {market_data.get('atr')}
        
        基于当前市场走势和持仓盈亏情况，建议怎么操作？
        请输出 JSON: action (HOLD/CLOSE), reason (简短理由), confidence (0-100).
        注意：如果趋势反转或风险过大，请果断建议 CLOSE。如果仍有盈利空间，建议 HOLD。
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
        
        return f"""
        请分析以下加密货币数据 ({data.get('symbol')}):
        
        [最近 12 小时价格走势 (从旧到新)]
        {kline_text}
        
        [当前技术指标]
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
           
        [分析任务]
        请结合上述 K 线序列形态（如吞没、启明星、双底等）和技术指标，判断未来的短期走势。
        
        [输出要求]
        请只输出 JSON 格式，包含字段: 
        - signal (BUY/SELL/HOLD)
        - confidence (0-100)
        - reason (简短理由，请引用具体 K 线形态或指标背离作为依据)
        """
