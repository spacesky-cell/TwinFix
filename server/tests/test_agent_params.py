"""Agent 运行参数：全局默认 → 项目覆盖 → 按 Agent 覆盖；API 校验与回显。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tests.conftest import Harness
from twinfix.agents.params import resolve
from twinfix.api.routes import router
from twinfix.config import Settings
from twinfix.domain.enums import AgentName
from twinfix.services import IncidentService


def _settings(**kw: Any) -> Settings:
    return Settings(database_url="postgresql+psycopg://x@localhost/x", llm_model="m-default", **kw)


def test_defaults_are_generous() -> None:
    s = Settings(database_url="postgresql+psycopg://x@localhost/x")
    assert s.budget_max_tokens == 2_000_000 and s.budget_max_tool_calls >= 120


def test_project_and_per_agent_overrides() -> None:
    cfg = {
        "agent": {
            "max_tokens": 3_000_000,
            "model": "deepseek-v4-1-flash",
            "reasoning_effort": "low",
            "per_agent": {"reproducer": {"max_tool_calls": 200, "reasoning_effort": "high"}},
        }
    }
    s = _settings()
    rep = resolve(s, cfg, AgentName.REPRODUCER)
    diag = resolve(s, cfg, AgentName.DIAGNOSER)
    assert rep.max_tokens == diag.max_tokens == 3_000_000
    assert rep.model == diag.model == "deepseek-v4-1-flash"
    assert rep.max_tool_calls == 200 and diag.max_tool_calls == s.budget_max_tool_calls
    assert rep.reasoning_effort == "high" and diag.reasoning_effort == "low"


def test_invalid_stored_config_falls_back_to_defaults() -> None:
    s = _settings()
    p = resolve(s, {"agent": {"max_tokens": -1}}, AgentName.FIXER)
    assert p.max_tokens == s.budget_max_tokens and p.model == "m-default"


@pytest.fixture
async def client(harness: Harness, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.state.settings = settings
    app.state.store = harness.rt.store
    app.state.service = IncidentService(harness.rt.store, harness.dispatcher)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/api") as c:
        yield c


@pytest.mark.integration
async def test_agent_config_api(client: httpx.AsyncClient, harness: Harness, settings: Settings) -> None:
    project = await harness.project(keep="me")
    url = f"/projects/{project.name}/agent-config"
    view = (await client.get(url)).json()
    assert view["overrides"] == {} and view["defaults"]["max_tokens"] == settings.budget_max_tokens

    body = {"max_tool_calls": 150, "per_agent": {"fixer": {"max_turns": 80}}}
    view = (await client.put(url, json=body)).json()
    assert view["overrides"] == body
    assert view["effective"]["fixer"]["max_turns"] == 80 and view["effective"]["reproducer"]["max_tool_calls"] == 150

    assert (await client.put(url, json={"max_tokens": 5})).status_code == 422
    assert (await client.put(url, json={"reasoning_effort": "extreme"})).status_code == 422
    assert (await client.put(url, json={"per_agent": {"hacker": {}}})).status_code == 422
    assert (await client.get("/projects/nope/agent-config")).status_code == 404

    # 其他项目配置不被覆盖
    async with harness.rt.store.read() as s:
        from sqlalchemy import select

        from twinfix.domain.models import Project

        row = await s.scalar(select(Project).where(Project.name == project.name))
    assert row is not None and row.config["keep"] == "me" and row.config["agent"]["max_tool_calls"] == 150
