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
 # 本地直接导入不使用 importlib.util

# 允许从 examples 目录运行并正确导入项目包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from binance.ws.orderbook_manager import OrderBookManager
from binance.async_client import AsyncClient

# 顶层本地直接导入信号检测模块
import futures_signal_detector as sd_module
SignalDetector = sd_module.SignalDetector


async def main():
    # 从环境变量读取 HTTP 代理地址，例如：PROXY_URL=http://127.0.0.1:7897
    proxy_url = os.getenv('PROXY_URL','http://127.0.0.1:7897')
    # CSV 路径（保存在 examples 目录下）
    csv_path = os.path.join(os.path.dirname(__file__), 'orderbook_BTCUSDT.csv')

    # 以覆盖模式打开 CSV 文件，始终只保存最新一次更新的1000档（买+卖）
    csv_file = open(csv_path, 'w+', newline='', encoding='utf-8')
    csv_writer = csv.writer(csv_file)

    # 动态区间状态：由K线“平均百分比振幅”驱动，每30分钟更新一次
    interval_state = {
        'size': 0.001,             # 初始占位为比例(≈0.1%)，启动后会立刻用K线计算覆盖
    }

    async def compute_avg_amplitude(client: AsyncClient) -> float:
        """获取BTCUSDT最近100根1分钟K线的平均百分比振幅 ((高-低)/收盘价)，并取其10%。"""
        try:
            klines = await client.futures_klines(
                symbol='BTCUSDT',
                interval=AsyncClient.KLINE_INTERVAL_1MINUTE,
                limit=100,
            )
            if not klines:
                return interval_state['size']
            total = 0.0
            count = 0
            for k in klines:
                # kline结构: [openTime, open, high, low, close, volume, closeTime, ...]
                high = float(k[2])
                low = float(k[3])
                close = float(k[4])
                if close <= 0:
                    continue
                ratio = max(0.0, (high - low) / close)
                total += ratio
                count += 1
            if count == 0:
                return interval_state['size']
            avg_ratio = total / count
            return avg_ratio * 0.1
        except Exception:
            # 网络或API异常时保持上次区间，不引入假数据
            return interval_state['size']

    async def interval_updater(client: AsyncClient):
        """每30分钟更新一次聚合区间大小。"""
        while True:
            new_size = await compute_avg_amplitude(client)
            interval_state['size'] = new_size
            await asyncio.sleep(30 * 60)  # 30分钟

    # 使用顶层导入的 SignalDetector

    detector = None

    # 组合更新回调：区间聚合写入CSV，并计算动态窗口传入检测器
    def on_update(ob):
        import time

        # 最近60根K线的平均“百分比振幅”
        avg_ratio = float(interval_state['size'])

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

        # 区间宽度 = 当前价 × 百分比振幅
        interval_width = current_price * avg_ratio

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
        snapshot_rows = []  # [(start, end, bid_vol, ask_vol)]
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
            snapshot_rows.append((start_v, end_v, volumes[start_scaled]['bid'], volumes[start_scaled]['ask']))
        csv_file.flush()

        # 动态窗口大小（基于本次快照真实量分布）：活动越强，窗口越小；活动越弱，窗口越大
        if detector and snapshot_rows:
            combined = [r[2] + r[3] for r in snapshot_rows]
            count = len(combined)
            mean_vol = sum(combined) / count if count > 0 else 0.0
            sorted_comb = sorted(combined)
            idx95 = max(0, min(count - 1, int(0.95 * count)))
            p95 = sorted_comb[idx95] if count > 0 else 0.0
            activity_ratio = (p95 / mean_vol) if mean_vol > 0 else 0.0
            base_window = 10
            min_window = 4
            max_window = 40
            gamma = 0.8
            scale = 1.0 + gamma * max(0.0, min(activity_ratio - 1.0, 9.0))
            window_size = int(max(min_window, min(max_window, round(base_window / scale))))
            window_size = max(window_size, 2)


            try:
                detector.process_snapshot(snapshot_rows=snapshot_rows, window_size=window_size, timestamp=ts)
            except Exception:
                # 保持主流程稳定；检测器内部异常不影响CSV输出
                pass

    # 初始化REST客户端并计算首个动态区间（直接构造，避免现货域名 ping）
    client = AsyncClient(https_proxy=proxy_url)
    try:
        interval_state['size'] = await compute_avg_amplitude(client)
    except Exception:
        pass  # 启动失败时沿用默认值

    # 启动30分钟更新任务
    asyncio.create_task(interval_updater(client))

    # 启动信号检测器（aggTrade 成交量确认），与现有客户端共享连接
    try:
        detector = SignalDetector(symbol="BTCUSDT")
        await detector.start(client)
    except Exception:
        detector = None

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
        # 关闭检测器任务
        try:
            if detector:
                await detector.stop()
        except Exception:
            pass
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