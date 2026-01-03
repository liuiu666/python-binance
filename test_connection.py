from trade import TradeExecutor
from loguru import logger
import sys

try:
    logger.info("开始连接测试...")
    executor = TradeExecutor()
    executor.connect()
    # Check if client is actually connected and working
    server_time = executor.client.get_server_time()
    logger.info(f"测试成功! 服务器时间: {server_time['serverTime']}")
except Exception as e:
    logger.error(f"测试失败: {e}")
    sys.exit(1)
