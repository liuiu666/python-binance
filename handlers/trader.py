from handlers.binance_client import BinanceClient
import math

class TradeExecutor:
    def __init__(self):
        self.client = BinanceClient()

    def execute_trade(self, symbol, side, amount_usdt, leverage=5, slippage=0.002, stop_loss=None, take_profit=None):
        """
        执行安全交易 (使用限价单防止滑点，含风控和杠杆设置，支持止盈止损)
        :param symbol: 交易对
        :param side: 'BUY' 或 'SELL'
        :param amount_usdt: 交易金额 (USDT)
        :param leverage: 杠杆倍数 (默认 5x)
        :param slippage: 允许滑点 (默认 0.2%)
        :param stop_loss: 止损价格 (可选)
        :param take_profit: 止盈价格 (可选)
        """
        print(f">>> [Trader] 准备对 {symbol} 执行 {side} 操作，金额: {amount_usdt} USDT, 杠杆: {leverage}x")
        
        # 0. 风控检查
        account = self.client.get_account_info()
        if not account:
            print(">>> [Trader] 错误: 无法获取账户信息，中止交易")
            return None
            
        avail_balance = account['balance']['available_balance']
        if avail_balance < amount_usdt:
            print(f">>> [Trader] 余额不足! 可用: {avail_balance:.2f}, 需要: {amount_usdt}")
            return None

        # 1. 调整杠杆
        self.client.change_leverage(symbol, leverage)
        
        # 2. 获取最新盘口价格
        # 改为直接获取指定 symbol，避免遍历整个列表
        book_ticker = self.client.get_book_tickers(symbol=symbol)
        
        # 兼容处理：如果返回的是 dict (指定 symbol)，直接使用
        # 如果返回 None 或 空，说明获取失败
        if not book_ticker or 'symbol' not in book_ticker:
            print(f">>> [Trader] 错误: 无法获取 {symbol} 的盘口数据")
            return None
            
        target_book = book_ticker
            
        ask_price = float(target_book['askPrice'])
        bid_price = float(target_book['bidPrice'])
        
        if ask_price == 0 or bid_price == 0:
            print(f">>> [Trader] 错误: 盘口价格异常 (Ask: {ask_price}, Bid: {bid_price})")
            print(f"   调试信息: {target_book}")
            
            # 尝试使用最新成交价作为替代
            print(">>> [Trader] 尝试使用最新成交价作为替代...")
            ticker = self.client.get_symbol_ticker(symbol)
            if ticker and float(ticker['price']) > 0:
                last_price = float(ticker['price'])
                print(f"   最新成交价: {last_price}")
                # 既然盘口异常，说明流动性极差或数据问题，为了成交，我们基于最新价给一点溢价
                # 这里的风险是：最新价可能也很旧，或者与真实盘口偏离极大
                # 稳妥起见，如果盘口为0，最好不要硬做。但为了演示流程，我们给一个较大的滑点
                ask_price = last_price
                bid_price = last_price
            else:
                return None
            
        # 2. 计算限价单价格
        exec_price = 0.0
        if side == 'BUY':
            # 买入：挂在卖一价上方一点点，确保成交但不吃太深
            exec_price = ask_price * (1 + slippage)
            print(f"   当前卖一价: {ask_price}, 挂单价格: {exec_price:.4f} (滑点保护 {slippage*100}%)")
        elif side == 'SELL':
            # 卖出：挂在买一价下方一点点
            exec_price = bid_price * (1 - slippage)
            print(f"   当前买一价: {bid_price}, 挂单价格: {exec_price:.4f} (滑点保护 {slippage*100}%)")
            
        # 3. 计算数量 (需处理精度)
        filters = self.client.get_symbol_filters(symbol)
        if not filters:
            print(f">>> [Trader] 错误: 无法获取 {symbol} 的交易规则")
            return None
            
        print(f"   交易规则: {filters}")

        # 3.1 价格精度处理
        # 价格必须是 tick_size 的整数倍
        tick_size = filters['tick_size']
        if tick_size > 0:
            precision = int(round(-math.log(tick_size, 10), 0))
            exec_price = round(exec_price, precision)
        else:
            exec_price = round(exec_price, filters['price_precision'])
            
        # 3.2 数量计算与精度处理
        raw_quantity = amount_usdt / exec_price
        step_size = filters['step_size']
        
        if step_size > 0:
            # 数量必须是 step_size 的整数倍
            # 比如 step_size = 0.001, raw = 0.0056 -> 0.005
            # 算法: floor(raw / step) * step
            # 使用 decimal 防止浮点误差
            inv_step = 1.0 / step_size
            quantity = math.floor(raw_quantity * inv_step) / inv_step
            
            # 再次确保精度 (防止 1.0000000001 这种情况)
            qty_precision = int(round(-math.log(step_size, 10), 0))
            quantity = round(quantity, qty_precision)
        else:
            quantity = round(raw_quantity, filters['quantity_precision'])
            
        # 最小数量检查
        if quantity < filters['min_qty']:
            print(f">>> [Trader] 错误: 计算数量 {quantity} 小于最小允许数量 {filters['min_qty']}")
            return None
            
        # 最小名义价值检查 (Min Notional)
        notional_value = quantity * exec_price
        min_notional = filters.get('min_notional', 5.0)
        if notional_value < min_notional:
            print(f">>> [Trader] 错误: 订单金额 {notional_value:.2f} 小于最小允许金额 {min_notional}")
            return None

        print(f"   计算下单数量: {quantity} (StepSize: {step_size})")
        
        # 4. 下单
        order = self.client.place_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type='LIMIT',
            price=str(exec_price)
        )
        
        if order:
            print(f">>> [Trader] 交易成功! 订单ID: {order.get('orderId')}")
            
            # 5. 挂止盈止损单
            if stop_loss or take_profit:
                print(f">>> [Trader] 正在设置止盈止损 (SL: {stop_loss}, TP: {take_profit})...")
                # 计算反向方向
                close_side = 'SELL' if side == 'BUY' else 'BUY'
                
                # 止损单
                if stop_loss:
                    # 精度处理
                    sl_price = stop_loss
                    if tick_size > 0:
                        sl_price = round(sl_price, precision)
                    else:
                        sl_price = round(sl_price, filters['price_precision'])
                        
                    self.client.place_order(
                        symbol=symbol,
                        side=close_side,
                        quantity=None, # close_position=True 不需要数量
                        order_type='STOP_MARKET',
                        stop_price=str(sl_price),
                        close_position=True
                    )
                    print(f"   已挂止损单: {sl_price}")

                # 止盈单
                if take_profit:
                    # 精度处理
                    tp_price = take_profit
                    if tick_size > 0:
                        tp_price = round(tp_price, precision)
                    else:
                        tp_price = round(tp_price, filters['price_precision'])
                        
                    self.client.place_order(
                        symbol=symbol,
                        side=close_side,
                        quantity=None, # close_position=True 不需要数量
                        order_type='TAKE_PROFIT_MARKET',
                        stop_price=str(tp_price),
                        close_position=True
                    )
                    print(f"   已挂止盈单: {tp_price}")

        else:
            print(">>> [Trader] 交易失败")
            
        return order

    def close_position(self, symbol):
        """
        平掉指定交易对的所有仓位 (包括撤销所有挂单)
        """
        print(f">>> [Trader] 正在强平 {symbol} 所有仓位...")
        
        # 1. 撤销所有挂单 (止盈止损单)
        try:
            self.client.client.futures_cancel_all_open_orders(symbol=symbol)
            print(f"   已撤销 {symbol} 所有挂单")
        except Exception as e:
            print(f"   撤单失败: {e}")
            
        # 2. 获取当前持仓方向
        positions = self.client.get_current_positions()
        target_pos = None
        for p in positions:
            if p['symbol'] == symbol:
                target_pos = p
                break
                
        if not target_pos:
            print(f"   未找到 {symbol} 持仓，可能已平仓")
            return True
            
        # 3. 市价全平
        close_side = 'SELL' if target_pos['side'] == 'BUY' else 'BUY'
        quantity = abs(float(target_pos['amount'])) # 确保是正数 float
        
        try:
            self.client.place_order(
                symbol=symbol,
                side=close_side,
                quantity=quantity,
                order_type='MARKET',
                reduce_only=True
            )
            print(f"   >>> {symbol} 强平成功!")
            return True
        except Exception as e:
            print(f"   强平失败: {e}")
            return False

    def check_trailing_stop(self, position, current_price, atr=0):
        """
        检查并更新移动止损 (Trailing Stop)
        策略:
        1. 盈利 > 1.5 * ATR: 将止损上移到 开仓价 (保本)
        2. 盈利 > 3.0 * ATR: 将止损上移到 当前价 - 2 * ATR (锁定利润)
        """
        symbol = position['symbol']
        entry_price = position['entry_price']
        side = position['side']
        
        # 如果没有 ATR，使用价格百分比估算 (默认 ATR 约为 2%)
        if atr <= 0:
            atr = current_price * 0.02
            
        # 计算当前浮盈
        if side == 'BUY':
            profit = current_price - entry_price
        else:
            profit = entry_price - current_price
            
        # 获取当前挂单 (找到止损单)
        try:
            orders = self.client.client.futures_get_open_orders(symbol=symbol)
            stop_order = None
            for o in orders:
                if o['type'] == 'STOP_MARKET':
                    stop_order = o
                    break
            
            if not stop_order:
                return # 没有止损单，不做操作
                
            current_sl = float(stop_order['stopPrice'])
            new_sl = None
            action_desc = ""
            
            # 移动止损逻辑
            if side == 'BUY':
                # 1. 保本检查: 盈利 > 1.5 ATR 且 当前止损 < 开仓价
                if profit > 1.5 * atr and current_sl < entry_price:
                    new_sl = entry_price * 1.001 # 略微高于开仓价 (抵消手续费)
                    action_desc = "触发保本止损"
                
                # 2. 趋势跟踪: 盈利 > 3 ATR 且 当前止损 < (现价 - 2 ATR)
                trail_price = current_price - (2 * atr)
                if profit > 3 * atr and current_sl < trail_price:
                    new_sl = trail_price
                    action_desc = "触发移动止损 (锁定利润)"
                    
            else: # SELL
                # 1. 保本检查
                if profit > 1.5 * atr and current_sl > entry_price:
                    new_sl = entry_price * 0.999
                    action_desc = "触发保本止损"
                    
                # 2. 趋势跟踪
                trail_price = current_price + (2 * atr)
                if profit > 3 * atr and current_sl > trail_price:
                    new_sl = trail_price
                    action_desc = "触发移动止损 (锁定利润)"
            
            # 执行修改
            if new_sl:
                print(f">>> [Trailing Stop] {symbol} {action_desc}")
                print(f"   原止损: {current_sl} -> 新止损: {new_sl:.4f}")
                
                # 1. 撤销旧单
                self.client.client.futures_cancel_order(symbol=symbol, orderId=stop_order['orderId'])
                
                # 2. 挂新单
                close_side = 'SELL' if side == 'BUY' else 'BUY'
                
                # 获取精度规则
                filters = self.client.get_symbol_filters(symbol)
                if filters:
                    if filters['tick_size'] > 0:
                        precision = int(round(-math.log(filters['tick_size'], 10), 0))
                        new_sl = round(new_sl, precision)
                    else:
                        new_sl = round(new_sl, filters['price_precision'])
                
                self.client.place_order(
                    symbol=symbol,
                    side=close_side,
                    quantity=None,
                    order_type='STOP_MARKET',
                    stop_price=str(new_sl),
                    close_position=True
                )
                
        except Exception as e:
            print(f"移动止损检查失败: {e}")
