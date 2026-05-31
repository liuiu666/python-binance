"""
模拟下单 (Paper Trading) 路由
在本地 PostgreSQL 数据库中模拟撮合，生成虚拟成交记录与仓位快照
"""

from __future__ import annotations

import uuid
from typing import Any, Dict
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from common.db import db
from common.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()


class PaperOrderRequest(BaseModel):
    symbol: str = Field(..., description="交易对，如 BTCUSDT")
    side: str = Field(..., description="交易方向 BUY / SELL")
    quantity: float = Field(..., gt=0, description="委托数量")
    type: str = Field(..., description="委托类型 MARKET / LIMIT")
    price: float = Field(..., gt=0, description="成交价格")


@router.post("/paper/order")
async def place_paper_order(req: PaperOrderRequest) -> Dict[str, Any]:
    """
    在本地 PostgreSQL 数据库中模拟报单并进行虚拟撮合成交
    """
    symbol = req.symbol.upper()
    side = req.side.upper()
    quantity = req.quantity
    price = req.price

    if side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="Invalid side. Must be BUY or SELL.")

    try:
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                # 1. 查询当前持仓
                pos_row = await conn.fetchrow(
                    "SELECT side, quantity, entry_price, opened_at FROM positions WHERE symbol = $1",
                    symbol
                )

                signal_id = f"paper_{uuid.uuid4().hex[:16]}"

                if pos_row:
                    pos_side = pos_row["side"]
                    pos_qty = float(pos_row["quantity"])
                    pos_entry_price = float(pos_row["entry_price"])
                    pos_opened_at = pos_row["opened_at"]

                    if side == pos_side:
                        # 同向加仓：计算加权均价，更新持仓
                        new_qty = pos_qty + quantity
                        new_entry_price = (pos_qty * pos_entry_price + quantity * price) / new_qty

                        await conn.execute(
                            "UPDATE positions SET quantity = $1, entry_price = $2, updated_at = NOW() WHERE symbol = $3",
                            new_qty, new_entry_price, symbol
                        )

                        # 记录开仓成交记录
                        await conn.execute(
                            """
                            INSERT INTO trades (signal_id, symbol, side, action, quantity, entry_price, status, opened_at, strategy)
                            VALUES ($1, $2, $3, 'OPEN', $4, $5, 'OPENED', NOW(), 'Manual_Sandbox')
                            """,
                            signal_id, symbol, side, quantity, price
                        )
                    else:
                        # 反向减仓/平仓/反手
                        if quantity < pos_qty:
                            # 部分平仓：更新持仓数量，计算实现盈亏
                            new_qty = pos_qty - quantity
                            pnl = quantity * (price - pos_entry_price) if pos_side == "BUY" else quantity * (pos_entry_price - price)

                            await conn.execute(
                                "UPDATE positions SET quantity = $1, updated_at = NOW() WHERE symbol = $2",
                                new_qty, symbol
                            )

                            # 记录平仓成交记录
                            await conn.execute(
                                """
                                INSERT INTO trades (signal_id, symbol, side, action, quantity, entry_price, exit_price, pnl, status, opened_at, closed_at, strategy)
                                VALUES ($1, $2, $3, 'CLOSE', $4, $5, $6, $7, 'CLOSED', $8, NOW(), 'Manual_Sandbox')
                                """,
                                signal_id, symbol, side, quantity, pos_entry_price, price, pnl, pos_opened_at
                            )
                        elif quantity == pos_qty:
                            # 完全平仓：删除持仓，计算实现盈亏
                            pnl = quantity * (price - pos_entry_price) if pos_side == "BUY" else quantity * (pos_entry_price - price)

                            await conn.execute("DELETE FROM positions WHERE symbol = $1", symbol)

                            # 记录平仓成交记录
                            await conn.execute(
                                """
                                INSERT INTO trades (signal_id, symbol, side, action, quantity, entry_price, exit_price, pnl, status, opened_at, closed_at, strategy)
                                VALUES ($1, $2, $3, 'CLOSE', $4, $5, $6, $7, 'CLOSED', $8, NOW(), 'Manual_Sandbox')
                                """,
                                signal_id, symbol, side, quantity, pos_entry_price, price, pnl, pos_opened_at
                            )
                        else:
                            # 反手开仓 (quantity > pos_qty)：先平掉老持仓，再开相反新持仓
                            pnl = pos_qty * (price - pos_entry_price) if pos_side == "BUY" else pos_qty * (pos_entry_price - price)

                            # 1. 记录老持仓的平仓记录
                            await conn.execute(
                                """
                                INSERT INTO trades (signal_id, symbol, side, action, quantity, entry_price, exit_price, pnl, status, opened_at, closed_at, strategy)
                                VALUES ($1, $2, $3, 'CLOSE', $4, $5, $6, $7, 'CLOSED', $8, NOW(), 'Manual_Sandbox')
                                """,
                                signal_id, symbol, side, pos_qty, pos_entry_price, price, pnl, pos_opened_at
                            )

                            # 2. 更新持仓为新方向和剩余数量
                            rem_qty = quantity - pos_qty
                            await conn.execute(
                                "UPDATE positions SET side = $1, quantity = $2, entry_price = $3, opened_at = NOW(), updated_at = NOW() WHERE symbol = $4",
                                side, rem_qty, price, symbol
                            )

                            # 3. 记录新方向的开仓记录
                            new_signal_id = f"paper_{uuid.uuid4().hex[:16]}"
                            await conn.execute(
                                """
                                INSERT INTO trades (signal_id, symbol, side, action, quantity, entry_price, status, opened_at, strategy)
                                VALUES ($1, $2, $3, 'OPEN', $4, $5, 'OPENED', NOW(), 'Manual_Sandbox')
                                """,
                                new_signal_id, symbol, side, rem_qty, price
                            )
                else:
                    # 无任何持仓，直接开仓
                    await conn.execute(
                        "INSERT INTO positions (symbol, side, quantity, entry_price, opened_at, updated_at) VALUES ($1, $2, $3, $4, NOW(), NOW())",
                        symbol, side, quantity, price
                    )

                    await conn.execute(
                        """
                        INSERT INTO trades (signal_id, symbol, side, action, quantity, entry_price, status, opened_at, strategy)
                        VALUES ($1, $2, $3, 'OPEN', $4, $5, 'OPENED', NOW(), 'Manual_Sandbox')
                        """,
                        signal_id, symbol, side, quantity, price
                    )

        return {"status": "success", "signal_id": signal_id}
    except Exception as e:
        logger.exception("paper.order_error")
        raise HTTPException(status_code=500, detail=str(e))
