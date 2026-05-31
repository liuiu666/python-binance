"""
ClickHouse 客户端封装 — 异步批量写入历史行情数据
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Sequence

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from common.config import settings
from common.logger import get_logger

logger = get_logger(__name__)


# K 线表建表语句
_KLINES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS klines (
    symbol          String,
    open_time       DateTime64(3, 'UTC'),
    close_time      DateTime64(3, 'UTC'),
    interval        String,
    open_price      Float64,
    high_price      Float64,
    low_price       Float64,
    close_price     Float64,
    volume          Float64,
    quote_volume    Float64,
    trades_count    UInt32,
    taker_buy_volume Float64,
    local_recv_ts   DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(open_time)
ORDER BY (symbol, interval, open_time)
TTL toDateTime(open_time) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192
"""

# 成交明细表建表语句
_AGGTRADES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS agg_trades (
    symbol          String,
    agg_trade_id    UInt64,
    price           Float64,
    quantity        Float64,
    first_trade_id  UInt64,
    last_trade_id   UInt64,
    timestamp       DateTime64(3, 'UTC'),
    is_buyer_maker  UInt8,
    local_recv_ts   DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (symbol, timestamp)
TTL toDateTime(timestamp) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192
"""


class ClickHouseClient:
    """
    ClickHouse 客户端
    使用同步驱动 + asyncio.to_thread 实现非阻塞写入
    支持批量攒写: 达到 batch_size 或 flush_interval 后统一写入
    """

    def __init__(
        self,
        batch_size: int = 100,
        flush_interval: float = 5.0,
    ) -> None:
        self._client: Optional[Client] = None
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        # 批量缓冲区: {table_name: [row_dict, ...]}
        self._buffers: Dict[str, List[Dict[str, Any]]] = {}
        self._flush_task: Optional[asyncio.Task] = None
        # 写入锁: 防止多个 asyncio.to_thread 并发使用同一个 ClickHouse client
        self._write_lock = asyncio.Lock()

    def connect(self) -> None:
        """建立 ClickHouse 连接"""
        self._client = clickhouse_connect.get_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database=settings.clickhouse_database,
        )
        logger.info(
            "clickhouse.connected",
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            database=settings.clickhouse_database,
        )

    async def ensure_tables(self) -> None:
        """创建所需的数据表"""
        if self._client is None:
            raise RuntimeError("ClickHouse 未连接")
        for ddl in (_KLINES_TABLE_DDL, _AGGTRADES_TABLE_DDL):
            await asyncio.to_thread(self._client.command, ddl)
        logger.info("clickhouse.tables_ready")

    def close(self) -> None:
        """关闭连接"""
        if self._client:
            self._client.close()
            logger.info("clickhouse.closed")

    # ============================================================
    # 批量写入
    # ============================================================

    async def start_flush_loop(self) -> None:
        """启动定时刷新协程"""
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop_flush_loop(self) -> None:
        """停止定时刷新并写入剩余数据"""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_all()

    async def insert(self, table: str, row: Dict[str, Any]) -> None:
        """
        写入一行数据到缓冲区
        达到 batch_size 时自动触发写入

        Args:
            table: 目标表名
            row: 行数据字典
        """
        if table not in self._buffers:
            self._buffers[table] = []
        self._buffers[table].append(row)

        if len(self._buffers[table]) >= self._batch_size:
            await self._flush_table(table)

    async def _flush_loop(self) -> None:
        """定时刷新缓冲区"""
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._flush_all()

    async def _flush_all(self) -> None:
        """刷新所有表的缓冲区"""
        for table in list(self._buffers.keys()):
            if self._buffers[table]:
                await self._flush_table(table)

    async def _flush_table(self, table: str) -> None:
        """将指定表的缓冲区数据批量写入 ClickHouse"""
        rows = self._buffers.pop(table, [])
        if not rows or self._client is None:
            return

        try:
            from datetime import datetime, timezone
            # ClickHouse DateTime64 列需要 datetime 对象, 不能传 float 时间戳
            # 找出所有时间戳列 (以 _time / _ts 结尾或叫 timestamp 的字段)
            ts_cols = {c for c in rows[0] if c.endswith("_time") or c.endswith("_ts") or c == "timestamp"}
            columns = list(rows[0].keys())
            converted = []
            for row in rows:
                new_row = {}
                for col in columns:
                    val = row.get(col)
                    # 毫秒级 float → datetime
                    if col in ts_cols and isinstance(val, (int, float)):
                        val = datetime.fromtimestamp(val / 1000, tz=timezone.utc)
                    new_row[col] = val
                converted.append([new_row.get(col) for col in columns])
            # 加锁: ClickHouse sync client 不支持同连接并发写入
            async with self._write_lock:
                await asyncio.to_thread(
                    self._client.insert, table, converted, column_names=columns
                )
            logger.debug("clickhouse.flushed", table=table, count=len(rows))
        except Exception:
            logger.exception("clickhouse.flush_error", table=table)
            # 写入失败, 将数据放回缓冲区, 避免丢失
            self._buffers.setdefault(table, []).extend(rows)

    # ============================================================
    # 查询
    # ============================================================

    async def query(self, sql: str, params: Optional[Dict] = None) -> Any:
        """执行查询并返回结果"""
        if self._client is None:
            raise RuntimeError("ClickHouse 未连接")
        return await asyncio.to_thread(self._client.query, sql, parameters=params)


# 全局单例
clickhouse_client = ClickHouseClient()
