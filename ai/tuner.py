"""
参数微调 Agent
功能:
- 每日凌晨运行
- 分析最近 7 天的 K 线特征 (波动率/趋势度)
- 调用 LLM 根据市况特征输出参数建议
- 写入 Redis, 策略模块下次 tick 时读取并决定是否采纳
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from common.config import settings
from common.logger import get_logger
from common.redis_client import redis_client

logger = get_logger(__name__)

# 参数微调 Prompt
TUNER_PROMPT = """你是一个专业的量化策略参数优化师。

请根据以下市场特征数据, 为 EMA 均线交叉策略和突破策略提供参数建议。

## 最近 7 天市场特征
{features_text}

## 当前策略参数
- EMA 交叉策略: ema_fast=9, ema_slow=21, atr_multiplier=2.0
- 突破策略: lookback=20, volume_ratio=1.5, atr_multiplier=1.5

## 要求
根据当前市况 (高波动/低波动/趋势/震荡), 建议是否需要调整参数。
只输出你建议调整的参数, 不需要调整的不要输出。

请严格按以下 JSON 格式输出:
{{
    "market_regime": "trending_volatile",
    "strategies": {{
        "ema_cross": {{
            "ema_fast": 7,
            "ema_slow": 25,
            "atr_multiplier": 2.5,
            "reason": "近期波动率上升,建议加宽止损,缩短快线提高灵敏度"
        }},
        "breakout": {{
            "lookback": 30,
            "volume_ratio": 1.2,
            "reason": "震荡市中增加回望周期减少假突破"
        }}
    }}
}}"""


class TunerAgent:
    """
    参数微调 Agent

    工作流程:
    1. 从 ClickHouse 读取最近 7 天 K 线
    2. 计算市场特征 (波动率/趋势度/成交量变化)
    3. 调用 LLM 获取参数建议
    4. 写入 Redis 供策略读取
    """

    def __init__(self) -> None:
        self._gemini_api_key = settings.gemini_api_key
        self._openai_api_key = settings.openai_api_key

    async def run(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        执行参数微调

        Args:
            symbols: 需要分析的交易对

        Returns:
            参数建议字典
        """
        symbols = symbols or settings.symbols
        logger.info("tuner.running", symbols=symbols)

        # 1. 获取市场特征
        features = await self._compute_features(symbols)
        if not features:
            logger.warning("tuner.no_features")
            return {}

        # 2. 调用 LLM
        features_text = json.dumps(features, indent=2, ensure_ascii=False)
        result = await self._call_llm(features_text)
        if not result:
            logger.warning("tuner.llm_failed")
            return {}

        # 3. 写入 Redis
        strategies = result.get("strategies", {})
        for strategy_name, params in strategies.items():
            key = f"ai:params:{strategy_name}"
            await redis_client.set(key, json.dumps(params, ensure_ascii=False), ex=86400)

        logger.info(
            "tuner.completed",
            regime=result.get("market_regime"),
            strategies=list(strategies.keys()),
        )
        return result

    async def _compute_features(self, symbols: List[str]) -> Dict[str, Any]:
        """
        计算市场特征

        Returns:
            {symbol: {avg_atr, trend_slope, volatility_change, volume_trend, ...}}
        """
        from common.clickhouse import clickhouse_client

        features = {}
        for symbol in symbols:
            try:
                result = await clickhouse_client.query(
                    """
                    SELECT
                        avg(high_price - low_price) as avg_range,
                        stddev(close_price) / avg(close_price) as volatility,
                        count() as bars,
                        sum(volume) as total_volume,
                        (last(close_price) - first(close_price)) / first(close_price) as price_change_pct
                    FROM klines
                    WHERE symbol = %(symbol)s
                      AND interval = '1m'
                      AND open_time >= now() - INTERVAL 7 DAY
                    """,
                    params={"symbol": symbol},
                )
                if result and result.result_rows:
                    row = result.result_rows[0]
                    features[symbol] = {
                        "avg_range": round(float(row[0] or 0), 4),
                        "volatility": round(float(row[1] or 0), 6),
                        "bars": int(row[2] or 0),
                        "total_volume": round(float(row[3] or 0), 2),
                        "price_change_pct": round(float(row[4] or 0) * 100, 2),
                    }
            except Exception:
                logger.exception("tuner.feature_error", symbol=symbol)

        return features

    async def _call_llm(self, features_text: str) -> Optional[Dict]:
        """调用 LLM"""
        prompt = TUNER_PROMPT.format(features_text=features_text)

        if self._gemini_api_key:
            result = await self._call_gemini(prompt)
            if result:
                return result

        if self._openai_api_key:
            result = await self._call_openai(prompt)
            if result:
                return result

        return None

    async def _call_gemini(self, prompt: str) -> Optional[Dict]:
        """调用 Gemini"""
        try:
            import google.generativeai as genai
            genai.configure(api_key=self._gemini_api_key)
            model = genai.GenerativeModel("gemini-pro")
            response = model.generate_content(prompt)
            return self._parse_json(response.text)
        except Exception:
            logger.exception("tuner.gemini_error")
            return None

    async def _call_openai(self, prompt: str) -> Optional[Dict]:
        """调用 OpenAI"""
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self._openai_api_key)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=800,
            )
            return self._parse_json(response.choices[0].message.content)
        except Exception:
            logger.exception("tuner.openai_error")
            return None

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict]:
        """从 LLM 响应中提取 JSON"""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            import re
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            return None
