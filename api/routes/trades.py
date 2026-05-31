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
    end_time: Optional[int] = Query(None, description="结束时间戳(秒)，用于向前翻页加载更早数据"),
) -> List[Dict[str, Any]]:
    """
    从 ClickHouse 获取历史 K 线数据
    支持end_time参数实现滑动加载历史数据
    """
    from common.clickhouse import clickhouse_client
    from common.logger import get_logger
    
    local_logger = get_logger("api.routes.trades")
    
    # 根据是否有end_time构建不同的WHERE条件
    if end_time is not None:
        time_condition = "AND open_time < fromUnixTimestamp(%(end_time)s)"
    else:
        time_condition = ""
    
    sql = f"""
        SELECT 
            toUnixTimestamp(open_time) as time,
            argMax(open_price, local_recv_ts) as open,
            argMax(high_price, local_recv_ts) as high,
            argMax(low_price, local_recv_ts) as low,
            argMax(close_price, local_recv_ts) as close,
            argMax(volume, local_recv_ts) as volume,
            argMax(trades_count, local_recv_ts) as trades_count
        FROM klines
        WHERE symbol = %(symbol)s AND interval = %(interval)s {time_condition}
        GROUP BY open_time
        ORDER BY open_time DESC
        LIMIT %(limit)s
    """
    
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time is not None:
        params["end_time"] = end_time
    
    try:
        result = await clickhouse_client.query(sql, params)
        
        rows = []
        if result and result.result_rows:
            for row in reversed(result.result_rows):
                rows.append({
                    "time": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "trades_count": int(row[6]) if row[6] is not None else 0
                })
        return rows
    except Exception as e:
        local_logger.error(f"Failed to query klines from ClickHouse: {e}")
        return []


@router.get("/klines-with-anomalies")
async def get_klines_with_anomalies(
    symbol: str = Query("BTCUSDT", description="交易对"),
    interval: str = Query("1m", description="K线周期"),
    limit: int = Query(200, ge=60, le=5000, description="K线数量"),
    window_size: int = Query(60, ge=20, le=500, description="泊松检测滚动窗口大小"),
    end_time: Optional[int] = Query(None, description="结束时间戳(秒)，用于向前翻页加载更早数据"),
) -> Dict[str, Any]:
    """
    获取K线数据并计算泊松异常检测结果
    返回完整的K线数据 + 成交量异常标记
    支持end_time参数实现滑动加载历史数据
    """
    from common.clickhouse import clickhouse_client
    from strategy.poisson_detector import PoissonDetector, AnomalyLevel
    from common.logger import get_logger
    
    local_logger = get_logger("api.routes.trades")
    
    # 根据是否有end_time构建不同的WHERE条件
    if end_time is not None:
        time_condition = "AND open_time < fromUnixTimestamp(%(end_time)s)"
    else:
        time_condition = ""
    
    sql = f"""
        SELECT 
            toUnixTimestamp(open_time) as time,
            argMax(open_price, local_recv_ts) as open,
            argMax(high_price, local_recv_ts) as high,
            argMax(low_price, local_recv_ts) as low,
            argMax(close_price, local_recv_ts) as close,
            argMax(volume, local_recv_ts) as volume,
            argMax(trades_count, local_recv_ts) as trades_count
        FROM klines
        WHERE symbol = %(symbol)s AND interval = %(interval)s {time_condition}
        GROUP BY open_time
        ORDER BY open_time DESC
        LIMIT %(limit)s
    """
    
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time is not None:
        params["end_time"] = end_time
    
    try:
        result = await clickhouse_client.query(sql, params)
        
        if not result or not result.result_rows:
            return {"candles": [], "anomalies": [], "lambda_estimate": 0}
        
        rows = []
        for row in reversed(result.result_rows):
            rows.append({
                "time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "trades_count": int(row[6]) if row[6] is not None else 0
            })
        
        detector = PoissonDetector(window_size=window_size, ema_alpha=0.05, overdispersion_factor=1.2)
        
        anomalies = []
        processed_candles = []
        
        for i, candle in enumerate(rows):
            trade_count = candle.get("trades_count", 0)
            
            if trade_count > 0:
                result_det = detector.update(trade_count)
                candle["lambda"] = result_det.lambda_estimate
                candle["p_value"] = result_det.p_value
                candle["anomaly_level"] = result_det.anomaly_level.value
                candle["z_score"] = result_det.z_score
                candle["direction"] = result_det.direction
            else:
                candle["lambda"] = 0
                candle["p_value"] = 1.0
                candle["anomaly_level"] = AnomalyLevel.NORMAL.value
                candle["z_score"] = 0.0
                candle["direction"] = "NORMAL"
            
            processed_candles.append(candle)
            
            if result_det.anomaly_level in (AnomalyLevel.ANOMALY, AnomalyLevel.EXTREME):
                prev_close = processed_candles[i-1]["close"] if i > 0 else candle["open"]
                price_change = (candle["close"] - prev_close) / prev_close * 100 if prev_close > 0 else 0
                
                anomalies.append({
                    "time": candle["time"],
                    "anomaly_level": result_det.anomaly_level.value,
                    "trades_count": trade_count,
                    "lambda": result_det.lambda_estimate,
                    "p_value": result_det.p_value,
                    "z_score": result_det.z_score,
                    "price_change_pct": round(price_change, 3),
                    "direction": result_det.direction,
                    "candle_index": i
                })
        
        return {
            "candles": processed_candles,
            "anomalies": anomalies,
            "lambda_estimate": detector.get_lambda(),
            "window_size": window_size
        }
        
    except Exception as e:
        local_logger.error(f"Failed to query klines with anomalies: {e}")
        return {"candles": [], "anomalies": [], "lambda_estimate": 0, "error": str(e)}
