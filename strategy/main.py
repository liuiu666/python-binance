"""
策略引擎 — 启动入口
从 Redis Streams 消费行情数据, 计算指标, 运行策略, 输出交易信号

启动方式: python -m strategy.main
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from typing import Any, Dict, List, Optional

from common.config import settings
from common.logger import get_logger
from common.redis_client import (
    redis_client,
    STREAM_MARKET,
    STREAM_SIGNAL,
    GROUP_STRATEGY,
)
from strategy.ring_buffer import BufferManager
from strategy.indicators import compute_all_indicators, get_latest_indicators
from strategy.base_strategy import BaseStrategy, Signal
from strategy.strategies.ema_cross import EMACrossStrategy
from strategy.strategies.breakout import BreakoutStrategy

logger = get_logger(__name__)

# 消费者名称
CONSUMER_NAME = "strategy-1"


class StrategyEngine:
    """
    策略引擎服务编排器
    负责消费行情 → 更新缓冲区 → 计算指标 → 运行策略 → 发布信号
    """

    def __init__(self) -> None:
        self._buffer_mgr = BufferManager(buffer_size=500)
        self._strategies: List[BaseStrategy] = []
        self._running = False

    def register_strategy(self, strategy: BaseStrategy) -> None:
        """
        注册策略实例

        Args:
            strategy: 策略实例
        """
        self._strategies.append(strategy)
        logger.info(
            "strategy.registered",
            name=strategy.name,
            symbols=strategy._symbols,
        )

    async def start(self) -> None:
        """启动策略引擎"""
        self._running = True
        logger.info("strategy.starting", symbols=settings.symbols)

        # ---- 注册默认策略 ----
        if not self._strategies:
            self.register_strategy(EMACrossStrategy(
                symbols=settings.symbols,
                quantity=0.001,
                leverage=1,
            ))
            self.register_strategy(BreakoutStrategy(
                symbols=settings.symbols,
                quantity=0.001,
                leverage=1,
            ))

        # ---- 初始化连接 ----
        await redis_client.connect()

        # ---- 创建消费者组 ----
        for symbol in settings.symbols:
            stream = STREAM_MARKET.format(symbol=symbol.lower())
            await redis_client.create_group(stream, GROUP_STRATEGY, id="$")

        # ---- 启动 AI 参数监听 ----
        ai_task = asyncio.create_task(
            self._watch_ai_params(),
            name="ai-param-watcher",
        )

        # ---- 启动控制指令监听 (暂停/恢复) ----
        control_task = asyncio.create_task(
            self._watch_control_commands(),
            name="control-watcher",
        )

        # ---- 主循环: 消费行情 ----
        consume_task = asyncio.create_task(
            self._consume_market_data(),
            name="market-consumer",
        )

        logger.info(
            "strategy.all_started",
            strategies=[s.name for s in self._strategies],
        )

        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            for task in [ai_task, control_task, consume_task]:
                if not task.done():
                    task.cancel()
            await redis_client.close()
            logger.info("strategy.stopped")

    async def stop(self) -> None:
        """停止策略引擎"""
        self._running = False

    # ============================================================
    # 行情消费
    # ============================================================

    async def _consume_market_data(self) -> None:
        """从 Redis Streams 消费行情数据"""
        # 构建消费流映射
        streams = {}
        for symbol in settings.symbols:
            stream = STREAM_MARKET.format(symbol=symbol.lower())
            streams[stream] = ">"

        logger.info("strategy.consuming", streams=list(streams.keys()))

        while self._running:
            try:
                results = await redis_client.xreadgroup(
                    group=GROUP_STRATEGY,
                    consumer=CONSUMER_NAME,
                    streams=streams,
                    count=50,
                    block=5000,
                )

                if not results:
                    continue

                for stream_name, messages in results:
                    for msg_id, fields in messages:
                        await self._process_market_message(
                            stream_name, msg_id, fields
                        )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("strategy.consume_error")
                await asyncio.sleep(1)

    async def _process_market_message(
        self, stream_name: str, msg_id: str, fields: Dict[str, str]
    ) -> None:
        """
        处理单条行情消息

        只处理 K 线闭合事件 (is_closed=true), 触发策略计算
        """
        try:
            # 解析消息类型
            msg_type = fields.get("type", "kline")

            if msg_type == "kline":
                # K 线消息
                is_closed = fields.get("is_closed", "False") == "True"
                symbol = fields.get("symbol", "")

                # 更新缓冲区
                kline = self._parse_kline(fields)
                self._buffer_mgr.update(symbol, kline)

                # K 线闭合时触发策略
                if is_closed:
                    await self._on_kline_closed(symbol)

            # 确认消息
            await redis_client.xack(stream_name, GROUP_STRATEGY, msg_id)

        except Exception:
            logger.exception("strategy.process_error", msg_id=msg_id)

    def _parse_kline(self, fields: Dict[str, str]) -> Dict:
        """将 Redis 中的字符串字段转为 K 线字典"""
        return {
            "open_time": int(fields.get("open_time", 0)),
            "close_time": int(fields.get("close_time", 0)),
            "open_price": float(fields.get("open_price", 0)),
            "high_price": float(fields.get("high_price", 0)),
            "low_price": float(fields.get("low_price", 0)),
            "close_price": float(fields.get("close_price", 0)),
            "volume": float(fields.get("volume", 0)),
            "is_closed": fields.get("is_closed", "False") == "True",
        }

    # ============================================================
    # 策略执行
    # ============================================================

    async def _on_kline_closed(self, symbol: str) -> None:
        """
        K 线闭合时执行所有注册的策略

        Args:
            symbol: 交易对
        """
        buf = self._buffer_mgr.get_buffer(symbol)
        df = buf.df

        if df.empty or len(df) < 30:
            return

        # 计算指标
        df = compute_all_indicators(df)

        # 依次运行所有策略
        for strategy in self._strategies:
            if not strategy.enabled:
                continue
            if strategy._symbols and symbol not in strategy._symbols:
                continue

            try:
                signal = await strategy.on_kline(symbol, df)
                if signal:
                    await self._publish_signal(signal)
            except Exception:
                logger.exception(
                    "strategy.execution_error",
                    strategy=strategy.name,
                    symbol=symbol,
                )

    async def _publish_signal(self, signal: Signal) -> None:
        """
        将交易信号发布到 Redis Streams

        Args:
            signal: 交易信号
        """
        signal_dict = signal.to_dict()
        await redis_client.xadd(STREAM_SIGNAL, signal_dict, maxlen=10000)

        logger.info(
            "strategy.signal_published",
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            action=signal.action,
            side=signal.side,
            strategy=signal.strategy,
        )

    # ============================================================
    # AI 参数监听
    # ============================================================

    async def _watch_ai_params(self) -> None:
        """
        定期从 Redis 读取 AI 模块写入的参数建议和情绪指数
        更新到对应的策略实例
        """
        while self._running:
            try:
                for strategy in self._strategies:
                    # 读取情绪指数
                    for symbol in settings.symbols:
                        sentiment_key = f"ai:sentiment:{symbol}"
                        val = await redis_client.get(sentiment_key)
                        if val:
                            try:
                                data = json.loads(val)
                                strategy.set_sentiment(
                                    data.get("sentiment_score", 0)
                                )
                            except json.JSONDecodeError:
                                pass

                    # 读取参数建议
                    param_key = f"ai:params:{strategy.name}"
                    val = await redis_client.get(param_key)
                    if val:
                        try:
                            params = json.loads(val)
                            strategy.set_ai_params(params)
                        except json.JSONDecodeError:
                            pass

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("strategy.ai_params_error")

            await asyncio.sleep(60)

    # ============================================================
    # 控制指令监听 (暂停/恢复)
    # ============================================================

    async def _watch_control_commands(self) -> None:
        """
        订阅 Redis Pub/Sub 频道 control:strategy
        接收暂停/恢复指令, 批量切换所有策略的启用状态
        """
        import asyncio as _asyncio
        pubsub = redis_client.client.pubsub()
        await pubsub.subscribe("control:strategy")
        logger.info("strategy.control_subscribed")

        try:
            while self._running:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message.get("type") == "message":
                    # redis_client 已设 decode_responses=True, data 直接是 str
                    action = str(message.get("data", ""))

                    if action == "PAUSE":
                        for s in self._strategies:
                            s.disable()
                        logger.warning("strategy.paused_by_control")
                    elif action == "RESUME":
                        for s in self._strategies:
                            s.enable()
                        logger.warning("strategy.resumed_by_control")
                await _asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("strategy.control_error")
        finally:
            await pubsub.unsubscribe("control:strategy")
            await pubsub.close()


async def main() -> None:
    """服务主入口"""
    engine = StrategyEngine()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(engine)))

    try:
        await engine.start()
    except KeyboardInterrupt:
        logger.info("strategy.keyboard_interrupt")
    finally:
        if engine._running:
            await engine.stop()


async def _shutdown(engine: StrategyEngine) -> None:
    logger.info("strategy.shutdown_signal")
    engine._running = False


if __name__ == "__main__":
    asyncio.run(main())
