"""
AI 调度器 — 基于 APScheduler 的定时任务管理
功能:
- 情绪分析: 每 4h
- 交易复盘: 每日 22:00
- 参数微调: 每日 02:00
- 异常重试: 单次 Agent 失败后重试 2 次, 间隔 60s
- 超时保护: 单次 Agent 运行超过 120s 强制终止
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from common.config import settings
from common.logger import get_logger
from common.redis_client import redis_client
from common.db import db

from ai.sentiment import SentimentAgent
from ai.reviewer import ReviewerAgent
from ai.tuner import TunerAgent

logger = get_logger(__name__)

# Agent 运行超时 (秒)
AGENT_TIMEOUT = 120

# 重试次数
MAX_RETRIES = 2

# 重试间隔 (秒)
RETRY_DELAY = 60


class AIScheduler:
    """
    AI 模块调度器
    管理所有 AI Agent 的定时执行
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._sentiment = SentimentAgent()
        self._reviewer = ReviewerAgent()
        self._tuner = TunerAgent()
        self._running = False

    async def start(self) -> None:
        """启动调度器"""
        self._running = True
        logger.info("ai_scheduler.starting")

        # 初始化连接
        await redis_client.connect()
        await db.connect()
        await db.ensure_tables()

        # 注册定时任务
        self._scheduler.add_job(
            self._run_sentiment,
            trigger=IntervalTrigger(hours=4),
            id="sentiment",
            name="情绪分析",
            max_instances=1,
            misfire_grace_time=300,
        )

        self._scheduler.add_job(
            self._run_reviewer,
            trigger=CronTrigger(hour=22, minute=0),
            id="reviewer",
            name="交易复盘",
            max_instances=1,
            misfire_grace_time=300,
        )

        self._scheduler.add_job(
            self._run_tuner,
            trigger=CronTrigger(hour=2, minute=0),
            id="tuner",
            name="参数微调",
            max_instances=1,
            misfire_grace_time=300,
        )

        self._scheduler.start()
        logger.info("ai_scheduler.started", jobs=[
            j.name for j in self._scheduler.get_jobs()
        ])

        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            self._scheduler.shutdown(wait=False)
            await db.close()
            await redis_client.close()
            logger.info("ai_scheduler.stopped")

    async def stop(self) -> None:
        """停止调度器"""
        self._running = False

    # ============================================================
    # Agent 运行封装 (带重试和超时)
    # ============================================================

    async def _run_sentiment(self) -> None:
        """运行情绪分析 (带重试)"""
        await self._run_with_retry("sentiment", self._sentiment.run)

    async def _run_reviewer(self) -> None:
        """运行交易复盘 (带重试)"""
        await self._run_with_retry("reviewer", self._reviewer.run)

    async def _run_tuner(self) -> None:
        """运行参数微调 (带重试)"""
        await self._run_with_retry("tuner", self._tuner.run)

    async def _run_with_retry(self, name: str, func) -> None:
        """
        带重试和超时的 Agent 运行封装

        Args:
            name: Agent 名称
            func: Agent 的 run 方法
        """
        for attempt in range(MAX_RETRIES + 1):
            try:
                logger.info("ai_scheduler.running", agent=name, attempt=attempt)
                result = await asyncio.wait_for(
                    func(), timeout=AGENT_TIMEOUT
                )
                logger.info("ai_scheduler.completed", agent=name)
                return
            except asyncio.TimeoutError:
                logger.error(
                    "ai_scheduler.timeout",
                    agent=name,
                    timeout=AGENT_TIMEOUT,
                )
            except Exception:
                logger.exception(
                    "ai_scheduler.error",
                    agent=name,
                    attempt=attempt,
                )

            if attempt < MAX_RETRIES:
                logger.info("ai_scheduler.retrying", agent=name, delay=RETRY_DELAY)
                await asyncio.sleep(RETRY_DELAY)

        logger.error("ai_scheduler.failed", agent=name, retries=MAX_RETRIES)


async def main() -> None:
    """AI 调度器主入口"""
    scheduler = AIScheduler()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(scheduler)))

    try:
        await scheduler.start()
    except KeyboardInterrupt:
        logger.info("ai_scheduler.keyboard_interrupt")
    finally:
        if scheduler._running:
            await scheduler.stop()


async def _shutdown(scheduler: AIScheduler) -> None:
    logger.info("ai_scheduler.shutdown_signal")
    scheduler._running = False


if __name__ == "__main__":
    asyncio.run(main())
