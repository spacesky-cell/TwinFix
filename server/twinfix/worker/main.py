"""Worker：消费 incidents.step，每条消息推进事故一步。

并发与幂等
- 同一事故在 Redis 上加锁（带 TTL + 续期），保证全局单活；拿不到锁就延迟重投。
- 推进后若图还能继续，就带上新的 checkpoint_id 发下一条消息；过期消息由引擎丢弃。
- 步骤失败（StepFailedError）走延迟重试，超过上限进 DLQ；急停时延迟重投直到解除。
- 巡检任务：定期扫描超时事故（工作状态超时 → 推进一次让守卫转人工；评审超时 → 恢复为 timeout）；
  以及到期的孪生环境（TTL 过期或创建卡死）→ 销毁容器并落 DESTROYED。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import uuid
from typing import Any

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from redis.asyncio import Redis

from twinfix.config import Settings, get_settings
from twinfix.control import RedisControlPlane
from twinfix.db import use_selector_loop_on_windows
from twinfix.domain.enums import IncidentState
from twinfix.events import RedisPublisher
from twinfix.logscan import LogScanner
from twinfix.orchestrator.steps import StepFailedError
from twinfix.otel import current_trace_id, reset_tracing, setup_tracing, span
from twinfix.runtime import Runtime, build_runtime
from twinfix.services import IncidentService
from twinfix.tracing import HaltedError
from twinfix.worker.mq import DLQ, STEP_Q, RabbitDispatcher

log = logging.getLogger("twinfix.worker")

_RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""
_EXTEND_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end
"""


class IncidentLock:
    def __init__(self, redis: Redis, incident_id: str, ttl_s: int) -> None:
        self.redis = redis
        self.key = f"lock:incident:{incident_id}"
        self.token = uuid.uuid4().hex
        self.ttl_ms = ttl_s * 1000
        self._keepalive: asyncio.Task[None] | None = None

    async def acquire(self) -> bool:
        ok = await self.redis.set(self.key, self.token, nx=True, px=self.ttl_ms)
        if ok:
            self._keepalive = asyncio.create_task(self._extend_forever())
        return bool(ok)

    async def _extend_forever(self) -> None:
        while True:
            await asyncio.sleep(self.ttl_ms / 3000)
            await self.redis.eval(_EXTEND_LOCK, 1, self.key, self.token, str(self.ttl_ms))

    async def release(self) -> None:
        if self._keepalive:
            self._keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive
        await self.redis.eval(_RELEASE_LOCK, 1, self.key, self.token)


class Worker:
    def __init__(self, settings: Settings, rt: Runtime, redis: Redis, dispatcher: RabbitDispatcher) -> None:
        self.settings = settings
        self.rt = rt
        self.redis = redis
        self.dispatcher = dispatcher
        self.service = IncidentService(rt.store, dispatcher)
        self.log_scanner = LogScanner(rt.store, rt.adapters.observability, self.service, settings)

    async def handle(self, message: AbstractIncomingMessage) -> None:
        body: dict[str, Any] = json.loads(message.body)
        incident_id = body["incident_id"]
        raw_retries = (message.headers or {}).get("x-retries", 0)
        retries = raw_retries if isinstance(raw_retries, int) else 0
        lock = IncidentLock(self.redis, incident_id, self.settings.lock_ttl_s)
        if not await lock.acquire():
            # 同一事故正在被别的 worker 处理：稍后重投，不占用当前 worker
            await self.dispatcher.publish(body, delay_ms=2000, retries=retries)
            await message.ack()
            return
        # 持锁即说明上一个处理者已经不在了：它留下的 Run 不会再 finish，这里统一收尾
        try:
            stale = await self.rt.tracker.abandon_running(incident_id, reason="进程中断，运行被系统收尾")
            if stale:
                log.warning("abandoned %s orphaned run(s) for %s", stale, incident_id)
        except Exception:
            log.exception("abandoning orphaned runs failed for %s", incident_id)
        follow_up: dict[str, Any] | None = None
        try:
            follow_up = await self._process(body, retries)
            await message.ack()
        except Exception:
            log.exception("unexpected failure handling %s", body)
            await message.nack(requeue=False)
        finally:
            await lock.release()
        # 释放锁之后再派发下一步，否则下一条消息会撞上本 worker 尚未释放的锁而被延迟重投
        if follow_up is not None:
            await self.dispatcher.publish(follow_up)

    async def _process(self, body: dict[str, Any], retries: int) -> dict[str, Any] | None:
        incident_id = body["incident_id"]
        engine = self.rt.engine
        kind = "resume" if body.get("kind") == "resume" else "advance"
        with span(
            "worker.step",
            **{
                "twinfix.incident_id": incident_id,
                "twinfix.step.kind": kind,
                "twinfix.retries": retries,
            },
        ) as current:
            try:
                if kind == "resume":
                    result = await engine.advance(incident_id, resume=body["resume"])
                else:
                    result = await engine.advance(incident_id, expected_checkpoint=body.get("checkpoint"))
            except HaltedError:
                current.set_attribute("twinfix.step.result", "halted")
                log.info("halted; deferring %s", incident_id)
                await self.dispatcher.publish(body, delay_ms=self.settings.step_retry_delay_s * 1000, retries=retries)
                return None
            except StepFailedError as exc:
                current.set_attribute("twinfix.step.result", "failed")
                if retries + 1 >= self.settings.step_max_retries * 2:
                    log.error("giving up on %s after %s retries: %s", incident_id, retries, exc)
                    await self.dispatcher.publish({**body, "error": str(exc)}, routing_key=DLQ)
                    return None
                delay = self.settings.step_retry_delay_s * 1000 * (2 ** min(retries, 5))
                log.warning("step failed for %s (retry %s in %sms): %s", incident_id, retries + 1, delay, exc)
                # 重试时不带旧 checkpoint：失败的步骤没有推进 checkpoint，按当前状态重跑即可
                await self.dispatcher.publish(
                    {"incident_id": incident_id, "kind": "advance"}, delay_ms=delay, retries=retries + 1
                )
                return None

            log.info(
                "incident %s -> %s %s (trace %s)",
                incident_id,
                result.status,
                result.next_nodes,
                current_trace_id() or "-",
            )
            if result.should_continue:
                return {"incident_id": incident_id, "kind": "advance", "checkpoint": result.checkpoint_id}
            return None

    async def sweep_forever(self, interval_s: float = 30) -> None:
        while True:
            try:
                for incident_id, state in await self.rt.steps.pending_timeouts():
                    if state is IncidentState.AWAITING_REVIEW:
                        await self.service.expire_review(incident_id)
                    elif state is IncidentState.OBSERVING:
                        await self.service.wake_observation(incident_id)
                    else:
                        await self.dispatcher.advance(incident_id)
                # 孪生 TTL：事故没走到终态（停在 NEEDS_HUMAN 等人工）或创建途中进程崩了，到点回收
                for twin_id in await self.rt.steps.expired_twins():
                    try:
                        await self.rt.steps.reap_twin(twin_id)
                    except Exception:
                        # 单个孪生销毁失败（docker 忙、容器已不存在等）不能拖累其余超时事故的处理，
                        # 记录保持原状态，下一轮再试
                        log.exception("twin reap failed for %s", twin_id)
            except Exception:
                log.exception("sweep failed")
            await asyncio.sleep(interval_s)


async def run() -> None:
    settings = get_settings()
    # 链路追踪默认关闭；开启后每个 worker.step 是一条 trace 根，Agent / 工具 / LLM span 挂在下面
    setup_tracing(
        enabled=settings.otel_enabled,
        endpoint=settings.otel_endpoint,
        service_name=settings.otel_service_name,
        sample_ratio=settings.otel_sample_ratio,
    )
    redis = Redis.from_url(settings.redis_url)
    rt = await build_runtime(settings, publisher=RedisPublisher(redis), control=RedisControlPlane(redis))
    dispatcher = await RabbitDispatcher.connect(settings.amqp_url)
    worker = Worker(settings, rt, redis, dispatcher)

    consume_conn = await aio_pika.connect_robust(settings.amqp_url)
    channel = await consume_conn.channel()
    await channel.set_qos(prefetch_count=4)
    queue = await channel.get_queue(STEP_Q)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows 不支持 add_signal_handler
            loop.add_signal_handler(sig, stop.set)

    sweeper = asyncio.create_task(worker.sweep_forever())
    scanner: asyncio.Task[None] | None = None
    if settings.logscan_enabled:
        if not settings.elasticsearch_url:
            log.warning(
                "TWINFIX_LOGSCAN_ENABLED=true but TWINFIX_ELASTICSEARCH_URL is empty; scans will return nothing"
            )
        scanner = asyncio.create_task(worker.log_scanner.scan_forever())
        log.info("log scanner enabled (every %ss)", settings.logscan_interval_s)
    tag = await queue.consume(worker.handle)
    log.info("worker ready")
    try:
        await stop.wait()
    finally:
        await queue.cancel(tag)
        sweeper.cancel()
        if scanner is not None:
            scanner.cancel()
        await consume_conn.close()
        await dispatcher.close()
        await rt.close()
        await redis.aclose()
        reset_tracing()  # 刷出 BatchSpanProcessor 里尚未导出的 span


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    use_selector_loop_on_windows()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
