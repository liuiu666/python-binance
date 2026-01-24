import time
import sys
import traceback
from handlers.binance_client import BinanceClient
from strategy.analysis import MarketAnalyzer
from strategy.scanner import MarketScanner
from strategy.sentiment import SentimentAnalyzer
from strategy.ai_strategy import AIStrategy
from handlers.trader import TradeExecutor
from utils.state_manager import StateManager

def run_bot():
    print("=== 量化自动交易系统启动 ===")
    print(">>> 正在加载模块...")
    
    # 初始化模块
    client = None
    while client is None:
        try:
            client = BinanceClient()
        except Exception:
            print(">>>【系统】初始化失败，5秒后重试...")
            time.sleep(5)
            
    state_manager = StateManager()
    scanner = MarketScanner(client=client)
    analyzer = MarketAnalyzer()
    sentiment = SentimentAnalyzer(client=client)
    ai_strategy = AIStrategy(client=client)
    trader = TradeExecutor(client=client, state_manager=state_manager)
    
    while True:
        try:
            print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] === 开始新一轮扫描 ===")
            
            # 0. 持仓同步与检查
            # 首先获取交易所真实持仓
            real_positions = client.get_current_positions()
            
            # 获取本地记录的持仓
            local_pos = state_manager.get_position()
            
            # 状态同步逻辑
            if local_pos:
                # 检查本地持仓是否还在真实持仓中
                is_still_open = False
                for rp in real_positions:
                    if rp['symbol'] == local_pos['symbol']:
                        is_still_open = True
                        break
                
                if not is_still_open:
                    print(f">>>【系统】检测到 {local_pos['symbol']} 仓位已平（可能触发止盈止损），清除本地状态")
                    state_manager.clear_position()
                    local_pos = None
            
            # [新增] 清理“无关委托” (无持仓的挂单)
            try:
                open_orders = client.get_all_open_orders()
                if open_orders:
                    # 获取当前持仓的交易对列表
                    holding_symbols = [p['symbol'] for p in real_positions]
                    
                    # 按交易对分组挂单
                    orders_by_symbol = {}
                    for o in open_orders:
                        s = o['symbol']
                        if s not in orders_by_symbol:
                            orders_by_symbol[s] = []
                        orders_by_symbol[s].append(o)
                    
                    # 检查是否有无持仓的挂单
                    for s, orders in orders_by_symbol.items():
                        if s not in holding_symbols:
                            print(f">>>【系统】发现 {s} 无持仓但有 {len(orders)} 个挂单，正在清理无关委托...")
                            try:
                                client.client.futures_cancel_all_open_orders(symbol=s, recvWindow=10000)
                                print(f"   已撤销 {s} 所有挂单")
                            except Exception as e:
                                print(f"   清理失败: {e}")
            except Exception as e:
                print(f"【系统】挂单清理检查出错: {e}")

            # 再次检查是否有真实持仓 (强制单向持仓限制)
            # [修复] 结合本地状态，防止 API 延迟导致误判空仓
            has_position = len(real_positions) > 0
            if not has_position and local_pos:
                print(f">>>【系统】警告: 本地显示有持仓 {local_pos['symbol']} 但交易所返回空仓 (可能是延迟)，暂停开仓")
                # 强制跳过开仓逻辑
                time.sleep(5)
                continue
                
            if len(real_positions) > 0:
                print(f">>> 当前持有 {len(real_positions)} 个仓位:")
                for p in real_positions:
                    symbol = str(p['symbol'])
                    side_text = "多"
                    if p['side'] == 'SELL':
                        side_text = "空"
                    
                    # 获取本地止损信息用于显示
                    meta = state_manager.get_position_meta(symbol)
                    local_sl_info = "未设置"
                    if meta and meta.get('local_stop'):
                        local_sl_info = f"{meta['local_stop']}"
                        
                    print(f"   {symbol} ({side_text}): 数量 {p['amount']}, 浮盈亏 {p['unrealized_pnl']} 美元, 本地止损: {local_sl_info}")
                    
                    # 持仓巡检：智能评估 + 移动止损
                    # 获取K线
                    df = client.get_klines(symbol, '1m', limit=220)
                    df_large = client.get_klines(symbol, '15m', limit=220)
                    # df_small 已移除，直接复用 df (1m)
                    
                    if df is not None:
                        df_analyzed = analyzer.calculate_indicators(df)
                        df_large_analyzed = analyzer.calculate_indicators(df_large) if df_large is not None else None

                        
                        # [新增] 移动止损检查
                        current_atr = df_analyzed.iloc[-1].get('ATR', 0)
                        current_price = df_analyzed.iloc[-1]['收盘价']
                        
                        # 计算浮盈的波动率倍数
                        entry_price = float(p['entry_price'])
                        amount = abs(float(p['amount']))
                        side = p['side']
                        if side == 'BUY':
                            profit_per_share = current_price - entry_price
                        else:
                            profit_per_share = entry_price - current_price
                        
                        atr_multiple = profit_per_share / current_atr if current_atr > 0 else 0

                        trend_1m = analyzer.get_trend_bias(df_analyzed) if df_analyzed is not None else None
                        trend_15m = analyzer.get_trend_bias(df_large_analyzed) if df_large_analyzed is not None else None
                        
                        is_long = side == 'BUY'
                        align_15m = trend_15m == ('BUY_ONLY' if is_long else 'SELL_ONLY')
                        align_1m = trend_1m == ('BUY_ONLY' if is_long else 'SELL_ONLY')
                        
                        opp_15m = trend_15m == ('SELL_ONLY' if is_long else 'BUY_ONLY')
                        opp_1m = trend_1m == ('SELL_ONLY' if is_long else 'BUY_ONLY')
                        
                        alignment_score = int(align_15m) + int(align_1m)
                        opposite_score = int(opp_15m) + int(opp_1m)
                        has_alignment_data = trend_1m is not None or trend_15m is not None
                        
                        # 智能评估（获取信心分数用于加减仓判断）
                        # [新增] 获取当前挂单，辅助 AI 决策
                        open_orders = client.get_open_orders(symbol)
                        
                        ai_action, ai_info = ai_strategy.audit_position(p, df_analyzed, open_orders=open_orders)
                        ai_reason = ai_info.get('reason', '')
                        ai_confidence = ai_info.get('confidence', 0)
                        
                        # [新增] 处理 AI 的挂单调整建议 (Dynamic Adjustment)
                        adjustment = ai_info.get('adjustment')
                        if adjustment:
                            adj_type = adjustment.get('type')
                            adj_val = adjustment.get('value')
                            adj_reason = adjustment.get('reason', '')
                            
                            print(f"   >>> [AI 动态调整] {adj_type}: {adj_reason}")
                            
                            if adj_type == 'CANCEL_TP':
                                trader.update_take_profit(symbol, p['side'], 0) # 0 means cancel all
                                
                            elif adj_type in ['MOVE_SL', 'SET_SL'] and adj_val:
                                try:
                                    new_sl = float(adj_val)
                                    if side == 'BUY' and current_price > entry_price and new_sl < entry_price:
                                        print(f"       止损低于开仓价，已修正为保本价: {entry_price}")
                                        new_sl = entry_price
                                    if side == 'SELL' and current_price < entry_price and new_sl > entry_price:
                                        print(f"       止损高于开仓价，已修正为保本价: {entry_price}")
                                        new_sl = entry_price
                                    if side == 'BUY' and new_sl >= current_price:
                                        print(f"       止损触发价不合理，已忽略: {new_sl}")
                                    elif side == 'SELL' and new_sl <= current_price:
                                        print(f"       止损触发价不合理，已忽略: {new_sl}")
                                    else:
                                        trader.update_stop_loss(symbol, p['side'], new_sl)
                                except ValueError:
                                    print(f"       无效的止损数值: {adj_val}")
                                    
                            elif adj_type in ['MOVE_TP', 'SET_TP'] and adj_val:
                                try:
                                    new_tp = float(adj_val)
                                    trader.update_take_profit(symbol, p['side'], new_tp)
                                except ValueError:
                                    print(f"       无效的止盈数值: {adj_val}")
                                    
                        # 兼容旧逻辑 (如果 LLM 还在输出 adjust_suggestion 字符串)
                        adjust_suggestion = ai_info.get('adjust_suggestion')
                        if adjust_suggestion and not adjustment:
                             print(f"   >>> [AI 建议调整挂单] {adjust_suggestion}")
                             if "取消止盈" in adjust_suggestion:
                                 trader.update_take_profit(symbol, p['side'], 0)

                        # 浮动加减仓逻辑
                        is_scaling_op = False
                        
                        # [AI 动态加减仓] 优先执行 AI 的明确指令
                        if ai_action == 'ADD' and ai_confidence >= 65:
                            # 检查资金
                            balance_info = client.get_balance()
                            total_equity = balance_info.get('总权益', 0) if balance_info else 0
                            current_position_value = abs(float(p['amount'])) * current_price
                            
                            if total_equity > 0 and current_position_value < (total_equity * 0.40):
                                print(f">>>【智能加仓】AI 建议加仓 (信心 {ai_confidence}), 理由: {ai_reason}")
                                # 默认加仓 30%
                                add_amount = current_position_value * 0.3
                                if add_amount > 10:
                                    success = trader.increase_position(p, add_amount, current_price, current_atr)
                                    if success:
                                        is_scaling_op = True
                                        
                        elif ai_action == 'REDUCE' and ai_confidence >= 65:
                            print(f">>>【智能减仓】AI 建议减仓 (信心 {ai_confidence}), 理由: {ai_reason}")
                            # 默认减仓 30%
                            pnl = trader.reduce_position(p, 0.3, current_price)
                            if pnl is not None:
                                is_scaling_op = True
                                state_manager.update_pnl(pnl)
                        
                        # [规则加减仓] 仅当 AI 未操作时执行
                        if not is_scaling_op:
                            # 加仓逻辑：浮盈 > 2.5 倍波动率
                            if atr_multiple > 2.5 and alignment_score >= 2:
                                # 智能过滤：如果建议平仓且信心尚可，则不加仓
                                if ai_action == 'CLOSE' and ai_confidence > 50:
                                    print(f"   【策略】规则触发加仓，但智能评估建议平仓（信心 {ai_confidence}）-> 取消加仓")
                                else:
                                    # 检查是否已达到最大仓位（例如总权益的 10%）
                                    balance_info = client.get_balance()
                                    total_equity = balance_info.get('总权益', 0) if balance_info else 0
                                    current_position_value = abs(float(p['amount'])) * current_price
                                    
                                    if total_equity > 0 and current_position_value < (total_equity * 0.40):
                                        # 加仓：按当前仓位价值的 50% 加
                                        add_amount = current_position_value * 0.5 
                                        # 最小加仓限制
                                        if add_amount > 10:
                                            success = trader.increase_position(p, add_amount, current_price, current_atr)
                                            if success:
                                                is_scaling_op = True
                            
                            # 减仓逻辑：浮盈 > 4 倍波动率
                            elif atr_multiple > 4.0 or (has_alignment_data and alignment_score <= 1 and atr_multiple > 1.8):
                                # 智能过滤：如果强烈建议持有，则推迟减仓
                                if ai_action == 'HOLD' and ai_confidence >= 80 and opposite_score < 2:
                                    print(f"   【策略】规则触发减仓，但智能评估强烈建议持有（信心 {ai_confidence}）-> 暂不减仓")
                                else:
                                    # 减仓 30%
                                    pnl = trader.reduce_position(p, 0.3, current_price)
                                    if pnl is not None:
                                        is_scaling_op = True
                                        # 更新已实现盈亏
                                        state_manager.update_pnl(pnl)
                                        print(f"   【状态】减仓已实现盈亏: {pnl:.2f} 美元")
                                
                        # 如果没有执行加减仓
                        if not is_scaling_op:
                            # 检查是否需要全平
                            if ai_action == 'CLOSE' and (ai_confidence >= 65 or opposite_score >= 2):
                                print(f">>>【智能建议】对 {symbol} 执行平仓操作，理由: {ai_reason}")
                                success = trader.close_position(symbol)
                                if success:
                                    state_manager.clear_position(symbol=symbol, pnl=p['unrealized_pnl'])
                                    print(f"   {symbol} 已平仓")
                            else:
                                # 移动止损 (仅在未做其他操作时执行)
                                trader.check_trailing_stop(p, current_price, atr=current_atr)
                
                print(">>> [策略限制] 持仓中，暂停开新仓")
                time.sleep(60)
                continue

            # 1. 大盘熔断检测
            is_safe, reason = sentiment.check_market_sentiment()
            if not is_safe:
                print(f"【熔断】{reason} -> 暂停交易")
                time.sleep(300) # 暴跌时多睡会儿 (5分钟)
                continue


            # 2. 扫描筛选
            target_symbol = None
            target_trend_bias = None
            
            # [自动选币模式] 扫描市场筛选优质币种 (Top 5)
            # 增加一些币种进入候选池
            try:
                scanned_list = scanner.scan_market(top_n=5, min_volume=50000000) # 5000万成交额门槛
            except Exception as e:
                print(f"   [扫描] 扫描失败: {e}")
                scanned_list = []

            tickers = []
            if scanned_list is not None and not scanned_list.empty:
                tickers = scanned_list['symbol'].tolist()
            
            # [保留偏好] 始终包含 ACUUSDT (如果它还在交易中)
            if 'ACUUSDT' not in tickers:
                tickers.insert(0, 'ACUUSDT') # 放在首位
            
            print(f"   【扫描结果】当前关注列表: {tickers}")
            print(f"   正在分析趋势与资金流...")
 
            for sym in tickers:
                # 冷却检查
                if state_manager.is_in_cooldown(sym):
                    # 检查是否真的还在冷却期 (1小时)
                    # 优化：如果是 ADX 过滤导致的，可以缩短冷却时间
                    continue
                    
                # 获取 1m K线确认趋势
                df_1m = client.get_klines(sym, '1m', limit=100)
                if df_1m is None:
                    continue
                
                df_1m_analyzed = analyzer.calculate_indicators(df_1m)
                if df_1m_analyzed is None:
                    continue

                # 资金流向初步过滤 (基于K线计算的 CMF/NetFlow)
                cmf = df_1m_analyzed.iloc[-1].get('CMF', 0)
                net_flow_ma = df_1m_analyzed.iloc[-1].get('Net_Flow_MA5', 0)
                
                # 如果资金流极差，直接跳过 (不浪费后续计算资源)
                # 例如 CMF < -0.15 且 NetFlow < 0 (严重流出)
                if cmf < -0.15 and net_flow_ma < 0:
                    # print(f"   [过滤] {sym} 资金严重流出 (CMF: {cmf:.2f})")
                    continue

                trend_bias = analyzer.get_trend_bias(df_1m_analyzed)
                if trend_bias:
                    # 再次确认：如果是做多趋势，但 CMF 为负，则需要谨慎
                    if trend_bias == 'BUY_ONLY' and cmf < -0.05:
                         continue
                    # 如果是做空趋势，但 CMF 为正，也跳过
                    if trend_bias == 'SELL_ONLY' and cmf > 0.05:
                         continue
                         
                    target_symbol = sym
                    target_trend_bias = trend_bias
                    print(f"   => 锁定目标: {target_symbol} ({target_trend_bias}, CMF:{cmf:.2f})")
                    break
                else:
                    # 调试日志：为什么没选中
                    print(f"   [跳过] {sym} 趋势不明确 (MA未排列)")
            
            if not target_symbol:
                print("   没有发现合适的趋势币种，休息 30秒...")
                time.sleep(30)
                continue
                
            print(f"   正在深入分析 {target_symbol} ...")
            
            # 3. 详细数据获取 (1m + 5m)
            df_1m = client.get_klines(target_symbol, '1m', limit=220)
            df_5m = client.get_klines(target_symbol, '5m', limit=200)
            if df_1m is None or df_5m is None:
                # time.sleep(10)
                continue
                
            df_1m_analyzed = analyzer.calculate_indicators(df_1m)
            df_5m_analyzed = analyzer.calculate_indicators(df_5m)
            if df_1m_analyzed is None or df_5m_analyzed is None:
                # time.sleep(10)
                continue

            trend_bias = target_trend_bias or analyzer.get_trend_bias(df_1m_analyzed)
            if not trend_bias:
                print(f"   【策略】趋势不明确，跳过 {target_symbol}")
                state_manager.set_cooldown(target_symbol)
                # time.sleep(60)
                continue
            
            # 4. 策略 (AI 决策)
            # 传入 df_5m_analyzed 作为大周期参考
            signal, info = ai_strategy.analyze(df_1m_analyzed, symbol=target_symbol, trend_bias=trend_bias, df_larger=df_5m_analyzed)
            
            # 只有当 AI 明确给出信号时才使用，否则回退到规则策略
            if not signal:
                 signal, info = analyzer.check_trend_following(df_1m_analyzed, trend_bias=trend_bias)
            
            if signal:
                signal_text = "做多" if signal == 'BUY' else "做空"
                print(f"!!! 趋势信号触发: {signal_text} !!!")
                
                # 5. 交易
                # 动态仓位计算
                balance_info = client.get_balance()
                total_equity = balance_info.get('总权益', 0) if balance_info else 0
                
                trade_amount = 20 # 默认最小额
                
                # [优化] 根据 ATR 动态调整杠杆
                leverage = analyzer.suggest_leverage(df_1m_analyzed)
                print(f"   【风控】当前市场波动建议杠杆: {leverage}x")
                
                stop_loss = info.get('stop_loss')
                current_price = info.get('current_price')
                
                if total_equity > 0 and stop_loss and current_price:
                    # 风险模型: 单笔亏损不超过总权益的 6% (极度激进模式)
                    risk_per_trade = total_equity * 0.06 
                    
                    price_diff = abs(current_price - stop_loss)
                    if price_diff > 0:
                        # 仓位价值 = (风险金额 / 止损价差) * 当前价格
                        position_value = (risk_per_trade / price_diff) * current_price
                        
                        # 限制最大仓位为总权益的 95% * 杠杆 (几乎满仓)
                        max_position_value = total_equity * 0.95 * leverage 
                        trade_amount = min(position_value, max_position_value)
                        
                        # 再次检查最小交易额 (比如 10U)
                        trade_amount = max(trade_amount, 10.0)
                        
                        print(f"   【风控计算】风险额: {risk_per_trade:.2f}, 止损幅: {price_diff/current_price*100:.2f}%, 建议仓位: {trade_amount:.2f}")
                
                # 获取策略建议的止盈止损
                take_profit = info.get('take_profit')
                
                # [Fix] 如果没有止损价（比如 AI 未返回且规则策略也未覆盖），必须兜底
                if not stop_loss and info.get('current_price'):
                    fallback_price = info['current_price']
                    fallback_atr = info.get('atr', 0)
                    if fallback_atr <= 0:
                        fallback_atr = fallback_price * 0.01 # 默认 1% 波动
                    
                    if signal == 'BUY':
                        stop_loss = fallback_price - (3.0 * fallback_atr)
                    else:
                        stop_loss = fallback_price + (3.0 * fallback_atr)
                    print(f"   【风控修正】缺失止损价，已自动生成兜底止损: {stop_loss:.4f}")

                order = trader.execute_trade(
                    symbol=target_symbol, 
                    side=signal, 
                    amount_usdt=trade_amount, 
                    leverage=leverage,
                    stop_loss=stop_loss,
                    take_profit=take_profit
                )
                
                if order:
                    # 记录持仓状态
                    # 注意：这里需要获取真实的成交均价和数量，这里简化处理
                    orig_qty = float(order.get('origQty', 0) or 0)
                    
                    state_manager.set_position(
                        symbol=target_symbol, 
                        side=signal, 
                        entry_price=info['current_price'],
                        quantity=orig_qty
                    )
                    print(">>> 开仓成功，状态已保存。等待 15秒 让交易所同步状态...")
                    time.sleep(15) # [修复] 必须等待，防止 API 延迟导致重复下单
            else:
                print(f"   【策略】趋势策略暂不推荐交易 {target_symbol}，进入冷却（10分钟）")
                state_manager.set_cooldown(target_symbol)
                print("无信号，继续观察...")

        except KeyboardInterrupt:
            print("\n>>> 用户手动停止")
            sys.exit(0)
        except Exception as e:
            print(f"\n【异常】发生错误: {str(e)}")
            traceback.print_exc()
            print(">>> 系统将自动重试...")
        
        # 每轮间隔
        print(">>> 本轮结束，立即开始下一轮...")
        time.sleep(5) # [修复] 增加最小轮询间隔，防止死循环刷单

if __name__ == "__main__":
    run_bot()
