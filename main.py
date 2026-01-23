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
    print("=== 量化自动交易系统启动 (Daemon Mode + AI) ===")
    print(">>> 正在加载 Skills...")
    
    # 初始化模块
    client = BinanceClient()
    scanner = MarketScanner()
    analyzer = MarketAnalyzer()
    sentiment = SentimentAnalyzer()
    ai_strategy = AIStrategy()
    trader = TradeExecutor()
    state_manager = StateManager()
    
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
                    print(f">>> [System] 检测到 {local_pos['symbol']} 仓位已平 (可能是触发止盈止损)，清除本地状态")
                    state_manager.clear_position()
                    local_pos = None
            
            # 0.5 每日风控检查
            balance_info = client.get_balance()
            if balance_info:
                is_safe_daily, reason_daily = state_manager.check_daily_risk(balance_info['总权益'])
                if not is_safe_daily:
                     print(f"[每日风控] {reason_daily} -> 今日停止开新仓")
                     if len(real_positions) == 0:
                         time.sleep(3600) # 休息1小时
                         continue

            # 再次检查是否有真实持仓 (强制单向持仓限制)
            if len(real_positions) > 0:
                print(f">>> 当前持有 {len(real_positions)} 个仓位:")
                for p in real_positions:
                    symbol = p['symbol']
                    print(f"   {symbol} ({p['side']}): 数量 {p['amount']}, 浮盈亏 {p['unrealized_pnl']} U")
                    
                    # 持仓巡检：AI 评估 + 移动止损
                    # 获取 K 线
                    df = client.get_klines(symbol, '1h', limit=100)
                    if df is not None:
                        df_analyzed = analyzer.calculate_indicators(df)
                        
                        # [新增] 移动止损检查
                        current_atr = df_analyzed.iloc[-1].get('ATR', 0)
                        current_price = df_analyzed.iloc[-1]['收盘价']
                        trader.check_trailing_stop(p, current_price, atr=current_atr)
                        
                        # AI 评估
                        action, reason = ai_strategy.audit_position(p, df_analyzed)
                        
                        if action == 'CLOSE':
                            print(f">>> [AI 建议] 对 {symbol} 执行平仓操作，理由: {reason}")
                            success = trader.close_position(symbol)
                            if success:
                                state_manager.clear_position(symbol=symbol, pnl=p['unrealized_pnl'])
                                print(f"   {symbol} 已平仓")
                
                print(">>> [策略限制] 持仓中，暂停开新仓")
                time.sleep(60)
                continue

            # 1. 大盘熔断检测
            is_safe, reason = sentiment.check_market_sentiment()
            if not is_safe:
                print(f"[熔断] {reason} -> 暂停交易")
                time.sleep(300) # 暴跌时多睡会儿 (5分钟)
                continue

            # 2. 选币
            top_coins = scanner.scan_market(min_volume=10000000, max_spread=0.005, top_n=10) # 扩大筛选范围，应对冷却过滤
            target_symbol = None
            
            if top_coins is not None and not top_coins.empty:
                for _, row in top_coins.iterrows():
                    sym = row['symbol']
                    if not state_manager.is_in_cooldown(sym):
                        target_symbol = sym
                        break
                    else:
                        print(f"   [冷却] {sym} 刚交易过，跳过")
            
            if not target_symbol:
                print("未找到合适目标 (或都在冷却中)，休息 60秒...")
                time.sleep(60)
                continue
                
            print(f">> 锁定目标: {target_symbol}")

            # 3. 分析
            df = client.get_klines(target_symbol, '1h', limit=100)
            if df is None:
                time.sleep(10)
                continue
                
            df_analyzed = analyzer.calculate_indicators(df)
            
            # 4. 策略 (AI 决策)
            # signal, info = analyzer.check_breakout_strategy(df_analyzed)
            signal, info = ai_strategy.analyze(df_analyzed, target_symbol)
            
            if signal:
                print(f"!!! 信号触发: {signal} !!!")
                
                # 5. 交易
                # 动态仓位计算
                balance_info = client.get_balance()
                total_equity = balance_info.get('总权益', 0) if balance_info else 0
                
                trade_amount = 20 # 默认最小额
                leverage = 5      # 默认 5倍杠杆
                
                stop_loss = info.get('stop_loss')
                current_price = info.get('current_price')
                
                if total_equity > 0 and stop_loss and current_price:
                    # 风险模型: 单笔亏损不超过总权益的 2%
                    risk_per_trade = total_equity * 0.02 
                    
                    price_diff = abs(current_price - stop_loss)
                    if price_diff > 0:
                        # 仓位价值 = (风险金额 / 止损价差) * 当前价格
                        position_value = (risk_per_trade / price_diff) * current_price
                        
                        # 限制最大仓位为总权益的 50% (防止过度杠杆)
                        max_position_value = total_equity * 0.5 * leverage 
                        trade_amount = min(position_value, max_position_value)
                        
                        # 再次检查最小交易额 (比如 10U)
                        trade_amount = max(trade_amount, 10.0)
                        
                        print(f"   [风控计算] 风险额: {risk_per_trade:.2f}, 止损幅: {price_diff/current_price*100:.2f}%, 建议仓位: {trade_amount:.2f}")
                
                # 获取策略建议的止盈止损
                take_profit = info.get('take_profit')
                take_profit = info.get('take_profit')
                
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
                    state_manager.set_position(
                        symbol=target_symbol, 
                        side=signal, 
                        entry_price=info['current_price'],
                        quantity=order.get('origQty', 0) # 假设
                    )
                    print(">>> 开仓成功，状态已保存")
            else:
                print("无信号，继续观察...")

        except KeyboardInterrupt:
            print("\n>>> 用户手动停止")
            sys.exit(0)
        except Exception as e:
            print(f"\n[Error] 发生异常: {str(e)}")
            traceback.print_exc()
            print(">>> 系统将自动重试...")
        
        # 每轮间隔
        print(">>> 本轮结束，休眠 60秒...")
        time.sleep(60)

if __name__ == "__main__":
    run_bot()
