from handlers.binance_client import BinanceClient
import math

class TradeExecutor:
    def __init__(self, client=None, state_manager=None):
        self.client = client or BinanceClient()
        self.state_manager = state_manager
        self._local_trailing_stops = {}

    def _get_local_stop(self, symbol):
        if self.state_manager:
            meta = self.state_manager.get_position_meta(symbol) or {}
            val = meta.get('local_stop')
            if val is not None:
                try:
                    return float(val)
                except Exception:
                    return None
            return None
        return self._local_trailing_stops.get(symbol)

    def _set_local_stop(self, symbol, side, stop_price):
        try:
            stop_price = float(stop_price)
        except Exception:
            return

        old_val = self._get_local_stop(symbol)
        if old_val is None:
            new_val = stop_price
        else:
            if side == 'BUY':
                new_val = max(float(old_val), stop_price)
            else:
                new_val = min(float(old_val), stop_price)

        if self.state_manager:
            self.state_manager.set_position_meta(symbol, local_stop=new_val, local_stop_side=side)
        else:
            self._local_trailing_stops[symbol] = new_val

    def _clear_local_stop(self, symbol):
        if self.state_manager:
            self.state_manager.set_position_meta(symbol, local_stop=None, local_stop_side=None)
        if symbol in self._local_trailing_stops:
            del self._local_trailing_stops[symbol]

    def _place_stop_protection(self, symbol, side, stop_price):
        if not self.client.is_algo_available():
            self._set_local_stop(symbol, side, stop_price)
            print(f"   [本地止损] 条件单不可用，本地止损价: {self._get_local_stop(symbol):.4f}")
            return False

        close_side = 'SELL' if side == 'BUY' else 'BUY'
        
        # 尝试1: 使用 closePosition=True
        order = self.client.place_order(
            symbol=symbol,
            side=close_side,
            order_type='STOP_MARKET',
            stop_price=str(stop_price),
            close_position=True
        )
        
        if order:
            return True
            
        # 尝试2: 失败后尝试使用 reduceOnly=True (针对 Error -4120)
        # 获取持仓数量
        positions = self.client.get_current_positions()
        target_pos = next((p for p in positions if p['symbol'] == symbol), None)
        
        if target_pos:
            quantity = abs(float(target_pos['amount']))
            filters = self.client.get_symbol_filters(symbol)
            if filters:
                quantity = self._quantize_quantity(quantity, filters)
                
            print(f"   [重试] 尝试使用 ReduceOnly 模式挂止损 (数量: {quantity})...")
            order = self.client.place_order(
                symbol=symbol,
                side=close_side,
                quantity=quantity,
                order_type='STOP_MARKET',
                stop_price=str(stop_price),
                reduce_only=True
            )
            if order:
                print(f"   [成功] ReduceOnly 止损单下单成功")
                return True

        self._set_local_stop(symbol, side, stop_price)
        print(f"   [本地止损] 条件单下单失败，本地止损价: {self._get_local_stop(symbol):.4f}")
        return False

    def _quantize_price(self, price, filters):
        tick_size = float(filters.get('tick_size', 0) or 0)
        if tick_size > 0:
            precision = int(round(-math.log(tick_size, 10), 0))
            return round(float(price), precision)
        return round(float(price), int(filters.get('price_precision', 8) or 8))

    def _quantize_quantity(self, quantity, filters):
        step_size = float(filters.get('step_size', 0) or 0)
        if step_size > 0:
            inv_step = 1.0 / step_size
            qty = math.floor(float(quantity) * inv_step) / inv_step
            qty_precision = int(round(-math.log(step_size, 10), 0))
            return round(qty, qty_precision)
        return round(float(quantity), int(filters.get('quantity_precision', 8) or 8))

    def _place_split_take_profit(self, symbol, open_side, entry_price, take_profit, total_quantity, filters):
        try:
            entry_price = float(entry_price)
            take_profit = float(take_profit)
            total_quantity = float(total_quantity)
        except Exception:
            return False

        if total_quantity <= 0 or entry_price <= 0 or take_profit <= 0:
            return False

        is_long = (open_side == 'BUY')
        delta = abs(take_profit - entry_price)
        if delta <= 0:
            return False

        ratios = [0.4, 0.3, 0.3]
        if is_long:
            raw_prices = [entry_price + 0.5 * delta, entry_price + 0.8 * delta, take_profit]
        else:
            raw_prices = [entry_price - 0.5 * delta, entry_price - 0.8 * delta, take_profit]

        tick_size = float(filters.get('tick_size', 0) or 0)
        prices = []
        for p in raw_prices:
            q = self._quantize_price(p, filters)
            prices.append(q)

        if tick_size > 0:
            if is_long:
                if prices[1] <= prices[0]:
                    prices[1] = self._quantize_price(prices[0] + tick_size, filters)
                if prices[2] <= prices[1]:
                    prices[2] = self._quantize_price(prices[1] + tick_size, filters)
            else:
                if prices[1] >= prices[0]:
                    prices[1] = self._quantize_price(prices[0] - tick_size, filters)
                if prices[2] >= prices[1]:
                    prices[2] = self._quantize_price(prices[1] - tick_size, filters)

        min_qty = float(filters.get('min_qty', 0) or 0)
        targets = []
        used = 0.0

        for i in range(len(ratios) - 1):
            raw_qty = total_quantity * ratios[i]
            qty = self._quantize_quantity(raw_qty, filters)
            if qty < min_qty:
                continue
            if used + qty > total_quantity:
                qty = self._quantize_quantity(max(total_quantity - used, 0), filters)
            if qty >= min_qty:
                targets.append((prices[i], qty))
                used += qty

        remaining = self._quantize_quantity(max(total_quantity - used, 0), filters)
        if remaining >= min_qty:
            targets.append((prices[-1], remaining))
            used += remaining

        close_side = 'SELL' if is_long else 'BUY'
        if not targets:
            qty = self._quantize_quantity(total_quantity, filters)
            if qty >= min_qty:
                targets = [(prices[-1], qty)]
            else:
                return False

        ok_any = False
        for tp_price, tp_qty in targets:
            order = self.client.place_order(
                symbol=symbol,
                side=close_side,
                quantity=tp_qty,
                order_type='LIMIT',
                price=str(tp_price),
                reduce_only=True
            )
            if order:
                ok_any = True
                print(f"   已挂分批止盈单: 价 {tp_price}，量 {tp_qty}")

        return ok_any

    def execute_trade(self, symbol, side, amount_usdt, leverage=5, slippage=0.01, stop_loss=None, take_profit=None):
        """
        执行安全交易 (使用限价单防止滑点，含风控和杠杆设置，支持止盈止损)
        :param symbol: 交易对
        :param side: 'BUY' 或 'SELL'
        :param amount_usdt: 交易金额 (USDT)
        :param leverage: 杠杆倍数 (默认 5x)
        :param slippage: 允许滑点 (默认 1.0% - 针对小币优化)
        :param stop_loss: 止损价格 (可选)
        :param take_profit: 止盈价格 (可选)
        """
        side_text = "做多" if side == 'BUY' else "做空"
        print(f">>>【交易执行】准备对 {symbol} 执行 {side_text} 操作，金额: {amount_usdt}，杠杆: {leverage}倍")
        
        # 0. 风控检查
        account = self.client.get_account_info()
        if not account:
            print(">>>【交易执行】错误: 无法获取账户信息，中止交易")
            return None
            
        avail_balance = account['balance']['available_balance']
        if avail_balance < amount_usdt:
            print(f">>>【交易执行】余额不足! 可用: {avail_balance:.2f}, 需要: {amount_usdt}")
            return None

        # 1. 调整杠杆
        self.client.change_leverage(symbol, leverage)
        
        # 2. 获取最新盘口价格
        book_ticker = self.client.get_book_tickers(symbol=symbol)
        
        if not book_ticker or 'symbol' not in book_ticker:
            print(f">>>【交易执行】错误: 无法获取 {symbol} 的盘口数据")
            return None
            
        target_book = book_ticker
            
        ask_price = float(target_book['askPrice'])
        bid_price = float(target_book['bidPrice'])
        
        if ask_price == 0 or bid_price == 0:
            print(f">>>【交易执行】错误: 盘口价格异常（卖一: {ask_price}, 买一: {bid_price}）")
            
            # 尝试使用最新成交价作为替代
            print(">>>【交易执行】尝试使用最新成交价作为替代...")
            ticker = self.client.get_symbol_ticker(symbol)
            if ticker and float(ticker['price']) > 0:
                last_price = float(ticker['price'])
                print(f"   最新成交价: {last_price}")
                ask_price = last_price
                bid_price = last_price
            else:
                return None
            
        # 2. 计算限价单价格
        exec_price = 0.0
        if side == 'BUY':
            # 买入：挂在卖一价上方一点点，确保成交但不吃太深
            exec_price = ask_price * (1 + slippage)
            print(f"   当前卖一价: {ask_price}, 挂单价格: {exec_price:.4f}（滑点保护 {slippage*100:.2f}%）")
        elif side == 'SELL':
            # 卖出：挂在买一价下方一点点
            exec_price = bid_price * (1 - slippage)
            print(f"   当前买一价: {bid_price}, 挂单价格: {exec_price:.4f}（滑点保护 {slippage*100:.2f}%）")
            
        # 3. 计算数量
        filters = self.client.get_symbol_filters(symbol)
        if not filters:
            print(f">>>【交易执行】错误: 无法获取 {symbol} 的交易规则")
            return None
        
        try:
            min_notional = float(filters.get('min_notional', 5.0) or 5.0)
            print(f"   交易规则: 最小数量 {filters.get('min_qty')}, 数量步进 {filters.get('step_size')}, 价格步进 {filters.get('tick_size')}, 最小金额 {min_notional}")
        except Exception:
            print("   交易规则: 已获取")

        # 3.1 价格精度处理
        tick_size = filters['tick_size']
        if tick_size > 0:
            precision = int(round(-math.log(tick_size, 10), 0))
            exec_price = round(exec_price, precision)
        else:
            exec_price = round(exec_price, filters['price_precision'])
            
        # 3.2 数量精度处理
        raw_quantity = amount_usdt / exec_price
        step_size = filters['step_size']
        
        if step_size > 0:
            inv_step = 1.0 / step_size
            quantity = math.floor(raw_quantity * inv_step) / inv_step
            
            qty_precision = int(round(-math.log(step_size, 10), 0))
            quantity = round(quantity, qty_precision)
        else:
            quantity = round(raw_quantity, filters['quantity_precision'])
            
        # 最小数量检查
        if quantity < filters['min_qty']:
            print(f">>>【交易执行】错误: 计算数量 {quantity} 小于最小允许数量 {filters['min_qty']}")
            return None
            
        notional_value = quantity * exec_price
        min_notional = filters.get('min_notional', 5.0)
        if notional_value < min_notional:
            print(f">>>【交易执行】错误: 订单金额 {notional_value:.2f} 小于最小允许金额 {min_notional}")
            return None

        print(f"   计算下单数量: {quantity}")
        
        # 4. 下单
        order = self.client.place_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type='LIMIT',
            price=str(exec_price)
        )
        
        if order:
            print(f">>>【交易执行】交易成功! 订单号: {order.get('orderId')}")
            
            # 5. 挂止盈止损单
            if stop_loss or take_profit:
                print(f">>>【交易执行】正在设置止盈止损（止损: {stop_loss}, 止盈: {take_profit}）...")
                
                # 止损单
                if stop_loss:
                    # 精度处理
                    sl_price = stop_loss
                    if tick_size > 0:
                        sl_price = round(sl_price, precision)
                    else:
                        sl_price = round(sl_price, filters['price_precision'])
                    ok = self._place_stop_protection(symbol, side, sl_price)
                    if ok:
                        print(f"   已设置止损触发价: {sl_price}")

                # 止盈单
                if take_profit:
                    tp_price = self._quantize_price(take_profit, filters)
                    ok = self._place_split_take_profit(
                        symbol=symbol,
                        open_side=side,
                        entry_price=exec_price,
                        take_profit=tp_price,
                        total_quantity=quantity,
                        filters=filters
                    )
                    if not ok:
                        close_side = 'SELL' if side == 'BUY' else 'BUY'
                        self.client.place_order(
                            symbol=symbol,
                            side=close_side,
                            quantity=quantity,
                            order_type='LIMIT',
                            price=str(tp_price),
                            reduce_only=True
                        )
                        print(f"   已挂止盈单: {tp_price}")

        else:
            print(">>>【交易执行】交易失败")
            
        return order

    def close_position(self, symbol):
        """
        平掉指定交易对的所有仓位（包括撤销所有挂单）
        """
        print(f">>>【交易执行】正在平掉 {symbol} 所有仓位...")
        
        # 1. 撤销所有挂单 (止盈止损单)
        try:
            self.client.client.futures_cancel_all_open_orders(symbol=symbol, recvWindow=10000)
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
        quantity = abs(float(target_pos['amount']))
        
        try:
            self.client.place_order(
                symbol=symbol,
                side=close_side,
                quantity=quantity,
                order_type='MARKET',
                reduce_only=True
            )
            print(f"   {symbol} 平仓成功")
            self._clear_local_stop(symbol)
            return True
        except Exception as e:
            print(f"   强平失败: {e}")
            return False

    def check_trailing_stop(self, position, current_price, atr=0):
        """
        检查并更新移动止损
        策略:
        1. 盈利 > 1.5 倍波动率：将止损上移到开仓价（保本）
        2. 盈利 > 3.0 倍波动率：将止损上移到更接近当前价（锁定利润）
        """
        symbol = position['symbol']
        entry_price = position['entry_price']
        side = position['side']

        local_sl = self._get_local_stop(symbol)
        if local_sl is not None:
            if side == 'BUY' and current_price <= local_sl:
                print(f">>>【本地止损】{symbol} 触发止损价 {local_sl:.4f}，执行市价平仓")
                self.close_position(symbol)
                return
            if side == 'SELL' and current_price >= local_sl:
                print(f">>>【本地止损】{symbol} 触发止损价 {local_sl:.4f}，执行市价平仓")
                self.close_position(symbol)
                return
        
        # 如果没有波动率，使用价格百分比估算
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
            
            # 检查并清理错误的挂单，并确保止损单唯一
            stop_orders = []
            for o in orders:
                if o['type'] == 'STOP':
                    print(f">>>【系统】发现不支持的止损挂单（订单号: {o['orderId']}），正在撤销...")
                    try:
                        self.client.client.futures_cancel_order(symbol=symbol, orderId=o['orderId'], recvWindow=10000)
                    except Exception as e:
                        print(f"   撤销失败: {e}")
                elif o['type'] == 'STOP_MARKET':
                    stop_orders.append(o)
            
            # 确保只有一个止损单
            if len(stop_orders) > 1:
                print(f">>>【系统】发现 {len(stop_orders)} 个止损单，正在清理多余委托...")
                if side == 'BUY':
                    stop_orders.sort(key=lambda x: float(x['stopPrice']), reverse=True)
                else:
                    stop_orders.sort(key=lambda x: float(x['stopPrice']), reverse=False)
                
                keep_order = stop_orders[0]
                for o in stop_orders[1:]:
                    try:
                        print(f"   撤销冗余止损单（触发价: {o['stopPrice']}）")
                        self.client.client.futures_cancel_order(symbol=symbol, orderId=o['orderId'], recvWindow=10000)
                    except Exception as e:
                        print(f"   撤销失败: {e}")
                
                stop_order = keep_order
            elif len(stop_orders) == 1:
                stop_order = stop_orders[0]
            else:
                stop_order = None

            if not stop_order:
                print(f">>>【移动止损】{symbol} 未检测到止损单，正在计算补单...")
                
                if atr <= 0: atr = current_price * 0.02
                
                if side == 'BUY':
                    base_sl = entry_price - (2 * atr)
                    if base_sl >= current_price:
                        base_sl = current_price - (2 * atr)
                        print(f"   【风险】已跌破原始止损，重设为更低止损: {base_sl:.4f}")
                else:
                    base_sl = entry_price + (2 * atr)
                    if base_sl <= current_price:
                        base_sl = current_price + (2 * atr)
                        print(f"   【风险】已涨破原始止损，重设为更高止损: {base_sl:.4f}")

                current_sl = base_sl
                new_sl = base_sl
                
                stop_order = {'orderId': None}
            else:
                current_sl = float(stop_order['stopPrice'])
                new_sl = None
            
            action_desc = ""
            
            # 混合移动止损逻辑：波动率跟踪 + 收益率锁定
            roi_pct = (current_price - entry_price) / entry_price if side == 'BUY' else (entry_price - current_price) / entry_price
            
            # 临时变量，用于比较最优止损位
            candidate_sl = current_sl 
            
            if side == 'BUY':
                # 放宽保本止损触发条件，避免频繁被打掉
                if (profit > 2.5 * atr or roi_pct > 0.03) and candidate_sl < entry_price:
                    candidate_sl = entry_price * 1.002 # 保本 + 手续费
                    action_desc = "触发保本止损"
                
                atr_trail = current_price - (3 * atr) # 从 2 放宽到 3
                if profit > 4 * atr and atr_trail > candidate_sl: # 必须有显著盈利才开始移动止损
                    candidate_sl = atr_trail
                    action_desc = "触发波动率移动止损"
                    
                if roi_pct > 0.08: # 放宽到 8%
                    roi_lock = entry_price * 1.01
                    if roi_lock > candidate_sl:
                        candidate_sl = roi_lock
                        action_desc = "触发收益率锁定利润（>8%）"
                        
                if roi_pct > 0.15: # 放宽到 15%
                    profit_lock = entry_price + (current_price - entry_price) * 0.6
                    if profit_lock > candidate_sl:
                        candidate_sl = profit_lock
                        action_desc = "触发利润回撤保护（>10%）"
                        
                # 确认是否更新
                if candidate_sl > current_sl:
                    new_sl = candidate_sl

            else:
                if (profit > 2.5 * atr or roi_pct > 0.03) and candidate_sl > entry_price:
                    candidate_sl = entry_price * 0.998
                    action_desc = "触发保本止损"
                    
                atr_trail = current_price + (3 * atr)
                if profit > 4 * atr and atr_trail < candidate_sl:
                    candidate_sl = atr_trail
                    action_desc = "触发波动率移动止损"
                    
                if roi_pct > 0.08:
                    roi_lock = entry_price * 0.99
                    if roi_lock < candidate_sl:
                        candidate_sl = roi_lock
                        action_desc = "触发收益率锁定利润（>8%）"
                        
                if roi_pct > 0.15:
                    profit_lock = entry_price + (current_price - entry_price) * 0.6
                    if profit_lock < candidate_sl:
                        candidate_sl = profit_lock
                        action_desc = "触发利润回撤保护 (>10%)"
                
                if candidate_sl < current_sl:
                    new_sl = candidate_sl
            
            if new_sl:
                print(f">>>【移动止损】{symbol} {action_desc}")
                print(f"   原止损: {current_sl} -> 新止损: {new_sl:.4f}")
                
                # 1. 撤销旧单 (如果有)
                if stop_order.get('orderId'):
                    try:
                        self.client.client.futures_cancel_order(symbol=symbol, orderId=stop_order['orderId'], recvWindow=10000)
                    except Exception as e:
                        print(f"   撤销旧止损单失败: {e}")
                else:
                    print("   正在挂初始止损单")
                
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
                
                limit_price = new_sl
                if close_side == 'SELL':
                     limit_price = new_sl * 0.95
                else:
                     limit_price = new_sl * 1.05
                     
                if filters:
                    if filters['tick_size'] > 0:
                        limit_price = round(limit_price, precision)
                    else:
                        limit_price = round(limit_price, filters['price_precision'])

                ok = self._place_stop_protection(symbol, side, new_sl)
                if not ok:
                    return
                
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
                self.client.client.futures_cancel_all_open_orders(symbol=symbol, recvWindow=10000)
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
            # 计算 Limit Price
            limit_price = new_sl
            if close_side == 'SELL':
                    limit_price = new_sl * 0.95
            else:
                    limit_price = new_sl * 1.05
                    
            if filters['tick_size'] > 0:
                limit_price = round(limit_price, precision)
            else:
                limit_price = round(limit_price, filters['price_precision'])
            
            # 确保 total_quantity 精度正确
            total_quantity = self._quantize_quantity(total_quantity, filters)

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
                self.client.client.futures_cancel_all_open_orders(symbol=symbol, recvWindow=10000)
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
