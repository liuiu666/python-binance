"""
全局配置模块 — 通过 pydantic-settings 从环境变量 / .env 文件读取配置
所有模块统一通过 from common.config import settings 获取配置
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录 (bxm40/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """
    系统全局配置
    优先级: 环境变量 > .env 文件 > 默认值
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
    # 是否使用测试网
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

    # ---- 交易参数 ----
    symbols: List[str] = Field(default=["BTCUSDT", "ETHUSDT"])
    max_order_pct: float = 5.0        # 单笔最大金额占账户净值百分比
    max_positions: int = 3            # 最大同时持仓数
    max_daily_loss: float = 500.0     # 日最大亏损 USDT
    max_leverage: int = 10            # 最大杠杆倍数

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, v: Any) -> List[str]:
        """
        兼容两种格式:
        - JSON 列表: ["BTCUSDT","ETHUSDT"]
        - 逗号分隔: BTCUSDT,ETHUSDT
        """
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json
                return json.loads(v)
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        if isinstance(v, list):
            return [s.strip().upper() if isinstance(s, str) else s for s in v]
        return v

    # ---- WebSocket 参数 ----
    ws_ping_interval: int = 20        # 心跳间隔 (秒)
    ws_ping_timeout: int = 10         # 心跳超时 (秒)
    ws_reconnect_base: float = 1.0    # 重连基础延迟 (秒)
    ws_reconnect_max: float = 60.0    # 重连最大延迟 (秒)
    ws_24h_reconnect_offset: int = 300  # 24h 热切换提前量 (秒), 默认 5 分钟

    # ---- REST 校准 ----
    rest_compensate_interval: int = 30  # 校准间隔 (秒)

    # ---- 通知 ----
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ---- 管理接口鉴权 ----
    admin_api_key: str = ""  # 管理接口密钥, 空字符串表示不鉴权

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
