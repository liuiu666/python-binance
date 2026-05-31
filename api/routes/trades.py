"""
交易记录路由
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from common.db import db

router = APIRouter()


@router.get("/trades")
async def get_trades(
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(20, ge=1, le=100, description="每页数量"),
    symbol: Optional[str] = Query(None, description="按交易对筛选"),
) -> Dict[str, Any]:
    """
    获取交易记录 (分页)
    """
    offset = (page - 1) * size

    where_clause = ""
    params = []
    if symbol:
        where_clause = "WHERE symbol = $1"
        params.append(symbol)

    try:
        async with db.pool.acquire() as conn:
            # 查询总数
            count_sql = f"SELECT COUNT(*) FROM trades {where_clause}"
            total = await conn.fetchval(count_sql, *params)

            # 查询数据
            data_sql = (
                f"SELECT signal_id, symbol, side, action, quantity, "
                f"entry_price, exit_price, stop_loss, take_profit, "
                f"pnl, fee, leverage, strategy, reason, status, "
                f"opened_at, closed_at, created_at "
                f"FROM trades {where_clause} "
                f"ORDER BY created_at DESC LIMIT {size} OFFSET {offset}"
            )
            rows = await conn.fetch(data_sql, *params)

            return {
                "total": total,
                "page": page,
                "size": size,
                "data": [dict(row) for row in rows],
            }
    except Exception:
        return {"total": 0, "page": page, "size": size, "data": []}


@router.get("/daily-pnl")
async def get_daily_pnl(days: int = Query(30, ge=1, le=365)) -> List[Dict[str, Any]]:
    """
    获取每日盈亏统计
    """
    try:
        async with db.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT trade_date, total_pnl, total_fee, trade_count,
                       win_count, loss_count
                FROM daily_pnl
                ORDER BY trade_date DESC
                LIMIT $1
                """,
                days,
            )
            return [dict(row) for row in rows]
    except Exception:
        return []


@router.get("/stats")
async def get_stats() -> Dict[str, Any]:
    """
    获取交易统计: 胜率、盈亏比、最大回撤、夏普比率
    """
    try:
        async with db.pool.acquire() as conn:
            # 基础统计
            stats = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) as total_trades,
                    COUNT(*) FILTER (WHERE pnl > 0) as win_trades,
                    COUNT(*) FILTER (WHERE pnl < 0) as loss_trades,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    COALESCE(AVG(pnl) FILTER (WHERE pnl > 0), 0) as avg_win,
                    COALESCE(AVG(pnl) FILTER (WHERE pnl < 0), 0) as avg_loss,
                    COALESCE(MAX(pnl), 0) as max_win,
                    COALESCE(MIN(pnl), 0) as max_loss
                FROM trades
                WHERE status = 'CLOSED' AND pnl IS NOT NULL
                """
            )

            if not stats or stats["total_trades"] == 0:
                return {"total_trades": 0}

            total = stats["total_trades"]
            wins = stats["win_trades"]
            avg_win = abs(float(stats["avg_win"]))
            avg_loss = abs(float(stats["avg_loss"]))

            # 胜率
            win_rate = wins / total if total > 0 else 0

            # 盈亏比
            profit_factor = avg_win / avg_loss if avg_loss > 0 else float("inf")

            # 计算最大回撤 (从累计盈亏曲线)
            daily_rows = await conn.fetch(
                """
                SELECT trade_date, total_pnl
                FROM daily_pnl
                ORDER BY trade_date
                """
            )
            cumulative = 0.0
            peak = 0.0
            max_drawdown = 0.0
            for row in daily_rows:
                cumulative += float(row["total_pnl"])
                if cumulative > peak:
                    peak = cumulative
                dd = peak - cumulative
                if dd > max_drawdown:
                    max_drawdown = dd

            return {
                "total_trades": total,
                "win_trades": wins,
                "loss_trades": stats["loss_trades"],
                "win_rate": round(win_rate, 4),
                "profit_factor": round(profit_factor, 2),
                "total_pnl": float(stats["total_pnl"]),
                "avg_win": float(stats["avg_win"]),
                "avg_loss": float(stats["avg_loss"]),
                "max_win": float(stats["max_win"]),
                "max_loss": float(stats["max_loss"]),
                "max_drawdown": round(max_drawdown, 2),
            }
    except Exception:
        return {"total_trades": 0}


@router.get("/klines")
async def get_klines(
    symbol: str = Query(..., description="交易对"),
    interval: str = Query("1m", description="K线周期"),
    limit: int = Query(100, ge=1, le=20000, description="K线数量"),
) -> List[Dict[str, Any]]:
    """
    从 ClickHouse 获取历史 K 线数据
    """
    from common.clickhouse import clickhouse_client
    from common.logger import get_logger
    
    local_logger = get_logger("api.routes.trades")
    
    sql = """
        SELECT 
            toUnixTimestamp(open_time) as time,
            argMax(open_price, local_recv_ts) as open,
            argMax(high_price, local_recv_ts) as high,
            argMax(low_price, local_recv_ts) as low,
            argMax(close_price, local_recv_ts) as close,
            argMax(volume, local_recv_ts) as volume
        FROM klines
        WHERE symbol = %(symbol)s AND interval = %(interval)s
        GROUP BY open_time
        ORDER BY open_time DESC
        LIMIT %(limit)s
    """
    try:
        result = await clickhouse_client.query(sql, {"symbol": symbol, "interval": interval, "limit": limit})
        
        rows = []
        if result and result.result_rows:
            for row in reversed(result.result_rows):
                rows.append({
                    "time": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5])
                })
        return rows
    except Exception as e:
        local_logger.error(f"Failed to query klines from ClickHouse: {e}")
        return []
