
import time
import sys
import traceback
from handlers.binance_client import BinanceClient
from handlers.trader import TradeExecutor
from strategy.sentiment import SentimentAnalyzer
from strategy.ai_strategy import AIStrategy
from strategy.risk_manager import RiskManager
from strategy.position_manager import PositionManager
from strategy.entry_manager import EntryManager
from utils.state_manager import StateManager
from utils.logger import logger

class BotManager:
    def __init__(self):
        logger.info("=== 量化自动交易系统启动 ===")
        logger.info(">>> 正在加载模块...")
        
        # 1. 初始化客户端
        self.client = None
        while self.client is None:
            try:
                self.client = BinanceClient()
            except Exception:
                logger.error(">>>【系统】初始化失败，5秒后重试...")
                time.sleep(5)
                
        # 2. 初始化各个模块
        self.state_manager = StateManager()
        self.risk_manager = RiskManager(self.client)
        self.trader = TradeExecutor(client=self.client, state_manager=self.state_manager)
        self.sentiment = SentimentAnalyzer(client=self.client)
        self.ai_strategy = AIStrategy(client=self.client)
        
        # 3. 初始化策略管理器
        self.position_manager = PositionManager(
            client=self.client,
            state_manager=self.state_manager,
            ai_strategy=self.ai_strategy,
            trader=self.trader,
            risk_manager=self.risk_manager
        )
        
        self.entry_manager = EntryManager(
            client=self.client,
            state_manager=self.state_manager,
            ai_strategy=self.ai_strategy,
            trader=self.trader,
            risk_manager=self.risk_manager
        )

    def run(self):
        while True:
            try:
                logger.info(f"=== 开始新一轮扫描 ===")
                
                # 1. 持仓管理 (Step 0)
                # 如果持仓巡检返回 True (表示有持仓且限制开新仓)，则跳过后续步骤
                is_holding = self.position_manager.sync_and_audit_positions()
                if is_holding:
                    continue

                # 2. 市场熔断检测 (Step 1)
                is_safe, reason = self.sentiment.check_market_sentiment()
                if not is_safe:
                    logger.warning(f"【熔断】{reason} -> 暂停交易")
                    time.sleep(300)
                    continue

                # 3. 扫描与进场 (Step 2-5)
                self.entry_manager.scan_and_trade()

                # 每轮间隔
                logger.info(">>> 本轮结束，立即开始下一轮...")
                time.sleep(5)

            except KeyboardInterrupt:
                logger.info("\n>>> 用户手动停止")
                sys.exit(0)
            except Exception as e:
                logger.error(f"【异常】发生错误: {str(e)}")
                # traceback.print_exc() # Logger 会自动记录 stack trace 如果使用 exc_info=True，但这里简单记录即可
                logger.error(traceback.format_exc())
                logger.info(">>> 系统将自动重试...")
                time.sleep(5)

if __name__ == "__main__":
    bot = BotManager()
    bot.run()
