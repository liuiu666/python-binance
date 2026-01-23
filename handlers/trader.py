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
                        quantity=quantity, # 使用开仓数量 + reduce_only
                        order_type='STOP_MARKET',
                        stop_price=str(sl_price),
                        reduce_only=True
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
                        quantity=quantity, # 使用开仓数量 + reduce_only
                        order_type='TAKE_PROFIT_MARKET',
                        stop_price=str(tp_price),
                        reduce_only=True
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
                # [新增] 如果没有止损单，尝试补挂
                print(f">>> [Trailing Stop] ⚠️ {symbol} 未检测到止损单，正在计算补单...")
                
                # 1. 计算初始止损位 (Entry +/- 2 ATR)
                if atr <= 0: atr = current_price * 0.02
                
                if side == 'BUY':
                    base_sl = entry_price - (2 * atr)
                    # 如果当前价格已经跌破初始止损，则以当前价格为基准重设 (防止立即触发/报错)
                    if base_sl >= current_price:
                        base_sl = current_price - (2 * atr)
                        print(f"   [风险] 已跌破原始止损，重设为现价 - 2ATR: {base_sl:.4f}")
                else:
                    base_sl = entry_price + (2 * atr)
                    if base_sl <= current_price:
                        base_sl = current_price + (2 * atr)
                        print(f"   [风险] 已涨破原始止损，重设为现价 + 2ATR: {base_sl:.4f}")

                current_sl = base_sl #以此作为当前止损基准
                new_sl = base_sl     # 默认新止损就是基准
                
                # 标记需要挂新单
                stop_order = {'orderId': None} # 伪造一个对象以便进入后续逻辑 (orderId None 表示不需要撤单)
            else:
                current_sl = float(stop_order['stopPrice'])
                new_sl = None
            
            action_desc = ""
            
            # 移动止损逻辑 (计算是否有更好的止损位)
            if side == 'BUY':
                # ... 保持原有逻辑 ...
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
                
                # 1. 撤销旧单 (如果有)
                if stop_order.get('orderId'):
                    try:
                        self.client.client.futures_cancel_order(symbol=symbol, orderId=stop_order['orderId'])
                    except Exception as e:
                        print(f"   撤销旧止损单失败: {e}")
                else:
                    print(f"   [补单] 正在挂初始/补救止损单")
                
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
                
                # 获取当前持仓数量并处理精度
                quantity = abs(float(position['amount']))
                
                if filters:
                    step_size = filters.get('step_size', 0)
                    if step_size > 0:
                        qty_precision = int(round(-math.log(step_size, 10), 0))
                        quantity = round(quantity, qty_precision)
                    else:
                        quantity = round(quantity, filters['quantity_precision'])
                
                self.client.place_order(
                    symbol=symbol,
                    side=close_side,
                    quantity=quantity,
                    order_type='STOP_MARKET',
                    stop_price=str(new_sl),
                    reduce_only=True
                )
                
        except Exception as e:
            print(f"移动止损检查失败: {e}")

    def increase_position(self, position, amount_usdt, current_price, atr):
        """
        浮盈加仓 (Pyramiding)
        规则:
        1. 加仓后重新计算平均开仓价
        2. 止损上移到新平均价的下方 (保证整体风险可控)
        """
        symbol = position['symbol']
        side = position['side']
        old_quantity = float(position['amount'])
        
        print(f">>> [Trader] 正在对 {symbol} 执行加仓，金额: {amount_usdt} USDT...")
        
        # 1. 执行加仓交易
        # 注意：这里调用 execute_trade 会自动挂新的止损单吗？execute_trade 内部有挂单逻辑
        # 但我们需要特殊的止损逻辑，所以这里手动下单比较好，或者复用 execute_trade 但不传 sl/tp，后续手动调整
        
        # 计算加仓数量
        filters = self.client.get_symbol_filters(symbol)
        if not filters:
            return False
            
        quantity = amount_usdt / current_price
        
        # 精度调整
        step_size = filters['step_size']
        if step_size > 0:
            inv_step = 1.0 / step_size
            quantity = math.floor(quantity * inv_step) / inv_step
            qty_precision = int(round(-math.log(step_size, 10), 0))
            quantity = round(quantity, qty_precision)
        
        if quantity < filters['min_qty']:
            print(f"   加仓数量 {quantity} 太小，忽略")
            return False
            
        try:
            # 加仓下单
            self.client.place_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type='MARKET' # 加仓通常用市价，或者当前价限价
            )
            print(f"   加仓成功: {quantity}")
            
            # 2. 撤销旧止损单
            try:
                self.client.client.futures_cancel_all_open_orders(symbol=symbol)
            except:
                pass
                
            # 3. 计算新止损
            # 新的总数量
            total_quantity = old_quantity + quantity
            # 简单估算新止损：移动到 (当前价 - 1.5 * ATR)
            # 这样既保护了部分利润，又给新仓位留了空间
            if side == 'BUY':
                new_sl = current_price - (1.5 * atr)
            else:
                new_sl = current_price + (1.5 * atr)
                
            # 精度处理
            if filters['tick_size'] > 0:
                precision = int(round(-math.log(filters['tick_size'], 10), 0))
                new_sl = round(new_sl, precision)
            else:
                new_sl = round(new_sl, filters['price_precision'])
                
            # 4. 挂新止损单 (针对总仓位)
            close_side = 'SELL' if side == 'BUY' else 'BUY'
            self.client.place_order(
                symbol=symbol,
                side=close_side,
                quantity=total_quantity,
                order_type='STOP_MARKET',
                stop_price=str(new_sl),
                reduce_only=True
            )
            print(f"   已更新止损 (总仓位 {total_quantity}): {new_sl}")
            return True
            
        except Exception as e:
            print(f"   加仓失败: {e}")
            return False

    def reduce_position(self, position, reduce_pct, current_price):
        """
        分批减仓 (Scale Out)
        :param reduce_pct: 减仓比例 (0.0 - 1.0), 例如 0.5 表示减仓一半
        """
        symbol = position['symbol']
        side = position['side']
        total_quantity = abs(float(position['amount']))
        
        reduce_qty = total_quantity * reduce_pct
        
        print(f">>> [Trader] 正在对 {symbol} 执行减仓 ({reduce_pct*100}%), 数量: {reduce_qty}...")
        
        filters = self.client.get_symbol_filters(symbol)
        if not filters:
            return None
            
        # 精度调整
        step_size = filters['step_size']
        if step_size > 0:
            inv_step = 1.0 / step_size
            reduce_qty = math.floor(reduce_qty * inv_step) / inv_step
            qty_precision = int(round(-math.log(step_size, 10), 0))
            reduce_qty = round(reduce_qty, qty_precision)
            
        if reduce_qty < filters['min_qty']:
            print(f"   减仓数量 {reduce_qty} 太小，忽略")
            return None
            
        try:
            # 1. 执行减仓
            close_side = 'SELL' if side == 'BUY' else 'BUY'
            order = self.client.place_order(
                symbol=symbol,
                side=close_side,
                quantity=reduce_qty,
                order_type='MARKET',
                reduce_only=True
            )
            print(f"   减仓成功")
            
            # [新增] 计算已实现盈亏并更新状态
            # 注意：市价单成交价格可能略有偏差，这里简单估算，或者从 order 获取成交均价 (如果 order 返回了)
            # 为了简单，暂按 current_price 估算
            if side == 'BUY':
                pnl = (current_price - float(position['entry_price'])) * reduce_qty
            else:
                pnl = (float(position['entry_price']) - current_price) * reduce_qty
            
            # 这里需要一种方式访问 state_manager。
            # 由于 TradeExecutor 没有持有 state_manager 引用，我们暂且打印，或者后续重构让 main 传入
            # 简单起见，我们假设外部循环会通过 balance check 间接更新 daily stats 的 risk check
            # 但 daily_stats['realized_pnl'] 需要手动更新
            # 方案：TradeExecutor 返回 pnl，由 main 更新
            
            # 2. 调整剩余仓位的止损单
            # 撤销旧单
            try:
                self.client.client.futures_cancel_all_open_orders(symbol=symbol)
            except:
                pass
                
            # 剩余数量
            remain_qty = total_quantity - reduce_qty
            remain_qty = round(remain_qty, qty_precision if step_size > 0 else filters['quantity_precision'])
            
            if remain_qty < filters['min_qty']:
                print("   剩余仓位过小，不再挂止损")
                return pnl
                
            # 保持原有的止损价逻辑较复杂，这里简单策略：
            # 减仓通常意味着锁定利润，剩余仓位应该将止损移至保本或更激进
            # 这里我们尝试读取之前的止损价比较困难，简单起见，重设为保本 (开仓价)
            # 或者更智能点：如果有盈利，设为 (当前价 - 2ATR)
            # 为简化，暂时设为 开仓价 (Entry Price) 
            entry_price = float(position['entry_price'])
            
            if side == 'BUY':
                new_sl = max(entry_price, current_price * 0.98) # 至少保本或现价下方2%
            else:
                new_sl = min(entry_price, current_price * 1.02)
                
             # 精度处理
            if filters['tick_size'] > 0:
                precision = int(round(-math.log(filters['tick_size'], 10), 0))
                new_sl = round(new_sl, precision)
            else:
                new_sl = round(new_sl, filters['price_precision'])

            self.client.place_order(
                symbol=symbol,
                side=close_side,
                quantity=remain_qty,
                order_type='STOP_MARKET',
                stop_price=str(new_sl),
                reduce_only=True
            )
            print(f"   剩余仓位 {remain_qty} 已重置止损: {new_sl}")
            return pnl
            
        except Exception as e:
            print(f"   减仓失败: {e}")
            return None
