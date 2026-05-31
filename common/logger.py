"""
统一日志模块 — 基于 structlog 的 JSON 结构化日志
所有模块统一通过 from common.logger import get_logger 获取日志实例
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog

from common.config import settings


def _setup_logging() -> None:
    """
    初始化 structlog 配置
    - 控制台输出: 彩色格式 (开发友好)
    - 文件输出: JSON 格式 (便于 ELK 采集)
    兼容 structlog >= 22.0 (包括 25.x)
    """
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    log_path = settings.log_path
    log_path.mkdir(parents=True, exist_ok=True)

    # structlog 共享处理器链
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 根日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # 控制台 Handler — 开发友好的彩色输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    # structlog 25.x: ProcessorFormatter 只需传 processor, 不再有 foreign_processors
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
        # 保持对旧版 structlog 的兼容: 仅在参数存在时传入
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # 文件 Handler — JSON 格式, 便于日志分析
    file_handler = logging.FileHandler(
        log_path / "bxm40.log", encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
    )
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    获取指定模块的日志实例

    Args:
        name: 模块名称, 建议 __name__

    Returns:
        structlog 绑定日志器
    """
    return structlog.get_logger(name)


# 模块导入时自动初始化日志
_setup_logging()
