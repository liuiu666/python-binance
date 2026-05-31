"""
健康监控模块
功能:
- 记录每个 symbol 最后一条消息的时间
- 15s 无数据 → 触发重连 + 日志告警
- 提供 HTTP /health 端点 (供 Docker 健康检查)
- 定期打印运行状态摘要
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from aiohttp import web

from common.config import settings
from common.logger import get_logger
from common.notify import notifier

logger = get_logger(__name__)

# 心跳检测阈值 (秒)
HEARTBEAT_TIMEOUT = 15.0

# 告警最小间隔 (秒) — 同一 symbol 最多每 60 秒告警一次, 避免告警风暴
ALERT_MIN_INTERVAL = 60.0

# 状态打印间隔 (秒)
STATUS_PRINT_INTERVAL = 60.0


@dataclass
class SymbolHealth:
    """单个交易对的健康状态"""
    symbol: str
    last_message_ts: float = 0.0
    total_messages: int = 0
    last_kline_close: float = 0.0
    last_kline_time: int = 0
    last_alert_ts: float = 0.0  # 上次发送告警的时间, 用于频率限制


class HealthMonitor:
    """
    健康监控器

    职责:
    1. 追踪每个 symbol 的消息接收状态
    2. 超时检测并触发告警
    3. 提供 HTTP 健康检查端点
    4. 定期打印运行状态
    """

    def __init__(self, symbols: Optional[list] = None) -> None:
        self._symbols = symbols or settings.symbols
        self._health: Dict[str, SymbolHealth] = {
            s: SymbolHealth(symbol=s) for s in self._symbols
        }
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._status_task: Optional[asyncio.Task] = None
        self._web_runner: Optional[web.AppRunner] = None

        # 启动时间
        self._start_time: float = 0.0

    # ============================================================
    # 公共 API
    # ============================================================

    def record_message(self, symbol: str, **kwargs) -> None:
        """
        记录收到消息 (由 WS 消息处理函数调用)

        Args:
            symbol: 交易对
            **kwargs: 附加信息 (如 close_price, kline_time)
        """
        symbol = symbol.upper()
        if symbol in self._health:
            h = self._health[symbol]
            h.last_message_ts = time.monotonic()
            h.total_messages += 1
            if "close_price" in kwargs:
                h.last_kline_close = kwargs["close_price"]
            if "kline_time" in kwargs:
                h.last_kline_time = kwargs["kline_time"]

    async def start(self, http_port: int = 8080) -> None:
        """
        启动健康监控

        Args:
            http_port: HTTP 健康检查端口
        """
        self._running = True
        self._start_time = time.time()
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._status_task = asyncio.create_task(self._status_loop())
        await self._start_http_server(http_port)
        logger.info("health.started", port=http_port)

    async def stop(self) -> None:
        """停止监控"""
        self._running = False
        for task in (self._monitor_task, self._status_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._web_runner:
            await self._web_runner.cleanup()
        logger.info("health.stopped")

    # ============================================================
    # 监控循环
    # ============================================================

    async def _monitor_loop(self) -> None:
        """心跳检测循环 — 每 5 秒检查一次"""
        while self._running:
            await asyncio.sleep(5)
            now = time.monotonic()

            for symbol, h in self._health.items():
                if h.last_message_ts == 0:
                    continue
                age = now - h.last_message_ts
                if age > HEARTBEAT_TIMEOUT:
                    logger.warning(
                        "health.heartbeat_timeout",
                        symbol=symbol,
                        age_seconds=f"{age:.1f}",
                    )
                    # 同一 symbol 最多每 ALERT_MIN_INTERVAL 秒告警一次
                    if now - h.last_alert_ts >= ALERT_MIN_INTERVAL:
                        h.last_alert_ts = now
                        await notifier.notify_alert(
                            "WARN",
                            f"{symbol} 超过 {HEARTBEAT_TIMEOUT:.0f}s 未收到数据 (已 {age:.0f}s)",
                        )

    async def _status_loop(self) -> None:
        """定期打印运行状态"""
        while self._running:
            await asyncio.sleep(STATUS_PRINT_INTERVAL)
            self._print_status()

    def _print_status(self) -> None:
        """打印当前运行状态摘要"""
        uptime = time.time() - self._start_time
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)

        lines = [f"运行时间: {hours}h{minutes}m"]
        for symbol, h in self._health.items():
            if h.total_messages > 0:
                age = time.monotonic() - h.last_message_ts
                lines.append(
                    f"  {symbol}: 消息={h.total_messages}, "
                    f"最新价={h.last_kline_close}, "
                    f"延迟={age:.1f}s"
                )
            else:
                lines.append(f"  {symbol}: 未收到数据")

        logger.info("health.status", status=" | ".join(lines))

    # ============================================================
    # HTTP 健康检查端点
    # ============================================================

    async def _start_http_server(self, port: int) -> None:
        """启动 HTTP 健康检查服务"""
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()
        site = web.TCPSite(self._web_runner, "0.0.0.0", port)
        await site.start()

    async def _handle_health(self, request: web.Request) -> web.Response:
        """
        GET /health 端点
        返回 JSON 格式的健康状态
        """
        now = time.monotonic()
        statuses = {}
        all_healthy = True

        for symbol, h in self._health.items():
            if h.last_message_ts == 0:
                age = -1
                healthy = False
            else:
                age = now - h.last_message_ts
                healthy = age < HEARTBEAT_TIMEOUT

            if not healthy:
                all_healthy = False

            statuses[symbol] = {
                "healthy": healthy,
                "last_message_age_seconds": round(age, 1) if age >= 0 else None,
                "total_messages": h.total_messages,
                "last_price": h.last_kline_close or None,
            }

        status_code = 200 if all_healthy else 503
        body = json.dumps({
            "status": "healthy" if all_healthy else "degraded",
            "uptime_seconds": int(time.time() - self._start_time),
            "symbols": statuses,
        })

        return web.Response(
            text=body,
            status=status_code,
            content_type="application/json",
        )
