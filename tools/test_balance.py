
import sys
import logging
from pathlib import Path
from decimal import Decimal

# Setup path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client, FuturesTrader, LLMAdvisor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    logger.info("Testing Balance Fetching...")
    
    settings = Settings.load(ROOT)
    client = create_client(settings)
    trader = FuturesTrader(client)
    advisor = LLMAdvisor(trader)
    
    # 1. Check Raw Balance
    logger.info("Fetching USDT Balance from Trader...")
    balance = trader.get_usdt_balance()
    logger.info(f"Raw USDT Balance: {balance}")
    
    # 2. Check Dynamic Calculation
    logger.info("Calculating Default Amount (5% logic)...")
    default_amount = advisor._calc_default_usdt()
    logger.info(f"Calculated Default Amount: {default_amount}")
    
    if balance == 0:
        logger.warning("Balance is 0. Check API Key permissions or network.")
    elif balance < 400:
        logger.info(f"Balance {balance} < 400, so 5% ({balance * Decimal('0.05')}) is less than 20.")
        logger.info("System falls back to minimum 20 USDT.")
    else:
        logger.info(f"Balance {balance} >= 400, dynamic amount should be {balance * Decimal('0.05')}.")

if __name__ == "__main__":
    main()
