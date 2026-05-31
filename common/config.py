"""
全局配置模块 — 通过 pydantic-settings 从环境变量 / .env 文件读取配置
所有模块统一通过 from common.config import settings 获取配置

交易参数 (symbols, max_order_pct 等) 存 PostgreSQL system_config 表,
启动时从数据库加载, 运行时可通过 /api/config 端点动态修改。
基础设施连接 (redis_url, pg_dsn 等) 仍从 .env 读取。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, List

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录 (bxm40/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_symbols(v: Any) -> Any:
    """
    将 symbols 字段从各种格式转为 List[str]
    兼容: JSON 数组字符串 / 逗号分隔字符串 / 已经是 list
    """
    if isinstance(v, list):
        return [s.upper() if isinstance(s, str) else s for s in v]
    if isinstance(v, str):
        v = v.strip()
        if v.startswith("["):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [s.upper() if isinstance(s, str) else s for s in parsed]
            except json.JSONDecodeError:
                pass
        return [s.strip().upper() for s in v.split(",") if s.strip()]
    return v


class Settings(BaseSettings):
    """
    系统全局配置

    分两层:
    1. 基础设施 (.env): API Key, 连接地址 — 改了要重启
    2. 交易参数 (DB): symbols, 杠杆, 仓位限制 — 可运行时修改
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 币安 API ----
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"

    # ---- ClickHouse ----
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_user: str = "default"
    clickhouse_password: str = ""
    clickhouse_database: str = "bxm40"

    # ---- PostgreSQL ----
    pg_dsn: str = "postgresql://bxm40:bxm40_secret@localhost:5432/bxm40"

    # ---- 交易参数 (从 DB 加载, .env 中也可以设默认值) ----
    # Annotated + BeforeValidator 在 pydantic-settings JSON 解析前拦截
    symbols: Annotated[List[str], BeforeValidator(_parse_symbols)] = Field(
        default=["BTCUSDT", "ETHUSDT"]
    )
    max_order_pct: float = 5.0
    max_positions: int = 3
    max_daily_loss: float = 500.0
    max_leverage: int = 10

    # ---- WebSocket 参数 ----
    ws_ping_interval: int = 20
    ws_ping_timeout: int = 10
    ws_reconnect_base: float = 1.0
    ws_reconnect_max: float = 60.0
    ws_24h_reconnect_offset: int = 300

    # ---- REST 校准 ----
    rest_compensate_interval: int = 30

    # ---- 通知 ----
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ---- 管理接口鉴权 ----
    admin_api_key: str = ""

    # ---- AI (Phase 5) ----
    gemini_api_key: str = ""
    openai_api_key: str = ""

    # ---- 日志 ----
    log_level: str = "INFO"
    log_dir: str = "logs"

    # ============================================================
    # 计算属性
    # ============================================================

    @property
    def binance_ws_base_url(self) -> str:
        """币安合约 WebSocket 基础地址"""
        if self.binance_testnet:
            return "wss://stream.binancefuture.com/ws"
        return "wss://fstream.binance.com/ws"

    @property
    def binance_rest_base_url(self) -> str:
        """币安合约 REST API 基础地址"""
        if self.binance_testnet:
            return "https://testnet.binancefuture.com"
        return "https://fapi.binance.com"

    @property
    def clickhouse_dsn(self) -> str:
        """ClickHouse 连接 DSN"""
        return f"http://{self.clickhouse_host}:{self.clickhouse_port}"

    @property
    def log_path(self) -> Path:
        """日志目录的完整路径"""
        return PROJECT_ROOT / self.log_dir


# 全局单例
settings = Settings()
