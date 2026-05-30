"""
持仓同步模块
功能:
- 每 60s 从币安 REST 拉取实际持仓, 与本地 PostgreSQL 比对
- 差异 → 记录告警日志 + 推送钉钉通知
- 同步更新风控模块的持仓状态
- 更新本地持仓快照表
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from common.config import settings
from common.logger import get_logger
from common.db import db
from common.notify import notifier
from collector.rest_client import rest_client

logger = get_logger(__name__)

# 同步间隔 (秒)
SYNC_INTERVAL = 60


class PositionSync:
    """
    持仓同步器
    定期从交易所获取实际持仓, 与本地状态比对并同步
    """

    def __init__(self, risk_manager: Any = None) -> None:
        self._risk_manager = risk_manager
        self._running = False
        self._sync_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """启动持仓同步"""
        self._running = True
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info("position_sync.started", interval=SYNC_INTERVAL)

    async def stop(self) -> None:
        """停止同步"""
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        logger.info("position_sync.stopped")

    # ============================================================
    # 同步循环
    # ============================================================

    async def _sync_loop(self) -> None:
        """定期同步持仓"""
        while self._running:
            try:
                await self._sync_once()
            except Exception:
                logger.exception("position_sync.error")

            await asyncio.sleep(SYNC_INTERVAL)

    async def _sync_once(self) -> None:
        """执行一次完整的持仓同步"""
        # 从交易所获取实际持仓
        remote_positions = await self._fetch_remote_positions()
        if remote_positions is None:
            logger.warning("position_sync.remote_fetch_failed")
            return

        # 过滤出有仓位的 (positionAmt != 0)
        active_positions = [
            p for p in remote_positions
            if abs(float(p.get("positionAmt", 0))) > 0
        ]

        # 从数据库获取本地持仓
        local_positions = await self._fetch_local_positions()

        # 比对并同步
        await self._compare_and_sync(active_positions, local_positions)

        # 更新风控模块状态
        if self._risk_manager:
            position_sides = {}
            for p in active_positions:
                amt = float(p.get("positionAmt", 0))
                symbol = p.get("symbol", "")
                position_sides[symbol] = "BUY" if amt > 0 else "SELL"

            self._risk_manager.update_state(
                open_positions=len(active_positions),
                position_sides=position_sides,
            )

        logger.debug(
            "position_sync.completed",
            remote_count=len(active_positions),
            local_count=len(local_positions),
        )

    # ============================================================
    # 数据获取
    # ============================================================

    async def _fetch_remote_positions(self) -> Optional[List[Dict]]:
        """从币安 API 获取实际持仓"""
        return await rest_client.get_positions()

    async def _fetch_local_positions(self) -> List[Dict]:
        """从 PostgreSQL 获取本地持仓快照"""
        try:
            async with db.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT symbol, side, quantity, entry_price, "
                    "unrealized_pnl FROM positions"
                )
                return [dict(row) for row in rows]
        except Exception:
            logger.exception("position_sync.local_fetch_error")
            return []

    # ============================================================
    # 比对与同步
    # ============================================================

    async def _compare_and_sync(
        self,
        remote: List[Dict],
        local: List[Dict],
    ) -> None:
        """
        比对远程和本地持仓, 发现差异时告警并同步

        Args:
            remote: 交易所持仓列表
            local: 本地数据库持仓列表
        """
        remote_map = {p["symbol"]: p for p in remote}
        local_map = {p["symbol"]: p for p in local}

        all_symbols = set(remote_map.keys()) | set(local_map.keys())

        for symbol in all_symbols:
            r = remote_map.get(symbol)
            l = local_map.get(symbol)

            if r and not l:
                # 远程有, 本地没有 → 缺失
                logger.warning(
                    "position_sync.missing_local",
                    symbol=symbol,
                    amount=r.get("positionAmt"),
                )
                await self._upsert_position(r)
                await notifier.notify_alert(
                    "WARN",
                    f"持仓不一致: {symbol} 远程有仓但本地缺失, 已同步"
                )

            elif l and not r:
                # 本地有, 远程没有 → 残留
                logger.warning(
                    "position_sync.stale_local",
                    symbol=symbol,
                    local_qty=l.get("quantity"),
                )
                await self._remove_position(symbol)
                await notifier.notify_alert(
                    "WARN",
                    f"持仓不一致: {symbol} 本地有仓但远程已平, 已清理"
                )

            elif r and l:
                # 两边都有, 检查数量是否一致
                remote_qty = abs(float(r.get("positionAmt", 0)))
                local_qty = float(l.get("quantity", 0))
                if abs(remote_qty - local_qty) > 0.0001:
                    logger.warning(
                        "position_sync.quantity_mismatch",
                        symbol=symbol,
                        remote=remote_qty,
                        local=local_qty,
                    )
                    await self._upsert_position(r)
                    await notifier.notify_alert(
                        "WARN",
                        f"持仓不一致: {symbol} 数量不匹配 (远程={remote_qty}, 本地={local_qty})"
                    )

    async def _upsert_position(self, remote_pos: Dict) -> None:
        """
        将远程持仓数据写入/更新到本地数据库

        Args:
            remote_pos: 币安 API 返回的持仓数据
        """
        symbol = remote_pos.get("symbol", "")
        amt = float(remote_pos.get("positionAmt", 0))
        entry_price = float(remote_pos.get("entryPrice", 0))
        unrealized_pnl = float(remote_pos.get("unRealizedProfit", 0))
        leverage = int(remote_pos.get("leverage", 1))
        side = "BUY" if amt > 0 else "SELL"
        quantity = abs(amt)

        try:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO positions (symbol, side, quantity, entry_price,
                                          unrealized_pnl, leverage, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, NOW())
                    ON CONFLICT (symbol) DO UPDATE SET
                        side = EXCLUDED.side,
                        quantity = EXCLUDED.quantity,
                        entry_price = EXCLUDED.entry_price,
                        unrealized_pnl = EXCLUDED.unrealized_pnl,
                        leverage = EXCLUDED.leverage,
                        updated_at = NOW()
                    """,
                    symbol, side, quantity, entry_price,
                    unrealized_pnl, leverage,
                )
        except Exception:
            logger.exception("position_sync.upsert_error", symbol=symbol)

    async def _remove_position(self, symbol: str) -> None:
        """
        从本地数据库删除持仓记录

        Args:
            symbol: 交易对
        """
        try:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM positions WHERE symbol = $1", symbol
                )
        except Exception:
            logger.exception("position_sync.remove_error", symbol=symbol)
