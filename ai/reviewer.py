"""
交易复盘 Agent
功能:
- 每日 22:00 触发
- 从 PostgreSQL 读取当日所有交易记录
- 构造 Prompt, 调用 LLM 生成复盘报告
- 报告推送至钉钉 + 存入数据库
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from common.config import settings
from common.logger import get_logger
from common.db import db
from common.redis_client import redis_client
from common.notify import notifier

logger = get_logger(__name__)

# 复盘报告 Prompt
REVIEW_PROMPT = """你是一个专业的量化交易复盘分析师。

请分析以下今日交易记录, 生成一份详细的复盘报告。

## 今日交易记录
{trades_text}

## 要求
请从以下维度进行分析:

1. **整体表现评分** (1-10分)
2. **最佳交易**: 哪笔交易执行最好, 为什么
3. **最差交易**: 哪笔交易执行最差, 为什么
4. **策略评估**: 策略是否适应当前市场
5. **改进建议**: 3-5 条具体可执行的改进建议
6. **发现的非理性行为**: 是否有追涨杀跌、过度交易等

请使用中文, 以 Markdown 格式输出, 简洁清晰。"""


class ReviewerAgent:
    """
    交易复盘 Agent

    工作流程:
    1. 从 PostgreSQL 读取今日交易
    2. 格式化为文本
    3. 调用 LLM 生成报告
    4. 推送至钉钉/Telegram
    5. 存入数据库
    """

    def __init__(self) -> None:
        self._gemini_api_key = settings.gemini_api_key
        self._openai_api_key = settings.openai_api_key

    async def run(self, target_date: Optional[date] = None) -> str:
        """
        执行交易复盘

        Args:
            target_date: 目标日期, 默认今天

        Returns:
            复盘报告文本
        """
        target_date = target_date or date.today()
        logger.info("reviewer.running", date=str(target_date))

        # 1. 读取交易记录
        trades = await self._fetch_trades(target_date)
        if not trades:
            report = f"## {target_date} 复盘报告\n\n今日无交易记录。"
            await notifier.send(title=f"复盘报告 {target_date}", text=report)
            return report

        # 2. 格式化
        trades_text = self._format_trades(trades)

        # 3. 调用 LLM
        report = await self._generate_report(trades_text)
        if not report:
            report = f"## {target_date} 复盘报告\n\nLLM 生成失败, 请手动查看交易记录。"

        # 添加统计摘要
        summary = self._generate_summary(trades, target_date)
        full_report = f"{summary}\n\n{report}"

        # 4. 推送通知
        await notifier.send(
            title=f"每日复盘 {target_date}",
            text=full_report[:4000],  # 钉钉消息长度限制
        )

        # 5. 存入数据库
        await self._save_report(target_date, full_report)

        logger.info("reviewer.completed", date=str(target_date))
        return full_report

    async def _fetch_trades(self, target_date: date) -> List[Dict]:
        """从 PostgreSQL 读取指定日期的交易"""
        try:
            async with db.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT symbol, side, action, quantity, entry_price,
                           exit_price, pnl, fee, strategy, reason, status,
                           opened_at, closed_at
                    FROM trades
                    WHERE DATE(created_at) = $1
                    ORDER BY created_at
                    """,
                    target_date,
                )
                return [dict(row) for row in rows]
        except Exception:
            logger.exception("reviewer.fetch_error")
            return []

    def _format_trades(self, trades: List[Dict]) -> str:
        """格式化交易记录为文本"""
        lines = []
        for i, t in enumerate(trades, 1):
            pnl = t.get("pnl")
            pnl_str = f"{float(pnl):+.2f} USDT" if pnl else "N/A"
            lines.append(
                f"{i}. {t['symbol']} {t['side']} | "
                f"数量: {t['quantity']} | "
                f"入场: {t.get('entry_price', 'N/A')} | "
                f"出场: {t.get('exit_price', 'N/A')} | "
                f"盈亏: {pnl_str} | "
                f"策略: {t.get('strategy', 'N/A')} | "
                f"原因: {t.get('reason', 'N/A')}"
            )
        return "\n".join(lines)

    def _generate_summary(self, trades: List[Dict], target_date: date) -> str:
        """生成统计摘要"""
        total = len(trades)
        wins = sum(1 for t in trades if t.get("pnl") and float(t["pnl"]) > 0)
        losses = sum(1 for t in trades if t.get("pnl") and float(t["pnl"]) < 0)
        total_pnl = sum(float(t.get("pnl", 0)) for t in trades if t.get("pnl"))

        return (
            f"## {target_date} 交易复盘\n\n"
            f"| 指标 | 值 |\n|:---|:---|\n"
            f"| 总交易数 | {total} |\n"
            f"| 盈利次数 | {wins} |\n"
            f"| 亏损次数 | {losses} |\n"
            f"| 胜率 | {wins/total*100:.1f}% |\n"
            f"| 总盈亏 | {total_pnl:+.2f} USDT |\n"
        )

    async def _generate_report(self, trades_text: str) -> Optional[str]:
        """调用 LLM 生成复盘报告"""
        prompt = REVIEW_PROMPT.format(trades_text=trades_text)

        # 尝试 Gemini
        if self._gemini_api_key:
            report = await self._call_gemini(prompt)
            if report:
                return report

        # 尝试 OpenAI
        if self._openai_api_key:
            report = await self._call_openai(prompt)
            if report:
                return report

        return None

    async def _call_gemini(self, prompt: str) -> Optional[str]:
        """调用 Gemini"""
        try:
            import google.generativeai as genai
            genai.configure(api_key=self._gemini_api_key)
            model = genai.GenerativeModel("gemini-pro")
            response = model.generate_content(prompt)
            return response.text
        except Exception:
            logger.exception("reviewer.gemini_error")
            return None

    async def _call_openai(self, prompt: str) -> Optional[str]:
        """调用 OpenAI"""
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self._openai_api_key)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=2000,
            )
            return response.choices[0].message.content
        except Exception:
            logger.exception("reviewer.openai_error")
            return None

    async def _save_report(self, target_date: date, report: str) -> None:
        """保存报告到数据库"""
        try:
            async with db.pool.acquire() as conn:
                # 创建 AI 报告表 (如果不存在)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ai_reports (
                        id BIGSERIAL PRIMARY KEY,
                        report_date DATE NOT NULL,
                        report_type VARCHAR(50) NOT NULL DEFAULT 'daily_review',
                        content TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                await conn.execute(
                    """
                    INSERT INTO ai_reports (report_date, report_type, content)
                    VALUES ($1, 'daily_review', $2)
                    ON CONFLICT DO NOTHING
                    """,
                    target_date, report,
                )
        except Exception:
            logger.exception("reviewer.save_error")
