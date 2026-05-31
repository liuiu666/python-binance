"""
WebSocket 推送处理器
从 Redis Streams 订阅实时数据, 通过 WebSocket 转发至前端
"""

from __future__ import annotations

import asyncio
import json
from typing import Dict, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from common.logger import get_logger
from common.redis_client import redis_client

logger = get_logger(__name__)

router = APIRouter()


class ConnectionManager:
    """
    WebSocket 连接管理器
    管理所有活跃的 WebSocket 连接, 支持广播和按频道推送
    """

    def __init__(self) -> None:
        # 活跃连接集合
        self._connections: Set[WebSocket] = set()
        # 连接订阅的频道: {websocket: {channel1, channel2}}
        self._subscriptions: Dict[WebSocket, Set[str]] = {}

    async def connect(self, ws: WebSocket) -> None:
        """接受新的 WebSocket 连接"""
        await ws.accept()
        self._connections.add(ws)
        self._subscriptions[ws] = set()
        logger.info("ws_handler.connected", total=len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        """断开连接"""
        self._connections.discard(ws)
        self._subscriptions.pop(ws, None)
        logger.info("ws_handler.disconnected", total=len(self._connections))

    def subscribe(self, ws: WebSocket, channel: str) -> None:
        """订阅频道"""
        if ws in self._subscriptions:
            self._subscriptions[ws].add(channel)

    def unsubscribe(self, ws: WebSocket, channel: str) -> None:
        """取消订阅"""
        if ws in self._subscriptions:
            self._subscriptions[ws].discard(channel)

    async def broadcast(self, channel: str, data: dict) -> None:
        """向订阅了指定频道的所有连接推送消息"""
        message = json.dumps({"channel": channel, "data": data})
        disconnected = set()

        for ws in self._connections:
            if channel in self._subscriptions.get(ws, set()):
                try:
                    await ws.send_text(message)
                except Exception:
                    disconnected.add(ws)

        # 清理断开的连接
        for ws in disconnected:
            self.disconnect(ws)


# 全局连接管理器
manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    WebSocket 主端点

    协议:
    - 客户端连接后发送 {"action": "subscribe", "channel": "market:btcusdt"} 订阅频道
    - 服务端推送格式: {"channel": "xxx", "data": {...}}
    - 支持的频道: market:*, signal:trade, order:update, account:updates
    """
    await manager.connect(ws)

    try:
        while True:
            # 接收客户端消息 (订阅/取消订阅)
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                action = msg.get("action", "")
                channel = msg.get("channel", "")

                if action == "subscribe" and channel:
                    manager.subscribe(ws, channel)
                    await ws.send_text(json.dumps({
                        "type": "subscribed",
                        "channel": channel,
                    }))
                elif action == "unsubscribe" and channel:
                    manager.unsubscribe(ws, channel)
                    await ws.send_text(json.dumps({
                        "type": "unsubscribed",
                        "channel": channel,
                    }))
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "message": "无效 JSON"}))

    except WebSocketDisconnect:
        manager.disconnect(ws)


async def start_redis_forwarder() -> None:
    """
    启动 Redis → WebSocket 转发器
    从 Redis Streams 读取实时数据, 通过 ConnectionManager 推送到前端
    """
    import asyncio
    from common.config import settings as s
    from common.redis_client import STREAM_ORDER, STREAM_SIGNAL, STREAM_ACCOUNT, STREAM_MARKET, STREAM_TICKER, STREAM_DEPTH

    logger.info("ws_handler.forwarder_starting")

    async def forward_stream(stream: str, group: str, consumer: str):
        """转发单个 Stream"""
        await redis_client.create_group(stream, group, id="$")
        while True:
            try:
                results = await redis_client.xreadgroup(
                    group=group,
                    consumer=consumer,
                    streams={stream: ">"},
                    count=10,
                    block=2000,
                )
                if results:
                    for s_name, messages in results:
                        for msg_id, fields in messages:
                            await manager.broadcast(stream, fields)
                            await redis_client.xack(stream, group, msg_id)
            except Exception:
                logger.exception("ws_handler.forward_error", stream=stream)
                await asyncio.sleep(1)

    # 并发转发多个 Stream
    tasks = [
        asyncio.create_task(forward_stream(STREAM_ORDER, "api-ws-group", "ws-1")),
        asyncio.create_task(forward_stream(STREAM_SIGNAL, "api-ws-group", "ws-1")),
    ]
    for symbol in s.symbols:
        # K 线行情流 (1m 级别价格)
        stream_market = STREAM_MARKET.format(symbol=symbol.lower())
        tasks.append(
            asyncio.create_task(forward_stream(stream_market, "api-ws-group", "ws-1"))
        )
        # 高频盘口报价流 (最优买卖价 tick-by-tick)
        stream_ticker = STREAM_TICKER.format(symbol=symbol.lower())
        tasks.append(
            asyncio.create_task(forward_stream(stream_ticker, "api-ws-group", "ws-1"))
        )
        # 盘口深度流 (L2 Order Book)
        stream_depth = STREAM_DEPTH.format(symbol=symbol.lower())
        tasks.append(
            asyncio.create_task(forward_stream(stream_depth, "api-ws-group", "ws-1"))
        )

    await asyncio.gather(*tasks)
