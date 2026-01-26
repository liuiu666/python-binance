
import sys
import logging
from pathlib import Path
from pprint import pprint

# Setup path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_skills import Settings, create_client, FuturesTrader

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    logger.info("Testing Data Fetching...")
    
    settings = Settings.load(ROOT)
    client = create_client(settings)
    trader = FuturesTrader(client)
    
    symbol = "BTCUSDT"
    
    # Test Ticker
    logger.info(f"Fetching Ticker for {symbol}...")
    ticker = trader.get_ticker(symbol)
    logger.info(f"Ticker Result: {ticker}")
    
    # Test Klines
    logger.info(f"Fetching Klines for {symbol}...")
    klines = trader.get_klines(symbol, interval="1h", limit=5)
    logger.info(f"Klines Count: {len(klines)}")
    if klines:
        logger.info(f"First Kline: {klines[0]}")
        
    # Test Smart Analyzer Import
    try:
        from analysis.smart_analyzer import SmartAnalyzer
        logger.info(f"SmartAnalyzer imported successfully: {SmartAnalyzer}")
    except ImportError as e:
        logger.error(f"Failed to import SmartAnalyzer (direct): {e}")
        try:
            from src.analysis.smart_analyzer import SmartAnalyzer
            logger.info(f"SmartAnalyzer imported successfully (src): {SmartAnalyzer}")
        except ImportError as e2:
            logger.error(f"Failed to import SmartAnalyzer (src): {e2}")

if __name__ == "__main__":
    main()
