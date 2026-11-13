"""SSE：先按 Last-Event-ID 从数据库补齐历史事件，再订阅 Redis 实时推送。

去重用"已发送 id 集合"而不是单一水位线：事件 id 来自序列，并发事务里较小的 id
可能晚提交，只看水位线会把它丢掉。心跳时回看水位线之前一小段窗口，兜住晚提交与推送失败。
前端按 id 排序展示。
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select

from twinfix.db import Store
from twinfix.domain.models import IncidentEvent
from twinfix.domain.repo import event_dict
from twinfix.events import GLOBAL_CHANNEL, incident_channel

HEARTBEAT_S = 15
LOOKBACK = 200  # 心跳补齐时回看的 id 窗口


class _Seen:
    def __init__(self, cap: int = 5000) -> None:
        self.ids: set[int] = set()
        self.order: deque[int] = deque()
        self.cap = cap
        self.high = 0

    def add(self, i: int) -> bool:
        if i in self.ids:
            return False
        self.ids.add(i)
        self.order.append(i)
        self.high = max(self.high, i)
        if len(self.order) > self.cap:
            self.ids.discard(self.order.popleft())
        return True


async def _backlog(store: Store, incident_id: str | None, after: int, limit: int = 1000) -> list[dict[str, Any]]:
    async with store.read() as s:
        q = select(IncidentEvent).where(IncidentEvent.id > after).order_by(IncidentEvent.id).limit(limit)
        if incident_id:
            q = q.where(IncidentEvent.incident_id == incident_id)
        return [event_dict(e) for e in (await s.scalars(q)).all()]


async def event_stream(
    store: Store, redis: Redis | None, incident_id: str | None, last_id: int
) -> AsyncGenerator[dict[str, Any]]:
    channel = incident_channel(incident_id) if incident_id else GLOBAL_CHANNEL
    pubsub = redis.pubsub() if redis is not None else None
    if pubsub is not None:
        await pubsub.subscribe(channel)  # 先订阅再补齐，避免两者之间的空窗丢事件
    seen = _Seen()
    seen.high = last_id

    async def catch_up(after: int) -> AsyncIterator[dict[str, Any]]:
        for ev in await _backlog(store, incident_id, after):
            if seen.add(ev["id"]):
                yield _sse(ev)

    try:
        async for item in catch_up(last_id):
            yield item
        while True:
            msg = None
            if pubsub is not None:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=HEARTBEAT_S)
            else:
                await asyncio.sleep(1)  # 无 Redis 时退化为轮询
            if msg is None:
                if pubsub is not None:
                    yield {"event": "ping", "data": "{}"}
                async for item in catch_up(max(last_id, seen.high - LOOKBACK)):
                    yield item
                continue
            ev = json.loads(msg["data"])
            if seen.add(ev["id"]):
                yield _sse(ev)
    finally:
        if pubsub is not None:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()  # type: ignore[no-untyped-call]


def _sse(ev: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(ev["id"]), "event": "incident", "data": json.dumps(ev, ensure_ascii=False, default=str)}
