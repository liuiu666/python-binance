"""
情绪分析 Agent
功能:
- 每 4 小时运行一次
- 从多个数据源 (币安广场/Twitter/CryptoNews) 抓取市场相关文本
- 调用 LLM (Gemini/OpenAI) 分析情绪
- 输出情绪指数 (-1 ~ +1) 写入 Redis
- 策略模块可选择性参考情绪指数
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import httpx

from common.config import settings
from common.logger import get_logger
from common.redis_client import redis_client

logger = get_logger(__name__)

# 情绪分析 Prompt
SENTIMENT_PROMPT = """你是一个专业的加密货币市场情绪分析师。

请分析以下市场文本数据, 输出 JSON 格式的情绪分析结果。

要求:
1. sentiment_score: -1.0 (极度恐慌) 到 +1.0 (极度贪婪)
2. dominant_emotion: 主要情绪 (如: fear, greed, cautious_optimism, panic, euphoria)
3. key_events: 列出 3-5 个关键事件或主题
4. suggestion: 一句话建议 (如: 维持多头倾向但缩小仓位)

数据:
{texts}

请严格按以下 JSON 格式输出, 不要包含其他内容:
{{
    "sentiment_score": 0.3,
    "dominant_emotion": "cautious_optimism",
    "key_events": ["事件1", "事件2", "事件3"],
    "suggestion": "维持多头倾向但缩小仓位"
}}"""


class SentimentAgent:
    """
    情绪分析 Agent

    工作流程:
    1. 从数据源抓取文本
    2. 拼接为 Prompt
    3. 调用 LLM 分析
    4. 解析结果写入 Redis
    """

    def __init__(self) -> None:
        self._gemini_api_key = settings.gemini_api_key
        self._openai_api_key = settings.openai_api_key

    async def run(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        执行情绪分析

        Args:
            symbols: 需要分析的交易对

        Returns:
            情绪分析结果
        """
        symbols = symbols or settings.symbols
        logger.info("sentiment.running", symbols=symbols)

        # 1. 抓取数据
        texts = await self._fetch_texts(symbols)
        if not texts:
            logger.warning("sentiment.no_data")
            return {}

        # 2. 调用 LLM
        result = await self._analyze(texts)
        if not result:
            logger.warning("sentiment.analysis_failed")
            return {}

        # 3. 写入 Redis
        for symbol in symbols:
            key = f"ai:sentiment:{symbol}"
            await redis_client.set(key, json.dumps(result, ensure_ascii=False), ex=86400)

        logger.info(
            "sentiment.completed",
            score=result.get("sentiment_score"),
            emotion=result.get("dominant_emotion"),
        )
        return result

    async def _fetch_texts(self, symbols: List[str]) -> List[str]:
        """
        从数据源抓取文本

        当前实现: 从币安广场 API 获取最新帖子
        可扩展: Twitter API, CryptoNews RSS 等
        """
        texts = []

        # 币安广场 API
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for symbol in symbols:
                    try:
                        resp = await client.get(
                            "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
                            params={
                                "type": 1,
                                "catalogId": 48,
                                "pageNo": 1,
                                "pageSize": 10,
                            },
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            articles = data.get("data", {}).get("articles", [])
                            for article in articles:
                                title = article.get("title", "")
                                if title:
                                    texts.append(title)
                    except Exception:
                        logger.debug("sentiment.binance_square_error", symbol=symbol)
        except Exception:
            logger.exception("sentiment.fetch_error")

        return texts[:50]  # 限制文本数量, 避免超出 token 限制

    async def _analyze(self, texts: List[str]) -> Optional[Dict]:
        """
        调用 LLM 分析情绪

        优先使用 Gemini, 不可用时回退到 OpenAI
        """
        prompt = SENTIMENT_PROMPT.format(texts="\n".join(f"- {t}" for t in texts))

        # 尝试 Gemini
        if self._gemini_api_key:
            result = await self._call_gemini(prompt)
            if result:
                return result

        # 尝试 OpenAI
        if self._openai_api_key:
            result = await self._call_openai(prompt)
            if result:
                return result

        logger.warning("sentiment.no_llm_available")
        return None

    async def _call_gemini(self, prompt: str) -> Optional[Dict]:
        """调用 Google Gemini API"""
        try:
            import google.generativeai as genai
            genai.configure(api_key=self._gemini_api_key)
            model = genai.GenerativeModel("gemini-pro")
            response = model.generate_content(prompt)
            text = response.text

            # 提取 JSON
            return self._parse_json_response(text)
        except Exception:
            logger.exception("sentiment.gemini_error")
            return None

    async def _call_openai(self, prompt: str) -> Optional[Dict]:
        """调用 OpenAI API"""
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self._openai_api_key)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=500,
            )
            text = response.choices[0].message.content
            return self._parse_json_response(text)
        except Exception:
            logger.exception("sentiment.openai_error")
            return None

    @staticmethod
    def _parse_json_response(text: str) -> Optional[Dict]:
        """从 LLM 响应中提取 JSON"""
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 JSON 块
        import re
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        return None
