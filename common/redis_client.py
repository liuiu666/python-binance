"""
Redis Streams 客户端封装
提供 XADD / XREADGROUP / XACK / XTRIM 等操作的异步封装
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis

from common.config import settings
from common.logger import get_logger

logger = get_logger(__name__)

# Redis Streams 命名规范
STREAM_MARKET = "market:{symbol}"       # 行情数据流 (K 线)
STREAM_DEPTH = "depth:{symbol}"         # 深度数据流
STREAM_TICKER = "ticker:{symbol}"       # 最优挂价数据流
STREAM_SIGNAL = "signal:trade"           # 交易信号流
STREAM_ORDER = "order:update"            # 订单状态更新流
STREAM_ACCOUNT = "account:updates"       # 账户事件流

# 消费者组命名
GROUP_COLLECTOR = "collector-group"
GROUP_STRATEGY = "strategy-group"
GROUP_EXECUTOR = "executor-group"
GROUP_API = "api-group"


class RedisClient:
    """
    Redis Streams 异步客户端
    使用连接池管理, 支持发布/订阅和流操作
    """

    def __init__(self) -> None:
        self._pool: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        """建立 Redis 连接池"""
        self._pool = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=20,
        )
        logger.info("redis.connected", url=settings.redis_url)

    async def close(self) -> None:
        """关闭连接池"""
        if self._pool:
            await self._pool.close()
            logger.info("redis.closed")

    @property
    def client(self) -> aioredis.Redis:
        """获取底层 Redis 客户端"""
        if self._pool is None:
            raise RuntimeError("Redis 未连接, 请先调用 connect()")
        return self._pool

    # ============================================================
    # Stream 操作
    # ============================================================

    async def xadd(
        self,
        stream: str,
        data: Dict[str, Any],
        maxlen: Optional[int] = None,
    ) -> str:
        """
        向 Stream 追加消息

        Args:
            stream: Stream 名称
            data: 消息数据 (自动 JSON 序列化嵌套值)
            maxlen: 最大长度, 超出后裁剪旧消息

        Returns:
            消息 ID
        """
        # Redis Stream 只接受字符串值, 嵌套结构需要 JSON 序列化
        flat_data: Dict[str, str] = {}
        for k, v in data.items():
            if isinstance(v, (dict, list, tuple)):
                flat_data[k] = json.dumps(v, ensure_ascii=False)
            else:
                flat_data[k] = str(v)

        msg_id = await self.client.xadd(
            stream, flat_data, maxlen=maxlen
        )
        return msg_id

    async def xreadgroup(
        self,
        group: str,
        consumer: str,
        streams: Dict[str, str],
        count: int = 10,
        block: int = 5000,
    ) -> List:
        """
        从消费者组读取消息

        Args:
            group: 消费者组名称
            consumer: 消费者名称
            streams: {stream_name: last_id} 字典, ">" 表示只读新消息
            count: 单次最大读取条数
            block: 阻塞等待毫秒数, 0 表示不阻塞

        Returns:
            [(stream_name, [(msg_id, {field: value}), ...]), ...]
        """
        stream_names = list(streams.keys())
        stream_ids = list(streams.values())
        return await self.client.xreadgroup(
            group, consumer, dict(zip(stream_names, stream_ids)),
            count=count, block=block,
        )

    async def xack(self, stream: str, group: str, *msg_ids: str) -> int:
        """确认消息已处理"""
        return await self.client.xack(stream, group, *msg_ids)

    async def xtrim(self, stream: str, maxlen: int, approximate: bool = True) -> int:
        """裁剪 Stream 到指定长度"""
        return await self.client.xtrim(stream, maxlen=maxlen, approximate=approximate)

    async def create_group(
        self, stream: str, group: str, id: str = "$"
    ) -> None:
        """
        创建消费者组

        Args:
            stream: Stream 名称
            group: 消费者组名称
            id: 起始 ID, "$" 表示只消费新消息, "0" 表示从头开始
        """
        try:
            await self.client.xgroup_create(stream, group, id)
            logger.info("redis.group_created", stream=stream, group=group)
        except aioredis.ResponseError as e:
            # 消费者组已存在是正常情况
            if "BUSYGROUP" in str(e):
                logger.debug("redis.group_exists", stream=stream, group=group)
            else:
                raise

    # ============================================================
    # 通用操作
    # ============================================================

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        """设置键值"""
        await self.client.set(key, value, ex=ex)

    async def get(self, key: str) -> Optional[str]:
        """获取键值"""
        return await self.client.get(key)

    async def sadd(self, key: str, *values: str) -> int:
        """向 SET 添加元素"""
        return await self.client.sadd(key, *values)

    async def sismember(self, key: str, value: str) -> bool:
        """检查元素是否在 SET 中"""
        return await self.client.sismember(key, value)

    async def publish(self, channel: str, message: str) -> int:
        """发布消息到频道"""
        return await self.client.publish(channel, message)


# 全局单例
redis_client = RedisClient()
