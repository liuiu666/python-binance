"""
模块功能：账户数据获取
主要作用：
1. 获取合约账户余额（USDT）
2. 获取当前持仓信息（包括未实现盈亏、保证金等）
3. 获取当前挂单信息
4. 提供账户数据的快照功能，用于决策时的状态参考
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandas as pd
from binance.client import Client

from .binance_client import call_with_retry


def _d(v: Any) -> Decimal:
    return Decimal(str(v))


@dataclass(frozen=True)
class AccountSnapshot:
    usdt_available_balance: Decimal | None
    positions: pd.DataFrame
    open_orders: pd.DataFrame


class FuturesAccountData:
    def __init__(self, client: Client):
        self._client = client

    def fetch_account(self) -> dict[str, Any]:
        return call_with_retry(lambda: self._client.futures_account())

    def fetch_account_balance(self) -> pd.DataFrame:
        raw = call_with_retry(lambda: self._client.futures_account_balance())
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        for col in ["balance", "withdrawAvailable", "crossWalletBalance", "crossUnPnl", "availableBalance", "maxWithdrawAmount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def fetch_positions(self, symbol: str | None = None) -> pd.DataFrame:
        raw = call_with_retry(lambda: self._client.futures_position_information(symbol=symbol) if symbol else self._client.futures_position_information())
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        for col in [
            "positionAmt",
            "entryPrice",
            "markPrice",
            "unRealizedProfit",
            "liquidationPrice",
            "leverage",
            "initialMargin",
            "maintMargin",
            "positionInitialMargin",
            "openOrderInitialMargin",
            "isolatedMargin",
            "notional",
        ]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def fetch_open_orders(self, symbol: str | None = None) -> pd.DataFrame:
        raw = call_with_retry(lambda: self._client.futures_get_open_orders(symbol=symbol) if symbol else self._client.futures_get_open_orders())
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        return df

    def snapshot(self, symbol: str | None = None) -> AccountSnapshot:
        bal = self.fetch_account_balance()
        usdt_avail: Decimal | None = None
        if not bal.empty and "asset" in bal.columns:
            row = bal[bal["asset"] == "USDT"]
            if not row.empty:
                if "availableBalance" in row.columns:
                    v = row.iloc[0]["availableBalance"]
                    if v is not None:
                        usdt_avail = _d(v)
                elif "withdrawAvailable" in row.columns:
                    v = row.iloc[0]["withdrawAvailable"]
                    if v is not None:
                        usdt_avail = _d(v)

        positions = self.fetch_positions(symbol)
        open_orders = self.fetch_open_orders(symbol)
        return AccountSnapshot(
            usdt_available_balance=usdt_avail,
            positions=positions,
            open_orders=open_orders,
        )

