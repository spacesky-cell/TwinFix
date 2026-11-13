"""测试夹具。

集成测试（标记 integration）需要 deploy/compose.dev.yml 中的 postgres；
使用独立库 twinfix_test，每个会话开始时重建 schema。没有数据库时自动跳过。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from twinfix.config import Settings
from twinfix.container import Adapters, build_adapters
from twinfix.control import MemoryControlPlane
from twinfix.db import use_selector_loop_on_windows
from twinfix.dispatch import InlineDispatcher
from twinfix.domain.models import Project
from twinfix.events import MemoryPublisher
from twinfix.ids import new_id
from twinfix.runtime import Runtime, build_runtime
from twinfix.services import IncidentService

use_selector_loop_on_windows()

# 测试不读开发者本机的 server/.env（里面可能是 llm/docker 等真实配置）
Settings.model_config["env_file"] = None

SERVER_DIR = Path(__file__).resolve().parents[1]
TEST_DB_URL = os.environ.get(
    "TWINFIX_TEST_DATABASE_URL", "postgresql+psycopg://twinfix:twinfix@127.0.0.1:5432/twinfix_test"
)


def _db_available() -> bool:
    try:
        with psycopg.connect(TEST_DB_URL.replace("+psycopg", ""), connect_timeout=3):
            return True
    except psycopg.OperationalError:
        return False


@pytest.fixture(scope="session")
def migrated_db() -> str:
    if not _db_available():
        pytest.skip("postgres not available (docker compose -f deploy/compose.dev.yml up -d postgres)")
    plain = TEST_DB_URL.replace("+psycopg", "")
    with psycopg.connect(plain, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "-x", f"url={TEST_DB_URL}", "upgrade", "head"],
        cwd=SERVER_DIR,
        check=True,
        capture_output=True,
    )
    return TEST_DB_URL


@pytest.fixture
def settings(migrated_db: str, tmp_path: Path) -> Settings:
    return Settings(
        database_url=migrated_db,
        data_dir=tmp_path,
        auth_disabled=True,
        step_max_retries=2,
        default_max_fix_attempts=3,
    )


class Harness:
    def __init__(
        self, rt: Runtime, service: IncidentService, dispatcher: InlineDispatcher, publisher: MemoryPublisher
    ) -> None:
        self.rt = rt
        self.service = service
        self.dispatcher = dispatcher
        self.publisher = publisher
        self.control: MemoryControlPlane = rt.control  # type: ignore[assignment]

    @property
    def adapters(self) -> Adapters:
        return self.rt.adapters

    async def project(self, **config: Any) -> Project:
        async with self.rt.store.tx() as tx:
            p = Project(
                name=f"demo-{new_id()[-6:].lower()}",
                repo="acme/demo-shop",
                branch_map={"test": "dev", "release": "release", "prod": "main"},
                # 测试默认关闭合并后观察期，保持端到端流程同步走完；观察期行为由专门用例覆盖
                config={"project_packages": ["com.acme"], "observe_s": 0, **config},
            )
            tx.session.add(p)
        return p


@pytest.fixture
def otel_exporter() -> Iterator[Any]:
    """把 OTel tracer 指向内存导出器；用例结束后恢复 NoOp，避免影响其他测试。"""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from twinfix import otel

    exporter = InMemorySpanExporter()
    otel.setup_tracing(enabled=False, exporter=exporter)
    try:
        yield exporter
    finally:
        otel.reset_tracing()


@pytest.fixture
async def harness(settings: Settings) -> AsyncIterator[Harness]:
    publisher = MemoryPublisher()
    dispatcher = InlineDispatcher()
    rt = await build_runtime(
        settings, publisher=publisher, control=MemoryControlPlane(), adapters=build_adapters(settings)
    )
    dispatcher.bind(rt.engine)
    try:
        yield Harness(rt, IncidentService(rt.store, dispatcher), dispatcher, publisher)
    finally:
        await rt.close()
