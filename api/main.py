"""
FastAPI 后端 — 启动入口
提供 REST API + WebSocket 转发
"""

from __future__ import annotations

import asyncio
import signal
import sys

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.config import settings
from common.logger import get_logger
from common.redis_client import redis_client
from common.db import db

from api.routes import account, trades, control
from api.ws_handler import router as ws_router

logger = get_logger(__name__)


def create_app() -> FastAPI:
    """
    创建 FastAPI 应用实例

    Returns:
        配置好的 FastAPI 应用
    """
    app = FastAPI(
        title="BXM40 量化交易系统",
        description="币安合约量化交易系统 API",
        version="2.0.0",
    )

    # CORS 中间件 (允许前端跨域)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由
    app.include_router(account.router, prefix="/api", tags=["账户"])
    app.include_router(trades.router, prefix="/api", tags=["交易"])
    app.include_router(control.router, prefix="/api", tags=["控制"])
    app.include_router(ws_router)

    # 生命周期事件
    @app.on_event("startup")
    async def startup():
        await redis_client.connect()
        await db.connect()
        await db.ensure_tables()
        logger.info("api.started")

    @app.on_event("shutdown")
    async def shutdown():
        await db.close()
        await redis_client.close()
        logger.info("api.stopped")

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level=settings.log_level.lower(),
    )
