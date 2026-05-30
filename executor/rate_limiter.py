"""
API 限流器 (交易执行器专用)
基于令牌桶算法, 确保币安 REST 请求不超过权重限制
与 collector/rest_client.py 中的 RateLimiter 共享相同逻辑, 但独立维护状态
"""

from __future__ import annotations

import asyncio
import time

from common.logger import get_logger

logger = get_logger(__name__)


class TokenBucketRateLimiter:
    """
    令牌桶限流器
    币安合约 API 限制: 1200 权重/分钟
    """

    def __init__(
        self,
        max_weight: int = 1200,
        window_seconds: float = 60.0,
    ) -> None:
        self._max_weight = max_weight
        self._window = window_seconds
        self._tokens = float(max_weight)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._cool_until: float = 0.0

    async def acquire(self, weight: int = 1) -> None:
        """
        获取指定权重的令牌

        Args:
            weight: 请求权重
        """
        while True:
            async with self._lock:
                now = time.monotonic()

                if now < self._cool_until:
                    wait = self._cool_until - now
                    logger.warning("executor_ratelimiter.cooling", wait=f"{wait:.1f}s")
                    await asyncio.sleep(wait)
                    continue

                elapsed = now - self._last_refill
                self._tokens = min(
                    self._max_weight,
                    self._tokens + elapsed * self._max_weight / self._window,
                )
                self._last_refill = now

                if self._tokens >= weight:
                    self._tokens -= weight
                    logger.debug(
                        "executor_ratelimiter.acquired",
                        weight=weight,
                        remaining=int(self._tokens),
                    )
                    return

            wait = (weight - self._tokens) * self._window / self._max_weight
            await asyncio.sleep(max(wait, 0.1))

    def trigger_cooldown(self, seconds: float = 60.0) -> None:
        """
        触发冷却模式 (收到 HTTP 429 时调用)
        在冷却期内所有请求都会被阻塞
        """
        self._cool_until = time.monotonic() + seconds
        logger.error("executor_ratelimiter.cooldown", seconds=seconds)

    @property
    def available_tokens(self) -> int:
        """当前可用令牌数"""
        return int(self._tokens)
