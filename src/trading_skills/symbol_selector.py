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
from .data_fetcher import FuturesDataFetcher

try:
    from analysis.smart_analyzer import analyze_symbol
except ImportError:
    from src.analysis.smart_analyzer import analyze_symbol

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
                    fee_df[c] = pd.to_numeric(fee_df[c], errors="coerce")
                tickers = pd.merge(tickers, fee_df, on="symbol", how="left")
        return tickers

    def fetch_commission_rate(self, symbol: str) -> dict[str, Any]:
        return call_with_retry(lambda: self._client.futures_commission_rate(symbol=symbol))

    def get_candidates_by_mode(self, mode: str = "small", limit: int = 20) -> list[str]:
        """
        根据预设模式获取目标分析币种
        mode='hot': 热门币 (成交量前N)
        mode='small': 小市值/次热门 (成交量排名 30-60，且>5M成交额)
        mode='cheap': 便宜小币 (价格<5U, 成交量>5M, 排除前20热门)
        """
        try:
            tickers = call_with_retry(lambda: self._client.futures_ticker())
            # Filter for USDT pairs ending in USDT, exclude special symbols
            usdt_tickers = [
                t for t in tickers 
                if t['symbol'].endswith('USDT') and not t['symbol'].startswith('_')
            ]
            
            # Sort by quote volume (volume in USDT)
            sorted_tickers = sorted(usdt_tickers, key=lambda x: float(x['quoteVolume']), reverse=True)
            
            if mode == 'hot':
                return [t['symbol'] for t in sorted_tickers[:limit]]
                
            elif mode == 'small':
                # 跳过前20个热门币，选取之后的币种
                start_rank = 20
                filtered = [
                    t for t in sorted_tickers[start_rank:] 
                    if float(t['quoteVolume']) > 5_000_000
                ]
                return [t['symbol'] for t in filtered[:limit]]
                
            elif mode == 'cheap':
                # 便宜小币: 价格低 + 排除超级热门 + 有一定流动性
                start_rank = 20
                filtered = [
                    t for t in sorted_tickers[start_rank:] 
                    if float(t['quoteVolume']) > 5_000_000 and float(t['lastPrice']) < 5.0
                ]
                return [t['symbol'] for t in filtered[:limit]]
            
            else:
                # Default fallback
                return [t['symbol'] for t in sorted_tickers[:limit]]
                
        except Exception as e:
            # Add logging here if possible, but print for now as in original
            print(f"获取币种失败: {e}")
            return []

    def get_smart_candidates(self, mode: str = "small", limit: int = 5) -> list[str]:
        """
        获取通过量化分析筛选的币种
        注意: 此方法需要请求K线数据，速度较慢
        """
        # 扩大初筛范围，确保能找到足够多的合格标的
        # 至少扫描 15 个，或者 limit 的 5 倍
        scan_limit = max(15, limit * 5)
        raw_candidates = self.get_candidates_by_mode(mode=mode, limit=scan_limit)
        
        print(f"开始智能筛选 (模式: {mode}, 扫描: {len(raw_candidates)} 个)...")
        
        fetcher = FuturesDataFetcher(self._client)
        results = []
        
        for symbol in raw_candidates:
            try:
                # 随机休眠避免API限制
                # time.sleep(0.1) 
                
                data = fetcher.fetch_analysis_data(symbol)
                res = analyze_symbol(data)
                
                if res['passed_screening']:
                    results.append(res)
            except Exception as e:
                print(f"分析失败 {symbol}: {e}")
                continue
        
        # 按分数降序排列
        results.sort(key=lambda x: x['score'], reverse=True)
        
        # 返回前 limit 个
        return [r['symbol'] for r in results[:limit]]
