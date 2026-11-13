"""事件发布。Redis Pub/Sub 只负责低延迟推送；事件的权威来源是 incident_event 表。"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from redis.asyncio import Redis

log = logging.getLogger(__name__)

GLOBAL_CHANNEL = "twinfix:events"


def incident_channel(incident_id: str) -> str:
    return f"twinfix:events:{incident_id}"


class EventPublisher(Protocol):
    async def publish(self, event: dict[str, Any]) -> None: ...


class NullPublisher:
    async def publish(self, event: dict[str, Any]) -> None:
        return None


class MemoryPublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class RedisPublisher:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def publish(self, event: dict[str, Any]) -> None:
        data = json.dumps(event, default=str, ensure_ascii=False)
        try:
            await self.redis.publish(incident_channel(event["incident_id"]), data)
            await self.redis.publish(GLOBAL_CHANNEL, data)
        except Exception:
            # 推送失败不影响业务：SSE 会按 Last-Event-ID 从数据库补齐
            log.warning("event publish failed", exc_info=True)
