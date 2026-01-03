from trade import TradeExecutor
from loguru import logger
import sys

try:
    executor = TradeExecutor()
    executor.connect()
    
    # 获取当前持仓
    logger.info("正在查询当前持仓...")
    position = executor.get_position()
    logger.info(f"当前持仓数量: {position}")
    
    # 注意：这里我们不进行实际下单测试，避免资金损失
    # 仅测试获取持仓接口是否正常
    
except Exception as e:
    logger.error(f"测试失败: {e}")
    sys.exit(1)
