import time
from loguru import logger
import config
from trade import TradeExecutor
from strategy import Strategy

def main():
    logger.info(f"自动交易系统启动 | 交易对: {config.SYMBOL} | 仓位管理: 动态风险 ({config.RISK_PERCENT*100}%)")
    logger.info(f"策略: {config.STRATEGY_NAME} | 周期: {config.TIMEFRAME}")
    
    # 初始化
    executor = TradeExecutor()
    executor.connect()
    strategy = Strategy()
    
    # 记录最高/最低价用于移动止盈
    highest_price = 0
    lowest_price = 99999999
    stop_loss_price = 0 # 记录固定止损价
    
    # 网络错误计数器
    error_count = 0

    while True:
        try:
            # 1. 获取持仓信息
            pos_info = executor.get_position()
            
            # 错误处理与自动重连
            if pos_info is None:
                error_count += 1
                logger.warning(f"获取持仓失败 (连续错误: {error_count})")
                if error_count > 3:
                    logger.warning("连续失败超过 3 次，尝试重新连接 API...")
                    executor.connect()
                    error_count = 0 # 重置计数
                    time.sleep(5)
                time.sleep(3)
                continue
                
            # 重置错误计数 (如果成功获取)
            error_count = 0
            
            position = pos_info['amt']
            entry_price = pos_info['entryPrice']
            unrealized_pnl = pos_info['unRealizedProfit'] # 币安计算的未实现盈亏(U)
            
            # 如果没有持仓，重置最高/最低价
            if position == 0:
                highest_price = 0
                lowest_price = 99999999
                stop_loss_price = 0
            
            # 2. 获取数据
            if config.HFT_MODE:
                # --- 高频模式 (基于盘口) ---
                depth = executor.get_orderbook(limit=config.DEPTH_LIMIT)
                hft_signal, imbalance = strategy.analyze_orderbook(depth)
                
                # 获取最新价格用于计算
                klines = executor.get_klines(limit=1)
                if klines:
                    current_price = float(klines[-1][4])
                else:
                    time.sleep(1)
                    continue

                # 高频开仓逻辑
                if position == 0 and hft_signal != 0:
                    balance = executor.get_balance()
                    quantity = int((balance * 0.95) / current_price) # 高频几近满仓(需谨慎)
                    # 也可以用固定小仓位刷单
                    # quantity = int(100 / current_price) # 固定 100U
                    
                    if quantity * current_price < 6: quantity = int(6 / current_price) + 1

                    if hft_signal == 1:
                        logger.info(f"盘口强买 ({imbalance:.2f}) -> 高频开多")
                        executor.place_order(side=1, quantity=quantity)
                        entry_price = current_price # 模拟更新，实际下个循环会更新
                    elif hft_signal == -1:
                        logger.info(f"盘口强卖 ({imbalance:.2f}) -> 高频开空")
                        executor.place_order(side=-1, quantity=quantity)
                        entry_price = current_price

                # 高频平仓逻辑 (止盈止损)
                if abs(position) > 0:
                    # 计算浮动盈亏 (百分比)
                    pnl_pct = 0
                    if position > 0: pnl_pct = (current_price - entry_price) / entry_price
                    elif position < 0: pnl_pct = (entry_price - current_price) / entry_price
                    
                    # 扣除手续费后的净利
                    net_pnl = pnl_pct - (config.FEE_RATE * 2)
                    
                    # 止盈
                    if net_pnl > config.SCALP_TP:
                        logger.success(f"高频止盈 ({net_pnl*100:.2f}%)")
                        executor.place_order(side=-1 if position > 0 else 1, quantity=abs(position))
                    
                    # 止损
                    elif net_pnl < -config.SCALP_SL:
                        logger.warning(f"高频止损 ({net_pnl*100:.2f}%)")
                        executor.place_order(side=-1 if position > 0 else 1, quantity=abs(position))
                
                logger.info("--------------------------------")
                time.sleep(config.HFT_INTERVAL)
                continue # 跳过后续的波段逻辑

            # --- 波段模式 (原有逻辑) ---
            klines = executor.get_klines(limit=300)
            if not klines:
                logger.warning("获取 K 线数据失败")
                time.sleep(3)
                continue
                
            current_price = float(klines[-1][4]) # 最新收盘价
            
            # 3. 策略分析
            # 返回: 信号, ATR, 布林中轨, RSI, ADX, 市场模式
            signal, current_atr, middle_band, current_rsi, adx, market_mode = strategy.check_signal(klines)
            
            # 恢复持仓时的止损价初始化
            if abs(position) > 0 and stop_loss_price == 0 and current_atr > 0:
                if position > 0:
                    stop_loss_price = entry_price - 2.5 * current_atr
                else:
                    stop_loss_price = entry_price + 2.5 * current_atr
                logger.info(f"恢复持仓监控 | 估算硬止损价: {stop_loss_price:.4f}")

            # 动态计算当前理论满仓数量 (用于显示或调试)
            balance = executor.get_balance()
            if balance is None: balance = 0 # 获取余额失败则视为 0，跳过开仓
            
            # --- 动态止盈止损逻辑 (RSI + ATR 利润锁定) ---
            if abs(position) > 0:
                # 初始化追踪价格 (针对程序重启或刚开仓的情况)
                if position > 0 and highest_price == 0:
                    highest_price = max(entry_price, current_price)
                if position < 0 and lowest_price == 99999999:
                    lowest_price = min(entry_price, current_price)

                # 更新最高/最低价 (用于移动止盈)
                if current_price > highest_price: highest_price = current_price
                if current_price < lowest_price: lowest_price = current_price

                # 计算浮动盈亏 (百分比)
                price_change_pct = 0
                if position > 0: price_change_pct = (current_price - entry_price) / entry_price
                elif position < 0: price_change_pct = (entry_price - current_price) / entry_price
                
                # 估算净盈亏 (扣除双边手续费)
                total_fee = config.FEE_RATE * 2
                net_pnl_pct = price_change_pct - total_fee
                
                # 计算当前盈利的 ATR 倍数 (基于最高/最低价计算历史最大盈利)
                max_profit_atr_multiple = 0
                if current_atr > 0:
                    max_profit_dist = 0
                    if position > 0: 
                        max_profit_dist = max(0, highest_price - entry_price)
                    elif position < 0: 
                        max_profit_dist = max(0, entry_price - lowest_price)
                    
                    max_profit_atr_multiple = max_profit_dist / current_atr

                logger.info(f"持仓 | 价格:{current_price} | 盈亏:{unrealized_pnl:.2f}U ({net_pnl_pct*100:.2f}%) | 移动止损:{stop_loss_price:.4f} | 最大盈利ATR:{max_profit_atr_multiple:.1f}")

                # 1. 利润保护逻辑 (保本 + 移动止盈)
                if config.PROFIT_LOCK_ENABLE and current_atr > 0:
                    
                    # 趋势策略无需中轨止盈，让利润奔跑
                    
                    # A. 移动止盈 (优先): 曾经盈利超过 TP_TRIGGER_ATR，启用回调止盈
                    if max_profit_atr_multiple > config.TP_TRIGGER_ATR:
                        # 多单: 最高价回撤超过 TP_CALLBACK_ATR
                        if position > 0 and current_price < (highest_price - config.TP_CALLBACK_ATR * current_atr):
                            logger.success(f"触发移动止盈 (回撤 {config.TP_CALLBACK_ATR} ATR)！锁定利润 | 最高:{highest_price}")
                            executor.place_order(side=-1, quantity=abs(position))
                            continue
                        # 空单: 最低价反弹超过 TP_CALLBACK_ATR
                        if position < 0 and current_price > (lowest_price + config.TP_CALLBACK_ATR * current_atr):
                            logger.success(f"触发移动止盈 (回撤 {config.TP_CALLBACK_ATR} ATR)！锁定利润 | 最低:{lowest_price}")
                            executor.place_order(side=1, quantity=abs(position))
                            continue

                    # B. 保本损: 曾经盈利超过 BREAKEVEN_ATR，现在必须保本(含微利)
                    if max_profit_atr_multiple > config.BREAKEVEN_ATR:
                        # 计算保本止损缓冲 (确保覆盖手续费 0.08% + 滑点)
                        # 使用 max(0.1 ATR, 0.15% 价格) 确保不亏本
                        safe_buffer = max(0.1 * current_atr, entry_price * 0.0015)
                        
                        # 多单保本
                        stop_price_long = entry_price + safe_buffer
                        if position > 0 and current_price < stop_price_long:
                            logger.warning(f"触发保本止损 (曾盈利 {max_profit_atr_multiple:.1f} ATR)！平多离场")
                            executor.place_order(side=-1, quantity=abs(position))
                            continue
                        
                        # 空单保本
                        stop_price_short = entry_price - safe_buffer
                        if position < 0 and current_price > stop_price_short:
                            logger.warning(f"触发保本止损 (曾盈利 {max_profit_atr_multiple:.1f} ATR)！平空离场")
                            executor.place_order(side=1, quantity=abs(position))
                            continue

                # 2. 均值回归特定止盈 (已禁用：趋势模式下不轻易中轨止盈)
                # 原逻辑：回归中轨且微利即跑，这在单边行情中会卖飞
                # 现逻辑：完全依赖 ATR 移动止盈来锁定大利润
                pass
                
                # 3. 止损逻辑 (移动硬止损 - 吊灯止损)
                # 随着价格有利移动，止损线也跟着移动 (始终保持 2.5 ATR 的距离，直到触发保本)
                if current_atr > 0:
                    if position > 0:
                        # 多单止损价 = 最高价 - 2.5 ATR (只升不降)
                        new_stop = highest_price - 2.5 * current_atr
                        if new_stop > stop_loss_price: stop_loss_price = new_stop
                        
                        if current_price < stop_loss_price:
                            logger.warning(f"触发移动硬止损 (价格 {current_price} < {stop_loss_price:.4f})！平多离场")
                            executor.place_order(side=-1, quantity=abs(position))
                            continue
                            
                    elif position < 0:
                        # 空单止损价 = 最低价 + 2.5 ATR (只降不升)
                        new_stop = lowest_price + 2.5 * current_atr
                        if stop_loss_price == 0 or new_stop < stop_loss_price: stop_loss_price = new_stop
                        
                        if current_price > stop_loss_price:
                            logger.warning(f"触发移动硬止损 (价格 {current_price} > {stop_loss_price:.4f})！平空离场")
                            executor.place_order(side=1, quantity=abs(position))
                            continue

            # 4. 执行开仓交易
            if position == 0 and signal != 0:
                # 计算动态仓位
                # 均值回归策略止损通常设为 2.5 ATR (给足波动空间)
                stop_loss_dist = 2.5 * current_atr
                
                if balance > 0 and current_atr > 0:
                    risk_amount = balance * config.RISK_PERCENT
                    
                    # 仓位 = 风险金额 / 止损距离
                    raw_qty = risk_amount / stop_loss_dist
                    quantity = int(raw_qty) # 向下取整
                    
                    # 最小下单数量检查 (假设最小 6 U)
                    if quantity * current_price < 6:
                        quantity = int(6 / current_price) + 1
                        
                    logger.info(f"资金: {balance:.2f} U | 风险额: {risk_amount:.2f} U | 止损距(2.5ATR): {stop_loss_dist:.5f} | 计算仓位: {quantity}")
                    
                    if signal == 1:
                        logger.info("趋势回调 -> 顺势开多")
                        executor.place_order(side=1, quantity=quantity)
                        highest_price = current_price # 重置追踪
                        stop_loss_price = current_price - stop_loss_dist # 记录止损价
                    elif signal == -1:
                        logger.info("趋势反弹 -> 顺势开空")
                        executor.place_order(side=-1, quantity=quantity)
                        lowest_price = current_price # 重置追踪
                        stop_loss_price = current_price + stop_loss_dist # 记录止损价
                else:
                    logger.warning("无法计算仓位 (余额或ATR不足)")

            logger.info("--------------------------------")
            time.sleep(3)
            
        except KeyboardInterrupt:
            logger.info("系统停止")
            break
        except Exception as e:
            logger.error(f"发生错误: {e}")
            time.sleep(3)

if __name__ == "__main__":
    main()
