#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
订单簿区间聚合工具类。

- 根据绝对价格单位或百分比计算区间宽度
- 提供对未聚合的买/卖档位进行区间聚合

用法示例：
    from examples.orderbook_aggregator import OrderbookAggregator

    agg = OrderbookAggregator()
    width = agg.compute_interval_width(current_price, unit_str="0.005", pct_str=None, default_width=0.005)
    rows = agg.aggregate(bids, asks, interval_width=width)
    # rows: [(start, end, bid_volume, ask_volume), ...]
"""

from typing import List, Tuple, Dict, Optional


class OrderbookAggregator:
    def compute_interval_width(
        self,
        current_price: float,
        unit_str: Optional[str] = None,
        pct_str: Optional[str] = None,
        default_width: float = 0.005,
    ) -> float:
        """计算区间宽度，优先使用绝对值，其次百分比，否则使用默认值。"""
        interval_width = default_width
        try:
            if unit_str:
                iw = float(unit_str)
                if iw > 0:
                    interval_width = iw
            elif pct_str:
                pr = float(pct_str)
                if pr > 0 and current_price > 0:
                    interval_width = current_price * pr
        except Exception:
            interval_width = default_width
        return interval_width

    @staticmethod
    def _scaled_interval(interval_width: float) -> int:
        # 用千分位缩放，避免浮点误差；至少为1
        return max(1, int(round(interval_width * 1000)))

    @staticmethod
    def _price_to_interval_scaled(price: float, scaled_interval: int) -> int:
        sp = int(price * 1000)
        return (sp // scaled_interval) * scaled_interval

    def aggregate(
        self,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        interval_width: float,
    ) -> List[Tuple[float, float, float, float]]:
        """将未聚合的买/卖档位聚合到固定区间。

        返回列表: [(interval_start, interval_end, bid_volume, ask_volume), ...]
        """
        if not bids and not asks:
            return []

        scaled_interval = self._scaled_interval(interval_width)

        # 全局范围（覆盖买卖两侧）
        candidates_min, candidates_max = [], []
        if bids:
            candidates_min.append(bids[-1][0])
            candidates_max.append(bids[0][0])
        if asks:
            candidates_min.append(asks[0][0])
            candidates_max.append(asks[-1][0])
        global_min = min(candidates_min)
        global_max = max(candidates_max)

        # 区间范围（缩放坐标）
        start_range_scaled = (int(global_min * 1000) // scaled_interval) * scaled_interval
        end_range_scaled = (int(global_max * 1000) // scaled_interval) * scaled_interval + scaled_interval
        intervals_scaled = list(range(int(start_range_scaled), int(end_range_scaled), scaled_interval))
        if not intervals_scaled:
            return []

        min_start_scaled = intervals_scaled[0]
        max_end_scaled = intervals_scaled[-1] + scaled_interval
        volumes: Dict[int, Dict[str, float]] = {start: {'bid': 0.0, 'ask': 0.0} for start in intervals_scaled}

        # 买单聚合
        for price, qty in bids:
            sp = int(price * 1000)
            if sp < min_start_scaled:
                break
            start_scaled = self._price_to_interval_scaled(price, scaled_interval)
            if start_scaled in volumes and sp < start_scaled + scaled_interval:  # 左闭右开
                volumes[start_scaled]['bid'] += qty

        # 卖单聚合
        for price, qty in asks:
            sp = int(price * 1000)
            if sp >= max_end_scaled:
                break
            start_scaled = self._price_to_interval_scaled(price, scaled_interval)
            if start_scaled in volumes and sp < start_scaled + scaled_interval:
                volumes[start_scaled]['ask'] += qty

        # 反缩放输出
        rows: List[Tuple[float, float, float, float]] = []
        for start_scaled in intervals_scaled:
            start_v = start_scaled / 1000.0
            end_v = (start_scaled + scaled_interval) / 1000.0
            rows.append((start_v, end_v, volumes[start_scaled]['bid'], volumes[start_scaled]['ask']))
        return rows