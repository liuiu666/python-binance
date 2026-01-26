"""
模块功能：Binance 客户端封装
主要作用：
1. 创建并配置 Binance Client（支持代理、测试网）
2. 封装 API 调用重试机制（call_with_retry）
3. 处理连接异常，增强稳定性
"""
from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from requests import RequestException

from .settings import Settings

T = TypeVar("T")
_CACHED_TS_OFFSET_MS: int | None = None


def create_client(settings: Settings) -> Client:
    if not settings.binance_api_key or not settings.binance_api_secret:
        raise RuntimeError("缺少 BINANCE_API_KEY 或 BINANCE_API_SECRET，无法进行签名请求")

    requests_params: dict[str, Any] = {"timeout": settings.request_timeout_sec}

    proxies: dict[str, str] = {}
    if settings.http_proxy:
        proxies["http"] = settings.http_proxy
    if settings.https_proxy:
        proxies["https"] = settings.https_proxy
    if proxies:
        requests_params["proxies"] = proxies

    client = Client(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
        requests_params=requests_params,
        testnet=False,
    )
    if settings.binance_futures_base_url:
        try:
            client.FUTURES_URL = settings.binance_futures_base_url.rstrip("/")
        except Exception:
            pass

    try:
        client.REQUEST_RECVWINDOW = 10_000
    except Exception:
        pass

    global _CACHED_TS_OFFSET_MS
    if _CACHED_TS_OFFSET_MS is not None:
        try:
            client.timestamp_offset = _CACHED_TS_OFFSET_MS
        except Exception:
            setattr(client, "timestamp_offset", _CACHED_TS_OFFSET_MS)
        return client

    server_ts: int | None = None
    try:
        if hasattr(client, "futures_time"):
            data = client.futures_time()
            if isinstance(data, dict) and data.get("serverTime") is not None:
                server_ts = int(data["serverTime"])
    except Exception:
        server_ts = None

    if server_ts is None:
        try:
            if hasattr(client, "get_server_time"):
                data = client.get_server_time()
                if isinstance(data, dict) and data.get("serverTime") is not None:
                    server_ts = int(data["serverTime"])
        except Exception:
            server_ts = None

    if server_ts is not None:
        local_ts = int(time.time() * 1000)
        _CACHED_TS_OFFSET_MS = server_ts - local_ts
        try:
            client.timestamp_offset = _CACHED_TS_OFFSET_MS
        except Exception:
            setattr(client, "timestamp_offset", _CACHED_TS_OFFSET_MS)
    else:
        if not hasattr(client, "timestamp_offset"):
            setattr(client, "timestamp_offset", 0)
    return client


def call_with_retry(
    fn: Callable[[], T],
    *,
    max_retry: int = 3,
    base_sleep_sec: float = 0.4,
) -> T:
    last_err: Exception | None = None
    for i in range(max_retry):
        try:
            return fn()
        except (BinanceRequestException, BinanceAPIException, RequestException) as e:
            last_err = e
            if i == max_retry - 1:
                raise
            time.sleep(base_sleep_sec * (2**i))
    if last_err:
        raise last_err
    raise RuntimeError("调用失败")
