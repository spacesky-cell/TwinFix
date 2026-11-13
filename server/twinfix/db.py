"""数据库会话与事务。

`Store.tx()` 是唯一的写入口：事务内产生的事件在提交成功后才发布，
保证前端看到的事件一定已经落库（SSE 断线重连时再从库里补齐）。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from twinfix.events import EventPublisher


def use_selector_loop_on_windows() -> None:
    """psycopg 异步模式在 Windows 上不支持 ProactorEventLoop。"""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined,unused-ignore]


def selector_loop() -> asyncio.AbstractEventLoop:
    """uvicorn 自定义事件循环：`--loop twinfix.db:selector_loop`。

    uvicorn ≥0.36 在 Windows 上默认 ProactorEventLoop，且不理会全局 policy。
    """
    return asyncio.SelectorEventLoop()


def make_engine(url: str, *, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(url, pool_size=pool_size, max_overflow=10, pool_pre_ping=True)


@dataclass
class Tx:
    session: AsyncSession
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def strip_nul(value: Any) -> Any:
    """PostgreSQL 的 text/jsonb 不接受 NUL（\\x00）。LLM 输出、二进制日志都可能夹带。"""
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_nul(v) for v in value]
    return value


def _strip_nul_before_flush(session: Session, _ctx: Any, _instances: Any) -> None:
    for obj in (*session.new, *session.dirty):
        for attr in inspect(obj).mapper.column_attrs:
            current = getattr(obj, attr.key, None)
            cleaned = strip_nul(current)
            if cleaned is not current and cleaned != current:
                setattr(obj, attr.key, cleaned)


class _CleanSession(Session):
    """所有写入都经过它：统一在 flush 前清理 NUL，而不是在每个写入点各自处理。"""


sa_event.listen(_CleanSession, "before_flush", _strip_nul_before_flush)


class Store:
    def __init__(self, engine: AsyncEngine, publisher: EventPublisher) -> None:
        self.engine = engine
        self.sessions = async_sessionmaker(engine, expire_on_commit=False, sync_session_class=_CleanSession)
        self.publisher = publisher

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[Tx]:
        async with self.sessions() as session, session.begin():
            tx = Tx(session)
            yield tx
        for event in tx.events:
            await self.publisher.publish(event)

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()
