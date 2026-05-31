"""
币安合约 User Data Stream 监听
功能:
- 自动获取和续期 listenKey
- 监听账户级事件: 余额变更 (BALANCE_UPDATE) 和订单状态 (ORDER_TRADE_UPDATE)
- 将事件写入 Redis Streams 供其他模块消费
- 自动重连 + 心跳检测
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional

import websockets

from common.config import settings
from common.logger import get_logger
from common.redis_client import redis_client, STREAM_ACCOUNT
from collector.rest_client import rest_client

logger = get_logger(__name__)

# 事件处理函数类型
EventHandler = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]


class UserDataStream:
    """
    币安合约 User Data Stream 客户端

    职责:
    1. 调用 REST API 获取 listenKey
    2. 每 30 分钟续期 listenKey
    3. 通过 WebSocket 监听账户事件
    4. 将事件写入 Redis Streams
    """

    def __init__(self) -> None:
        self._ws: Optional[websockets.asyncio.client.ClientConnection] = None
        self._running = False
        self._listen_key: Optional[str] = None
        self._renew_task: Optional[asyncio.Task] = None
        # 事件处理函数注册表
        self._handlers: Dict[str, List[EventHandler]] = {}

    # ============================================================
    # 公共 API
    # ============================================================

    def on(self, event_type: str, handler: EventHandler) -> None:
        """
        注册事件处理函数

        Args:
            event_type: 事件类型, 如 "ORDER_TRADE_UPDATE", "BALANCE_UPDATE"
            handler: 异步处理函数
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    async def start(self) -> None:
        """启动 User Data Stream (阻塞运行)"""
        self._running = True
        logger.info("user_stream.starting")

        while self._running:
            try:
                # 获取 listenKey
                self._listen_key = await self._get_listen_key()
                if not self._listen_key:
                    logger.warning("user_stream.listen_key_failed")
                    await asyncio.sleep(10)
                    continue

                # 启动续期任务
                self._renew_task = asyncio.create_task(self._renew_loop())

                # 连接并监听
                await self._connect_and_listen()

            except Exception:
                logger.exception("user_stream.error")

            finally:
                if self._renew_task:
                    self._renew_task.cancel()
                    try:
                        await self._renew_task
                    except asyncio.CancelledError:
                        pass

            if self._running:
                logger.warning("user_stream.reconnecting", delay=5)
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """优雅停止"""
        self._running = False
        if self._renew_task:
            self._renew_task.cancel()
        if self._ws:
            await self._ws.close()
        logger.info("user_stream.stopped")

    # ============================================================
    # 内部实现
    # ============================================================

    async def _get_listen_key(self) -> Optional[str]:
        """通过 REST API 获取 listenKey"""
        result = await rest_client.create_listen_key()
        if result and "listenKey" in result:
            logger.info("user_stream.listen_key_obtained")
            return result["listenKey"]
        return None

    async def _renew_loop(self) -> None:
        """每 30 分钟续期 listenKey"""
        while True:
            await asyncio.sleep(30 * 60)
            try:
                await rest_client.keepalive_listen_key()
                logger.debug("user_stream.listen_key_renewed")
            except Exception:
                logger.exception("user_stream.renew_failed")

    def _build_ws_url(self) -> str:
        """构建 User Data Stream WebSocket URL"""
        if settings.binance_testnet:
            base = "wss://stream.binancefuture.com/ws"
        else:
            base = "wss://fstream.binance.com/ws"
        return f"{base}/{self._listen_key}"

    async def _connect_and_listen(self) -> None:
        """建立 WebSocket 连接并监听事件"""
        url = self._build_ws_url()
        logger.info("user_stream.connecting", url=url[:80])

        async with websockets.connect(
            url,
            ping_interval=settings.ws_ping_interval,
            ping_timeout=settings.ws_ping_timeout,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            logger.info("user_stream.connected")

            async for raw_message in ws:
                await self._handle_message(raw_message)

    async def _handle_message(self, raw_message: str) -> None:
        """
        解析并处理 User Data Stream 消息

        事件类型:
        - listenKeyExpired: listenKey 过期
        - BALANCE_UPDATE: 余额变更 (含资金费率扣除)
        - ORDER_TRADE_UPDATE: 订单状态更新
        """
        try:
            msg = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("user_stream.invalid_json", raw=raw_message[:200])
            return

        event_type = msg.get("e", "")
        event_time = msg.get("E", 0)

        # listenKey 过期 → 主动关闭 WS 触发外层重连 (重新获取 listenKey)
        if event_type == "listenKeyExpired":
            logger.warning("user_stream.listen_key_expired")
            if self._ws:
                await self._ws.close()
            return

        # 附加本地接收时间
        msg["local_recv_ts"] = time.time() * 1000

        logger.debug(
            "user_stream.event",
            event_type=event_type,
            event_time=event_time,
        )

        # 写入 Redis Streams
        try:
            await redis_client.xadd(STREAM_ACCOUNT, msg)
        except Exception:
            logger.exception("user_stream.redis_error", event_type=event_type)

        # 分发到注册的处理函数
        handlers = self._handlers.get(event_type, [])
        for handler in handlers:
            try:
                await handler(msg)
            except Exception:
                logger.exception(
                    "user_stream.handler_error", event_type=event_type
                )
