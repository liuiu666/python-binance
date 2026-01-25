"""
模块功能：交易对筛选器
主要作用：
1. 获取所有 USDT 本位永续合约
2. 根据价格、成交量、费率等条件筛选交易对
3. 支持按活跃度（成交量）排序
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
from binance.client import Client

from .binance_client import call_with_retry


@dataclass(frozen=True)
class SymbolFilter:
    max_price: float = 10.0
    min_price: float = 0.0001
    min_quote_volume_24h: float = 20_000_000.0
    top_n: int = 30
    include_fee: bool = False


class FuturesSymbolSelector:
    def __init__(self, client: Client):
        self._client = client

    def list_usdt_perpetual_symbols(self) -> list[str]:
        info = call_with_retry(lambda: self._client.futures_exchange_info())
        symbols: list[str] = []
        for item in info.get("symbols", []):
            if not isinstance(item, dict):
                continue
            if item.get("contractType") != "PERPETUAL":
                continue
            if item.get("quoteAsset") != "USDT":
                continue
            if item.get("status") != "TRADING":
                continue
            symbol = item.get("symbol")
            if isinstance(symbol, str) and symbol:
                symbols.append(symbol)
        return symbols

    def fetch_tickers(self) -> pd.DataFrame:
        raw = call_with_retry(lambda: self._client.futures_ticker())
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        for col in ["lastPrice", "quoteVolume", "volume", "priceChangePercent"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def select_symbols(self, flt: SymbolFilter | None = None) -> pd.DataFrame:
        flt = flt or SymbolFilter()
        allowed = set(self.list_usdt_perpetual_symbols())
        tickers = self.fetch_tickers()
        if tickers.empty:
            return tickers

        tickers = tickers[tickers["symbol"].isin(allowed)].copy()

        if "lastPrice" in tickers.columns:
            tickers = tickers[(tickers["lastPrice"] <= flt.max_price) & (tickers["lastPrice"] >= flt.min_price)]
        if "quoteVolume" in tickers.columns:
            tickers = tickers[tickers["quoteVolume"] >= flt.min_quote_volume_24h]

        keep_cols = [
            "symbol",
            "lastPrice",
            "quoteVolume",
            "volume",
            "priceChangePercent",
        ]
        keep_cols = [c for c in keep_cols if c in tickers.columns]
        tickers = tickers[keep_cols].sort_values(by="quoteVolume", ascending=False)
        if flt.top_n > 0:
            tickers = tickers.head(flt.top_n).reset_index(drop=True)
        if flt.include_fee and not tickers.empty:
            fees = []
            for s in tickers["symbol"].tolist():
                try:
                    r = self.fetch_commission_rate(s)
                    fees.append(
                        {
                            "symbol": s,
                            "maker费率": r.get("makerCommissionRate"),
                            "taker费率": r.get("takerCommissionRate"),
                        }
                    )
                except Exception:
                    fees.append({"symbol": s, "maker费率": None, "taker费率": None})
            fee_df = pd.DataFrame(fees)
            if not fee_df.empty:
                for c in ["maker费率", "taker费率"]:
                    if c in fee_df.columns:
                        fee_df[c] = pd.to_numeric(fee_df[c], errors="coerce")
                tickers = tickers.merge(fee_df, on="symbol", how="left")
        return tickers

    def fetch_commission_rate(self, symbol: str) -> dict[str, Any]:
        return call_with_retry(lambda: self._client.futures_commission_rate(symbol=symbol))
