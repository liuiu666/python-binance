
import time
from strategy.scanner import MarketScanner
from strategy.analysis import MarketAnalyzer
from utils.logger import logger

class EntryManager:
    def __init__(self, client, state_manager, ai_strategy, trader, risk_manager, scanner=None):
        self.client = client
        self.state_manager = state_manager
        self.ai_strategy = ai_strategy
        self.trader = trader
        self.risk_manager = risk_manager
        self.scanner = scanner or MarketScanner(client)
        self.analyzer = MarketAnalyzer()

    def scan_and_trade(self):
        """
        扫描市场并执行交易 (Step 2-5)
        """
        # 1. 扫描筛选
        tickers = self._scan_candidates()
        logger.info(f"   【扫描结果】当前关注列表: {tickers}")
        logger.info(f"   正在分析趋势与资金流...")

        target_symbol = None
        target_trend_bias = None

        # 2. 快速过滤 (1m Trend & CMF)
        for sym in tickers:
            if self.state_manager.is_in_cooldown(sym):
                continue
                
            df_1m = self.client.get_klines(sym, '1m', limit=100)
            if df_1m is None: continue
            
            df_analyzed = self.analyzer.calculate_indicators(df_1m)
            if df_analyzed is None: continue
            
            # 资金流初步过滤
            cmf = df_analyzed.iloc[-1].get('CMF', 0)
            net_flow_ma = df_analyzed.iloc[-1].get('Net_Flow_MA5', 0)
            
            # [强化] 增加过滤条件，必须 CMF 和 资金流 同向且强劲
            # 多头：CMF > 0.05 且 资金流 > 0
            # 空头：CMF < -0.05 且 资金流 < 0
            
            trend_bias = self.analyzer.get_trend_bias(df_analyzed)
            if trend_bias:
                # 顺势检查 + 强资金流确认
                if trend_bias == 'BUY_ONLY':
                    if cmf < 0.05 or net_flow_ma < 0: continue
                if trend_bias == 'SELL_ONLY':
                    if cmf > -0.05 or net_flow_ma > 0: continue
                
                target_symbol = sym
                target_trend_bias = trend_bias
                logger.info(f"   => 锁定目标: {target_symbol} ({target_trend_bias}, CMF:{cmf:.2f}, FlowMA:{net_flow_ma:.2f})")
                break
            else:
                logger.info(f"   [跳过] {sym} 趋势不明确")

        if not target_symbol:
            logger.info("   没有发现合适的趋势币种，休息 30秒...")
            time.sleep(30)
            return

        # 3. 深入分析 & 交易
        self._analyze_and_execute(target_symbol, target_trend_bias)

    def _scan_candidates(self):
        try:
            scanned_list = self.scanner.scan_market(top_n=5, min_volume=50000000)
        except Exception as e:
            logger.error(f"   [扫描] 扫描失败: {e}")
            scanned_list = None
            
        tickers = []
        if scanned_list is not None and not scanned_list.empty:
            tickers = scanned_list['symbol'].tolist()
            
        if 'ACUUSDT' not in tickers:
            tickers.insert(0, 'ACUUSDT')
            
        return tickers

    def _analyze_and_execute(self, symbol, trend_bias):
        logger.info(f"   正在深入分析 {symbol} ...")
        
        # 获取多周期数据
        df_1m = self.client.get_klines(symbol, '1m', limit=220)
        df_5m = self.client.get_klines(symbol, '5m', limit=200)
        df_1h = self.client.get_klines(symbol, '1h', limit=100)
        
        if df_1m is None or df_5m is None or df_1h is None: return
        
        df_1m_an = self.analyzer.calculate_indicators(df_1m)
        df_5m_an = self.analyzer.calculate_indicators(df_5m)
        df_1h_an = self.analyzer.calculate_indicators(df_1h)
        
        if df_1m_an is None: return

        # 再次确认趋势
        current_trend = trend_bias or self.analyzer.get_trend_bias(df_1m_an)
        if not current_trend:
            logger.info(f"   【策略】趋势不明确，跳过 {symbol}")
            self.state_manager.set_cooldown(symbol)
            return

        # AI 分析
        signal, info = self.ai_strategy.analyze(df_1m_an, symbol=symbol, trend_bias=current_trend, df_larger=df_5m_an, df_1h=df_1h_an)
        
        # 规则回退逻辑
        if not signal:
            ai_stop_loss = info.get('stop_loss')
            signal, info = self.analyzer.check_trend_following(df_1m_an, trend_bias=current_trend)
            if signal and ai_stop_loss:
                info['stop_loss'] = ai_stop_loss
                logger.info(f"   【策略融合】使用规则信号 + AI 建议止损")
        
        if signal:
            self._execute_signal(symbol, signal, info, df_1m_an)
        else:
            logger.info(f"   【策略】暂不推荐交易 {symbol}，进入冷却")
            self.state_manager.set_cooldown(symbol)

    def _execute_signal(self, symbol, signal, info, df_1m):
        signal_text = "做多" if signal == 'BUY' else "做空"
        logger.info(f"!!! 趋势信号触发: {signal_text} !!!")
        
        leverage = self.analyzer.suggest_leverage(df_1m)
        logger.info(f"   【风控】建议杠杆: {leverage}x")
        
        current_price = info.get('current_price')
        stop_loss = info.get('stop_loss')
        take_profit = info.get('take_profit')
        atr = info.get('atr', 0)
        
        # 兜底止损
        if not stop_loss and current_price:
            stop_loss = self.risk_manager.get_fallback_stop_loss(signal, current_price, atr)
            logger.warning(f"   【风控修正】自动生成宽幅兜底止损: {stop_loss:.4f}")
            
        # 计算仓位
        amount_usdt, risk_val = self.risk_manager.calculate_position_size(symbol, leverage, stop_loss, current_price)
        logger.info(f"   【风控计算】风险额: {risk_val:.2f}, 建议仓位: {amount_usdt:.2f}")

        # 执行交易
        order = self.trader.execute_trade(
            symbol=symbol,
            side=signal,
            amount_usdt=amount_usdt,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit=take_profit
        )
        
        if order:
            orig_qty = float(order.get('origQty', 0) or 0)
            self.state_manager.set_position(
                symbol=symbol,
                side=signal,
                entry_price=current_price,
                quantity=orig_qty
            )
            logger.info(">>> 开仓成功，等待 15秒 同步...")
            time.sleep(15)
