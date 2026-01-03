from trade import TradeExecutor
from loguru import logger
import sys

try:
    executor = TradeExecutor()
    executor.connect()
    info = executor.client.futures_exchange_info()
    symbols = [s['symbol'] for s in info['symbols']]
    
    target = "WETUSDT"
    if target in symbols:
        logger.info(f"找到交易对: {target}")
    else:
        logger.warning(f"未找到交易对: {target}")
        # 模糊匹配
        similar = [s for s in symbols if "WET" in s or "VET" in s or "WIF" in s]
        logger.info(f"可能的相似交易对: {similar}")
        
except Exception as e:
    logger.error(f"检查失败: {e}")
