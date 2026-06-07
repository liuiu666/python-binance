"""
FastAPI 后端 — 启动入口
提供 REST API + WebSocket 转发
"""

from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from common.config import settings
from common.logger import get_logger
from common.redis_client import redis_client
from common.db import db
from collector.rest_client import rest_client

from api.routes import account, trades, control, config as config_routes, paper
from api.ws_handler import router as ws_router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 生命周期管理 (替代已废弃的 on_event)
    startup: 初始化所有连接
    shutdown: 优雅关闭
    """
    # ---- startup ----
    await redis_client.connect()
    await db.connect()
    await db.ensure_tables()
    # 连接 ClickHouse
    from common.clickhouse import clickhouse_client
    clickhouse_client.connect()
    await clickhouse_client.ensure_tables()
    # 从 DB 加载交易参数配置
    from common.db import config_store
    await config_store.load()
    # 初始化 REST 客户端 (control.py 全平/紧急下单需要)
    await rest_client.connect()
    # 启动 Redis → WebSocket 实时数据转发器 (后台任务)
    from api.ws_handler import start_redis_forwarder
    forwarder_task = asyncio.create_task(start_redis_forwarder())
    # 启动沙盒高频实时交易模拟引擎
    from api.sandbox_engine import sandbox_engine
    await sandbox_engine.start()
    
    logger.info("api.started")
    yield
    # ---- shutdown ----
    await sandbox_engine.stop()
    forwarder_task.cancel()
    await rest_client.close()
    await db.close()
    await redis_client.close()
    from common.clickhouse import clickhouse_client
    clickhouse_client.close()
    logger.info("api.stopped")


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
        lifespan=lifespan,
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
    app.include_router(control.router, prefix="/api/control", tags=["控制"])
    app.include_router(config_routes.router, prefix="/api", tags=["配置"])
    app.include_router(paper.router, prefix="/api", tags=["模拟交易"])
    app.include_router(ws_router)

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
