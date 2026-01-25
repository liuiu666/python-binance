
class RiskManager:
    def __init__(self, client):
        self.client = client

    def calculate_position_size(self, symbol, leverage, stop_loss, current_price, risk_pct=0.06):
        """
        计算仓位大小 (基于风险百分比)
        """
        try:
            balance_info = self.client.get_balance()
            total_equity = balance_info.get('总权益', 0) if balance_info else 0
            
            if total_equity <= 0:
                return 0.0, 0.0

            trade_amount = 20.0 # 默认最小额

            if stop_loss and current_price > 0:
                # 风险模型: 单笔亏损不超过总权益的 N%
                risk_per_trade = total_equity * risk_pct
                
                price_diff = abs(current_price - stop_loss)
                if price_diff > 0:
                    # 仓位价值 = (风险金额 / 止损价差) * 当前价格
                    position_value = (risk_per_trade / price_diff) * current_price
                    
                    # 限制最大仓位为总权益的 95% * 杠杆 (几乎满仓)
                    max_position_value = total_equity * 0.95 * leverage 
                    trade_amount = min(position_value, max_position_value)
                    
                    # 再次检查最小交易额
                    trade_amount = max(trade_amount, 10.0)
                    
                    return trade_amount, risk_per_trade
            
            return trade_amount, 0.0
            
        except Exception as e:
            print(f"   [风控] 计算仓位大小失败: {e}")
            return 20.0, 0.0

    def get_fallback_stop_loss(self, side, current_price, atr):
        """
        计算兜底止损 (5.0 ATR)
        """
        fallback_multiplier = 5.0
        if atr <= 0:
            atr = current_price * 0.01 # 默认 1%

        if side == 'BUY':
            stop_loss = current_price - (fallback_multiplier * atr)
        else:
            stop_loss = current_price + (fallback_multiplier * atr)
            
        return stop_loss
