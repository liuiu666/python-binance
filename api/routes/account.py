"""
账户信息路由
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from typing import Any, Dict, List, Optional

from common.db import db
from collector.rest_client import rest_client

router = APIRouter()


@router.get("/account")
async def get_account() -> Dict[str, Any]:
    """
    获取账户信息: 权益、余额、未实现盈亏
    直接从币安 API 实时获取
    """
    account = await rest_client.get_account()
    if not account:
        return {"error": "无法获取账户信息"}

    return {
        "total_wallet_balance": float(account.get("totalWalletBalance", 0)),
        "total_unrealized_profit": float(account.get("totalUnrealizedProfit", 0)),
        "total_margin_balance": float(account.get("totalMarginBalance", 0)),
        "available_balance": float(account.get("availableBalance", 0)),
        "max_withdraw_amount": float(account.get("maxWithdrawAmount", 0)),
    }


@router.get("/positions")
async def get_positions() -> List[Dict[str, Any]]:
    """
    获取当前持仓列表
    从 PostgreSQL 读取本地持仓快照
    """
    try:
        async with db.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT symbol, side, quantity, entry_price, "
                "unrealized_pnl, leverage, opened_at, updated_at "
                "FROM positions ORDER BY symbol"
            )
            return [dict(row) for row in rows]
    except Exception:
        return []
