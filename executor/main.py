"""
交易执行器 — 启动入口
从 Redis Streams 消费交易信号, 经风控校验后调用币安 API 下单
同时管理持仓同步和账户状态

启动方式: python -m executor.main
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from typing import Any, Dict, Optional

from common.config import settings
from common.logger import get_logger
from common.redis_client import (
    redis_client,
    STREAM_SIGNAL,
    STREAM_ACCOUNT,
    GROUP_EXECUTOR,
)
from common.db import db
from collector.rest_client import rest_client

from executor.risk_manager import RiskManager
from executor.order_manager import OrderManager
from executor.position_sync import PositionSync

logger = get_logger(__name__)

# 消费者名称 (同一消费者组内唯一标识)
CONSUMER_NAME = "executor-1"


class ExecutorService:
    """
    交易执行器服务编排器
    负责初始化所有组件, 从 Redis Streams 消费信号, 编排风控→下单流程
    """

    def __init__(self) -> None:
        self._risk_mgr = RiskManager()
        self._order_mgr = OrderManager()
        self._pos_sync = PositionSync(risk_manager=self._risk_mgr)
        self._running = False

    async def start(self) -> None:
        """启动执行器"""
        self._running = True
        logger.info("executor.starting")

        # ---- 初始化连接 ----
        await redis_client.connect()
        await rest_client.connect()
        await db.connect()
        await db.ensure_tables()

        # ---- 创建消费者组 ----
        await redis_client.create_group(STREAM_SIGNAL, GROUP_EXECUTOR, id="$")
        await redis_client.create_group(STREAM_ACCOUNT, GROUP_EXECUTOR, id="$")

        # ---- 同步初始状态 ----
        await self._sync_initial_state()

        # ---- 启动子服务 ----
        await self._order_mgr.start()
        await self._pos_sync.start()

        # ---- 启动账户事件监听 ----
        account_task = asyncio.create_task(
            self._consume_account_updates(),
            name="account-consumer",
        )

        # ---- 主循环: 消费交易信号 ----
        signal_task = asyncio.create_task(
            self._consume_signals(),
            name="signal-consumer",
        )

        logger.info("executor.all_started")

        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self._stop_all([account_task, signal_task])

    async def _stop_all(self, tasks: list) -> None:
        """优雅停止所有组件"""
        logger.info("executor.stopping")
        self._running = False

        await self._order_mgr.stop()
        await self._pos_sync.stop()

        for task in tasks:
            if not task.done():
                task.cancel()

        await rest_client.close()
        await db.close()
        await redis_client.close()

        logger.info("executor.stopped")

    # ============================================================
    # 初始状态同步
    # ============================================================

    async def _sync_initial_state(self) -> None:
        """启动时从交易所同步账户状态"""
        account = await rest_client.get_account()
        if account:
            balance = float(account.get("totalWalletBalance", 0))
            equity = float(account.get("totalCrossWalletBalance", 0))
            self._risk_mgr.update_state(
                balance=balance,
                equity=equity,
            )
            logger.info(
                "executor.initial_state",
                balance=balance,
                equity=equity,
            )

        # 同步持仓
        positions = await rest_client.get_positions()
        if positions:
            open_positions = [
                p for p in positions
                if abs(float(p.get("positionAmt", 0))) > 0
            ]
            position_sides = {}
            for p in open_positions:
                amt = float(p.get("positionAmt", 0))
                symbol = p.get("symbol", "")
                position_sides[symbol] = "BUY" if amt > 0 else "SELL"

            self._risk_mgr.update_state(
                open_positions=len(open_positions),
                position_sides=position_sides,
            )
            logger.info(
                "executor.positions_synced",
                count=len(open_positions),
                symbols=list(position_sides.keys()),
            )

    # ============================================================
    # 信号消费
    # ============================================================

    async def _consume_signals(self) -> None:
        """从 Redis Streams 消费交易信号"""
        logger.info("executor.signal_consumer_started")

        while self._running:
            try:
                results = await redis_client.xreadgroup(
                    group=GROUP_EXECUTOR,
                    consumer=CONSUMER_NAME,
                    streams={STREAM_SIGNAL: ">"},
                    count=10,
                    block=5000,
                )

                if not results:
                    continue

                for stream_name, messages in results:
                    for msg_id, fields in messages:
                        await self._process_signal(msg_id, fields)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("executor.signal_consume_error")
                await asyncio.sleep(1)

    async def _process_signal(self, msg_id: str, fields: Dict[str, str]) -> None:
        """
        处理单条交易信号

        流程: 解析 → 风控检查 → 下单 → 确认消息
        """
        try:
            # 解析信号 (可能包含 JSON 序列化的字段)
            signal: Dict[str, Any] = {}
            for k, v in fields.items():
                try:
                    signal[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    signal[k] = v

            signal_id = signal.get("signal_id", "")
            symbol = signal.get("symbol", "")
            action = signal.get("action", "")
            side = signal.get("side", "")

            logger.info(
                "executor.signal_received",
                signal_id=signal_id,
                symbol=symbol,
                action=action,
                side=side,
            )

            # ---- 风控检查 ----
            risk_result = await self._risk_mgr.check_signal(signal)
            if not risk_result.passed:
                logger.warning(
                    "executor.signal_rejected",
                    signal_id=signal_id,
                    rule=risk_result.rule_name,
                    reason=risk_result.reason,
                )
                await redis_client.xack(STREAM_SIGNAL, GROUP_EXECUTOR, msg_id)
                return

            # ---- 执行下单 ----
            result = await self._order_mgr.place_order(signal)

            # ---- 确认消息 ----
            await redis_client.xack(STREAM_SIGNAL, GROUP_EXECUTOR, msg_id)

        except Exception:
            logger.exception("executor.signal_process_error", msg_id=msg_id)

    # ============================================================
    # 账户事件消费
    # ============================================================

    async def _consume_account_updates(self) -> None:
        """从 Redis Streams 消费账户事件 (订单状态更新等)"""
        logger.info("executor.account_consumer_started")

        while self._running:
            try:
                results = await redis_client.xreadgroup(
                    group=GROUP_EXECUTOR,
                    consumer=CONSUMER_NAME,
                    streams={STREAM_ACCOUNT: ">"},
                    count=10,
                    block=5000,
                )

                if not results:
                    continue

                for stream_name, messages in results:
                    for msg_id, fields in messages:
                        await self._process_account_event(msg_id, fields)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("executor.account_consume_error")
                await asyncio.sleep(1)

    async def _process_account_event(
        self, msg_id: str, fields: Dict[str, str]
    ) -> None:
        """处理账户事件"""
        event_type = fields.get("e", "")

        if event_type == "ORDER_TRADE_UPDATE":
            await self._order_mgr.handle_order_update(fields)

        elif event_type == "BALANCE_UPDATE":
            asset = fields.get("a", "")
            delta = fields.get("d", "")
            logger.info(
                "executor.balance_update",
                asset=asset,
                delta=delta,
            )

        await redis_client.xack(STREAM_ACCOUNT, GROUP_EXECUTOR, msg_id)


async def main() -> None:
    """服务主入口"""
    service = ExecutorService()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(service)))

    try:
        await service.start()
    except KeyboardInterrupt:
        logger.info("executor.keyboard_interrupt")
    finally:
        if service._running:
            await service._stop_all([])


async def _shutdown(service: ExecutorService) -> None:
    logger.info("executor.shutdown_signal")
    service._running = False


if __name__ == "__main__":
    asyncio.run(main())
