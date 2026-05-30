"""
数据采集服务 — 启动入口
组装并启动所有采集子模块:
1. BinanceWSClient — 行情数据 WebSocket
2. BinanceRESTClient + DataCompensator — REST 校准
3. UserDataStream — 账户事件监听
4. HealthMonitor — 健康监控
5. ClickHouse — 异步持久化

启动方式: python -m collector.main
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from typing import Any, Dict

from common.config import settings
from common.logger import get_logger
from common.redis_client import redis_client, STREAM_MARKET
from common.clickhouse import clickhouse_client
from common.notify import notifier

from collector.ws_client import BinanceWSClient
from collector.rest_client import rest_client, DataCompensator
from collector.user_stream import UserDataStream
from collector.health import HealthMonitor

logger = get_logger(__name__)


class CollectorService:
    """
    数据采集服务编排器
    负责初始化所有组件, 注册消息处理链路, 并管理生命周期
    """

    def __init__(self) -> None:
        self._ws_client = BinanceWSClient()
        self._compensator = DataCompensator(rest_client)
        self._user_stream = UserDataStream()
        self._health = HealthMonitor()
        self._running = False

    async def start(self) -> None:
        """启动所有组件"""
        self._running = True
        logger.info("collector.starting", symbols=settings.symbols)

        # ---- 初始化连接 ----
        await redis_client.connect()
        await rest_client.connect()

        # ClickHouse 连接 + 建表 (非阻塞, 失败不阻塞主流程)
        try:
            clickhouse_client.connect()
            await clickhouse_client.ensure_tables()
            await clickhouse_client.start_flush_loop()
        except Exception:
            logger.exception("collector.clickhouse_init_failed")
            logger.warning("collector.clickhouse_disabled")

        # ---- 注册消息处理函数 ----
        self._register_handlers()

        # ---- 启动子服务 ----
        tasks = []

        # 健康监控 (含 HTTP 端点)
        tasks.append(asyncio.create_task(
            self._health.start(http_port=8080),
            name="health-monitor",
        ))

        # REST 校准器
        tasks.append(asyncio.create_task(
            self._compensator.start(),
            name="data-compensator",
        ))

        # User Data Stream (需要 API Key)
        if settings.binance_api_key:
            tasks.append(asyncio.create_task(
                self._user_stream.start(),
                name="user-data-stream",
            ))
        else:
            logger.warning("collector.user_stream_skipped", reason="no_api_key")

        # WebSocket 行情 (主数据源, 最后启动)
        tasks.append(asyncio.create_task(
            self._ws_client.start(),
            name="ws-client",
        ))

        logger.info("collector.all_started")

        # ---- 等待停止信号 ----
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self._stop_all(tasks)

    async def _stop_all(self, tasks: list) -> None:
        """优雅停止所有组件"""
        logger.info("collector.stopping")
        self._running = False

        # 停止 WebSocket
        await self._ws_client.stop()

        # 停止校准器
        await self._compensator.stop()

        # 停止 User Stream
        await self._user_stream.stop()

        # 停止健康监控
        await self._health.stop()

        # 刷新 ClickHouse 缓冲区
        await clickhouse_client.stop_flush_loop()
        clickhouse_client.close()

        # 关闭连接
        await rest_client.close()
        await redis_client.close()

        # 取消残留任务
        for task in tasks:
            if not task.done():
                task.cancel()

        logger.info("collector.stopped")

    # ============================================================
    # 消息处理链路注册
    # ============================================================

    def _register_handlers(self) -> None:
        """注册各类型消息的处理函数"""

        # K 线消息 → Redis + ClickHouse + 健康监控 + 校准缓存
        self._ws_client.on("kline", self._handle_kline)

        # 成交明细 → Redis + ClickHouse
        self._ws_client.on("aggTrade", self._handle_agg_trade)

        # 深度数据 → Redis
        self._ws_client.on("depth", self._handle_depth)

        # 最优挂单 → Redis
        self._ws_client.on("bookTicker", self._handle_book_ticker)

        # 账户事件 → 日志
        self._user_stream.on("ORDER_TRADE_UPDATE", self._handle_order_update)
        self._user_stream.on("BALANCE_UPDATE", self._handle_balance_update)

    async def _handle_kline(self, data: Dict[str, Any]) -> None:
        """
        处理 K 线消息

        币安 K 线数据格式:
        {
            "e": "kline",
            "k": {
                "s": "BTCUSDT",
                "i": "1m",
                "t": 1717000000000,    // 开盘时间
                "T": 1717000059999,    // 收盘时间
                "o": "68000.00",       // 开盘价
                "h": "68100.00",       // 最高价
                "l": "67900.00",       // 最低价
                "c": "68050.00",       // 收盘价
                "v": "123.456",        // 成交量
                "x": false,            // 是否闭合
                ...
            }
        }
        """
        k = data.get("k", {})
        symbol = k.get("s", "")
        is_closed = k.get("x", False)

        kline = {
            "symbol": symbol,
            "interval": k.get("i", "1m"),
            "open_time": k.get("t", 0),
            "close_time": k.get("T", 0),
            "open_price": float(k.get("o", 0)),
            "high_price": float(k.get("h", 0)),
            "low_price": float(k.get("l", 0)),
            "close_price": float(k.get("c", 0)),
            "volume": float(k.get("v", 0)),
            "quote_volume": float(k.get("q", 0)),
            "trades_count": int(k.get("n", 0)),
            "taker_buy_volume": float(k.get("V", 0)),
            "is_closed": is_closed,
            "local_recv_ts": data.get("local_recv_ts", time.time() * 1000),
        }

        # 更新健康监控
        self._health.record_message(
            symbol, close_price=kline["close_price"], kline_time=kline["open_time"]
        )

        # 写入 Redis Streams
        stream_name = STREAM_MARKET.format(symbol=symbol.lower())
        await redis_client.xadd(stream_name, kline, maxlen=10000)

        # K 线闭合时写入 ClickHouse 和更新校准缓存
        if is_closed:
            await clickhouse_client.insert("klines", {
                k: v for k, v in kline.items() if k != "is_closed"
            })
            self._compensator.update_kline_cache(symbol, kline)

            logger.debug(
                "collector.kline_closed",
                symbol=symbol,
                close=kline["close_price"],
                volume=kline["volume"],
            )

    async def _handle_agg_trade(self, data: Dict[str, Any]) -> None:
        """处理归集成交消息"""
        trade = {
            "symbol": data.get("s", ""),
            "agg_trade_id": data.get("a", 0),
            "price": float(data.get("p", 0)),
            "quantity": float(data.get("q", 0)),
            "timestamp": data.get("T", 0),
            "is_buyer_maker": data.get("m", False),
            "local_recv_ts": data.get("local_recv_ts", time.time() * 1000),
        }

        # 更新健康监控
        self._health.record_message(trade["symbol"])

        # 写入 ClickHouse (异步批量)
        await clickhouse_client.insert("agg_trades", trade)

    async def _handle_depth(self, data: Dict[str, Any]) -> None:
        """处理深度数据"""
        symbol = data.get("s", "")
        self._health.record_message(symbol)

        stream_name = STREAM_MARKET.format(symbol=symbol.lower())
        await redis_client.xadd(
            stream_name,
            {"type": "depth", "data": data},
            maxlen=5000,
        )

    async def _handle_book_ticker(self, data: Dict[str, Any]) -> None:
        """处理最优挂单数据"""
        symbol = data.get("s", "")
        self._health.record_message(symbol)

    async def _handle_order_update(self, data: Dict[str, Any]) -> None:
        """处理订单状态更新"""
        order_data = data.get("o", {})
        logger.info(
            "collector.order_update",
            symbol=order_data.get("s"),
            status=order_data.get("X"),
            order_id=order_data.get("i"),
        )

    async def _handle_balance_update(self, data: Dict[str, Any]) -> None:
        """处理余额变更"""
        logger.info(
            "collector.balance_update",
            asset=data.get("a"),
            balance_delta=data.get("d"),
            reason=data.get("T"),
        )


async def main() -> None:
    """服务主入口"""
    service = CollectorService()

    # 注册信号处理 (优雅退出)
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(service)))

    try:
        await service.start()
    except KeyboardInterrupt:
        logger.info("collector.keyboard_interrupt")
    finally:
        if service._running:
            await service._stop_all([])


async def _shutdown(service: CollectorService) -> None:
    """信号触发的优雅退出"""
    logger.info("collector.shutdown_signal")
    service._running = False


if __name__ == "__main__":
    asyncio.run(main())
