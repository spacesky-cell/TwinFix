"""FastAPI 应用工厂。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from twinfix.api.metrics import router as metrics_router
from twinfix.api.routes import router
from twinfix.api.webhooks import router as webhook_router
from twinfix.arena.evalruns import EvalRunManager
from twinfix.config import Settings, get_settings
from twinfix.control import RedisControlPlane
from twinfix.db import Store, make_engine, use_selector_loop_on_windows
from twinfix.events import RedisPublisher
from twinfix.services import IncidentService
from twinfix.worker.mq import RabbitDispatcher

log = logging.getLogger("twinfix.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    use_selector_loop_on_windows()
    if not settings.auth_disabled and len(settings.jwt_secret.get_secret_value()) < 32:
        raise RuntimeError("TWINFIX_JWT_SECRET must be at least 32 bytes unless TWINFIX_AUTH_DISABLED=true")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        redis = Redis.from_url(settings.redis_url)
        store = Store(make_engine(settings.database_url), RedisPublisher(redis))
        dispatcher = await RabbitDispatcher.connect(settings.amqp_url)
        app.state.settings = settings
        app.state.redis = redis
        app.state.store = store
        app.state.control = RedisControlPlane(redis)
        app.state.service = IncidentService(store, dispatcher)
        # 控制台发起的评测：子进程回连本 API，状态落在 data_dir/eval_runs
        app.state.eval_runs = EvalRunManager(settings)
        if settings.auth_disabled:
            log.warning("authentication is DISABLED (TWINFIX_AUTH_DISABLED=true); do not expose this instance")
        try:
            yield
        finally:
            await app.state.eval_runs.stop()
            adapters = getattr(app.state, "adapters", None)
            if adapters is not None:
                # 自检等接口按需装配的适配器可能持有 httpx / docker 客户端
                for target in [
                    *adapters.notifiers,
                    adapters.ci,
                    adapters.codehost,
                    adapters.sandbox,
                    adapters.twin,
                    adapters.observability,
                ]:
                    aclose = getattr(target, "aclose", None)
                    if aclose is not None:
                        try:
                            await aclose()
                        except Exception:
                            log.warning("adapter %r aclose failed", type(target).__name__, exc_info=True)
            await dispatcher.close()
            await store.close()
            await redis.aclose()

    app = FastAPI(title="TwinFix", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "PUT"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    )
    app.include_router(router)
    app.include_router(webhook_router)
    app.include_router(metrics_router)
    return app
