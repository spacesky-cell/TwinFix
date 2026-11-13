"""Prometheus 指标端点：聚合口径、text format 与鉴权。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from tests.conftest import Harness
from twinfix.api.metrics import _escape, router, sample
from twinfix.config import Settings
from twinfix.domain.enums import AgentName, RunStatus, SignalSource, ToolCallStatus, ToolLevel
from twinfix.domain.models import Run, ToolCall
from twinfix.services import SignalIn


def test_escape_and_sample_format() -> None:
    assert _escape('a"b\\c\nd') == 'a\\"b\\\\c\\nd'
    # label 按 key 排序，值做转义
    assert sample("x", 1, {"b": "2", "a": "1"}) == 'x{a="1",b="2"} 1'
    assert sample("x", 0.5) == "x 0.5"
    assert sample("x", 0.0) == "x 0"


@asynccontextmanager
async def _client(harness: Harness, settings: Settings, **overrides) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.state.settings = settings.model_copy(update=overrides)
    app.state.store = harness.rt.store
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/api") as c:
        yield c


async def _seed(harness: Harness) -> str:
    """建一个 NEW 事故，并补一条 Run / ToolCall 让 agent 与 tool 维度有数据。"""
    harness.dispatcher.auto = False
    project = await harness.project()
    res = await harness.service.ingest(SignalIn(project=project.name, source=SignalSource.MANUAL, title="指标端点测试"))
    async with harness.rt.store.tx() as tx:
        run = Run(
            incident_id=res.incident_id,
            agent=AgentName.DIAGNOSER,
            status=RunStatus.SUCCEEDED,
            input_tokens=10,
            output_tokens=5,
            cost=Decimal("0.01"),
            latency_ms=1200,
        )
        tx.session.add(run)
        await tx.session.flush()
        tx.session.add(
            ToolCall(
                run_id=run.id,
                incident_id=res.incident_id,
                agent=AgentName.DIAGNOSER,
                tool="query_metrics",
                level=ToolLevel.L0,
                status=ToolCallStatus.OK,
                latency_ms=250,
            )
        )
        # 同一工具的另一种状态：延迟指标必须按工具汇总，不能输出重复 label set
        tx.session.add(
            ToolCall(
                run_id=run.id,
                incident_id=res.incident_id,
                agent=AgentName.DIAGNOSER,
                tool="query_metrics",
                level=ToolLevel.L0,
                status=ToolCallStatus.ERROR,
                latency_ms=50,
            )
        )
    return res.incident_id


def _value(text: str, metric: str) -> float:
    """解析某个已渲染样本的值；label 集合必须完整匹配。"""
    for line in text.splitlines():
        if line.startswith(f"{metric} "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"metric {metric} not found in:\n{text}")


@pytest.mark.integration
async def test_metrics_expose_global_state(harness: Harness, settings: Settings) -> None:
    await _seed(harness)
    async with _client(harness, settings, auth_disabled=True, metrics_token=SecretStr("")) as c:
        r = await c.get("/metrics")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    text = r.text
    # 测试库是全套共享的，其他用例也会留下数据：断言“至少包含本次种子”，不要求精确总数
    assert _value(text, "twinfix_up") == 1
    assert _value(text, "twinfix_projects") >= 1
    assert _value(text, 'twinfix_incidents{state="NEW"}') >= 1
    assert 'twinfix_incident_oldest_age_seconds{state="NEW"}' in text
    assert _value(text, 'twinfix_runs{status="succeeded"}') >= 1
    assert _value(text, 'twinfix_llm_tokens_total{agent="diagnoser"}') >= 15
    assert _value(text, 'twinfix_llm_cost_total{agent="diagnoser"}') >= 0.01
    assert _value(text, 'twinfix_tool_calls_total{status="ok",tool="query_metrics"}') >= 1
    assert _value(text, 'twinfix_tool_calls_total{status="error",tool="query_metrics"}') >= 1
    assert _value(text, 'twinfix_tool_latency_seconds_total{tool="query_metrics"}') >= 0.3
    # 延迟指标按工具聚合，同一 label set 只能出现一次
    assert text.count('twinfix_tool_latency_seconds_total{tool="query_metrics"}') == 1
    assert "twinfix_active_runs" in text
    assert "# TYPE twinfix_llm_tokens_total counter" in text
    assert "# TYPE twinfix_incidents gauge" in text


@pytest.mark.integration
async def test_metrics_require_token_when_auth_enabled(harness: Harness, settings: Settings) -> None:
    async with _client(harness, settings, auth_disabled=False, metrics_token=SecretStr("scrape-token")) as c:
        assert (await c.get("/metrics")).status_code == 401
        assert (await c.get("/metrics", headers={"Authorization": "Bearer wrong"})).status_code == 401
        ok = await c.get("/metrics", headers={"Authorization": "Bearer scrape-token"})
        assert ok.status_code == 200 and "twinfix_up 1" in ok.text
        # 与 webhook 一致，也接受 X-TwinFix-Token
        assert (await c.get("/metrics", headers={"X-TwinFix-Token": "scrape-token"})).status_code == 200


@pytest.mark.integration
async def test_metrics_unauthorized_without_token_when_auth_enabled(harness: Harness, settings: Settings) -> None:
    async with _client(harness, settings, auth_disabled=False, metrics_token=SecretStr("")) as c:
        assert (await c.get("/metrics")).status_code == 401


@pytest.mark.integration
async def test_metrics_can_be_disabled(harness: Harness, settings: Settings) -> None:
    async with _client(harness, settings, auth_disabled=True, metrics_enabled=False) as c:
        assert (await c.get("/metrics")).status_code == 404
