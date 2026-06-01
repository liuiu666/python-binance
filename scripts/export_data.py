"""
从 ClickHouse 导出数据到 data/ 目录
用法: python scripts/export_data.py [--symbol BTCUSDT] [--interval 1m] [--days 7]
"""

import sys
import os
import csv
import argparse
from datetime import datetime, timezone
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import clickhouse_connect
from common.config import settings


def export_klines(symbol: str, interval: str, days: int, output_dir: Path):
    """导出K线数据到CSV"""
    client = clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
    )

    sql = """
        SELECT 
            symbol,
            open_time,
            close_time,
            interval,
            open_price,
            high_price,
            low_price,
            close_price,
            volume,
            quote_volume,
            trades_count,
            taker_buy_volume
        FROM klines
        WHERE symbol = %(symbol)s 
          AND interval = %(interval)s
          AND open_time >= now() - INTERVAL %(days)s DAY
        ORDER BY open_time ASC
    """

    result = client.query(sql, parameters={
        "symbol": symbol,
        "interval": interval,
        "days": days,
    })

    if not result.result_rows:
        print(f"没有找到 {symbol} {interval} 最近{days}天的数据")
        return

    # 写入CSV
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = output_dir / f"klines_{symbol}_{interval}_{days}d.csv"

    columns = ["symbol", "open_time", "close_time", "interval",
               "open_price", "high_price", "low_price", "close_price",
               "volume", "quote_volume", "trades_count", "taker_buy_volume"]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in result.result_rows:
            writer.writerow(row)

    print(f"✅ K线数据已导出: {filename} ({len(result.result_rows)} 条)")


def export_agg_trades(symbol: str, days: int, output_dir: Path):
    """导出成交明细到CSV"""
    client = clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
    )

    sql = """
        SELECT 
            symbol, agg_trade_id, price, quantity,
            first_trade_id, last_trade_id, timestamp, is_buyer_maker
        FROM agg_trades
        WHERE symbol = %(symbol)s
          AND timestamp >= now() - INTERVAL %(days)s DAY
        ORDER BY timestamp ASC
        LIMIT 1000000
    """

    result = client.query(sql, parameters={
        "symbol": symbol,
        "days": days,
    })

    if not result.result_rows:
        print(f"没有找到 {symbol} 最近{days}天的成交明细")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    filename = output_dir / f"agg_trades_{symbol}_{days}d.csv"

    columns = ["symbol", "agg_trade_id", "price", "quantity",
               "first_trade_id", "last_trade_id", "timestamp", "is_buyer_maker"]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in result.result_rows:
            writer.writerow(row)

    print(f"✅ 成交明细已导出: {filename} ({len(result.result_rows)} 条)")


def main():
    parser = argparse.ArgumentParser(description="从 ClickHouse 导出数据到 data/ 目录")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对 (默认 BTCUSDT)")
    parser.add_argument("--interval", default="1m", help="K线周期 (默认 1m)")
    parser.add_argument("--days", type=int, default=7, help="导出最近N天的数据 (默认 7)")
    parser.add_argument("--type", choices=["klines", "trades", "all"], default="all",
                        help="导出类型: klines=K线, trades=成交明细, all=全部 (默认 all)")
    args = parser.parse_args()

    output_dir = PROJECT_ROOT / "data"

    print(f"📊 从 ClickHouse 导出数据")
    print(f"   交易对: {args.symbol}")
    print(f"   K线周期: {args.interval}")
    print(f"   时间范围: 最近 {args.days} 天")
    print(f"   输出目录: {output_dir}")
    print()

    if args.type in ("klines", "all"):
        export_klines(args.symbol, args.interval, args.days, output_dir)

    if args.type in ("trades", "all"):
        export_agg_trades(args.symbol, args.days, output_dir)

    print("\n🎉 导出完成！")


if __name__ == "__main__":
    main()
