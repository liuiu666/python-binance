"""
PostgreSQL 异步连接模块 — 基于 asyncpg
用于存储交易记录、持仓快照、每日盈亏等业务数据
"""

from __future__ import annotations

import asyncpg
from typing import Optional

from common.config import settings
from common.logger import get_logger

logger = get_logger(__name__)


# 交易记录表
_TRADES_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       VARCHAR(64) UNIQUE NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    side            VARCHAR(10) NOT NULL,
    action          VARCHAR(10) NOT NULL,
    quantity        NUMERIC(20, 8) NOT NULL,
    entry_price     NUMERIC(20, 8),
    exit_price      NUMERIC(20, 8),
    stop_loss       NUMERIC(20, 8),
    take_profit     NUMERIC(20, 8),
    pnl             NUMERIC(20, 8),
    fee             NUMERIC(20, 8),
    leverage        INT,
    order_id        BIGINT,
    strategy        VARCHAR(50),
    reason          TEXT,
    status          VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    opened_at       TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at);
"""

# 持仓快照表
_POSITIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS positions (
    id              BIGSERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    side            VARCHAR(10) NOT NULL,
    quantity        NUMERIC(20, 8) NOT NULL,
    entry_price     NUMERIC(20, 8) NOT NULL,
    unrealized_pnl  NUMERIC(20, 8) DEFAULT 0,
    leverage        INT,
    signal_id       VARCHAR(64),
    strategy        VARCHAR(50),
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
"""

# 每日盈亏汇总表
_DAILY_PNL_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS daily_pnl (
    id              BIGSERIAL PRIMARY KEY,
    trade_date      DATE NOT NULL,
    total_pnl       NUMERIC(20, 8) DEFAULT 0,
    total_fee       NUMERIC(20, 8) DEFAULT 0,
    trade_count     INT DEFAULT 0,
    win_count       INT DEFAULT 0,
    loss_count      INT DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_pnl_date ON daily_pnl(trade_date);
"""

# 系统配置表 — 交易参数存数据库, 运行时可动态修改
_SYSTEM_CONFIG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS system_config (
    key             VARCHAR(64) PRIMARY KEY,
    value           JSONB NOT NULL,
    description     TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class Database:
    """
    PostgreSQL 异步连接管理器
    """

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """创建连接池"""
        self._pool = await asyncpg.create_pool(
            settings.pg_dsn,
            min_size=2,
            max_size=10,
        )
        # 脱敏: 只打印 DSN 中 @ 之后的部分, 不暴露密码
        safe_dsn = settings.pg_dsn.split("@")[-1] if "@" in settings.pg_dsn else "***"
        logger.info("postgres.connected", host=safe_dsn)

    async def ensure_tables(self) -> None:
        """创建所需的数据表"""
        if self._pool is None:
            raise RuntimeError("PostgreSQL 未连接")
        async with self._pool.acquire() as conn:
            for ddl in (_TRADES_TABLE_DDL, _POSITIONS_TABLE_DDL, _DAILY_PNL_TABLE_DDL, _SYSTEM_CONFIG_TABLE_DDL):
                await conn.execute(ddl)
        logger.info("postgres.tables_ready")

    async def close(self) -> None:
        """关闭连接池"""
        if self._pool:
            await self._pool.close()
            logger.info("postgres.closed")

    @property
    def pool(self) -> asyncpg.Pool:
        """获取连接池"""
        if self._pool is None:
            raise RuntimeError("PostgreSQL 未连接, 请先调用 connect()")
        return self._pool


# 全局单例
db = Database()


# ============================================================
# 配置存储 — 交易参数从 PostgreSQL 读写
# ============================================================

import json as _json
from typing import Any, Dict, Optional as _Optional

# 默认交易参数 (首次启动时写入 DB)
_DEFAULT_CONFIG: Dict[str, Dict[str, Any]] = {
    "symbols": {
        "value": ["BTCUSDT", "ETHUSDT"],
        "description": "监控的交易对列表",
    },
    "max_order_pct": {
        "value": 5.0,
        "description": "单笔最大金额占账户净值百分比",
    },
    "max_positions": {
        "value": 3,
        "description": "最大同时持仓数",
    },
    "max_daily_loss": {
        "value": 500.0,
        "description": "日最大亏损 USDT",
    },
    "max_leverage": {
        "value": 10,
        "description": "最大杠杆倍数",
    },
}


class ConfigStore:
    """
    配置存储器 — 从 PostgreSQL system_config 表读写交易参数

    用法:
        store = ConfigStore()
        await store.load()          # 启动时从 DB 加载
        symbols = store.get("symbols")
        await store.set("max_leverage", 5)  # 运行时修改
    """

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}

    async def load(self) -> None:
        """
        从 DB 加载配置, 如果 DB 中没有则写入默认值
        """
        # 先确保默认值存在
        async with db.pool.acquire() as conn:
            for key, item in _DEFAULT_CONFIG.items():
                existing = await conn.fetchval(
                    "SELECT value FROM system_config WHERE key = $1", key
                )
                if existing is None:
                    await conn.execute(
                        "INSERT INTO system_config (key, value, description) VALUES ($1, $2, $3)",
                        key,
                        _json.dumps(item["value"]),
                        item["description"],
                    )

            # 加载所有配置
            rows = await conn.fetch("SELECT key, value FROM system_config")
            for row in rows:
                self._cache[row["key"]] = _json.loads(row["value"])

        logger.info("config_store.loaded", keys=list(self._cache.keys()))

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        return self._cache.get(key, default)

    def get_all(self) -> Dict[str, Any]:
        """获取所有配置"""
        return dict(self._cache)

    async def set(self, key: str, value: Any) -> None:
        """
        设置配置值, 同步写入 DB 并更新内存缓存
        """
        async with db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO system_config (key, value, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """,
                key,
                _json.dumps(value),
            )
        self._cache[key] = value
        logger.info("config_store.updated", key=key, value=value)


# 全局单例
config_store = ConfigStore()
