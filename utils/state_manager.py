import json
import os
import time
from datetime import datetime

class StateManager:
    def __init__(self, filename='position.json'):
        self.filename = filename
        self.state = self._load_state()
        self._init_daily_stats()

    def _load_state(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def _init_daily_stats(self):
        """初始化每日统计数据"""
        today = datetime.utcnow().strftime('%Y-%m-%d')
        if 'daily_stats' not in self.state or self.state['daily_stats']['date'] != today:
            self.state['daily_stats'] = {
                'date': today,
                'initial_balance': 0.0, # 需要在主循环中更新
                'realized_pnl': 0.0,
                'trade_count': 0
            }
            # 清理过期的冷却记录
            self.state['cooldowns'] = {}
            self.save_state()

    def save_state(self):
        with open(self.filename, 'w') as f:
            json.dump(self.state, f, indent=4)

    def set_position(self, symbol, side, entry_price, quantity):
        self.state['current_position'] = {
            'symbol': symbol,
            'side': side,
            'entry_price': entry_price,
            'quantity': quantity
        }
        self.save_state()

    def clear_position(self, symbol=None, pnl=0.0):
        """
        平仓时调用
        :param symbol: 交易对
        :param pnl: 本次交易盈亏 (用于统计)
        """
        self.state['current_position'] = None
        
        # 更新冷却时间
        if symbol:
            if 'cooldowns' not in self.state:
                self.state['cooldowns'] = {}
            self.state['cooldowns'][symbol] = time.time()
            
        # 更新每日统计
        self._init_daily_stats() # 检查日期变更
        self.state['daily_stats']['realized_pnl'] += pnl
        self.state['daily_stats']['trade_count'] += 1
        
        self.save_state()

    def get_position(self):
        return self.state.get('current_position')
        
    def is_in_cooldown(self, symbol, cooldown_seconds=3600):
        """检查是否在冷却期"""
        if 'cooldowns' not in self.state:
            return False
            
        last_time = self.state['cooldowns'].get(symbol, 0)
        if time.time() - last_time < cooldown_seconds:
            return True
        return False
        
    def check_daily_risk(self, current_balance, max_daily_loss_pct=0.05):
        """
        检查是否触发每日风控
        :return: (is_safe, reason)
        """
        self._init_daily_stats()
        
        # 如果还没记录初始余额，记录一下
        if self.state['daily_stats']['initial_balance'] <= 0:
            self.state['daily_stats']['initial_balance'] = current_balance
            self.save_state()
            return True, "Initialized"
            
        init_bal = self.state['daily_stats']['initial_balance']
        current_pnl = current_balance - init_bal
        
        loss_pct = -current_pnl / init_bal
        
        if loss_pct > max_daily_loss_pct:
            return False, f"当日亏损 {loss_pct*100:.2f}% 超过阈值 {max_daily_loss_pct*100}%"
            
        return True, "Safe"
