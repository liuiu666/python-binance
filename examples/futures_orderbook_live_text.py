#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
实时打印期货 BTCUSDT 的1000档订单簿（买/卖）文本格式。

使用 OrderBookManager 提供的 update_callback，在每次订单簿更新时重绘终端文本。
可选读取环境变量：
  PROXY_URL=http://127.0.0.1:7897           # HTTP 代理
  AGG_INTERVAL_UNIT=0.005                   # 区间宽度（绝对价格单位，优先）
  AGG_INTERVAL_PCT=0.005                    # 区间宽度（百分比，相对当前价），示例0.005=0.5%
"""

import asyncio
import os
import sys
import csv
import time
 # 本地直接导入不使用 importlib.util

# 允许从 examples 目录运行并正确导入项目包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from binance.ws.orderbook_manager import OrderBookManager
from binance.async_client import AsyncClient


async def main():
    # 从环境变量读取 HTTP 代理地址，例如：PROXY_URL=http://127.0.0.1:7897
    proxy_url = os.getenv('PROXY_URL','http://127.0.0.1:7897')
    # CSV 路径（保存在 examples 目录下）
    csv_path = os.path.join(os.path.dirname(__file__), 'orderbook_BTCUSDT.csv')

    # 以覆盖模式打开 CSV 文件，始终只保存最新一次更新的1000档（买+卖）
    csv_file = open(csv_path, 'w+', newline='', encoding='utf-8')
    csv_writer = csv.writer(csv_file)



    # 组合更新回调：区间聚合写入CSV，并计算动态窗口传入检测器
    def on_update(ob):
        import time
        # 读取订单簿数据
        ts = ob.get('timestamp', time.time())
        last_id = ob.get('last_update_id', 0)
        bids = ob.get('bids', [])[:1000]  # 降序
        asks = ob.get('asks', [])[:1000]  # 升序

        # 当前价用订单簿中间价
        def derive_last_price(bids, asks):
            if bids and asks:
                return (bids[0][0] + asks[0][0]) / 2.0
            elif bids:
                return bids[0][0]
            elif asks:
                return asks[0][0]
            else:
                return 0.0

        current_price = derive_last_price(bids, asks)
        if current_price <= 0:
            return

        # 动态可配置的区间宽度：
        # - 绝对价格单位：环境变量 AGG_INTERVAL_UNIT（例如 "0.005" 表示每0.005一个区间）
        # - 百分比（相对当前价）：环境变量 AGG_INTERVAL_PCT（例如 "0.005" 表示0.5%）
        # 优先级：AGG_INTERVAL_UNIT > AGG_INTERVAL_PCT > 默认 0.01
        interval_width = 0.001
        unit_str = os.getenv('AGG_INTERVAL_UNIT')
        pct_str = os.getenv('AGG_INTERVAL_PCT')
        try:
            if unit_str:
                iw = float(unit_str)
                if iw > 0:
                    interval_width = iw
            elif pct_str:
                pr = float(pct_str)
                if pr > 0:
                    interval_width = current_price * pr
        except Exception:
            # 非法环境变量值时回退默认
            interval_width = 0.01

        # 价格→区间起点映射（左闭右开），用千分位缩放避免浮点误差与浮点步长问题
        scaled_interval = max(1, int(round(interval_width * 1000)))  # 步长（千分之一为单位），至少1避免为0

        def price_to_interval_scaled(price):
            sp = int(price * 1000)
            return (sp // scaled_interval) * scaled_interval

        # 以当前订单簿1000档的完整价格范围生成区间
        if not bids and not asks:
            return  # 无数据则不写

        # 全局最小/最大价格（覆盖买卖两侧的1000档）
        candidates_min = []
        candidates_max = []
        if bids:
            candidates_min.append(bids[-1][0])   # 买单最低价（列表末尾）
            candidates_max.append(bids[0][0])    # 买单最高价（列表首位）
        if asks:
            candidates_min.append(asks[0][0])    # 卖单最低价（列表首位）
            candidates_max.append(asks[-1][0])   # 卖单最高价（列表末尾）

        global_min = min(candidates_min)
        global_max = max(candidates_max)

        # 区间起止（缩放后向下取整至区间起点，右端加一个区间保证左闭右开）
        start_range_scaled = (int(global_min * 1000) // scaled_interval) * scaled_interval
        end_range_scaled = (int(global_max * 1000) // scaled_interval) * scaled_interval + scaled_interval

        # 生成覆盖整个范围的区间列表（缩放坐标）
        intervals_scaled = list(range(int(start_range_scaled), int(end_range_scaled), scaled_interval))
        if not intervals_scaled:
            return
        min_start_scaled = intervals_scaled[0]
        max_end_scaled = intervals_scaled[-1] + scaled_interval

        # 准备聚合容器
        volumes = {start: {'bid': 0.0, 'ask': 0.0} for start in intervals_scaled}

        # 买单聚合：降序遍历，价格低于最小区间起点则提前停止
        for price, qty in bids:
            sp = int(price * 1000)
            if sp < min_start_scaled:
                break
            start_scaled = price_to_interval_scaled(price)
            if start_scaled in volumes and sp < start_scaled + scaled_interval:  # 左闭右开
                volumes[start_scaled]['bid'] += qty

        # 卖单聚合：升序遍历，价格达到最大区间右端则提前停止
        for price, qty in asks:
            sp = int(price * 1000)
            if sp >= max_end_scaled:
                break
            start_scaled = price_to_interval_scaled(price)
            if start_scaled in volumes and sp < start_scaled + scaled_interval:  # 左闭右开
                volumes[start_scaled]['ask'] += qty

        # 覆盖写入 CSV（仅保存最新聚合结果）
        csv_file.seek(0)
        csv_file.truncate()
        csv_writer.writerow(['timestamp', 'last_update_id', 'interval_start', 'interval_end', 'bid_volume', 'ask_volume'])
        for start_scaled in intervals_scaled:
            start_v = start_scaled / 1000.0
            end_v = (start_scaled + scaled_interval) / 1000.0
            csv_writer.writerow([
                ts,
                last_id,
                f"{start_v:.3f}",
                f"{end_v:.3f}",
                f"{volumes[start_scaled]['bid']:.8f}",
                f"{volumes[start_scaled]['ask']:.8f}",
            ])
        csv_file.flush()

    # 初始化REST客户端并计算首个动态区间（直接构造，避免现货域名 ping）
    client = AsyncClient(https_proxy=proxy_url)
 

    manager = OrderBookManager(
        symbol="MINAUSDT",
        proxy_url=proxy_url,
        update_callback=on_update,
    )

    try:
        # 无限运行，按 Ctrl+C 退出
        await manager.run(duration=None)
    except KeyboardInterrupt:
        pass
    finally:
        await manager.close()
      
        try:
            await client.close_connection()
        except Exception:
            pass
        try:
            csv_file.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())