"""
手动干预路由 — 一键全平、暂停策略、紧急市价单
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from common.redis_client import redis_client
from common.db import db
from common.logger import get_logger
from collector.rest_client import rest_client

logger = get_logger(__name__)

router = APIRouter()


class EmergencyOrderRequest(BaseModel):
    """紧急市价单请求"""
    symbol: str = Field(..., description="交易对")
    side: str = Field(..., description="BUY / SELL")
    quantity: float = Field(..., gt=0, description="数量")


class PauseRequest(BaseModel):
    """暂停/恢复请求"""
    paused: bool = Field(..., description="true=暂停, false=恢复")


@router.post("/close-all")
async def close_all_positions() -> Dict[str, Any]:
    """
    一键全平 — 平掉所有持仓
    """
    try:
        # 获取所有持仓
        positions = await rest_client.get_positions()
        if not positions:
            return {"status": "no_positions"}

        results = []
        for pos in positions:
            amt = float(pos.get("positionAmt", 0))
            symbol = pos.get("symbol", "")
            if abs(amt) < 0.0001:
                continue

            # 反向下单平仓
            side = "SELL" if amt > 0 else "BUY"
            result = await rest_client.place_order({
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": abs(amt),
            })
            results.append({
                "symbol": symbol,
                "side": side,
                "quantity": abs(amt),
                "result": "ok" if result else "failed",
            })

            logger.warning("control.close_all", symbol=symbol, side=side, qty=abs(amt))

        return {"status": "done", "closed": results}

    except Exception as e:
        logger.exception("control.close_all_error")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pause")
async def toggle_pause(request: PauseRequest) -> Dict[str, Any]:
    """
    暂停/恢复策略
    通过 Redis 发布控制命令
    """
    action = "PAUSE" if request.paused else "RESUME"
    await redis_client.publish("control:strategy", action)
    logger.info("control.pause_toggled", action=action)
    return {"status": action.lower(), "paused": request.paused}


@router.post("/emergency-order")
async def emergency_order(req: EmergencyOrderRequest) -> Dict[str, Any]:
    """
    紧急市价单 — 立即执行, 跳过风控
    """
    try:
        result = await rest_client.place_order({
            "symbol": req.symbol,
            "side": req.side,
            "type": "MARKET",
            "quantity": req.quantity,
        })

        if result:
            logger.warning(
                "control.emergency_order",
                symbol=req.symbol,
                side=req.side,
                quantity=req.quantity,
                order_id=result.get("orderId"),
            )
            return {"status": "ok", "order_id": result.get("orderId")}
        else:
            raise HTTPException(status_code=500, detail="下单失败")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("control.emergency_order_error")
        raise HTTPException(status_code=500, detail=str(e))
