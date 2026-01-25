
from handlers.binance_client import BinanceClient
import math
from utils.logger import logger

class TradeExecutor:
    def __init__(self, client=None, state_manager=None):
        self.client = client or BinanceClient()
        self.state_manager = state_manager

    def get_current_stop_loss(self, symbol):
        """
        从交易所挂单中获取当前止损价
        """
        try:
            open_orders = self.client.get_open_orders(symbol=symbol)
            # 筛选 STOP 或 STOP_MARKET 且为 reduceOnly 或 closePosition 的订单
            sl_orders = [o for o in open_orders if o.get('type') in ['STOP_MARKET', 'STOP'] and (o.get('reduceOnly') or o.get('closePosition'))]
            
            if not sl_orders:
                return None
                
            # 返回第一个有效的止损价
            for o in sl_orders:
                sp = float(o.get('stopPrice') or 0)
                if sp > 0:
                    return sp
            return None
        except Exception:
            return None

    def place_stop_protection(self, symbol, side, stop_price, quantity=None, cancel_existing=True):
        """
        设置止损保护
        优先使用 STOP_MARKET，失败则使用 STOP 限价单
        """
        # 1. 准备参数
        close_side = 'SELL' if side == 'BUY' else 'BUY'
        
        # 精度处理
        filters = self.client.get_symbol_filters(symbol)
        if filters:
            stop_price = self._quantize_price(stop_price, filters)
            
        # 2. 尝试发送交易所订单
        try:
            if cancel_existing:
                # 撤销旧止损
                try:
                    open_orders = self.client.get_open_orders(symbol=symbol)
                    if open_orders:
                        cancelled_any = False
                        for o in open_orders:
                            if o.get('type') in ['STOP', 'STOP_MARKET'] and (o.get('reduceOnly') or o.get('closePosition')):
                                self.client.client.futures_cancel_order(symbol=symbol, orderId=o['orderId'])
                                cancelled_any = True
                        
                        if cancelled_any:
                            # 增加短暂缓冲，等待交易所完成撤单处理
                            import time
                            time.sleep(1)
                except Exception as e:
                    pass

            # 如果没有提供数量，尝试获取当前持仓
            if quantity is None:
                try:
                    positions = self.client.get_current_positions()
                    target = next((p for p in positions if p['symbol'] == symbol), None)
                    if target:
                        quantity = abs(float(target['amount']))
                except:
                    pass

            if quantity and quantity > 0:
                if filters:
                    quantity = self._quantize_quantity(quantity, filters)

                # 增加重试机制：如果止损设置失败，最多重试 5 次
                for i in range(5):
                    # 方案 1: STOP_MARKET + reduceOnly
                    try:
                        order = self.client.place_order(
                            symbol=symbol,
                            side=close_side,
                            order_type='STOP_MARKET',
                            stop_price=stop_price,
                            reduce_only=True,
                            quantity=quantity
                        )
                        if order:
                            logger.info(f"   [成功] 交易所止损单已设置 (STOP_MARKET, 触发价: {stop_price})")
                            return True
                    except Exception as e:
                        pass

                    # 方案 2: STOP (Limit) + reduceOnly
                    try:
                        limit_price = stop_price * 0.9 if close_side == 'SELL' else stop_price * 1.1
                        limit_price = self._quantize_price(limit_price, filters)
                        
                        order = self.client.place_order(
                            symbol=symbol,
                            side=close_side,
                            order_type='STOP',
                            stop_price=stop_price,
                            price=limit_price,
                            reduce_only=True,
                            quantity=quantity
                        )
                        if order:
                            logger.info(f"   [成功] 交易所止损单已设置 (STOP Limit, 触发价: {stop_price})")
                            return True
                    except Exception as e:
                        pass
                    
                    logger.warning(f"   [警告] 交易所止损设置失败 (尝试 {i+1}/5)，等待 2秒后重试...")
                    import time
                    time.sleep(2)

            logger.error(f"   [严重错误] 交易所止损设置最终失败 (已重试5次)！请手动检查！")
            return False 

        except Exception as e:
            logger.error(f"   [止损失败] 止损流程异常: {e}")
        
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

        try:
            open_orders = self.client.get_open_orders(symbol=symbol)
            if open_orders:
                for o in open_orders:
                    if o.get('type') in ['TAKE_PROFIT', 'TAKE_PROFIT_MARKET'] or \
                       (o.get('type') == 'LIMIT' and (o.get('reduceOnly') or o.get('closePosition'))):
                        self.client.client.futures_cancel_order(symbol=symbol, orderId=o['orderId'])
        except Exception as e:
            logger.warning(f"   [提示] 撤销旧止盈单失败: {e}")

        is_long = (open_side == 'BUY')
        delta = abs(take_profit - entry_price)
        tick_size = float(filters.get('tick_size', 0) or 0)
        
        # 如果止盈空间太小 (小于 10 倍 tick_size)，不分批，直接单笔
        if delta <= 0 or (tick_size > 0 and delta < 10 * tick_size):
            return False

        ratios = [0.3, 0.3, 0.4] 
        if is_long:
            raw_prices = [entry_price + 0.6 * delta, entry_price + 0.8 * delta, take_profit]
        else:
            raw_prices = [entry_price - 0.6 * delta, entry_price - 0.8 * delta, take_profit]

        prices = []
        for p in raw_prices:
            prices.append(self._quantize_price(p, filters))

        # Check ordering
        tick_size = float(filters.get('tick_size', 0) or 0)
        if tick_size > 0:
            if is_long:
                if prices[1] <= prices[0]: prices[1] = self._quantize_price(prices[0] + tick_size, filters)
                if prices[2] <= prices[1]: prices[2] = self._quantize_price(prices[1] + tick_size, filters)
            else:
                if prices[1] >= prices[0]: prices[1] = self._quantize_price(prices[0] - tick_size, filters)
                if prices[2] >= prices[1]: prices[2] = self._quantize_price(prices[1] - tick_size, filters)

        min_qty = float(filters.get('min_qty', 0) or 0)
        targets = []
        used = 0.0

        for i in range(len(ratios) - 1):
            raw_qty = total_quantity * ratios[i]
            qty = self._quantize_quantity(raw_qty, filters)
            if qty < min_qty: continue
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
                logger.info(f"   已挂分批止盈单: 价 {tp_price}，量 {tp_qty}")

        return ok_any

    def execute_trade(self, symbol, side, amount_usdt, leverage=5, slippage=0.01, stop_loss=None, take_profit=None):
        side_text = "做多" if side == 'BUY' else "做空"
        logger.info(f">>>【交易执行】准备对 {symbol} 执行 {side_text} 操作，金额: {amount_usdt}，杠杆: {leverage}倍")
        
        # 0. 风控检查
        account = self.client.get_account_info()
        if not account:
            logger.error(">>>【交易执行】错误: 无法获取账户信息，中止交易")
            return None
            
        avail_balance = account['balance']['available_balance']
        required_margin = amount_usdt / leverage
        if avail_balance < required_margin:
            logger.warning(f">>>【交易执行】余额不足! 可用: {avail_balance:.2f}, 需要保证金: {required_margin:.2f}")
            return None

        # 1. 调整杠杆
        self.client.change_leverage(symbol, leverage)
        
        # 2. 获取盘口
        book_ticker = self.client.get_book_tickers(symbol=symbol)
        if not book_ticker: return None
        
        ask_price = float(book_ticker['askPrice'])
        bid_price = float(book_ticker['bidPrice'])
        
        if ask_price == 0 or bid_price == 0:
            ticker = self.client.get_symbol_ticker(symbol)
            if ticker:
                ask_price = bid_price = float(ticker['price'])
            else:
                return None
            
        # 2. 计算价格
        exec_price = ask_price * (1 + slippage) if side == 'BUY' else bid_price * (1 - slippage)
            
        # 3. 计算数量
        filters = self.client.get_symbol_filters(symbol)
        if not filters: return None
        
        tick_size = filters['tick_size']
        if tick_size > 0:
            precision = int(round(-math.log(tick_size, 10), 0))
            exec_price = round(exec_price, precision)
        else:
            exec_price = round(exec_price, filters['price_precision'])
            
        raw_quantity = amount_usdt / exec_price
        quantity = self._quantize_quantity(raw_quantity, filters)
        
        if quantity < filters['min_qty']:
            logger.warning(f">>>【交易执行】数量 {quantity} 小于最小允许数量")
            return None
            
        # 4. 下单
        logger.info(f"   计算下单数量: {quantity}")
        order = self.client.place_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type='LIMIT',
            price=str(exec_price)
        )
        
        if order:
            logger.info(f">>>【交易执行】交易成功! 订单号: {order.get('orderId')}")
            
            # 5. 挂止盈止损
            if stop_loss or take_profit:
                # 增加缓冲时间，等待交易所持仓数据同步，防止 reduceOnly 报错
                import time
                time.sleep(2)
                
                logger.info(f">>>【交易执行】设置止盈止损...")
                
                if stop_loss:
                    # 循环重试止损设置，确保安全
                    sl_ok = False
                    for i in range(3):
                        if self.place_stop_protection(symbol, side, stop_loss, quantity=quantity, cancel_existing=True):
                            sl_ok = True
                            break
                        logger.warning(f"   [警告] 止损设置失败，正在重试 ({i+1}/3)...")
                        import time
                        time.sleep(1)
                    
                    if not sl_ok:
                        logger.error(f"!!! [严重警告] {symbol} 止损单设置完全失败！请手动检查！ !!!")

                if take_profit:
                    tp_price = self._quantize_price(take_profit, filters)
                    ok = self._place_split_take_profit(symbol, side, exec_price, tp_price, quantity, filters)
                    if not ok:
                        # 降级为单笔止盈
                        close_side = 'SELL' if side == 'BUY' else 'BUY'
                        self.client.place_order(symbol, close_side, quantity, 'LIMIT', str(tp_price), reduce_only=True)
                        logger.info(f"   已挂止盈单: {tp_price}")

        return order

    def update_stop_loss(self, symbol, side, new_price):
        return self.place_stop_protection(symbol, side, new_price, cancel_existing=True)

    def update_take_profit(self, symbol, side, new_price):
        logger.info(f">>> [Trader] 正在更新 {symbol} 止盈到 {new_price}...")
        try:
            open_orders = self.client.get_open_orders(symbol=symbol)
            tp_orders = [o for o in open_orders if o.get('type') in ['TAKE_PROFIT', 'TAKE_PROFIT_MARKET', 'LIMIT'] and (o.get('reduceOnly') or o.get('closePosition'))]
            
            for o in tp_orders:
                self.client.client.futures_cancel_order(symbol=symbol, orderId=o['orderId'])
            
            if new_price <= 0:
                logger.info(f"   已取消所有止盈单")
                return True

            positions = self.client.get_current_positions()
            target_pos = next((p for p in positions if p['symbol'] == symbol), None)
            if not target_pos: return False
                
            quantity = abs(float(target_pos['amount']))
            close_side = 'SELL' if side == 'BUY' else 'BUY'
            
            filters = self.client.get_symbol_filters(symbol)
            if filters:
                new_price = self._quantize_price(new_price, filters)
                quantity = self._quantize_quantity(quantity, filters)
            
            self.client.place_order(
                symbol=symbol,
                side=close_side,
                quantity=quantity,
                order_type='LIMIT',
                price=str(new_price),
                reduce_only=True
            )
            logger.info(f"   新止盈已设置: {new_price}")
            return True
        except Exception as e:
            logger.error(f"   更新止盈失败: {e}")
            return False

    def cancel_take_profit(self, symbol):
        """取消所有止盈单"""
        try:
            open_orders = self.client.get_open_orders(symbol=symbol)
            tp_orders = [o for o in open_orders if o.get('type') in ['TAKE_PROFIT', 'TAKE_PROFIT_MARKET', 'LIMIT'] and (o.get('reduceOnly') or o.get('closePosition'))]
            if tp_orders:
                logger.info(f"   [Trader] 撤销 {symbol} 所有止盈单...")
                for o in tp_orders:
                    self.client.client.futures_cancel_order(symbol=symbol, orderId=o['orderId'])
            return True
        except Exception:
            return False

    def close_position(self, symbol):
        logger.info(f">>>【交易执行】正在平掉 {symbol} 所有仓位...")
        try:
            self.client.client.futures_cancel_all_open_orders(symbol=symbol, recvWindow=10000)
        except Exception: pass
            
        positions = self.client.get_current_positions()
        target_pos = next((p for p in positions if p['symbol'] == symbol), None)
        if not target_pos: return True
            
        close_side = 'SELL' if target_pos['side'] == 'BUY' else 'BUY'
        quantity = abs(float(target_pos['amount']))
        
        try:
            self.client.place_order(symbol, close_side, quantity, 'MARKET', reduce_only=True)
            logger.info(f"   {symbol} 平仓成功")
            return True
        except Exception as e:
            logger.error(f"   强平失败: {e}")
            return False

    def increase_position(self, position, amount_usdt, current_price, atr):
        symbol = position['symbol']
        side = position['side']
        old_quantity = float(position['amount'])
        
        logger.info(f">>> [Trader] 正在对 {symbol} 执行加仓，金额: {amount_usdt} USDT...")
        
        filters = self.client.get_symbol_filters(symbol)
        if not filters: return False
            
        quantity = amount_usdt / current_price
        quantity = self._quantize_quantity(quantity, filters)
        
        if quantity < filters['min_qty']: return False
            
        try:
            self.client.place_order(symbol, side, quantity, 'MARKET')
            logger.info(f"   加仓成功: {quantity}")
            
            # 撤销旧止损
            try: self.client.client.futures_cancel_all_open_orders(symbol=symbol, recvWindow=10000)
            except: pass
                
            # 新止损
            total_quantity = old_quantity + quantity
            if side == 'BUY':
                new_sl = current_price - (1.5 * atr)
            else:
                new_sl = current_price + (1.5 * atr)
                
            self.place_stop_protection(symbol, side, new_sl, quantity=total_quantity)
            logger.info(f"   已更新止损 (总仓位 {total_quantity}): {new_sl}")
            return True
        except Exception as e:
            logger.error(f"   加仓失败: {e}")
            return False

    def reduce_position(self, position, reduce_pct, current_price):
        symbol = position['symbol']
        side = position['side']
        total_quantity = abs(float(position['amount']))
        reduce_qty = total_quantity * reduce_pct
        
        logger.info(f">>> [Trader] 正在对 {symbol} 执行减仓 ({reduce_pct*100}%), 数量: {reduce_qty}...")
        
        filters = self.client.get_symbol_filters(symbol)
        if not filters: return None
        
        reduce_qty = self._quantize_quantity(reduce_qty, filters)
        if reduce_qty < filters['min_qty']: return None
            
        try:
            close_side = 'SELL' if side == 'BUY' else 'BUY'
            self.client.place_order(symbol, close_side, reduce_qty, 'MARKET', reduce_only=True)
            logger.info(f"   减仓成功")
            
            if side == 'BUY':
                pnl = (current_price - float(position['entry_price'])) * reduce_qty
            else:
                pnl = (float(position['entry_price']) - current_price) * reduce_qty
            
            remain_qty = total_quantity - reduce_qty
            remain_qty = self._quantize_quantity(remain_qty, filters)
            
            if remain_qty >= filters['min_qty']:
                entry_price = float(position['entry_price'])
                if side == 'BUY':
                    new_sl = max(entry_price, current_price * 0.98)
                else:
                    new_sl = min(entry_price, current_price * 1.02)
                self.place_stop_protection(symbol, side, new_sl, quantity=remain_qty, cancel_existing=True)
            
            return pnl
        except Exception as e:
            logger.error(f"   减仓失败: {e}")
            return None
