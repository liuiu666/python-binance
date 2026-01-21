from loguru import logger
import pandas as pd
import config
from trade import TradeExecutor
import os

def download_data():
    logger.info(f"开始下载 1分钟 历史数据 | 交易对: {config.SYMBOL}")
    
    executor = TradeExecutor()
    executor.connect()
    
    # 下载 1m 数据 (Binance 单次最多 1500)
    # 如果需要更多，需要循环下载，这里先下载最近的 1500 条
    limit = 1500
    interval = '1m'
    
    logger.info(f"正在获取最近 {limit} 条 {interval} K线数据...")
    klines = executor.get_klines(symbol=config.SYMBOL, interval=interval, limit=limit)
    
    if not klines:
        logger.error("数据获取失败")
        return

    # 转换为 DataFrame
    columns = [
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
    ]
    df = pd.DataFrame(klines, columns=columns)
    
    # 格式化数据
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
    
    # 保存到 CSV
    filename = f"{config.SYMBOL}_{interval}.csv"
    file_path = os.path.join(os.getcwd(), filename)
    df.to_csv(file_path, index=False)
    
    logger.success(f"数据已保存到: {file_path}")
    logger.info(f"数据范围: {df['timestamp'].iloc[0]} - {df['timestamp'].iloc[-1]}")
    logger.info(f"数据行数: {len(df)}")

if __name__ == "__main__":
    download_data()
