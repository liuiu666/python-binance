# 回测脚本
from loguru import logger
import pandas as pd
import numpy as np
import config
from trade import TradeExecutor
from strategy import Strategy
import matplotlib.pyplot as plt

def backtest():
    logger.info(f"开始回测 | 交易对: {config.SYMBOL} | 策略: {config.STRATEGY_NAME}")
    
    # 1. 获取历史数据
    executor = TradeExecutor()
    executor.connect()
    
    # 获取最近 1000 根 K 线
    logger.info("正在下载历史数据...")
    klines = executor.get_klines(limit=1500)
    
    if not klines:
        logger.error("无法获取历史数据")
        return

    # 转换为 DataFrame
    df = pd.DataFrame(klines, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
    ])
    df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    
    # 2. 计算指标
    strategy = Strategy()
    df = strategy.calculate_indicators(df)
    
    # 3. 模拟回测循环
    balance = 1000.0 # 初始资金 1000 U
    position = 0     # 持仓数量
    entry_price = 0  # 开仓均价
    stop_loss_price = 0 # 固定的止损价格
    highest_price = 0 # 持仓期间最高价
    lowest_price = 99999999 # 持仓期间最低价
    
    trades = [] # 交易记录
    equity_curve = [] # 资金曲线
    
    logger.info("开始模拟交易...")
    
    # 从第 200 根 K 线开始 (预留 EMA200 计算窗口)
    for i in range(200, len(df)):
        curr = df.iloc[i]
        prev = df.iloc[i-1]
        
        current_price = curr['close']
        current_atr = curr['atr']
        current_rsi = curr['rsi']
        middle_band = curr['bb_middle']
        bb_upper = curr['bb_upper']
        bb_lower = curr['bb_lower']
        ema_trend = curr['ema_trend']
        timestamp = curr['timestamp']
        
        # 模拟 check_signal 逻辑 (需要传入切片数据，但为了回测效率直接用已计算的指标)
        # 这里复刻 strategy.check_signal 的核心判断逻辑
        signal = 0
        
        # 多头趋势 (价格 > EMA200) 只做多
        if current_price > ema_trend:
            if current_price < bb_lower and current_rsi < config.RSI_OVERSOLD:
                signal = 1
                
        # 空头趋势 (价格 < EMA200) 只做空
        elif current_price < ema_trend:
            if current_price > bb_upper and current_rsi > config.RSI_OVERBOUGHT:
                signal = -1
            
        # --- 止盈止损逻辑 (复刻 main.py) ---
        if position != 0:
            # 更新最高/最低价
            if current_price > highest_price: highest_price = current_price
            if current_price < lowest_price: lowest_price = current_price
            
            # 计算盈亏
            pnl_pct = 0
            if position > 0: pnl_pct = (current_price - entry_price) / entry_price
            elif position < 0: pnl_pct = (entry_price - current_price) / entry_price
            
            # 计算最大盈利 ATR 倍数
            max_profit_atr = 0
            if position > 0: max_profit_atr = (highest_price - entry_price) / current_atr
            else: max_profit_atr = (entry_price - lowest_price) / current_atr
            
            action = None # close_long, close_short
            reason = ""
            
            # 1. 利润保护
            if max_profit_atr > config.BREAKEVEN_ATR:
                # 保本
                if position > 0 and current_price < (entry_price + 0.1 * current_atr):
                    action = "close_long"
                    reason = "保本止损"
                elif position < 0 and current_price > (entry_price - 0.1 * current_atr):
                    action = "close_short"
                    reason = "保本止损"
            
            if max_profit_atr > config.TP_TRIGGER_ATR:
                # 移动止盈
                if position > 0 and current_price < (highest_price - config.TP_CALLBACK_ATR * current_atr):
                    action = "close_long"
                    reason = f"移动止盈(回撤{config.TP_CALLBACK_ATR})"
                elif position < 0 and current_price > (lowest_price + config.TP_CALLBACK_ATR * current_atr):
                    action = "close_short"
                    reason = f"移动止盈(回撤{config.TP_CALLBACK_ATR})"
                    
            # 2. 回归中轨止盈 (有利润才走)
            net_pnl_pct = pnl_pct - (config.FEE_RATE * 2)
            if position > 0 and current_price >= middle_band and net_pnl_pct > 0.002:
                action = "close_long"
                reason = "回归中轨止盈"
            elif position < 0 and current_price <= middle_band and net_pnl_pct > 0.002:
                action = "close_short"
                reason = "回归中轨止盈"
                
            # 3. 硬止损 (使用开仓时确定的固定止损价)
            # 检查是否在 K 线内触发
            if position > 0:
                # 多单止损: 最低价跌破止损价
                if curr['low'] < stop_loss_price:
                    action = "close_long"
                    reason = "硬止损"
                    # 确定成交价: 如果开盘就跌破，按开盘价；否则按止损价 (加滑点)
                    if curr['open'] < stop_loss_price:
                        current_price = curr['open']
                    else:
                        current_price = stop_loss_price * (1 - config.SLIPPAGE)
                        
            elif position < 0:
                # 空单止损: 最高价涨破止损价
                if curr['high'] > stop_loss_price:
                    action = "close_short"
                    reason = "硬止损"
                    # 确定成交价: 如果开盘就涨破，按开盘价；否则按止损价 (加滑点)
                    if curr['open'] > stop_loss_price:
                        current_price = curr['open']
                    else:
                        current_price = stop_loss_price * (1 + config.SLIPPAGE)
                
            # 执行平仓
            if action:
                pnl = 0
                fee = abs(position) * current_price * config.FEE_RATE
                
                if action == "close_long":
                    pnl = (current_price - entry_price) * abs(position)
                    # 修正: balance 代表总权益，平仓时只需加上盈亏和减去手续费，不能加回本金(因为开仓没减)
                    balance += pnl - fee
                    position = 0
                elif action == "close_short":
                    pnl = (entry_price - current_price) * abs(position)
                    balance += pnl - fee
                    position = 0
                
                trades.append({
                    'time': timestamp,
                    'type': action,
                    'price': current_price,
                    'pnl': pnl - fee,
                    'reason': reason,
                    'balance': balance
                })
                # logger.info(f"{timestamp} 平仓 | {reason} | 盈亏: {pnl-fee:.2f} | 余额: {balance:.2f}")

        # --- 开仓逻辑 ---
        if position == 0 and signal != 0:
            risk_amount = balance * config.RISK_PERCENT
            stop_loss_dist = 2.5 * current_atr
            if stop_loss_dist == 0: stop_loss_dist = current_price * 0.01 # 防止除0
            
            qty = int(risk_amount / stop_loss_dist)
            if qty * current_price < 6: qty = int(6 / current_price) + 1
            
            fee = qty * current_price * config.FEE_RATE
            
            if signal == 1:
                # 开多
                position = qty
                entry_price = current_price
                stop_loss_price = current_price - stop_loss_dist # 固定止损价
                balance -= fee # 扣除开仓手续费 (简化处理，实际是从余额扣)
                highest_price = current_price
                trades.append({'time': timestamp, 'type': 'open_long', 'price': current_price, 'qty': qty, 'balance': balance, 'pnl': 0, 'reason': '信号开多'})
                # logger.info(f"{timestamp} 开多 | 价格: {current_price}")
                
            elif signal == -1:
                # 开空
                position = -qty
                entry_price = current_price
                stop_loss_price = current_price + stop_loss_dist # 固定止损价
                balance -= fee
                lowest_price = current_price
                trades.append({'time': timestamp, 'type': 'open_short', 'price': current_price, 'qty': qty, 'balance': balance, 'pnl': 0, 'reason': '信号开空'})
                # logger.info(f"{timestamp} 开空 | 价格: {current_price}")

        equity_curve.append({'time': timestamp, 'balance': balance})

    # 4. 输出报告
    df_trades = pd.DataFrame(trades)
    if df_trades.empty:
        logger.warning("回测期间无交易")
        return

    total_trades = len(df_trades[df_trades['type'].str.contains('close')])
    winning_trades = len(df_trades[df_trades['pnl'] > 0])
    win_rate = winning_trades / total_trades * 100 if total_trades > 0 else 0
    total_pnl = balance - 1000.0
    
    logger.success("================ 回测报告 ================")
    logger.info(f"初始资金: 1000 U | 最终资金: {balance:.2f} U")
    logger.info(f"总收益: {total_pnl:.2f} U ({total_pnl/10:.2f}%)")
    logger.info(f"交易次数: {total_trades} | 胜率: {win_rate:.1f}%")
    logger.info("==========================================")
    
    # 打印最近 5 笔交易
    logger.info("最近 5 笔交易详情:")
    logger.info(df_trades.tail(5)[['time', 'type', 'price', 'pnl', 'reason']])

if __name__ == "__main__":
    backtest()
