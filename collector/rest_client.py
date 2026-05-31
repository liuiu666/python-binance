"""
币安合约 REST API 客户端
功能:
- K线数据拉取 (历史补缺)
- 标记价格和资金费率查询
- 内置令牌桶限流 (1200 权重/分钟)
- 数据校准: 每 30s 比对 WS 数据, 发现缺失自动补写
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import Any, Dict, List, Optional

import httpx

from common.config import settings
from common.logger import get_logger

logger = get_logger(__name__)


class RateLimiter:
    """
    令牌桶限流器
    币安合约 API 限制: 1200 权重/分钟
    """

    def __init__(
        self,
        max_weight: int = 1200,
        window_seconds: float = 60.0,
    ) -> None:
        self._max_weight = max_weight
        self._window = window_seconds
        self._tokens = max_weight
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        # 收到 429 后的冷却截止时间
        self._cool_until: float = 0.0

    async def acquire(self, weight: int = 1) -> None:
        """
        获取指定权重的令牌, 不足则等待

        Args:
            weight: 请求权重
        """
        while True:
            wait = 0.0

            async with self._lock:
                now = time.monotonic()

                # 冷却期内: 计算等待时间, 但不在锁内 sleep
                if now < self._cool_until:
                    wait = self._cool_until - now
                    logger.warning("ratelimiter.cooling", wait_seconds=f"{wait:.1f}")

                # 补充令牌
                elapsed = now - self._last_refill
                self._tokens = min(
                    self._max_weight,
                    self._tokens + int(elapsed * self._max_weight / self._window),
                )
                self._last_refill = now

                if wait == 0.0 and self._tokens >= weight:
                    self._tokens -= weight
                    return

                # 令牌不足: 计算等待时间, 但不在锁内 sleep
                if wait == 0.0:
                    wait = (weight - self._tokens) * self._window / self._max_weight

            # 在锁外等待, 允许其他协程获取锁
            await asyncio.sleep(max(wait, 0.1))

    def trigger_cooldown(self, cooldown_seconds: float = 60.0) -> None:
        """触发冷却模式 (收到 HTTP 429 时调用)"""
        self._cool_until = time.monotonic() + cooldown_seconds
        logger.warning("ratelimiter.cooldown_triggered", seconds=cooldown_seconds)


class BinanceRESTClient:
    """
    币安合约 REST API 客户端
    支持签名请求和公开数据查询
    """

    def __init__(self) -> None:
        self._base_url = settings.binance_rest_base_url
        self._api_key = settings.binance_api_key
        self._api_secret = settings.binance_api_secret
        self._session: Optional[httpx.AsyncClient] = None
        self._rate_limiter = RateLimiter()

    async def connect(self) -> None:
        """创建 HTTP 连接会话"""
        self._session = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-MBX-APIKEY": self._api_key},
            timeout=httpx.Timeout(10.0),
        )
        logger.info("rest.connected", base_url=self._base_url)

    async def close(self) -> None:
        """关闭连接会话"""
        if self._session:
            await self._session.aclose()
            logger.info("rest.closed")

    # ============================================================
    # 签名请求
    # ============================================================

    def _sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        对请求参数进行 HMAC-SHA256 签名

        Args:
            params: 原始参数

        Returns:
            追加了 timestamp 和 signature 的参数
        """
        params["timestamp"] = int(time.time() * 1000)
        # 币安签名要求参数按原始插入顺序拼接, 不能 sorted() 排序
        query = "&".join(f"{k}={v}" for k, v in params.items())
        signature = hmac.new(
            self._api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature
        return params

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        signed: bool = False,
        weight: int = 1,
    ) -> Any:
        """
        发送 HTTP 请求

        Args:
            method: HTTP 方法
            path: API 路径
            params: 请求参数
            signed: 是否需要签名
            weight: 请求权重

        Returns:
            响应 JSON
        """
        if self._session is None:
            raise RuntimeError("REST 客户端未连接, 请先调用 connect()")

        await self._rate_limiter.acquire(weight)

        params = dict(params) if params else {}
        if signed:
            params = self._sign(params)

        try:
            resp = await self._session.request(method, path, params=params)

            if resp.status_code == 429:
                self._rate_limiter.trigger_cooldown()
                logger.error("rest.rate_limited", path=path)
                return None

            if resp.status_code != 200:
                logger.error(
                    "rest.error",
                    status=resp.status_code,
                    path=path,
                    body=resp.text[:200],
                )
                return None

            return resp.json()
        except httpx.TimeoutException:
            logger.warning("rest.timeout", path=path)
            return None
        except Exception:
            logger.exception("rest.exception", path=path)
            return None

    # ============================================================
    # 公开数据接口
    # ============================================================

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 5,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> Optional[List[Dict]]:
        """
        获取 K 线数据

        Args:
            symbol: 交易对, 如 "BTCUSDT"
            interval: K 线周期, 如 "1m", "5m", "1h"
            limit: 数量, 最大 1500
            start_time: 起始时间 (毫秒时间戳)
            end_time: 结束时间 (毫秒时间戳)

        Returns:
            K 线数据列表, 每条包含 OHLCV 信息
        """
        params: Dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        data = await self._request(
            "GET", "/fapi/v1/klines", params=params, weight=5
        )
        if not data:
            return None

        klines = []
        for item in data:
            klines.append({
                "symbol": symbol,
                "interval": interval,
                "open_time": item[0],
                "open_price": float(item[1]),
                "high_price": float(item[2]),
                "low_price": float(item[3]),
                "close_price": float(item[4]),
                "volume": float(item[5]),
                "close_time": item[6],
                "quote_volume": float(item[7]),
                "trades_count": int(item[8]),
                "taker_buy_volume": float(item[9]),
                "local_recv_ts": time.time() * 1000,
            })
        return klines

    async def get_mark_price(self, symbol: str) -> Optional[Dict]:
        """
        获取标记价格和资金费率

        Args:
            symbol: 交易对

        Returns:
            包含 markPrice, fundingRate, nextFundingTime 的字典
        """
        return await self._request(
            "GET",
            "/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            weight=1,
        )

    # ============================================================
    # 签名接口 (Phase 2 使用)
    # ============================================================

    async def get_account(self) -> Optional[Dict]:
        """获取账户信息 (余额/持仓等)"""
        return await self._request(
            "GET", "/fapi/v2/account", signed=True, weight=5
        )

    async def get_positions(self) -> Optional[List[Dict]]:
        """获取当前持仓"""
        return await self._request(
            "GET", "/fapi/v2/positionRisk", signed=True, weight=5
        )

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """
        设置合约杠杆倍数
        Returns: True 设置成功, False 失败
        """
        try:
            result = await self._request(
                "POST", "/fapi/v1/leverage",
                params={"symbol": symbol, "leverage": leverage},
                signed=True, weight=1,
            )
            if result:
                logger.info("rest_client.leverage_set", symbol=symbol, leverage=leverage)
                return True
            return False
        except Exception:
            logger.exception("rest_client.leverage_error", symbol=symbol)
            return False

    async def place_order(self, params: Dict) -> Optional[Dict]:
        """下单"""
        return await self._request(
            "POST", "/fapi/v1/order", params=params, signed=True, weight=1
        )

    async def cancel_order(
        self, symbol: str, order_id: int
    ) -> Optional[Dict]:
        """撤单"""
        return await self._request(
            "DELETE",
            "/fapi/v1/order",
            params={"symbol": symbol, "orderId": order_id},
            signed=True,
            weight=1,
        )

    async def create_listen_key(self) -> Optional[Dict]:
        """创建 User Data Stream 的 listenKey"""
        return await self._request(
            "POST", "/fapi/v1/listenKey", signed=True, weight=1
        )

    async def keepalive_listen_key(self) -> Optional[Dict]:
        """续期 listenKey"""
        return await self._request(
            "PUT", "/fapi/v1/listenKey", signed=True, weight=1
        )


# ============================================================
# 数据校准器
# ============================================================

class DataCompensator:
    """
    REST 数据校准器
    定期从 REST API 拉取 K 线数据, 与内存中的数据进行比对:
    - 发现缺失 → 补写 Redis Streams
    - 发现不一致 → 以 REST 为准, 记录告警
    """

    def __init__(
        self,
        rest_client: BinanceRESTClient,
        symbols: Optional[List[str]] = None,
    ) -> None:
        self._rest = rest_client
        self._symbols = symbols or settings.symbols
        self._interval = settings.rest_compensate_interval
        self._running = False
        # 内存中最近的 K 线缓存: {symbol: {open_time: kline_dict}}
        self._kline_cache: Dict[str, Dict[int, Dict]] = {}

    def update_kline_cache(self, symbol: str, kline: Dict) -> None:
        """
        更新内存中的 K 线缓存 (WS 推送时调用)

        Args:
            symbol: 交易对
            kline: K 线数据, 必须包含 open_time 字段
        """
        if symbol not in self._kline_cache:
            self._kline_cache[symbol] = {}
        self._kline_cache[symbol][kline["open_time"]] = kline

        # 清理旧条目, 只保留最近 50 根 K 线的缓存, 防止内存无限增长
        cache = self._kline_cache[symbol]
        if len(cache) > 50:
            sorted_keys = sorted(cache.keys())
            for old_key in sorted_keys[:-50]:
                del cache[old_key]

    async def start(self) -> None:
        """启动校准循环"""
        self._running = True
        logger.info("compensator.started", interval=self._interval)

        while self._running:
            await asyncio.sleep(self._interval)
            for symbol in self._symbols:
                await self._compensate(symbol)

    async def stop(self) -> None:
        """停止校准"""
        self._running = False
        logger.info("compensator.stopped")

    async def _compensate(self, symbol: str) -> None:
        """
        校准单个交易对的数据
        拉取最近 5 根 K 线, 与缓存比对
        发现缺失或不一致时, 将 REST 数据补写到 Redis Streams
        """
        klines = await self._rest.get_klines(symbol, limit=5)
        if not klines:
            return

        cached = self._kline_cache.get(symbol, {})
        missing_count = 0
        mismatch_count = 0
        # 需要补写到 Redis 的 K 线列表
        to_compensate = []

        for kline in klines:
            open_time = kline["open_time"]
            if open_time not in cached:
                missing_count += 1
                to_compensate.append(kline)
                logger.info(
                    "compensator.missing_kline",
                    symbol=symbol,
                    open_time=open_time,
                )
            else:
                cached_kline = cached[open_time]
                if not self._klines_match(cached_kline, kline):
                    mismatch_count += 1
                    to_compensate.append(kline)
                    logger.warning(
                        "compensator.mismatch",
                        symbol=symbol,
                        open_time=open_time,
                        cached_close=cached_kline.get("close_price"),
                        rest_close=kline["close_price"],
                    )

        # 补写缺失/不一致的 K 线到 Redis Streams 和 ClickHouse
        if to_compensate:
            from common.redis_client import redis_client, STREAM_MARKET
            stream_name = STREAM_MARKET.format(symbol=symbol.lower())
            for kline in to_compensate:
                kline["is_closed"] = True
                kline["source"] = "compensator"
                # 写入 Redis Streams (供策略消费)
                await redis_client.xadd(stream_name, kline, maxlen=10000)
            # 同步写入 ClickHouse (供 AI 调参和历史分析)
            try:
                from common.clickhouse import clickhouse_client
                # 过滤掉 ClickHouse 表中不存在的字段
                ch_fields = {
                    "symbol", "open_time", "close_time", "interval",
                    "open_price", "high_price", "low_price", "close_price",
                    "volume", "quote_volume", "trades_count", "taker_buy_volume",
                    "local_recv_ts",
                }
                for kline in to_compensate:
                    clean = {k: v for k, v in kline.items() if k in ch_fields}
                    await clickhouse_client.insert("klines", clean)
                # 立即刷新缓冲区
                await clickhouse_client._flush_table("klines")
            except Exception:
                logger.exception("compensator.clickhouse_error", symbol=symbol)
            logger.info(
                "compensator.compensated",
                symbol=symbol,
                count=len(to_compensate),
            )

        if missing_count or mismatch_count:
            logger.info(
                "compensator.result",
                symbol=symbol,
                total=len(klines),
                missing=missing_count,
                mismatch=mismatch_count,
                compensated=len(to_compensate),
            )

    @staticmethod
    def _klines_match(a: Dict, b: Dict, tolerance: float = 0.01) -> bool:
        """
        比较两条 K 线是否一致 (允许微小的浮点误差)

        Args:
            a: K 线 A
            b: K 线 B
            tolerance: 允许的相对误差

        Returns:
            是否一致
        """
        for field in ("open_price", "high_price", "low_price", "close_price", "volume"):
            va = a.get(field, 0)
            vb = b.get(field, 0)
            if abs(va - vb) > tolerance * max(abs(va), abs(vb), 1e-10):
                return False
        return True


# 全局单例
rest_client = BinanceRESTClient()
