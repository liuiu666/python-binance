#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
实时打印期货 BTCUSDT 的1000档订单簿（买/卖）文本格式。

使用 OrderBookManager 提供的 update_callback，在每次订单簿更新时重绘终端文本。
可选读取环境变量：
  PROXY_URL=http://127.0.0.1:7897           # HTTP 代理
  SYMBOL=BTCUSDT                            # 交易对（默认BTCUSDT）
"""

import asyncio
import os
import sys
import time
 # 本地直接导入不使用 importlib.util

# 允许从 examples 目录运行并正确导入项目包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from binance.ws.orderbook_manager import OrderBookManager
from binance.async_client import AsyncClient
from orderbook_analyzer import OrderBookAnalyzer


async def main():
    # 从环境变量读取 HTTP 代理地址，例如：PROXY_URL=http://127.0.0.1:7897
    proxy_url = os.getenv('PROXY_URL','http://127.0.0.1:7897')
    symbol = os.getenv('SYMBOL', 'BTCUSDT').upper()

    # 初始化分析器
    analyzer = OrderBookAnalyzer(symbol)
    # 输出文件路径（默认 examples/orderbook_<SYMBOL>_1000.txt，可用环境变量覆盖）
    output_path = os.getenv('ORDERBOOK_TEXT_SAVE_PATH', os.path.join(os.path.dirname(__file__), f'orderbook_{symbol}_1000.txt'))
    output_file = open(output_path, 'w+', encoding='utf-8')
    # 运行统计
    start_time = time.time()
    update_counter = 0
    def _fmt_num(v, places=8):
        try:
            return f"{float(v):.{places}f}"
        except Exception:
            return "N/A"

    # 手续费（需要API密钥），默认不设置则为 None
    api_key = os.getenv('BINANCE_API_KEY') or os.getenv('API_KEY','Pj4PyMhS6GmElbhQVi0n48WvFBEaGHEsT9njacTuBejXLYk7yWyQIDttI0tFLoIf')
    api_secret = os.getenv('BINANCE_API_SECRET') or os.getenv('API_SECRET','8ELgLtB7IFLEbek3DAOtw9orZkXeKbSQpnAL6o4gmi8GDlnsZT1kxZINQqEYVKWb')
    maker_rate = None
    taker_rate = None
    # 24h ticker REST快照（USDT-M），在启动时获取并用于摘要展示
    latest_ticker = {}

    def execute_trading_strategy(analysis):
        """执行交易策略"""
        signal = analysis['trading_signals']
        
        print(f"信号: {signal['signal']}, 置信度: {signal['confidence']:.2%}")
        
        if signal['signal'] == 'BUY' and signal['confidence'] > 0.7:
            # 执行买入逻辑
            print("执行买入操作")
            # place_buy_order()
            
        elif signal['signal'] == 'SELL' and signal['confidence'] > 0.7:
            # 执行卖出逻辑
            print("执行卖出操作")
            # place_sell_order()



    # 更新回调：写入未聚合的1000档（买+卖）到文件
    def on_update(ob):
        nonlocal update_counter, start_time
        update_counter += 1
        ts = ob.get('timestamp', time.time())
        last_id = ob.get('last_update_id', 0)
        bids = ob.get('bids', [])[:1000]  # 降序
        asks = ob.get('asks', [])[:1000]  # 升序
        # 计算常用统计
        best_bid_price = bids[0][0] if bids else None
        best_bid_qty = bids[0][1] if bids else None
        best_ask_price = asks[0][0] if asks else None
        best_ask_qty = asks[0][1] if asks else None
        mid_price = ((best_bid_price + best_ask_price) / 2) if (best_bid_price is not None and best_ask_price is not None) else None
        spread = (best_ask_price - best_bid_price) if (best_bid_price is not None and best_ask_price is not None) else None
        spread_pct = ((spread / best_ask_price) * 100) if (spread is not None and best_ask_price) else 0.0
        total_bid_vol = sum(qty for _, qty in bids) if bids else 0.0
        total_ask_vol = sum(qty for _, qty in asks) if asks else 0.0
        bid_vwap = (sum(price * qty for price, qty in bids) / total_bid_vol) if total_bid_vol > 0 else 0.0
        ask_vwap = (sum(price * qty for price, qty in asks) / total_ask_vol) if total_ask_vol > 0 else 0.0
        denom = (total_bid_vol + total_ask_vol)
        imbalance = ((total_bid_vol - total_ask_vol) / denom) if denom > 0 else 0.0
        # 档位范围
        highest_bid_price = bids[0][0] if bids else None
        lowest_bid_price = bids[-1][0] if bids else None
        lowest_ask_price = asks[0][0] if asks else None
        highest_ask_price = asks[-1][0] if asks else None
        # 人类可读时间
        ts_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))
        elapsed = time.time() - start_time
        freq = (update_counter / elapsed) if elapsed > 0 else 0.0


        analyzer.update_orderbook(bids,asks)
            
        # 生成分析报告
        analysis = analyzer.comprehensive_analysis()
        
            
        # 基于信号执行交易逻辑
        execute_trading_strategy(analysis)

        # 覆盖写入文件头部信息
        output_file.seek(0)
        output_file.truncate()
        output_file.write(f"=== {symbol} 订单簿（未聚合，前1000档）===\n")
        output_file.write(f"更新时间: {ts_str}  (epoch: {ts:.3f})  更新序号: {update_counter}  最后更新ID: {last_id}\n")
        output_file.write(f"- 运行时长: {elapsed:.1f}s  更新频率: {freq:.2f}/s\n")
        output_file.write(f"- 最优买价: {_fmt_num(best_bid_price)} x {_fmt_num(best_bid_qty)}    最优卖价: {_fmt_num(best_ask_price)} x {_fmt_num(best_ask_qty)}\n")
        output_file.write(f"- 中间价: {_fmt_num(mid_price)}    价差: {_fmt_num(spread)}    价差%: {spread_pct:.6f}%\n")
        output_file.write(f"- 买盘档位: {len(bids)}  卖盘档位: {len(asks)}\n")
        output_file.write(f"- 买盘总量: {_fmt_num(total_bid_vol)}  卖盘总量: {_fmt_num(total_ask_vol)}  深度不平衡: {imbalance:.6f}\n")
        output_file.write(f"- 买盘价区间: {_fmt_num(lowest_bid_price)} ~ {_fmt_num(highest_bid_price)}  卖盘价区间: {_fmt_num(lowest_ask_price)} ~ {_fmt_num(highest_ask_price)}\n")
        output_file.write(f"- 买盘VWAP: {_fmt_num(bid_vwap)}  卖盘VWAP: {_fmt_num(ask_vwap)}\n")
        # 写入最新24h Ticker（REST快照，USDT-M）
        if latest_ticker:
            try:
                lp = _fmt_num(latest_ticker.get('last_price'))
                op = _fmt_num(latest_ticker.get('open'))
                hp = _fmt_num(latest_ticker.get('high'))
                lw = _fmt_num(latest_ticker.get('low'))
                ch = _fmt_num(latest_ticker.get('price_change'))
                ch_pct = float(latest_ticker.get('price_change_percent') or 0.0)
                bv = _fmt_num(latest_ticker.get('base_volume'))
                qv = _fmt_num(latest_ticker.get('quote_volume'))
                te = latest_ticker.get('event_time')
                te_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(te/1000)) if te else 'N/A'
                output_file.write(
                    f"- Ticker(24h): last {lp}  open {op}  high {hp}  low {lw}  change {ch} ({ch_pct:.4f}%)  vol {bv}  quote {qv}  [E:{te_str}]\n"
                )
            except Exception:
                output_file.write(f"- Ticker(24h): N/A（解析失败）\n")
        else:
            output_file.write(f"- Ticker(24h): N/A（尚未获取REST快照）\n")
        # 写入手续费（UM合约）
        if maker_rate is not None and taker_rate is not None:
            output_file.write(f"- 手续费(UM): maker: {maker_rate:.6f} ({maker_rate*100:.4f}%)  taker: {taker_rate:.6f} ({taker_rate*100:.4f}%)\n\n")
        else:
            output_file.write(f"- 手续费(UM): N/A（未配置API密钥或查询失败）\n\n")


        # 写入卖盘（从高到低）
        output_file.write("\nASKS (price, quantity):\n")
        for i, (price, qty) in enumerate(reversed(asks), start=1):
            output_file.write(f"{i:4d}  {price:.8f}  {qty:.8f}\n")
          
        # 写入买盘（从高到低）
        output_file.write("BIDS (price, quantity):\n")
        for i, (price, qty) in enumerate(bids, start=1):
            output_file.write(f"{i:4d}  {price:.8f}  {qty:.8f}\n")
            
        output_file.flush()
      

    # 初始化REST客户端并计算首个动态区间（直接构造，避免现货域名 ping）
    client = AsyncClient(api_key=api_key, api_secret=api_secret, https_proxy=proxy_url)
    # 拉取用户期货手续费（需要API密钥），失败则保持为 None
    try:
        if api_key and api_secret:
            commission = await client.futures_commission_rate(symbol=symbol)
            mr = commission.get('makerCommissionRate')
            tr = commission.get('takerCommissionRate')
            maker_rate = float(mr) if mr is not None else None
            taker_rate = float(tr) if tr is not None else None
    except Exception:
        maker_rate = None
        taker_rate = None
    # 拉取24h ticker REST快照（USDT-M）
    try:
        ticker = await client.futures_ticker(symbol=symbol)
        # 映射REST字段到通用结构
        latest_ticker.update({
            'event_time': ticker.get('closeTime'),
            'last_price': ticker.get('lastPrice'),
            'open': ticker.get('openPrice'),
            'high': ticker.get('highPrice'),
            'low': ticker.get('lowPrice'),
            'price_change': ticker.get('priceChange'),
            'price_change_percent': ticker.get('priceChangePercent'),
            'base_volume': ticker.get('volume'),
            'quote_volume': ticker.get('quoteVolume'),
        })
    except Exception as e:
        # 保持为空以在摘要中显示N/A
        print(f"Failed to fetch REST ticker snapshot: {e}")
 

    manager = OrderBookManager(
        symbol=symbol,
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
            output_file.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())