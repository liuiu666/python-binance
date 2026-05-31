"""
手动干预路由 — 一键全平、暂停策略、紧急市价单
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Header, Depends, BackgroundTasks, Request, Query
from pydantic import BaseModel, Field

from common.redis_client import redis_client
from common.db import db, config_store
from common.logger import get_logger
from common.config import settings
from collector.rest_client import rest_client

logger = get_logger(__name__)


async def verify_admin_key(request: Request, x_admin_key: str = Header(None)):
    # 允许本地回环地址免鉴权 (开发调试便利)
    client_host = request.client.host if request.client else None
    if client_host in ("127.0.0.1", "::1", "localhost"):
        return

    # 生产环境下如果设置了真实的密钥，则进行强校验
    if settings.admin_api_key and settings.admin_api_key != "change_me":
        if x_admin_key != settings.admin_api_key:
            raise HTTPException(status_code=403, detail="Invalid admin API key")


router = APIRouter(dependencies=[Depends(verify_admin_key)])


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


# ============================================================
# 历史数据回填
# ============================================================

class CompensateRequest(BaseModel):
    """历史数据回填请求"""
    symbol: str = Field(..., description="交易对, 如 BTCUSDT")
    start_time: int = Field(..., description="开始时间毫秒时间戳")
    end_time: int = Field(..., description="结束时间毫秒时间戳")
    interval: str = Field(default="1m", description="K线周期")


async def _do_backfill(
    symbol: str,
    start_time: int,
    end_time: int,
    interval: str = "1m",
) -> Dict[str, Any]:
    """
    内部回填逻辑: 分批拉取币安 REST K线并写入 ClickHouse
    币安单批最多返回 1500 条, 每条 1m = 1500分钟 = 25小时
    """
    from common.clickhouse import clickhouse_client
    import time

    redis_key = f"backfill:status:{symbol}:{interval}"
    await redis_client.client.hset(
        redis_key,
        mapping={
            "status": "running",
            "progress": "0",
            "total_inserted": "0",
            "error_message": "",
            "last_update": str(int(time.time()))
        }
    )

    total_inserted = 0
    current_start = start_time
    batch_size = 1500

    # 每批覆盖的毫秒数 (1m周期 = 60*1000ms)
    interval_ms_map = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
    step_ms = batch_size * interval_ms_map.get(interval, 60_000)

    try:
        while current_start < end_time:
            batch_end = min(current_start + step_ms, end_time)
            klines = await rest_client.get_klines(
                symbol,
                interval=interval,
                limit=batch_size,
                start_time=current_start,
                end_time=batch_end,
            )
            if not klines:
                # 如果没有更多数据，但已经有写入，或者已经遍历完了
                break

            for k in klines:
                row = {
                    "symbol": k["symbol"],
                    "interval": k.get("interval", interval),
                    "open_time": k["open_time"],
                    "close_time": k["close_time"],
                    "open_price": k["open_price"],
                    "high_price": k["high_price"],
                    "low_price": k["low_price"],
                    "close_price": k["close_price"],
                    "volume": k["volume"],
                    "quote_volume": k.get("quote_volume", 0.0),
                    "trades_count": k.get("trades_count", 0),
                    "taker_buy_volume": k.get("taker_buy_volume", 0.0),
                    "local_recv_ts": time.time() * 1000,
                }
                await clickhouse_client.insert("klines", row)

            total_inserted += len(klines)
            logger.info(
                "control.backfill_batch",
                symbol=symbol,
                batch=len(klines),
                total=total_inserted,
            )

            # 计算百分比进度
            range_total = max(1, end_time - start_time)
            progress_pct = int(((current_start - start_time) / range_total) * 100)
            progress_pct = min(100, max(0, progress_pct))

            await redis_client.client.hset(
                redis_key,
                mapping={
                    "progress": str(progress_pct),
                    "total_inserted": str(total_inserted),
                    "last_update": str(int(time.time()))
                }
            )

            # 移动到下一批的起始时间
            last_close = klines[-1]["close_time"]
            current_start = last_close + 1

            # 限流: 避免请求过快
            await asyncio.sleep(0.2)

        # 强制刷入缓冲区
        await clickhouse_client._flush_all()

        await redis_client.client.hset(
            redis_key,
            mapping={
                "status": "completed",
                "progress": "100",
                "total_inserted": str(total_inserted),
                "last_update": str(int(time.time()))
            }
        )

    except Exception as e:
        logger.exception("control.backfill_error", symbol=symbol)
        await redis_client.client.hset(
            redis_key,
            mapping={
                "status": "failed",
                "error_message": str(e),
                "last_update": str(int(time.time()))
            }
        )

    return {"symbol": symbol, "inserted": total_inserted}



@router.post("/compensate")
async def compensate(
    req: CompensateRequest,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """
    历史数据回填 — 从币安 REST 拉取指定时间段的 K 线并写入 ClickHouse
    后台异步执行, 立即返回 202
    """
    duration_hours = (req.end_time - req.start_time) / 3_600_000
    if duration_hours > 30 * 24:
        raise HTTPException(
            status_code=400,
            detail="回填时间范围最大 30 天, 请分批操作"
        )
    if req.end_time <= req.start_time:
        raise HTTPException(status_code=400, detail="结束时间必须大于开始时间")

    logger.info(
        "control.backfill_started",
        symbol=req.symbol,
        interval=req.interval,
        duration_hours=f"{duration_hours:.1f}h",
    )

    background_tasks.add_task(
        _do_backfill, req.symbol, req.start_time, req.end_time, req.interval
    )
    return {
        "status": "started",
        "symbol": req.symbol,
        "interval": req.interval,
        "duration_hours": round(duration_hours, 1),
        "message": f"回填任务已启动, 预计拉取 {round(duration_hours * 60):.0f} 批 K 线"
    }


@router.get("/backfill-status")
async def get_backfill_status(symbol: str, interval: str = "1m") -> Dict[str, Any]:
    """
    获取指定币种和周期的历史数据回填任务状态
    """
    redis_key = f"backfill:status:{symbol}:{interval}"
    data = await redis_client.client.hgetall(redis_key)
    if not data:
        return {"status": "idle", "progress": 0, "total_inserted": 0, "error_message": ""}
    return {
        "status": data.get("status", "idle"),
        "progress": int(data.get("progress", "0")),
        "total_inserted": int(data.get("total_inserted", "0")),
        "error_message": data.get("error_message", ""),
        "last_update": int(data.get("last_update", "0"))
    }


@router.post("/prune")
async def prune_clickhouse() -> Dict[str, Any]:
    """
    触发 ClickHouse TTL 清理 — 截断 90 天前的历史数据
    """
    try:
        from common.clickhouse import clickhouse_client
        for table in ("klines", "agg_trades", "mark_price"):
            await clickhouse_client.query(f"OPTIMIZE TABLE {table} FINAL")
        logger.warning("control.prune_triggered")
        return {"status": "ok", "message": "ClickHouse TTL OPTIMIZE 已运行"}
    except Exception as e:
        logger.exception("control.prune_error")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 币种管理
# ============================================================

class SymbolsUpdateRequest(BaseModel):
    """币种列表更新请求"""
    symbols: List[str] = Field(..., description="交易对列表, 如 ['BTCUSDT','ETHUSDT']")


@router.get("/symbols")
async def get_symbols() -> Dict[str, Any]:
    """
    获取当前监控的币种列表
    """
    symbols = config_store.get("symbols", ["BTCUSDT"])
    return {"symbols": symbols}


@router.put("/symbols")
async def update_symbols(req: SymbolsUpdateRequest) -> Dict[str, Any]:
    """
    更新监控币种列表

    ✅ 热重载: 通过 Redis Pub/Sub 通知采集器动态 SUBSCRIBE/UNSUBSCRIBE,
    无需重启任何服务即可立即生效。
    """
    # 标准化大写
    normalized = [s.strip().upper() for s in req.symbols if s.strip()]
    if not normalized:
        raise HTTPException(status_code=400, detail="币种列表不能为空")
    if len(normalized) > 10:
        raise HTTPException(status_code=400, detail="最多监控 10 个币种")

    # 验证币种格式 (必须以 USDT 结尾)
    invalid = [s for s in normalized if not s.endswith("USDT")]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的币种格式 (必须 USDT 计价): {invalid}"
        )

    # 计算和旧列表的差集
    old_symbols: List[str] = config_store.get("symbols", ["BTCUSDT"])
    to_add    = [s for s in normalized if s not in old_symbols]
    to_remove = [s for s in old_symbols if s not in normalized]

    # 持久化到数据库 + 更新内存 settings
    await config_store.set("symbols", normalized)
    settings.symbols = normalized

    # 向采集器发布热重载指令 (ADD:XXX / REMOVE:XXX)
    added_live, removed_live = [], []
    for sym in to_add:
        await redis_client.publish("control:symbols", f"ADD:{sym}")
        added_live.append(sym)
    for sym in to_remove:
        await redis_client.publish("control:symbols", f"REMOVE:{sym}")
        removed_live.append(sym)

    logger.info(
        "control.symbols_updated",
        symbols=normalized,
        added=added_live,
        removed=removed_live,
    )
    return {
        "status": "ok",
        "symbols": normalized,
        "added": added_live,
        "removed": removed_live,
        "note": "采集器已收到热重载信号，无需重启。" if (added_live or removed_live)
                else "币种列表无变化。",
    }


@router.get("/db-ranges")
async def get_db_ranges() -> Dict[str, Any]:
    """
    获取 ClickHouse 中各币种的时序数据存储区间及总条数
    """
    from common.clickhouse import clickhouse_client

    # 1. 查询 K 线范围
    kline_sql = """
        SELECT 
            symbol,
            interval,
            toUnixTimestamp(min(open_time)) * 1000 as min_ts,
            toUnixTimestamp(max(open_time)) * 1000 as max_ts,
            count() as cnt
        FROM klines
        GROUP BY symbol, interval
    """

    # 2. 查询明细成交范围
    trade_sql = """
        SELECT 
            symbol,
            toUnixTimestamp(min(timestamp)) * 1000 as min_ts,
            toUnixTimestamp(max(timestamp)) * 1000 as max_ts,
            count() as cnt
        FROM agg_trades
        GROUP BY symbol
    """

    klines_stats = {}
    trades_stats = {}

    try:
        k_res = await clickhouse_client.query(kline_sql)
        if k_res and k_res.result_rows:
            for row in k_res.result_rows:
                key = f"{row[0]}:{row[1]}"
                klines_stats[key] = {
                    "symbol": row[0],
                    "interval": row[1],
                    "min_time": int(row[2]) if row[2] else 0,
                    "max_time": int(row[3]) if row[3] else 0,
                    "count": int(row[4])
                }
    except Exception as e:
        logger.warning("clickhouse.kline_stats_error", error=str(e))

    try:
        t_res = await clickhouse_client.query(trade_sql)
        if t_res and t_res.result_rows:
            for row in t_res.result_rows:
                trades_stats[row[0]] = {
                    "min_time": int(row[1]) if row[1] else 0,
                    "max_time": int(row[2]) if row[2] else 0,
                    "count": int(row[3])
                }
    except Exception as e:
        logger.warning("clickhouse.trade_stats_error", error=str(e))

    return {
        "klines": klines_stats,
        "trades": trades_stats
    }


@router.get("/check-gaps")
async def check_gaps(
    symbol: str = Query(..., description="交易对"),
    interval: str = Query("1m", description="周期"),
):
    """
    检查指定币种和周期在 ClickHouse 中的 K 线断流/缺失区间
    """
    from common.clickhouse import clickhouse_client
    
    interval_minutes_map = {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "1h": 60,
        "4h": 240,
    }
    threshold = interval_minutes_map.get(interval, 1)
    
    sql = """
        SELECT 
            toUnixTimestamp(prev_time) * 1000 as start_ts,
            toUnixTimestamp(open_time) * 1000 as end_ts,
            dateDiff('minute', prev_time, open_time) as gap_minutes
        FROM (
            SELECT 
                open_time,
                lagInFrame(open_time, 1) OVER (ORDER BY open_time ASC) as prev_time
            FROM klines
            WHERE symbol = %(symbol)s AND interval = %(interval)s
        )
        WHERE prev_time > toDateTime('1970-01-02 00:00:00') 
          AND gap_minutes > %(threshold)s
        ORDER BY gap_minutes DESC
        LIMIT 30
    """
    
    try:
        res = await clickhouse_client.query(
            sql, 
            {"symbol": symbol, "interval": interval, "threshold": threshold}
        )
        gaps = []
        if res and res.result_rows:
            for row in res.result_rows:
                gaps.append({
                    "start_time": int(row[0]),
                    "end_time": int(row[1]),
                    "gap_minutes": int(row[2])
                })
        return {
            "status": "success",
            "symbol": symbol,
            "interval": interval,
            "gaps": gaps,
            "has_gaps": len(gaps) > 0
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据库查询失败: {str(e)}")


@router.get("/fit-poisson")
async def fit_poisson(
    symbol: str = Query("BTCUSDT", description="交易对"),
    interval: str = Query("1m", description="K线周期"),
    limit: int = Query(5000, ge=100, le=20000, description="样本数量"),
):
    """
    拟合泊松分布和超额离散参数，分析成交量分布
    """
    from common.clickhouse import clickhouse_client
    import numpy as np
    try:
        from scipy.stats import poisson as scipy_poisson
        HAS_SCIPY = True
    except ImportError:
        HAS_SCIPY = False

    sql = """
        SELECT trades_count
        FROM klines
        WHERE symbol = %(symbol)s AND interval = %(interval)s
        ORDER BY open_time DESC
        LIMIT %(limit)s
    """
    
    try:
        res = await clickhouse_client.query(
            sql, 
            {"symbol": symbol, "interval": interval, "limit": limit}
        )
        if not res or not res.result_rows:
            return {
                "status": "error",
                "message": f"ClickHouse 中没有 {symbol} ({interval}) 的数据，请先回填数据。",
                "lambda": 0,
                "variance": 0,
                "overdispersion": 1.0,
                "anomaly_ratio": 0.0,
                "histogram": []
            }
        
        counts = [int(row[0]) for row in res.result_rows]
        counts_arr = np.array(counts)
        
        lambda_val = float(np.mean(counts_arr))
        variance_val = float(np.var(counts_arr, ddof=1)) if len(counts_arr) > 1 else 0.0
        overdispersion = variance_val / lambda_val if lambda_val > 0 else 1.0
        
        # 异常数据判定比例 (z-score > 3)
        std_val = np.sqrt(variance_val) if variance_val > 0 else 1.0
        anomalies = np.sum(np.abs(counts_arr - lambda_val) / std_val > 3.0)
        anomaly_ratio = float(anomalies) / len(counts_arr)
        
        # 进行直方图统计
        q01 = float(np.percentile(counts_arr, 1))
        q99 = float(np.percentile(counts_arr, 99))
        
        # 产生 15 个 bin
        bin_edges = np.linspace(q01, q99, 16)
        hist, _ = np.histogram(counts_arr, bins=bin_edges)
        
        histogram = []
        total_samples = len(counts_arr)
        
        for i in range(15):
            left = int(bin_edges[i])
            right = int(bin_edges[i+1])
            observed = int(hist[i])
            
            # 计算该区间内的泊松拟合期望值
            # 期望频数 = 样本总数 * P(left <= X <= right)
            if HAS_SCIPY:
                p_left = scipy_poisson.cdf(left - 1, lambda_val) if left > 0 else 0.0
                p_right = scipy_poisson.cdf(right, lambda_val)
                prob = max(p_right - p_left, 0.0)
            else:
                from strategy.poisson_detector import _pure_poisson_cdf
                p_left = _pure_poisson_cdf(left - 1, lambda_val) if left > 0 else 0.0
                p_right = _pure_poisson_cdf(right, lambda_val)
                prob = max(p_right - p_left, 0.0)
                
            poisson_fit = int(round(prob * total_samples))
            
            histogram.append({
                "volume_bucket": f"{int((left+right)/2)}",
                "observed": observed,
                "poisson_fit": poisson_fit
            })
            
        return {
            "status": "success",
            "symbol": symbol,
            "interval": interval,
            "lambda": round(lambda_val, 2),
            "variance": round(variance_val, 2),
            "overdispersion": round(overdispersion, 3),
            "anomaly_ratio": round(anomaly_ratio, 4),
            "histogram": histogram
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拟合计算失败: {str(e)}")


