"""
模块功能：市场数据获取
主要作用：
1. 获取 K 线数据（OHLCV）
2. 获取订单簿（Orderbook）和最新成交（Trades）
3. 获取资金费率、持仓量（Open Interest）、多空比等衍生数据
4. 支持将数据快照保存到本地文件
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from binance.client import Client

from .binance_client import call_with_retry


def _utc_now_str() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


@dataclass(frozen=True)
class FuturesSnapshot:
    symbol: str
    interval: str
    klines: pd.DataFrame
    order_book: dict[str, Any]
    agg_trades: pd.DataFrame
    open_interest: float | None
    funding_rate: pd.DataFrame
    premium_index: dict[str, Any]
    long_short_ratio: pd.DataFrame


class FuturesDataFetcher:
    def __init__(self, client: Client):
        self._client = client

    def fetch_klines(self, symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
        raw = call_with_retry(lambda: self._client.futures_klines(symbol=symbol, interval=interval, limit=limit))
        columns = [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trade_count",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
            "ignore",
        ]
        df = pd.DataFrame(raw, columns=columns)
        for col in [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
        ]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        df["trade_count"] = pd.to_numeric(df["trade_count"], errors="coerce").astype("Int64")

        df["taker_sell_quote_volume"] = (df["quote_volume"] - df["taker_buy_quote_volume"]).clip(lower=0)
        df["资金净流入_估算"] = df["taker_buy_quote_volume"] - df["taker_sell_quote_volume"]
        return df

    def fetch_order_book(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        return call_with_retry(lambda: self._client.futures_order_book(symbol=symbol, limit=limit))

    def fetch_agg_trades(self, symbol: str, limit: int = 1000) -> pd.DataFrame:
        raw = call_with_retry(lambda: self._client.futures_aggregate_trades(symbol=symbol, limit=limit))
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        df["T"] = pd.to_datetime(df["T"], unit="ms", utc=True)
        df["p"] = pd.to_numeric(df["p"], errors="coerce")
        df["q"] = pd.to_numeric(df["q"], errors="coerce")
        df["交易额"] = df["p"] * df["q"]
        df["主动卖出"] = df["m"].astype(bool)
        df["主动买入"] = ~df["主动卖出"]
        return df

    def fetch_open_interest(self, symbol: str) -> float | None:
        data = call_with_retry(lambda: self._client.futures_open_interest(symbol=symbol))
        if not isinstance(data, dict):
            return None
        return _to_float(data.get("openInterest"))

    def fetch_funding_rate(self, symbol: str, limit: int = 100) -> pd.DataFrame:
        raw = call_with_retry(lambda: self._client.futures_funding_rate(symbol=symbol, limit=limit))
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
        df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
        return df

    def fetch_mark_price(self, symbol: str) -> dict[str, Any]:
        return call_with_retry(lambda: self._client.futures_mark_price(symbol=symbol))

    def fetch_global_long_short_ratio(self, symbol: str, period: str = "5m", limit: int = 100) -> pd.DataFrame:
        raw = call_with_retry(
            lambda: self._client.futures_global_longshort_ratio(symbol=symbol, period=period, limit=limit)
        )
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        for col in ["longShortRatio", "longAccount", "shortAccount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def fetch_snapshot(
        self,
        symbol: str,
        interval: str,
        *,
        kline_limit: int = 500,
        orderbook_limit: int = 100,
        agg_trade_limit: int = 1000,
        funding_limit: int = 100,
        ratio_period: str = "5m",
        ratio_limit: int = 100,
    ) -> FuturesSnapshot:
        klines = self.fetch_klines(symbol, interval, limit=kline_limit)
        order_book = self.fetch_order_book(symbol, limit=orderbook_limit)
        agg_trades = self.fetch_agg_trades(symbol, limit=agg_trade_limit)
        open_interest = self.fetch_open_interest(symbol)
        funding_rate = self.fetch_funding_rate(symbol, limit=funding_limit)
        premium_index = self.fetch_mark_price(symbol)
        long_short_ratio = self.fetch_global_long_short_ratio(symbol, period=ratio_period, limit=ratio_limit)

        return FuturesSnapshot(
            symbol=symbol,
            interval=interval,
            klines=klines,
            order_book=order_book,
            agg_trades=agg_trades,
            open_interest=open_interest,
            funding_rate=funding_rate,
            premium_index=premium_index,
            long_short_ratio=long_short_ratio,
        )

    def save_snapshot(self, snapshot: FuturesSnapshot, base_dir: str = "data") -> dict[str, str]:
        ts = _utc_now_str()
        out_dir = f"{base_dir}/{snapshot.symbol}/{ts}"
        from pathlib import Path

        Path(out_dir).mkdir(parents=True, exist_ok=True)

        kline_path = f"{out_dir}/klines_{snapshot.interval}.csv"
        trades_path = f"{out_dir}/agg_trades.csv"
        funding_path = f"{out_dir}/funding_rate.csv"
        ratio_path = f"{out_dir}/long_short_ratio.csv"
        orderbook_path = f"{out_dir}/order_book.json"
        premium_path = f"{out_dir}/premium_index.json"
        meta_path = f"{out_dir}/meta.json"

        snapshot.klines.to_csv(kline_path, index=False, encoding="utf-8-sig")
        snapshot.agg_trades.to_csv(trades_path, index=False, encoding="utf-8-sig")
        snapshot.funding_rate.to_csv(funding_path, index=False, encoding="utf-8-sig")
        snapshot.long_short_ratio.to_csv(ratio_path, index=False, encoding="utf-8-sig")

        import json

        Path(orderbook_path).write_text(json.dumps(snapshot.order_book, ensure_ascii=False), encoding="utf-8")
        Path(premium_path).write_text(json.dumps(snapshot.premium_index, ensure_ascii=False), encoding="utf-8")
        Path(meta_path).write_text(
            json.dumps(
                {
                    "symbol": snapshot.symbol,
                    "interval": snapshot.interval,
                    "open_interest": snapshot.open_interest,
                    "saved_at_utc": ts,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        return {
            "目录": out_dir.replace("\\", "/"),
            "K线": kline_path.replace("\\", "/"),
            "订单簿": orderbook_path.replace("\\", "/"),
            "成交": trades_path.replace("\\", "/"),
            "资金费率": funding_path.replace("\\", "/"),
            "溢价指数": premium_path.replace("\\", "/"),
            "多空比": ratio_path.replace("\\", "/"),
            "元数据": meta_path.replace("\\", "/"),
        }
