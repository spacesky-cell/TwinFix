"""控制台运营接口：负责人、相似事故、报告下载、运行/成本统计、项目配置与自检。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.conftest import Harness
from twinfix.api.routes import router
from twinfix.config import Settings
from twinfix.domain.enums import IncidentState, ReportStatus, SignalSource, UserRole
from twinfix.domain.models import Incident, Report, User
from twinfix.ids import new_id
from twinfix.services import IncidentService, SignalIn

pytestmark = pytest.mark.integration
S = IncidentState


def _app(h: Harness, settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.store = h.rt.store
    app.state.redis = None
    app.state.control = h.control
    app.state.service = IncidentService(h.rt.store, h.dispatcher)
    app.include_router(router)
    return app


@pytest.fixture
def make_client(harness: Harness, settings: Settings) -> Any:
    @asynccontextmanager
    async def _make() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(harness, settings)), base_url="http://t/api"
        ) as c:
            yield c

    return _make


async def _incident(h: Harness, title: str = "超卖", **config: Any) -> str:
    project = await h.project(**config)
    res = await h.service.ingest(
        SignalIn(
            project=project.name,
            source=SignalSource.MANUAL,
            title=title,
            service="order",
            stacktrace="java.lang.IllegalStateException: stock < 0\n\tat com.acme.Inventory.deduct(Inventory.java:1)",
        )
    )
    assert res.created
    return res.incident_id


async def _user(h: Harness, name: str = "alice", role: UserRole = UserRole.MAINTAINER) -> User:
    async with h.rt.store.tx() as tx:
        u = User(name=f"{name}-{new_id()[-6:].lower()}", role=role)
        tx.session.add(u)
    return u


class TestAssignee:
    async def test_assign_clear_and_filter(self, harness: Harness, make_client: Any) -> None:
        iid = await _incident(harness, "指派测试")
        alice = await _user(harness)
        async with make_client() as c:
            r = await c.post(f"/incidents/{iid}/assignee", json={"user_id": alice.id})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["assignee_id"] == alice.id and body["assignee"]["name"] == alice.name

            page = await c.get("/incidents", params={"assignee_id": alice.id})
            assert [i["id"] for i in page.json()["items"]] == [iid]
            assert page.json()["items"][0]["assignee_id"] == alice.id

            other = await c.get("/incidents", params={"unassigned": True})
            assert iid not in [i["id"] for i in other.json()["items"]]

            # 取消指派
            r = await c.post(f"/incidents/{iid}/assignee", json={"user_id": None})
            assert r.status_code == 200 and r.json()["assignee_id"] is None

    async def test_unknown_user_and_incident(self, harness: Harness, make_client: Any) -> None:
        iid = await _incident(harness, "指派异常")
        async with make_client() as c:
            assert (await c.post(f"/incidents/{iid}/assignee", json={"user_id": "nope"})).status_code == 404
            assert (await c.post("/incidents/nope/assignee", json={"user_id": None})).status_code == 404

    async def test_terminal_incident_rejects_assignment(self, harness: Harness, make_client: Any) -> None:
        project = await harness.project()
        res = await harness.service.ingest(
            SignalIn(project=project.name, source=SignalSource.MANUAL, title="非缺陷", meta={"scenario": "not_bug"})
        )
        async with harness.rt.store.read() as s:
            inc = await s.get(Incident, res.incident_id)
        assert inc is not None and inc.state is S.NOT_BUG
        alice = await _user(harness)
        async with make_client() as c:
            r = await c.post(f"/incidents/{res.incident_id}/assignee", json={"user_id": alice.id})
            assert r.status_code == 409


class TestAssignableUsers:
    async def test_lists_id_name_role_without_email(self, harness: Harness, make_client: Any) -> None:
        alice = await _user(harness, "assignable", UserRole.MAINTAINER)
        async with make_client() as c:
            r = await c.get("/users/assignable")
            assert r.status_code == 200, r.text
            rows = r.json()
        row = next(x for x in rows if x["id"] == alice.id)
        # 指派负责人只需要 id / 名字 / 角色，不能把邮箱等账号信息带给前端
        assert set(row) == {"id", "name", "role"}
        assert row["name"] == alice.name and row["role"] == "maintainer"


class TestSimilar:
    async def test_empty_when_no_precedents(self, harness: Harness, make_client: Any) -> None:
        iid = await _incident(harness, "没有历史")
        async with make_client() as c:
            r = await c.get(f"/incidents/{iid}/similar")
            assert r.status_code == 200 and r.json()["items"] == []
            assert (await c.get("/incidents/nope/similar")).status_code == 404


class TestReportDownload:
    async def test_download_markdown(self, harness: Harness, make_client: Any) -> None:
        iid = await _incident(harness, "报告下载")
        async with harness.rt.store.read() as s:
            report = await s.scalar(select(Report).where(Report.incident_id == iid).order_by(Report.created_at))
        assert report is not None and report.status is ReportStatus.SENT
        async with make_client() as c:
            r = await c.get(f"/reports/{report.id}/download")
            assert r.status_code == 200, r.text
            assert r.headers["content-type"].startswith("text/markdown")
            assert report.type.value in r.headers["content-disposition"]
            assert iid in r.headers["content-disposition"]
            assert r.text == report.content
            assert (await c.get("/reports/nope/download")).status_code == 404


class TestStats:
    async def test_runtime_stats_shape(self, harness: Harness, make_client: Any) -> None:
        await _incident(harness, "运行统计")
        async with make_client() as c:
            r = await c.get("/stats/runtime")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["incidents"].get("AWAITING_REVIEW", 0) >= 1
            assert body["pending_approvals"] >= 1
            assert body["limits"]["state_timeout_s"] > 0
            assert body["twin_capacity"] >= 1

    async def test_cost_stats_shape(self, harness: Harness, make_client: Any) -> None:
        iid = await _incident(harness, "成本统计")
        # stub Agent 不花费 token；直接写入一次用量，验证聚合口径
        async with harness.rt.store.tx() as tx:
            row = await tx.session.get(Incident, iid)
            row.cost_tokens = 1234
        async with make_client() as c:
            r = await c.get("/stats/cost", params={"days": 7})
            assert r.status_code == 200, r.text
            body = r.json()
            assert set(body["totals"]) == {
                "runs",
                "input_tokens",
                "output_tokens",
                "tokens",
                "cost",
                "avg_latency_ms",
            }
            assert isinstance(body["by_agent"], list) and isinstance(body["daily"], list)
            assert any(i["id"] == iid for i in body["top_incidents"])
            assert (await c.get("/stats/cost", params={"days": 0})).status_code == 422


class TestProjectAdmin:
    async def test_patch_project_and_check(self, make_client: Any) -> None:
        async with make_client() as c:
            created = await c.post(
                "/projects",
                json={"name": "shop-x", "repo": "acme/shop-x", "branch_map": {"test": "dev"}},
            )
            assert created.status_code == 201, created.text
            r = await c.patch(
                "/projects/shop-x",
                json={"repo": "acme/shop-y", "config": {"project_packages": ["com.acme"], "observe_s": 60}},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["repo"] == "acme/shop-y"
            assert body["config"]["observe_s"] == 60
            assert (await c.patch("/projects/nope", json={"repo": "a/b"})).status_code == 404

            bad = await c.patch("/projects/shop-x", json={"config": {"repos": [{"repo": "not-owner-name"}]}})
            assert bad.status_code == 422, bad.text
            good = await c.patch(
                "/projects/shop-x",
                json={"config": {"repos": [{"name": "inventory", "repo": "acme/inventory"}]}},
            )
            assert good.status_code == 200, good.text

            check = await c.post("/projects/shop-x/check")
            assert check.status_code == 200, check.text
            result = check.json()
            assert result["project"] == "shop-x"
            names = {item["name"] for item in result["checks"]}
            assert {"repo_config", "codehost", "extra_repos", "twin_provider", "notifiers"} <= names
            # fake 代码托管：branch_head 有稳定返回值
            assert any(item["name"] == "codehost" and item["ok"] for item in result["checks"])
            assert (await c.post("/projects/nope/check")).status_code == 404
