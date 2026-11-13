"""RabbitMQ 拓扑与调度（docs/04-interfaces.md §6）。

  incidents.step        主队列，消息体 {incident_id, kind, checkpoint?, resume?}
  incidents.step.retry  延迟队列：消息带 per-message TTL，过期后经死信交换机回到主队列
  incidents.step.dlq    超过重试次数的死信，人工排查

消息持久化 + publisher confirm + 手动 ack；处理失败不丢消息。
"""

from __future__ import annotations

import json
from typing import Any

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection

EXCHANGE = "twinfix"
DLX = "twinfix.dlx"
STEP_Q = "incidents.step"
RETRY_Q = "incidents.step.retry"
DLQ = "incidents.step.dlq"


async def declare(channel: AbstractChannel) -> AbstractExchange:
    exchange = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    dlx = await channel.declare_exchange(DLX, aio_pika.ExchangeType.DIRECT, durable=True)

    step = await channel.declare_queue(STEP_Q, durable=True, arguments={"x-queue-type": "quorum"})
    await step.bind(exchange, routing_key=STEP_Q)

    # 重试队列无消费者：消息 TTL 到期 → 死信到 DLX(routing_key=incidents.step) → 回到主队列
    retry = await channel.declare_queue(
        RETRY_Q,
        durable=True,
        arguments={"x-dead-letter-exchange": DLX, "x-dead-letter-routing-key": STEP_Q},
    )
    await retry.bind(exchange, routing_key=RETRY_Q)
    await step.bind(dlx, routing_key=STEP_Q)

    dlq = await channel.declare_queue(DLQ, durable=True)
    await dlq.bind(exchange, routing_key=DLQ)
    return exchange


class RabbitDispatcher:
    def __init__(self, connection: AbstractRobustConnection) -> None:
        self.connection = connection
        self._channel: AbstractChannel | None = None
        self._exchange: AbstractExchange | None = None

    @classmethod
    async def connect(cls, url: str) -> RabbitDispatcher:
        conn = await aio_pika.connect_robust(url)
        self = cls(conn)
        self._channel = await conn.channel(publisher_confirms=True)
        self._exchange = await declare(self._channel)
        return self

    async def publish(
        self, body: dict[str, Any], *, routing_key: str = STEP_Q, delay_ms: int | None = None, retries: int = 0
    ) -> None:
        assert self._exchange is not None
        message = aio_pika.Message(
            json.dumps(body, ensure_ascii=False).encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
            headers={"x-retries": retries},
            expiration=delay_ms / 1000 if delay_ms else None,
        )
        await self._exchange.publish(message, routing_key=RETRY_Q if delay_ms else routing_key)

    async def advance(self, incident_id: str) -> None:
        await self.publish({"incident_id": incident_id, "kind": "advance"})

    async def resume(self, incident_id: str, decision: dict[str, Any]) -> None:
        await self.publish({"incident_id": incident_id, "kind": "resume", "resume": decision})

    async def close(self) -> None:
        await self.connection.close()
