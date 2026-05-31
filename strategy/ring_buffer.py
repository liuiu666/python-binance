"""
环形缓冲区 — 内存 K 线数据管理
功能:
- 每个 symbol 维护最近 N 根 K 线的 DataFrame
- 新 K 线到来时 append + drop oldest, O(1) 操作
- 支持多周期: 1m / 5m / 15m / 1h (从 1m 数据聚合生成)
- 线程安全, 异步友好
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from common.logger import get_logger

logger = get_logger(__name__)

# 默认缓冲区大小
DEFAULT_BUFFER_SIZE = 500


class RingBuffer:
    """
    单个交易对的 K 线环形缓冲区

    内部使用 pandas DataFrame 存储, 新数据到来时:
    1. 如果是最后一根 K 线的更新 → 替换最后一行
    2. 如果是新的 K 线 → append, 超出容量时删除最旧的
    """

    def __init__(self, symbol: str, max_size: int = DEFAULT_BUFFER_SIZE) -> None:
        self.symbol = symbol
        self._max_size = max_size
        self._df = pd.DataFrame()
        self._last_open_time: int = 0

        # 聚合周期的缓冲区
        self._agg_buffers: Dict[str, pd.DataFrame] = {}

    @property
    def df(self) -> pd.DataFrame:
        """获取当前 K 线 DataFrame"""
        return self._df

    @property
    def size(self) -> int:
        """当前缓冲区中的 K 线数量"""
        return len(self._df)

    @property
    def last_row(self) -> Optional[pd.Series]:
        """获取最后一根 K 线"""
        if self._df.empty:
            return None
        return self._df.iloc[-1]

    def update(self, kline: Dict) -> None:
        """
        更新缓冲区

        Args:
            kline: K 线数据字典, 包含:
                - open_time: 开盘时间 (毫秒时间戳)
                - open_price, high_price, low_price, close_price
                - volume, close_time, is_closed
        """
        open_time = kline.get("open_time", 0)
        is_closed = kline.get("is_closed", False)

        new_row = self._kline_to_row(kline)

        if self._df.empty:
            self._df = pd.DataFrame([new_row])
            self._last_open_time = open_time
            return

        if open_time == self._last_open_time:
            # 更新最后一根 K 线 (同一根 K 线的新 tick)
            # 使用 .at 直接定位赋值, 避免 iloc 链式赋值 (SettingWithCopyWarning)
            idx = self._df.index[-1]
            for col in new_row:
                if col in self._df.columns:
                    self._df.at[idx, col] = new_row[col]
        else:
            # 新的 K 线
            new_df = pd.DataFrame([new_row])
            self._df = pd.concat([self._df, new_df], ignore_index=True)
            self._last_open_time = open_time

            # 超出容量时裁剪
            if len(self._df) > self._max_size:
                self._df = self._df.iloc[-self._max_size:].reset_index(drop=True)

        # 如果 K 线闭合, 更新聚合缓冲区
        if is_closed:
            self._update_aggregated()

    def get_aggregated(self, interval: str) -> pd.DataFrame:
        """
        获取指定周期的聚合 K 线

        Args:
            interval: 目标周期, 如 "5m", "15m", "1h"

        Returns:
            聚合后的 DataFrame
        """
        if interval in self._agg_buffers:
            return self._agg_buffers[interval]
        return pd.DataFrame()

    def _update_aggregated(self) -> None:
        """从 1m 数据聚合生成多周期 K 线"""
        if self._df.empty:
            return

        for interval in ("5m", "15m", "1h"):
            self._agg_buffers[interval] = self._aggregate(self._df, interval)

    @staticmethod
    def _aggregate(df: pd.DataFrame, interval: str) -> pd.DataFrame:
        """
        将 1m K 线聚合为更大周期

        Args:
            df: 1m K 线 DataFrame
            interval: 目标周期

        Returns:
            聚合后的 DataFrame
        """
        if df.empty or "open_time" not in df.columns:
            return pd.DataFrame()

        # 计算分组键
        minutes = {"5m": 5, "15m": 15, "1h": 60}[interval]
        df_copy = df.copy()
        df_copy["group"] = (df_copy["open_time"] // (minutes * 60 * 1000))

        agg = df_copy.groupby("group").agg({
            "open_time": "first",
            "close_time": "last",
            "open_price": "first",
            "high_price": "max",
            "low_price": "min",
            "close_price": "last",
            "volume": "sum",
            "trades_count": "sum",
        }).reset_index(drop=True)

        return agg

    @staticmethod
    def _kline_to_row(kline: Dict) -> Dict:
        """将 K 线字典转为 DataFrame 行"""
        return {
            "open_time": kline.get("open_time", 0),
            "close_time": kline.get("close_time", 0),
            "open_price": float(kline.get("open_price", 0)),
            "high_price": float(kline.get("high_price", 0)),
            "low_price": float(kline.get("low_price", 0)),
            "close_price": float(kline.get("close_price", 0)),
            "volume": float(kline.get("volume", 0)),
            "trades_count": int(kline.get("trades_count", 0)),
            "is_closed": kline.get("is_closed", False),
        }


class BufferManager:
    """
    多交易对的缓冲区管理器
    为每个 symbol 维护独立的 RingBuffer
    """

    def __init__(self, symbols: Optional[List[str]] = None, buffer_size: int = DEFAULT_BUFFER_SIZE) -> None:
        self._symbols = symbols or []
        self._buffer_size = buffer_size
        self._buffers: Dict[str, RingBuffer] = {}

    def get_buffer(self, symbol: str) -> RingBuffer:
        """
        获取指定交易对的缓冲区

        Args:
            symbol: 交易对名称

        Returns:
            RingBuffer 实例
        """
        if symbol not in self._buffers:
            self._buffers[symbol] = RingBuffer(symbol, self._buffer_size)
        return self._buffers[symbol]

    def update(self, symbol: str, kline: Dict) -> None:
        """
        更新指定交易对的 K 线数据

        Args:
            symbol: 交易对
            kline: K 线数据
        """
        buf = self.get_buffer(symbol)
        buf.update(kline)

    def get_dataframe(self, symbol: str) -> pd.DataFrame:
        """
        获取指定交易对的 K 线 DataFrame

        Args:
            symbol: 交易对

        Returns:
            K 线 DataFrame
        """
        return self.get_buffer(symbol).df
