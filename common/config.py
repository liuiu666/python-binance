"""
全局配置模块 — 通过 pydantic-settings 从环境变量 / .env 文件读取配置
所有模块统一通过 from common.config import settings 获取配置
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple, Type

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# 项目根目录 (bxm40/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class CommaListSettingsSource(PydanticBaseSettingsSource):
    """
    自定义 Settings Source: 在 pydantic 解析前预处理环境变量
    将逗号分隔的字符串 (如 SYMBOLS=BTCUSDT,ETHUSDT) 转为 JSON 数组,
    避免 pydantic-settings 对 List[str] 字段默认做 json.loads() 时报错
    """

    # 需要预处理的字段名 (小写)
    COMMA_FIELDS = {"symbols"}

    def get_field_value(
        self, field: Any, field_name: str
    ) -> Tuple[Any, str, bool]:
        # 直接从 os.environ 读取原始值
        val = os.environ.get(field_name.upper()) or os.environ.get(field_name.lower())
        if val is not None:
            return val, field_name, False
        # 尝试从 .env 文件读取
        env_file = PROJECT_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip().lower() == field_name.lower():
                    return value.strip(), field_name, False
        return None, field_name, False

    def __call__(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for field_name in self.settings_cls.model_fields:
            if field_name.lower() not in self.COMMA_FIELDS:
                continue
            val, _, _ = self.get_field_value(None, field_name)
            if val is None:
                continue
            if isinstance(val, str):
                val = val.strip()
                if not val.startswith("["):
                    # 逗号分隔 → JSON 数组
                    val = json.dumps([s.strip() for s in val.split(",") if s.strip()])
            result[field_name] = val
        return result


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
    # 支持 JSON 数组 ["BTCUSDT","ETHUSDT"] 或逗号分隔 BTCUSDT,ETHUSDT
    symbols: List[str] = Field(default=["BTCUSDT", "ETHUSDT"])
    max_order_pct: float = 5.0        # 单笔最大金额占账户净值百分比
    max_positions: int = 3            # 最大同时持仓数
    max_daily_loss: float = 500.0     # 日最大亏损 USDT
    max_leverage: int = 10            # 最大杠杆倍数

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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        """
        自定义 settings sources 优先级
        CommaListSettingsSource 在 env_settings 之前运行,
        将逗号分隔的 SYMBOLS 转为 JSON 数组后, 后续的 json.loads() 就能正常工作
        """
        return (
            init_settings,
            CommaListSettingsSource(settings_cls),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )


# 全局单例
settings = Settings()
