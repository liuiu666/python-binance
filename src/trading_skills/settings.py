"""
模块功能：配置管理
主要作用：
1. 加载环境变量 (.env)
2. 加载配置文件 (config.json)
3. 提供统一的配置访问接口（API Key、代理、超时等）
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    binance_api_key: str | None
    binance_api_secret: str | None
    binance_futures_base_url: str | None
    http_proxy: str | None
    https_proxy: str | None
    request_timeout_sec: int
    llm_api_key: str | None
    llm_base_url: str | None
    llm_model: str | None

    @staticmethod
    def load(project_root: Path | None = None) -> "Settings":
        root = project_root or Path.cwd()
        load_dotenv(root / ".env")

        config_path = root / "config.json"
        config: dict[str, Any] = {}
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))

        llm_cfg = config.get("llm") if isinstance(config.get("llm"), dict) else {}

        def get_env(name: str) -> str | None:
            value = os.getenv(name)
            if value is None:
                return None
            value = value.strip()
            return value if value else None

        def get_cfg_str(key: str) -> str | None:
            value = llm_cfg.get(key)
            if not isinstance(value, str):
                return None
            value = value.strip()
            return value if value else None

        timeout = get_env("REQUEST_TIMEOUT_SEC")
        request_timeout = int(timeout) if timeout and timeout.isdigit() else 15

        return Settings(
            binance_api_key=get_env("BINANCE_API_KEY"),
            binance_api_secret=get_env("BINANCE_API_SECRET"),
            binance_futures_base_url=get_env("BINANCE_FUTURES_BASE_URL"),
            http_proxy=get_env("HTTP_PROXY"),
            https_proxy=get_env("HTTPS_PROXY"),
            request_timeout_sec=request_timeout,
            llm_api_key=get_cfg_str("api_key"),
            llm_base_url=get_cfg_str("base_url"),
            llm_model=get_cfg_str("model"),
        )
