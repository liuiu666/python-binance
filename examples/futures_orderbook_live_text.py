#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
实时打印期货 BTCUSDT 的1000档订单簿（买/卖）文本格式。

使用 OrderBookManager 提供的 update_callback，在每次订单簿更新时重绘终端文本。
可选读取环境变量 PROXY_URL 作为 HTTP 代理，例如：
  PROXY_URL=http://127.0.0.1:7897
"""

import asyncio
import os
import sys
import csv
import time

# 允许从 examples 目录运行并正确导入项目包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from binance.ws.orderbook_manager import OrderBookManager


def render_orderbook(ob):
    """禁用终端渲染：不在控制台打印任何内容。"""
    return


async def main():
    # 从环境变量读取 HTTP 代理地址，例如：PROXY_URL=http://127.0.0.1:7897
    proxy_url = os.getenv('PROXY_URL')
    # CSV 路径（保存在 examples 目录下）
    csv_path = os.path.join(os.path.dirname(__file__), 'orderbook_BTCUSDT.csv')

    # 以覆盖模式打开 CSV 文件，始终只保存最新一次更新的1000档（买+卖）
    csv_file = open(csv_path, 'w+', newline='', encoding='utf-8')
    csv_writer = csv.writer(csv_file)

    # 组合更新回调：渲染到终端 + 追加写入CSV
    def on_update(ob):
        # 不进行任何终端输出，仅写入CSV

        # 覆盖写入 CSV（仅保存最新1000档买单与卖单）
        csv_file.seek(0)
        csv_file.truncate()
        csv_writer.writerow(['timestamp', 'last_update_id', 'side', 'level', 'price', 'quantity'])

        ts = ob.get('timestamp', time.time())
        last_id = ob.get('last_update_id', 0)
        bids = ob.get('bids', [])[:1000]
        asks = ob.get('asks', [])[:1000]

        for i, (price, qty) in enumerate(bids, 1):
            csv_writer.writerow([ts, last_id, 'bid', i, f"{price:.8f}", f"{qty:.8f}"])
        for i, (price, qty) in enumerate(asks, 1):
            csv_writer.writerow([ts, last_id, 'ask', i, f"{price:.8f}", f"{qty:.8f}"])
        csv_file.flush()

    manager = OrderBookManager(
        symbol="BTCUSDT",
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
            csv_file.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())