"""
从 Redis Stream 导出订单簿深度数据到 data/ 目录
用法: python scripts/export_depth.py [--symbol BTCUSDT]
"""

import sys
import csv
import json
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import redis
from common.config import settings


def export_depth(symbol: str):
    """从 Redis Stream 导出订单簿深度数据"""
    r = redis.from_url(settings.redis_url, decode_responses=True)

    stream_name = f"depth:{symbol.lower()}"
    # 读取全部消息
    messages = r.xrange(stream_name, "-", "+", count=5000)

    if not messages:
        print(f"❌ Redis Stream '{stream_name}' 中没有数据")
        print("   可能采集服务未运行，或数据已过期")
        return

    output_dir = PROJECT_ROOT / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = output_dir / f"depth_{symbol}.csv"

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        # 表头：时间戳 + 20档买/卖
        headers = ["recv_ts", "symbol", "last_update_id"]
        for i in range(1, 21):
            headers.extend([f"bid_{i}_price", f"bid_{i}_qty"])
        for i in range(1, 21):
            headers.extend([f"ask_{i}_price", f"ask_{i}_qty"])
        writer.writerow(headers)

        count = 0
        for msg_id, fields in messages:
            try:
                data = json.loads(fields.get("data", "{}"))
            except json.JSONDecodeError:
                continue

            bids = data.get("b", [])
            asks = data.get("a", [])

            if not bids and not asks:
                continue

            recv_ts = data.get("local_recv_ts", "")
            sym = data.get("s", symbol)
            update_id = data.get("u", "")

            row = [recv_ts, sym, update_id]
            for i in range(20):
                if i < len(bids):
                    row.extend([bids[i][0], bids[i][1]])
                else:
                    row.extend(["", ""])
            for i in range(20):
                if i < len(asks):
                    row.extend([asks[i][0], asks[i][1]])
                else:
                    row.extend(["", ""])

            writer.writerow(row)
            count += 1

    print(f"✅ 订单簿数据已导出: {filename} ({count} 条快照)")


def main():
    parser = argparse.ArgumentParser(description="从 Redis 导出订单簿深度数据")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对 (默认 BTCUSDT)")
    args = parser.parse_args()

    print(f"📊 从 Redis Stream 导出订单簿数据")
    print(f"   交易对: {args.symbol}")
    print()
    export_depth(args.symbol)


if __name__ == "__main__":
    main()
