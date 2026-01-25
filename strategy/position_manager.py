
import time
from strategy.analysis import MarketAnalyzer
from utils.logger import logger

class PositionManager:
    def __init__(self, client, state_manager, ai_strategy, trader, risk_manager):
        self.client = client
        self.state_manager = state_manager
        self.ai_strategy = ai_strategy
        self.trader = trader
        self.risk_manager = risk_manager
        self.analyzer = MarketAnalyzer()

    def sync_and_audit_positions(self):
        """
        同步持仓状态并执行持仓巡检 (Step 0)
        """
        logger.info("=== 开始持仓巡检 ===")
        
        # 1. 获取真实持仓
        real_positions = self.client.get_current_positions()
        local_pos = self.state_manager.get_position()
        
        # 2. 同步状态 (检查本地持仓是否已平)
        if local_pos:
            is_still_open = False
            for rp in real_positions:
                if rp['symbol'] == local_pos['symbol']:
                    is_still_open = True
                    break
            
            if not is_still_open:
                logger.info(f">>>【系统】检测到 {local_pos['symbol']} 仓位已平（可能触发止盈止损），清除本地状态")
                self.state_manager.clear_position()
                local_pos = None

        # 3. 清理无关委托
        self._clean_zombie_orders(real_positions)

        # 4. 强制单向持仓检查 (防止 API 延迟)
        has_position = len(real_positions) > 0
        if not has_position and local_pos:
            logger.warning(f">>>【系统】警告: 本地显示有持仓 {local_pos['symbol']} 但交易所返回空仓 (可能是延迟)，暂停开仓")
            time.sleep(5)
            return True # 返回 True 表示正在持仓或有异常，建议暂停开新仓
            
        if not has_position:
            return False

        logger.info(f">>> 当前持有 {len(real_positions)} 个仓位:")
        for p in real_positions:
            self._audit_single_position(p)
            
        logger.info(">>> [策略限制] 持仓中，暂停开新仓")
        time.sleep(60)
        return True

    def _clean_zombie_orders(self, real_positions):
        """清理无持仓的挂单"""
        try:
            open_orders = self.client.get_all_open_orders()
            if open_orders:
                holding_symbols = [p['symbol'] for p in real_positions]
                orders_by_symbol = {}
                for o in open_orders:
                    s = o['symbol']
                    if s not in orders_by_symbol: orders_by_symbol[s] = []
                    orders_by_symbol[s].append(o)
                
                for s, orders in orders_by_symbol.items():
                    if s not in holding_symbols:
                        logger.info(f">>>【系统】发现 {s} 无持仓但有 {len(orders)} 个挂单，正在清理无关委托...")
                        try:
                            self.client.client.futures_cancel_all_open_orders(symbol=s, recvWindow=10000)
                            logger.info(f"   已撤销 {s} 所有挂单")
                        except Exception as e:
                            logger.error(f"   清理失败: {e}")
        except Exception as e:
            logger.error(f"【系统】挂单清理检查出错: {e}")

    def _audit_single_position(self, p):
        symbol = str(p['symbol'])
        side_text = "多" if p['side'] == 'BUY' else "空"
        
        # 显示信息
        meta = self.state_manager.get_position_meta(symbol)
        local_sl_info = f"{meta['local_stop']}" if meta and meta.get('local_stop') else "未设置"
        logger.info(f"   {symbol} ({side_text}): 数量 {p['amount']}, 浮盈亏 {p['unrealized_pnl']} 美元, 本地止损: {local_sl_info}")
        
        # 获取 K 线数据
        df = self.client.get_klines(symbol, '1m', limit=220)
        df_large = self.client.get_klines(symbol, '15m', limit=220)
        df_1h = self.client.get_klines(symbol, '1h', limit=220)
        
        if df is None:
            logger.warning(f"   [警告] 无法获取 {symbol} 的 K 线数据，跳过本次分析")
            return

        df_analyzed = self.analyzer.calculate_indicators(df)
        df_large_analyzed = self.analyzer.calculate_indicators(df_large) if df_large is not None else None
        df_1h_analyzed = self.analyzer.calculate_indicators(df_1h) if df_1h is not None else None
        
        # 1. 获取 AI 建议
        open_orders = self.client.get_open_orders(symbol)
        ai_action, ai_info = self.ai_strategy.audit_position(p, df_analyzed, open_orders=open_orders, df_1h=df_1h_analyzed)
        
        # 2. 处理 AI 动态调整 (止盈止损)
        self._handle_ai_adjustments(symbol, p, ai_info, df_analyzed)
        
        # 3. 处理加减仓逻辑
        is_scaling_op = self._handle_scaling(p, ai_action, ai_info, df_analyzed, df_large_analyzed)
        
        # 4. 如果没做加减仓，检查平仓或移动止损
        if not is_scaling_op:
            if ai_action == 'CLOSE' and (ai_info.get('confidence', 0) >= 65):
                logger.info(f">>>【智能建议】对 {symbol} 执行平仓操作，理由: {ai_info.get('reason')}")
                success = self.trader.close_position(symbol)
                if success:
                    self.state_manager.clear_position(symbol=symbol, pnl=p['unrealized_pnl'])
                    logger.info(f"   {symbol} 已平仓")
            else:
                # 移动止损
                ai_sl = ai_info.get('stop_loss')
                try: ai_sl = float(ai_sl) if ai_sl else None
                except: ai_sl = None
                
                current_price = df_analyzed.iloc[-1]['收盘价']
                current_atr = df_analyzed.iloc[-1].get('ATR', 0)
                
                # 调用重构后的移动止损逻辑
                self.check_trailing_stop(p, current_price, current_atr, ai_sl, df_analyzed)

    def _handle_ai_adjustments(self, symbol, position, ai_info, df_analyzed):
        """处理 AI 的动态调整建议"""
        current_price = df_analyzed.iloc[-1]['收盘价']
        adjustment = ai_info.get('adjustment')
        
        if adjustment:
            adj_type = adjustment.get('type')
            adj_val = adjustment.get('value')
            adj_reason = adjustment.get('reason', '')
            
            logger.info(f"   >>> [AI 动态调整] {adj_type}: {adj_reason}")
            
            if adj_type == 'CANCEL_TP':
                self.trader.update_take_profit(symbol, position['side'], 0)
                
            elif adj_type in ['MOVE_SL', 'SET_SL'] and adj_val:
                try:
                    new_sl = float(adj_val)
                    if position['side'] == 'BUY' and new_sl >= current_price:
                        logger.warning(f"       止损触发价不合理，已忽略: {new_sl}")
                    elif position['side'] == 'SELL' and new_sl <= current_price:
                        logger.warning(f"       止损触发价不合理，已忽略: {new_sl}")
                    else:
                        self.trader.update_stop_loss(symbol, position['side'], new_sl)
                except ValueError:
                    logger.error(f"       无效的止损数值: {adj_val}")
                    
            elif adj_type in ['MOVE_TP', 'SET_TP'] and adj_val:
                try:
                    new_tp = float(adj_val)
                    self.trader.update_take_profit(symbol, position['side'], new_tp)
                except ValueError:
                    logger.error(f"       无效的止盈数值: {adj_val}")

    def _handle_scaling(self, p, ai_action, ai_info, df_analyzed, df_large_analyzed):
        """处理加减仓"""
        current_price = df_analyzed.iloc[-1]['收盘价']
        current_atr = df_analyzed.iloc[-1].get('ATR', 0)
        ai_confidence = ai_info.get('confidence', 0)
        ai_reason = ai_info.get('reason', '')
        
        # 优先执行 AI 指令
        if ai_action == 'ADD' and ai_confidence >= 65:
            balance_info = self.client.get_balance()
            total_equity = balance_info.get('总权益', 0) if balance_info else 0
            current_position_value = abs(float(p['amount'])) * current_price
            
            if total_equity > 0 and current_position_value < (total_equity * 0.40):
                logger.info(f">>>【智能加仓】AI 建议加仓 (信心 {ai_confidence}), 理由: {ai_reason}")
                add_amount = current_position_value * 0.3
                if add_amount > 10:
                    success = self.trader.increase_position(p, add_amount, current_price, current_atr)
                    if success: return True
                    
        elif ai_action == 'REDUCE' and ai_confidence >= 65:
            logger.info(f">>>【智能减仓】AI 建议减仓 (信心 {ai_confidence}), 理由: {ai_reason}")
            pnl = self.trader.reduce_position(p, 0.3, current_price)
            if pnl is not None:
                self.state_manager.update_pnl(pnl)
                return True
                
        # 规则加减仓 (简化版，原逻辑比较复杂，这里保留核心逻辑)
        # 计算 ATR 倍数
        entry_price = float(p['entry_price'])
        side = p['side']
        profit_per_share = (current_price - entry_price) if side == 'BUY' else (entry_price - current_price)
        atr_multiple = profit_per_share / current_atr if current_atr > 0 else 0
        
        # 趋势对齐判断
        trend_1m = self.analyzer.get_trend_bias(df_analyzed)
        trend_15m = self.analyzer.get_trend_bias(df_large_analyzed) if df_large_analyzed is not None else None
        
        is_long = side == 'BUY'
        target_trend = 'BUY_ONLY' if is_long else 'SELL_ONLY'
        align_15m = trend_15m == target_trend
        align_1m = trend_1m == target_trend
        alignment_score = int(align_15m) + int(align_1m)

        # 加仓
        if atr_multiple > 2.5 and alignment_score >= 2:
            if not (ai_action == 'CLOSE' and ai_confidence > 50):
                balance_info = self.client.get_balance()
                total_equity = balance_info.get('总权益', 0) if balance_info else 0
                current_position_value = abs(float(p['amount'])) * current_price
                if total_equity > 0 and current_position_value < (total_equity * 0.40):
                    add_amount = current_position_value * 0.5
                    if add_amount > 10:
                         success = self.trader.increase_position(p, add_amount, current_price, current_atr)
                         if success: return True

        # 减仓
        if atr_multiple > 4.0:
             # 如果 AI 强烈建议持有 (信心 > 80)，则暂时不减仓
             if not (ai_action == 'HOLD' and ai_confidence >= 80):
                 # [优化] 根据趋势强度动态调整减仓比例
                 reduce_ratio = 0.3
                 if trend_1m == target_trend and alignment_score >= 2:
                     reduce_ratio = 0.2 # 趋势强劲，少减一点
                 
                 logger.info(f">>>【规则减仓】利润丰厚 ({atr_multiple:.1f} ATR)，锁定部分利润 ({reduce_ratio*100}%)")
                 pnl = self.trader.reduce_position(p, reduce_ratio, current_price)
                 if pnl is not None:
                     self.state_manager.update_pnl(pnl)
                     return True
                     
        return False

    def check_trailing_stop(self, position, current_price, atr=0, ai_sl=None, df_analyzed=None):
        """
        移动止损计算逻辑 (从 Trader 中迁移过来)
        """
        symbol = position['symbol']
        entry_price = float(position['entry_price'])
        side = position['side']
        
        # 获取当前止损
        current_sl = self.trader.get_current_stop_loss(symbol) # 需要在 Trader 中公开此方法
        
        if atr <= 0: atr = current_price * 0.02
        
        # 初始止损设置
        if current_sl is None:
            if ai_sl and ai_sl > 0:
                logger.info(f">>>【移动止损】{symbol} 初始止损采用 AI 建议值: {ai_sl}")
                current_sl = ai_sl
            else:
                logger.info(f">>>【移动止损】{symbol} 计算初始止损 (3.0 ATR)...")
                if side == 'BUY':
                    current_sl = entry_price - (3.0 * atr)
                    if current_sl >= current_price: current_sl = current_price - (3.0 * atr)
                else:
                    current_sl = entry_price + (3.0 * atr)
                    if current_sl <= current_price: current_sl = current_price + (3.0 * atr)
            
            self.trader.place_stop_protection(symbol, side, current_sl)
            return

        # 移动止损逻辑
        roi_pct = (current_price - entry_price) / entry_price if side == 'BUY' else (entry_price - current_price) / entry_price
        candidate_sl = current_sl
        action_desc = ""
        
        # AI 止损融合
        used_ai_sl = False
        if ai_sl and ai_sl > 0:
            if side == 'BUY':
                if ai_sl < current_price and ai_sl > candidate_sl:
                    candidate_sl = ai_sl
                    action_desc = "触发 AI 动态止损 (支撑位)"
                    used_ai_sl = True
            else:
                if ai_sl > current_price and ai_sl < candidate_sl:
                    candidate_sl = ai_sl
                    action_desc = "触发 AI 动态止损 (压力位)"
                    used_ai_sl = True

        # 规则止损逻辑
        if side == 'BUY':
            # 1. 快速保本逻辑：只要浮盈超过 1.5%，立即将止损提到开仓价上方一点点
            if roi_pct > 0.015 and candidate_sl < entry_price:
                candidate_sl = entry_price * 1.002
                action_desc = "触发快速保本止损 (>1.5%)"
            
            # 2. 利润锁定逻辑优化
            if not used_ai_sl:
                atr_trail = current_price - (4.5 * atr)
                
                # 动态调整 ATR 倍数 (基于趋势强度)
                # 如果 1m 趋势与持仓方向一致，且 CMF > 0，说明趋势强劲，放宽止损
                trend_1m = self.analyzer.get_trend_bias(df_analyzed)
                cmf = df_analyzed.iloc[-1].get('CMF', 0)
                is_trend_strong = (trend_1m == 'BUY_ONLY' and cmf > 0.05)
                
                atr_mult_tight = 3.5 if is_trend_strong else 2.5
                atr_mult_mod = 4.5 if is_trend_strong else 3.5
                
                if 0.015 < roi_pct <= 0.05:
                    atr_tight = current_price - (atr_mult_tight * atr)
                    if atr_tight > candidate_sl:
                        candidate_sl = atr_tight
                        action_desc = f"触发利润保护移动止损 ({atr_mult_tight} ATR, 趋势强:{is_trend_strong})"

                if 0.05 < roi_pct <= 0.08:
                    atr_moderate = current_price - (atr_mult_mod * atr)
                    if atr_moderate > candidate_sl:
                        candidate_sl = atr_moderate
                        action_desc = f"触发温和移动止损 ({atr_mult_mod} ATR)"
                        
                if roi_pct > 0.08 and atr_trail > candidate_sl:
                    candidate_sl = atr_trail
                    action_desc = "触发宽幅移动止损 (4.5 ATR)"
                    
            if roi_pct > 0.15: # 降低门槛从 0.2 -> 0.15
                profit_lock = entry_price + (current_price - entry_price) * 0.6 # 锁定 60% 利润
                if profit_lock > candidate_sl:
                    candidate_sl = profit_lock
                    action_desc = "触发利润回撤保护 (锁定60%)"
            
            if roi_pct > 0.30: # 降低门槛从 0.4 -> 0.3
                atr_tight = current_price - (2.5 * atr) # 收得更紧
                if atr_tight > candidate_sl:
                    candidate_sl = atr_tight
                    action_desc = "触发高位收紧止损"
                    
            if candidate_sl > current_sl:
                new_sl = candidate_sl
            else:
                new_sl = None

        else: # SELL
            # 1. 快速保本逻辑
            if roi_pct > 0.015 and candidate_sl > entry_price:
                candidate_sl = entry_price * 0.998
                action_desc = "触发快速保本止损 (>1.5%)"
                
            if not used_ai_sl:
                atr_trail = current_price + (4.5 * atr)
                
                # 动态调整 ATR 倍数
                trend_1m = self.analyzer.get_trend_bias(df_analyzed)
                cmf = df_analyzed.iloc[-1].get('CMF', 0)
                is_trend_strong = (trend_1m == 'SELL_ONLY' and cmf < -0.05)
                
                atr_mult_tight = 3.5 if is_trend_strong else 2.5
                atr_mult_mod = 4.5 if is_trend_strong else 3.5
                
                # 利润保护
                if 0.015 < roi_pct <= 0.05:
                    atr_tight = current_price + (atr_mult_tight * atr)
                    if atr_tight < candidate_sl:
                        candidate_sl = atr_tight
                        action_desc = f"触发利润保护移动止损 ({atr_mult_tight} ATR, 趋势强:{is_trend_strong})"

                if 0.05 < roi_pct <= 0.08:
                    atr_moderate = current_price + (atr_mult_mod * atr)
                    if atr_moderate < candidate_sl:
                        candidate_sl = atr_moderate
                        action_desc = f"触发温和移动止损 ({atr_mult_mod} ATR)"
                        
                if roi_pct > 0.08 and atr_trail < candidate_sl:
                    candidate_sl = atr_trail
                    action_desc = "触发宽幅移动止损"
            
            if roi_pct > 0.15:
                profit_lock = entry_price + (current_price - entry_price) * 0.6
                if profit_lock < candidate_sl:
                    candidate_sl = profit_lock
                    action_desc = "触发利润回撤保护 (锁定60%)"
                    
            if roi_pct > 0.30:
                atr_tight = current_price + (2.5 * atr)
                if atr_tight < candidate_sl:
                    candidate_sl = atr_tight
                    action_desc = "触发高位收紧止损"
            
            if candidate_sl < current_sl:
                new_sl = candidate_sl
            else:
                new_sl = None

        if new_sl:
            # 过滤微小调整：只有当新止损与旧止损差距超过 0.2% 时才执行
            price_diff_pct = abs(new_sl - current_sl) / current_sl
            if price_diff_pct < 0.002:
                # logger.info(f"   [移动止损] 调整幅度过小 ({price_diff_pct*100:.3f}%)，忽略本次更新")
                return

            logger.info(f">>>【移动止损】{symbol} {action_desc}")
            logger.info(f"   原止损: {current_sl} -> 新止损: {new_sl:.4f}")
            self.trader.place_stop_protection(symbol, side, new_sl)
            
            # 动态止盈撤销
            if roi_pct > 0.05:
                self.trader.cancel_take_profit(symbol)
