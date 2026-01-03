import time
from loguru import logger
import config
from trade import TradeExecutor
from strategy import Strategy

def main():
    logger.info(f"自动交易系统启动 | 交易对: {config.SYMBOL} | 仓位管理: 动态风险 ({config.RISK_PERCENT*100}%)")
    logger.info(f"策略: SuperTrend趋势追踪 | 周期: {config.TIMEFRAME}")
    
    # 初始化
    executor = TradeExecutor()
    executor.connect()
    strategy = Strategy()
    
    # 获取交易对精度信息 (简化处理，假设数量精度为0，即整数)
    # 实际应用中应从 executor.get_symbol_info() 获取 stepSize
    
    # 记录最高/最低价用于移动止盈
    highest_price = 0
    lowest_price = 99999999

    while True:
        try:
            # 1. 获取持仓信息
            pos_info = executor.get_position()
            position = pos_info['amt']
            entry_price = pos_info['entryPrice']
            unrealized_pnl = pos_info['unRealizedProfit'] # 币安计算的未实现盈亏(U)
            
            # 如果没有持仓，重置最高/最低价
            if position == 0:
                highest_price = 0
                lowest_price = 99999999
            
            # 2. 获取数据
            klines = executor.get_klines(limit=100)
            if not klines:
                time.sleep(10)
                continue
                
            current_price = float(klines[-1][4]) # 最新收盘价
            
            # 3. 策略分析 (获取信号和当前ATR)
            signal, current_atr, st_value = strategy.check_signal(klines)
            
            # --- 动态止盈止损逻辑 (SuperTrend + 利润锁定) ---
            # SuperTrend 是天然的止损线：
            # 持多单时，如果价格收盘跌破 SuperTrend，平多
            # 持空单时，如果价格收盘涨破 SuperTrend，平空
            if abs(position) > 0:
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
                        # 如果刚启动/重启，highest_price 可能刚初始化，确保它至少是 entry_price
                        if highest_price < entry_price: highest_price = current_price
                        max_profit_dist = highest_price - entry_price
                    elif position < 0: 
                        if lowest_price > entry_price: lowest_price = current_price
                        max_profit_dist = entry_price - lowest_price
                    
                    max_profit_atr_multiple = max_profit_dist / current_atr

                logger.info(f"持仓 | 价格:{current_price} | 盈亏:{unrealized_pnl:.2f}U ({net_pnl_pct*100:.2f}%) | 最大盈利ATR:{max_profit_atr_multiple:.1f} | ST:{st_value:.5f}")

                # 1. 利润保护逻辑 (保本 + 移动止盈)
                if config.PROFIT_LOCK_ENABLE and current_atr > 0:
                    # A. 保本损: 曾经盈利超过 BREAKEVEN_ATR，现在必须保本
                    if max_profit_atr_multiple > config.BREAKEVEN_ATR:
                        # 多单保本: 价格跌破开仓价 (加一点点利润保护手续费)
                        if position > 0 and current_price < entry_price * 1.002:
                            logger.warning(f"触发保本止损 (曾盈利 {max_profit_atr_multiple:.1f} ATR)！平多离场")
                            executor.place_order(side=-1, quantity=abs(position))
                            continue
                        # 空单保本: 价格涨破开仓价
                        if position < 0 and current_price > entry_price * 0.998:
                            logger.warning(f"触发保本止损 (曾盈利 {max_profit_atr_multiple:.1f} ATR)！平空离场")
                            executor.place_order(side=1, quantity=abs(position))
                            continue

                    # B. 移动止盈: 曾经盈利超过 TP_TRIGGER_ATR，启用回调止盈
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

                # 2. SuperTrend 止盈/止损 (只要趋势反转就走人)
                # 多单：当前价格跌破 SuperTrend (且 st_value > current_price 表示趋势已变空)
                # 注意：strategy.check_signal 返回的 st_value 是当前周期的值。
                # 如果 check_signal 已经给出了反向信号，这里可以直接用 signal 判断，或者自己判断价格与ST的关系
                
                # 简单逻辑：如果持有多单，但 SuperTrend 指示做空 (signal == -1)，则平多
                if position > 0 and signal == -1:
                    logger.warning(f"SuperTrend 翻空，多单止盈/止损离场！")
                    executor.place_order(side=-1, quantity=abs(position))
                    # 可以在平仓后立即反手开空，但为了稳健，下个循环再开
                    continue

                # 如果持有空单，但 SuperTrend 指示做多 (signal == 1)，则平空
                if position < 0 and signal == 1:
                    logger.warning(f"SuperTrend 翻多，空单止盈/止损离场！")
                    executor.place_order(side=1, quantity=abs(position))
                    continue

            # 4. 执行开仓交易
            if position == 0:
                # 策略逻辑升级：不仅在反转时买，如果当前是趋势中且位置合适，也顺势上车
                # 判断当前趋势
                is_bullish = current_price > st_value
                is_bearish = current_price < st_value
                
                # 如果没有反转信号，检查是否可以顺势进场
                if signal == 0:
                    dist = abs(current_price - st_value)
                    # 如果距离止损线在 4倍 ATR 以内 (风险可控)，且趋势明确，强行上车
                    if dist < 4 * current_atr:
                        if is_bullish:
                            logger.info(f"检测到多头趋势 (距止损 {dist/current_atr:.1f} ATR) -> 顺势开多补票")
                            signal = 1
                        elif is_bearish:
                            logger.info(f"检测到空头趋势 (距止损 {dist/current_atr:.1f} ATR) -> 顺势开空补票")
                            signal = -1
                    else:
                        logger.info(f"趋势明确但偏离止损线太远 ({dist/current_atr:.1f} ATR)，等待回调再上车")

                if signal != 0:
                    # 计算动态仓位
                    # 止损距离 = |价格 - SuperTrend|
                    # 为了防止刚突破时距离太近导致仓位过大，设置最小止损距离为 1*ATR
                    balance = executor.get_balance()
                    if balance > 0 and current_atr > 0:
                        risk_amount = balance * config.RISK_PERCENT
                        
                        dist = abs(current_price - st_value)
                        if dist < current_atr:
                            dist = current_atr # 最小止损距离
                            
                        raw_qty = risk_amount / dist
                        quantity = int(raw_qty) # 向下取整
                        
                        # 最小下单数量检查 (假设最小 6 U)
                        if quantity * current_price < 6:
                            quantity = int(6 / current_price) + 1
                            
                        logger.info(f"资金: {balance:.2f} U | 风险额: {risk_amount:.2f} U | 止损距: {dist:.5f} | 计算仓位: {quantity}")
                        
                        if signal == 1:
                            logger.info("买入信号 -> 开多")
                            executor.place_order(side=1, quantity=quantity)
                            highest_price = current_price # 重置追踪
                        elif signal == -1:
                            logger.info("卖出信号 -> 开空")
                            executor.place_order(side=-1, quantity=quantity)
                            lowest_price = current_price # 重置追踪
                    else:
                        logger.warning("无法计算仓位 (余额或ATR不足)")

            # 5. 反手逻辑 (如果持有反向仓位且出现强反转信号)
            # (为了稳健，暂时先平仓再开仓，分两步走，这里先不自动反手，避免双倍亏损)
            
            logger.info("--------------------------------")
            time.sleep(15)
            
        except KeyboardInterrupt:
            logger.info("系统停止")
            break
        except Exception as e:
            logger.error(f"发生错误: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
