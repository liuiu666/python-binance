"""
配置管理路由 — 交易参数存数据库, 运行时可动态修改
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from common.db import config_store
from common.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()


class ConfigUpdateRequest(BaseModel):
    """配置更新请求"""
    key: str
    value: Any


@router.get("/config")
async def get_config() -> Dict[str, Any]:
    """
    获取所有交易参数配置
    """
    return config_store.get_all()


@router.get("/config/{key}")
async def get_config_item(key: str) -> Any:
    """
    获取单个配置项
    """
    value = config_store.get(key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"配置项 '{key}' 不存在")
    return {"key": key, "value": value}


@router.put("/config")
async def update_config(req: ConfigUpdateRequest) -> Dict[str, str]:
    """
    修改配置项, 立即写入数据库并更新内存缓存
    不需要重启服务即可生效

    支持的配置项:
    - symbols: 交易对列表, 如 ["BTCUSDT","ETHUSDT"]
    - max_order_pct: 单笔最大占比
    - max_positions: 最大持仓数
    - max_daily_loss: 日最大亏损
    - max_leverage: 最大杠杆
    """
    # 允许的配置 key 白名单
    allowed_keys = {"symbols", "max_order_pct", "max_positions", "max_daily_loss", "max_leverage"}
    if req.key not in allowed_keys:
        raise HTTPException(
            status_code=400,
            detail=f"不允许修改 '{req.key}', 可用: {sorted(allowed_keys)}"
        )

    # 基础校验
    if req.key == "symbols" and not isinstance(req.value, list):
        raise HTTPException(status_code=400, detail="symbols 必须是数组")
    if req.key == "max_leverage" and (not isinstance(req.value, int) or req.value < 1 or req.value > 125):
        raise HTTPException(status_code=400, detail="max_leverage 必须是 1-125 的整数")

    await config_store.set(req.key, req.value)
    logger.info("api.config_updated", key=req.key, value=req.value)
    return {"status": "ok", "key": req.key}
