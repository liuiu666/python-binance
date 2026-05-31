"""
币安合约 WebSocket 客户端
功能:
- 多流复用: 单连接订阅 aggTrade / kline / depth / bookTicker
- Ping-Pong 心跳: 自动维护连接活跃
- 指数退避重连: 1s → 2s → 4s → ... → 60s 封顶
- 24h 热切换: 运行 23h55min 后主动建立新连接, 旧连接平滑关闭
- 消息分发: 按 stream 类型分发到对应处理函数
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional

import websockets
from websockets.asyncio.client import ClientConnection

from common.config import settings
from common.logger import get_logger

logger = get_logger(__name__)

# 消息处理函数类型: 接收解析后的消息字典
MessageHandler = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]


class BinanceWSClient:
    """
    币安合约 WebSocket 客户端

    使用方式:
        client = BinanceWSClient(symbols=["BTCUSDT", "ETHUSDT"])
        client.on("kline", my_kline_handler)
        client.on("aggTrade", my_trade_handler)
        await client.start()
    """

    def __init__(self, symbols: Optional[List[str]] = None) -> None:
        self._symbols: List[str] = list(symbols or settings.symbols)

        # WebSocket 连接实例
        self._ws: Optional[ClientConnection] = None
        self._running = False

        # 重连参数
        self._reconnect_delay = settings.ws_reconnect_base
        self._reconnect_max = settings.ws_reconnect_max

        # 24h 热切换
        self._connected_at: float = 0.0
        self._hot_swap_task: Optional[asyncio.Task] = None

        # 消息处理函数注册表: {stream_type: [handler, ...]}
        self._handlers: Dict[str, List[MessageHandler]] = {}

        # 最后一条消息时间 (用于心跳检测)
        self._last_message_ts: float = 0.0

        # WS 请求 ID 计数器 (SUBSCRIBE/UNSUBSCRIBE 需要)
        self._req_id: int = 0

    # ============================================================
    # 公共 API
    # ============================================================

    def on(self, stream_type: str, handler: MessageHandler) -> None:
        """
        注册消息处理函数

        Args:
            stream_type: 消息类型, 如 "kline", "aggTrade", "depth", "bookTicker"
            handler: 异步处理函数
        """
        if stream_type not in self._handlers:
            self._handlers[stream_type] = []
        self._handlers[stream_type].append(handler)

    async def start(self) -> None:
        """启动 WebSocket 客户端 (阻塞运行)"""
        self._running = True
        logger.info("ws.starting", symbols=self._symbols)

        while self._running:
            try:
                await self._connect_and_listen()
            except Exception:
                logger.exception("ws.unexpected_error")

            if self._running:
                # 指数退避重连
                logger.warning(
                    "ws.reconnecting",
                    delay=self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._reconnect_max
                )

    async def stop(self) -> None:
        """优雅停止"""
        self._running = False
        if self._hot_swap_task:
            self._hot_swap_task.cancel()
        if self._ws:
            await self._ws.close()
        logger.info("ws.stopped")

    @property
    def last_message_age(self) -> float:
        """距离最后一条消息的秒数"""
        if self._last_message_ts == 0:
            return float("inf")
        return time.monotonic() - self._last_message_ts

    @property
    def symbols(self) -> List[str]:
        """当前监控的交易对列表"""
        return list(self._symbols)

    async def add_symbol(self, symbol: str) -> bool:
        """
        动态添加交易对 — 无需重连, 通过 Binance WS SUBSCRIBE 指令实现热添加

        Args:
            symbol: 大写交易对, 如 "ETHUSDT"

        Returns:
            True 表示成功发送订阅请求, False 表示当前无连接 (将在下次重连时生效)
        """
        symbol = symbol.upper()
        if symbol in self._symbols:
            logger.info("ws.symbol_already_subscribed", symbol=symbol)
            return True

        self._symbols.append(symbol)
        streams = self._symbol_streams(symbol)

        if self._ws is None:
            logger.warning("ws.add_symbol_no_connection", symbol=symbol,
                           note="将在下次重连时自动订阅")
            return False

        self._req_id += 1
        payload = json.dumps({
            "method": "SUBSCRIBE",
            "params": streams,
            "id": self._req_id,
        })
        try:
            await self._ws.send(payload)
            logger.info("ws.symbol_subscribed", symbol=symbol, streams=streams)
            return True
        except Exception:
            logger.exception("ws.subscribe_error", symbol=symbol)
            return False

    async def remove_symbol(self, symbol: str) -> bool:
        """
        动态移除交易对 — 通过 Binance WS UNSUBSCRIBE 指令热移除

        Args:
            symbol: 大写交易对, 如 "ETHUSDT"

        Returns:
            True 表示成功发送取消订阅请求
        """
        symbol = symbol.upper()
        if symbol not in self._symbols:
            return True

        self._symbols.remove(symbol)
        streams = self._symbol_streams(symbol)

        if self._ws is None:
            return False

        self._req_id += 1
        payload = json.dumps({
            "method": "UNSUBSCRIBE",
            "params": streams,
            "id": self._req_id,
        })
        try:
            await self._ws.send(payload)
            logger.info("ws.symbol_unsubscribed", symbol=symbol)
            return True
        except Exception:
            logger.exception("ws.unsubscribe_error", symbol=symbol)
            return False

    # ============================================================
    # 内部实现
    # ============================================================

    def _symbol_streams(self, symbol: str) -> List[str]:
        """返回某个交易对需要订阅的所有流名称"""
        s = symbol.lower()
        return [
            f"{s}@aggTrade",
            f"{s}@kline_1m",
            f"{s}@depth20@500ms",
            f"{s}@bookTicker",
            f"{s}@markPrice@3s",
        ]

    def _build_url(self) -> str:
        """
        构建多流复用 WebSocket URL
        格式: wss://fstream.binance.com/stream?streams=stream1/stream2/...

        每个交易对订阅:
        - aggTrade       归集成交流 (量价分析)
        - kline_1m       1 分钟 K 线 (主策略周期)
        - depth20@500ms  20 档盘口深度 (支撑阻力)
        - bookTicker     最优买卖价 (滑点估算)
        - markPrice@3s   标记价格 (强平/资金费率, 每 3 秒)
        多周期 K 线 (5m/15m) 通过 REST API 补偿器获取, 不走 WS
        """
        streams: List[str] = []
        for symbol in self._symbols:
            streams.extend(self._symbol_streams(symbol))
        stream_str = "/".join(streams)
        return f"{settings.binance_ws_base_url.replace('/ws', '')}/stream?streams={stream_str}"

    async def _connect_and_listen(self) -> None:
        """建立连接并持续监听消息"""
        url = self._build_url()
        logger.info("ws.connecting", url=url[:120])

        async with websockets.connect(
            url,
            ping_interval=settings.ws_ping_interval,
            ping_timeout=settings.ws_ping_timeout,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._connected_at = time.monotonic()
            self._reconnect_delay = settings.ws_reconnect_base
            logger.info("ws.connected")

            # 启动 24h 热切换调度
            self._hot_swap_task = asyncio.create_task(self._schedule_hot_swap())

            try:
                async for raw_message in ws:
                    self._last_message_ts = time.monotonic()
                    await self._dispatch(raw_message)
            except websockets.ConnectionClosed as e:
                logger.warning("ws.connection_closed", code=e.code, reason=e.reason)
            finally:
                if self._hot_swap_task:
                    self._hot_swap_task.cancel()
                    try:
                        await self._hot_swap_task
                    except asyncio.CancelledError:
                        pass

    async def _dispatch(self, raw_message: str) -> None:
        """
        解析并分发消息到对应的处理函数

        币安多流复用格式:
        {
            "stream": "btcusdt@kline_1m",
            "data": { ... }
        }

        SUBSCRIBE/UNSUBSCRIBE 响应 (静默忽略):
        {"result": null, "id": 1}
        """
        try:
            msg = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("ws.invalid_json", raw=raw_message[:200])
            return

        # 币安 SUBSCRIBE / UNSUBSCRIBE 指令的响应包含 "result" 字段, 直接忽略
        if "result" in msg:
            req_id = msg.get("id")
            err = msg.get("error")
            if err:
                logger.warning("ws.subscribe_response_error", id=req_id, error=err)
            else:
                logger.debug("ws.subscribe_response_ok", id=req_id)
            return

        stream_name = msg.get("stream", "")
        data = msg.get("data", {})

        if not stream_name or not data:
            return

        # 提取事件类型:
        # - "btcusdt@kline_1m" -> "kline"
        # - "btcusdt@depth20@500ms" -> "depth"
        # - "btcusdt@markPrice@3s" -> "markPrice"
        parts = stream_name.split("@")
        if len(parts) < 2:
            return

        # 取得事件第一部分，如 "kline_1m" -> "kline", "depth20" -> "depth20", "markPrice" -> "markPrice"
        raw_event = parts[1].split("_")[0]
        # 兼容处理带数字的深度流名称，如 "depth20" -> "depth"
        if raw_event.startswith("depth"):
            event_type = "depth"
        else:
            event_type = raw_event

        # 附加本地接收时间戳
        data["local_recv_ts"] = time.time() * 1000

        # 分发到注册的处理函数
        handlers = self._handlers.get(event_type, [])
        for handler in handlers:
            try:
                await handler(data)
            except Exception:
                logger.exception("ws.handler_error", event_type=event_type)

    async def _schedule_hot_swap(self) -> None:
        """
        24h 热切换调度器
        币安 WS 每 24h 强制断连, 提前主动关闭当前连接,
        触发外层 while 循环自动重连, 避免数据中断

        修复说明: 旧方案通过替换 self._ws 引用来切换连接,
        但 async for 循环绑定的仍是旧 ws 局部变量, 替换无效。
        正确做法是主动关闭当前连接, 让 _connect_and_listen 正常退出,
        外层 while self._running 循环会自动重连。
        """
        offset = settings.ws_24h_reconnect_offset
        wait_seconds = 24 * 3600 - offset
        await asyncio.sleep(wait_seconds)

        if not self._running:
            return

        logger.info("ws.hot_swap_triggering")
        # 主动关闭当前连接, 外层 while 循环会自动重连
        if self._ws:
            await self._ws.close()
