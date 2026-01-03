from trade import TradeExecutor
from strategy import Strategy
from loguru import logger
import sys

try:
    # 1. 连接
    executor = TradeExecutor()
    executor.connect()
    
    # 2. 获取数据
    logger.info("正在获取 K 线数据...")
    klines = executor.get_klines(limit=30)
    
    if not klines:
        logger.error("未获取到数据")
        sys.exit(1)
        
    logger.info(f"成功获取 {len(klines)} 条 K 线数据")
    
    # 3. 测试策略计算
    strategy = Strategy()
    signal = strategy.check_signal(klines)
    logger.info(f"策略计算完成，当前信号: {signal}")
    
except Exception as e:
    logger.error(f"测试失败: {e}")
    sys.exit(1)
