"""HTTP 层：路由、序列化、鉴权、错误码。用内存调度驱动编排，不依赖 RabbitMQ/Redis。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from tests.conftest import Harness
from twinfix.api.auth import issue_token
from twinfix.api.routes import router
from twinfix.config import Settings
from twinfix.domain.enums import UserRole
from twinfix.services import IncidentService

pytestmark = pytest.mark.integration


def make_app(h: Harness, settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.store = h.rt.store
    app.state.redis = None  # SSE 退化为数据库轮询
    app.state.control = h.control
    app.state.service = IncidentService(h.rt.store, h.dispatcher)
    app.include_router(router)
    return app


@pytest.fixture
async def client(harness: Harness, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = make_app(harness, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/api") as c:
        yield c


async def create(c: httpx.AsyncClient, h: Harness, scenario: str = "happy") -> str:
    project = await h.project()
    body_in = {"project": project.name, "title": "超卖", "service": "order", "scenario": scenario}
    r = await c.post("/incidents", json=body_in)
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return str(body["incident_id"])


async def test_full_flow_over_http(client: httpx.AsyncClient, harness: Harness) -> None:
    iid = await create(client, harness)
    d = (await client.get(f"/incidents/{iid}")).json()
    assert d["state"] == "AWAITING_REVIEW"
    assert d["diagnosis"]["confidence"] > 0.6
    assert d["attempts"][0]["patch"]["files_changed"]
    assert d["attempts"][0]["verifications"][0]["passed"] is True
    assert d["twin"]["status"] == "ready"
    assert "workspace" not in d["meta"]  # 内部路径不外泄
    approval = next(a for a in d["approvals"] if a["status"] == "pending")

    r = await client.post(f"/approvals/{approval['id']}/decide", json={"approve": True, "comment": "ok"})
    assert r.status_code == 202
    d = (await client.get(f"/incidents/{iid}")).json()
    assert d["state"] == "CLOSED"
    assert [r["type"] for r in d["reports"]] == ["fixed", "merged"]
    agents = {r["agent"] for r in d["runs"]}
    assert agents >= {"diagnoser", "reproducer", "fixer", "verifier", "reporter", "orchestrator"}

    # 重复决定同一审批 → 409
    r = await client.post(f"/approvals/{approval['id']}/decide", json={"approve": True})
    assert r.status_code == 409

    events = (await client.get(f"/incidents/{iid}/events")).json()
    assert [e["to_state"] for e in events if e["kind"] == "transition"][-1] == "CLOSED"

    run_id = d["runs"][0]["id"]
    run = (await client.get(f"/runs/{run_id}")).json()
    assert run["tool_calls"] and all("level" in c for c in run["tool_calls"])

    evidence = (await client.get(f"/incidents/{iid}/evidence")).json()
    assert evidence and all({"id", "kind", "summary", "agent"} <= e.keys() for e in evidence)
    full = (await client.get(f"/evidence/{evidence[0]['id']}")).json()
    assert full["id"] == evidence[0]["id"] and "content" in full
    assert (await client.get("/evidence/nope")).status_code == 404


async def test_list_filters(client: httpx.AsyncClient, harness: Harness) -> None:
    iid = await create(client, harness, "not_bug")
    page = (await client.get("/incidents", params={"state": "NOT_BUG"})).json()
    assert iid in [i["id"] for i in page["items"]]
    page = (await client.get("/incidents", params={"state": "FIXING"})).json()
    assert iid not in [i["id"] for i in page["items"]]


async def test_list_search_filter_and_stats(client: httpx.AsyncClient, harness: Harness) -> None:
    harness.dispatcher.auto = False  # 只验证列表查询，不推进编排（否则会留下 pending 审批影响其他用例）
    project = await harness.project()
    r = await client.post(
        "/incidents",
        json={"project": project.name, "title": "库存扣减为负", "service": "inventory", "scenario": "happy"},
    )
    assert r.status_code == 201, r.text
    iid = r.json()["incident_id"]

    by_title = (await client.get("/incidents", params={"q": "库存扣减"})).json()
    assert iid in [i["id"] for i in by_title["items"]]
    assert all("库存扣减" in i["title"] for i in by_title["items"])
    # 服务筛选 + 搜索可以叠加；不匹配时为空
    hit = (await client.get("/incidents", params={"q": "库存", "service": "inventory"})).json()
    assert [i["id"] for i in hit["items"]] == [iid]
    miss = (await client.get("/incidents", params={"q": "库存", "service": "nope"})).json()
    assert miss["items"] == [] and miss["total"] == 0

    stats = (await client.get("/incidents/stats")).json()
    assert stats["total"] >= 1
    assert stats["ongoing"] == stats["total"] - stats["ended"]
    assert stats["waiting"] >= 0 and stats["high"] >= 0
    assert "inventory" in stats["services"]


async def test_approvals_list(client: httpx.AsyncClient, harness: Harness) -> None:
    iid = await create(client, harness)  # happy: 停在 AWAITING_REVIEW，带一个 pending 合并审批
    iid2 = await create(client, harness, "low_confidence")  # NEEDS_HUMAN：转人工审批

    page = (await client.get("/approvals", params={"status": "pending"})).json()
    rows = {r["incident"]["id"]: r for r in page["items"]}
    # 测试库是全文件共享的，其他用例也会留下 pending 审批：这里断言本用例的两条都在
    assert {iid, iid2} <= set(rows)
    merge = rows[iid]
    assert merge["action"] == "merge"
    assert merge["status"] == "pending"
    assert merge["payload"]["target_branch"] == "dev"
    assert merge["incident"]["title"] == "超卖"
    assert merge["incident"]["state"] == "AWAITING_REVIEW"
    assert merge["expires_at"] is not None
    takeover = rows[iid2]
    assert takeover["action"] == "human_takeover"
    assert takeover["incident"]["state"] == "NEEDS_HUMAN"

    # 批准合并后不再出现在 pending 里，且能按状态查到
    r = await client.post(f"/approvals/{merge['id']}/decide", json={"approve": True, "comment": "ok"})
    assert r.status_code == 202
    page = (await client.get("/approvals", params={"status": "pending"})).json()
    assert iid not in [r["incident"]["id"] for r in page["items"]]
    page = (await client.get("/approvals", params={"status": "approved"})).json()
    row = next(r for r in page["items"] if r["id"] == merge["id"])
    assert row["comment"] == "ok" and row["decided_at"] is not None
    # 按动作过滤 + 分页
    page = (await client.get("/approvals", params={"action": "human_takeover"})).json()
    assert {r["incident"]["id"] for r in page["items"]} == {iid2}
    page = (await client.get("/approvals", params={"limit": 1, "offset": 1})).json()
    assert page["total"] >= 2 and len(page["items"]) == 1

    # 取消事故 → 遗留的待决审批自动作废，不再出现在待办里
    iid3 = await create(client, harness)  # 又一个 AWAITING_REVIEW
    r = await client.post(f"/incidents/{iid3}/cancel", json={"reason": "不用了"})
    assert r.status_code == 202
    assert (await client.get(f"/incidents/{iid3}")).json()["state"] == "CANCELLED"
    page = (await client.get("/approvals", params={"status": "pending"})).json()
    assert iid3 not in [r["incident"]["id"] for r in page["items"]]
    page = (await client.get("/approvals", params={"status": "expired"})).json()
    assert iid3 in [r["incident"]["id"] for r in page["items"]]

    # 评审超时类路径：AWAITING_REVIEW → NEEDS_HUMAN 时遗留的合并审批同样作废
    # （happy 场景只有合并审批，没有转人工审批可留）
    iid4 = await create(client, harness)
    from twinfix.domain.enums import IncidentState as IS
    from twinfix.domain.models import Incident
    from twinfix.domain.repo import transition

    async with harness.rt.store.tx() as tx:
        inc = await tx.session.get(Incident, iid4)
        await transition(tx, inc, IS.NEEDS_HUMAN, reason="评审超时")
    page = (await client.get("/approvals", params={"status": "pending", "action": "merge"})).json()
    assert iid4 not in [r["incident"]["id"] for r in page["items"]]
    page = (await client.get("/approvals", params={"status": "expired", "action": "merge"})).json()
    assert iid4 in [r["incident"]["id"] for r in page["items"]]


async def test_arena_cases(harness: Harness, settings: Settings, tmp_path: Any) -> None:
    cases = tmp_path / "cases"
    (cases / "X001").mkdir(parents=True)
    (cases / "X001" / "case.yaml").write_text(
        """
id: X001
title: 优惠码为空时下单 NPE
category: npe
description: 演示用例
inject: {type: none, services: [order]}
signal: {title: "NPE on checkout", severity: S2}
expect:
  outcome: NOT_BUG
  root_cause_keywords: ["null"]
hidden_tests: []
""".lstrip(),
        encoding="utf-8",
    )
    (cases / "X002").mkdir()
    (cases / "X002" / "case.yaml").write_text("id: X002\ntitle: 坏用例\n", encoding="utf-8")  # 缺必填字段

    app = make_app(harness, settings.model_copy(update={"arena_cases_dir": cases}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/api") as c:
        rows = {r["id"]: r for r in (await c.get("/arena/cases")).json()}
        good = rows["X001"]
        assert good["title"] == "优惠码为空时下单 NPE"
        assert good["category"] == "npe"
        assert good["inject"]["type"] == "none" and good["inject"]["services"] == ["order"]
        assert good["expect"]["outcome"] == "NOT_BUG"
        assert good["severity"] == "S2" and good["hidden_tests"] == 0 and good["on_review"] is False
        assert "error" in rows["X002"]
    # 目录不存在 → 空列表而不是 500
    app = make_app(harness, settings.model_copy(update={"arena_cases_dir": tmp_path / "missing"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/api") as c:
        assert (await c.get("/arena/cases")).json() == []


async def test_errors(client: httpx.AsyncClient, harness: Harness) -> None:
    assert (await client.get("/incidents/01M590000000000000000000AA")).status_code == 404
    r = await client.post("/incidents", json={"project": "nope", "title": "x"})
    assert r.status_code == 404
    r = await client.post("/incidents", json={"project": "x", "title": ""})
    assert r.status_code == 422
    iid = await create(client, harness, "not_bug")
    r = await client.post(f"/incidents/{iid}/cancel", json={})
    assert r.status_code == 409  # 已是终态
    # 不存在的 id：404，而不是 500（领域层 NotFound 与 services.NotFound 是兄弟类）
    assert (await client.post("/incidents/nope/cancel", json={})).status_code == 404


async def test_needs_human_actions(client: httpx.AsyncClient, harness: Harness) -> None:
    iid = await create(client, harness, "low_confidence")
    assert (await client.get(f"/incidents/{iid}")).json()["state"] == "NEEDS_HUMAN"
    r = await client.post(f"/incidents/{iid}/resolve", json={"verdict": "not_bug", "reason": "预期行为"})
    assert r.status_code == 202
    assert (await client.get(f"/incidents/{iid}")).json()["state"] == "NOT_BUG"


async def test_sse_replays_history(client: httpx.AsyncClient, harness: Harness) -> None:
    # httpx 的 ASGITransport 会缓冲整个响应，无法测试无限流；这里直接测试流生成器，
    # HTTP 层的 SSE 由 scripts/e2e_smoke.py 在真实环境中覆盖
    from twinfix.api.stream import event_stream

    iid = await create(client, harness, "not_bug")
    events = (await client.get(f"/incidents/{iid}/events")).json()
    expected = [e["id"] for e in events]

    async def take(last_id: int, n: int) -> list[int]:
        got: list[int] = []
        gen = event_stream(harness.rt.store, None, iid, last_id)
        async for frame in gen:
            got.append(int(frame["id"]))
            if len(got) == n:
                break
        await gen.aclose()
        return got

    assert await take(0, len(expected)) == expected
    # Last-Event-ID：只补发之后的事件
    assert await take(expected[-3], 2) == expected[-2:]


async def test_halt_endpoint(client: httpx.AsyncClient, harness: Harness) -> None:
    assert (await client.post("/system/halt", json={"halted": True})).json() == {"halted": True}
    assert harness.control.halted is True
    await client.post("/system/halt", json={"halted": False})
    assert harness.control.halted is False


async def test_auth_and_roles(harness: Harness, settings: Settings) -> None:
    secret = SecretStr("test-secret-0123456789abcdef0123456789")
    secured = settings.model_copy(update={"auth_disabled": False, "jwt_secret": secret})
    app = make_app(harness, secured)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/api") as c:
        assert (await c.get("/incidents")).status_code == 401
        assert (await c.get("/incidents", headers={"Authorization": "Bearer junk"})).status_code == 401

        viewer = issue_token(secured, "u1", "vic", UserRole.VIEWER)
        hv = {"Authorization": f"Bearer {viewer}"}
        assert (await c.get("/incidents", headers=hv)).status_code == 200
        assert (await c.post("/system/halt", json={"halted": True}, headers=hv)).status_code == 403
        assert (await c.post("/incidents", json={"project": "x", "title": "t"}, headers=hv)).status_code == 403
        # 可指派账号列表会暴露账号名，只读角色不能拿
        assert (await c.get("/users/assignable", headers=hv)).status_code == 403

        maint = issue_token(secured, "u2", "mia", UserRole.MAINTAINER)
        hm = {"Authorization": f"Bearer {maint}"}
        assert (await c.post("/system/halt", json={"halted": True}, headers=hm)).status_code == 403
        assert (await c.get("/users/assignable", headers=hm)).status_code == 200
        # SSE 可以用 query 携带 token
        assert (await c.get("/incidents", params={"token": maint})).status_code == 200


async def test_eval_reports(harness: Harness, settings: Settings, tmp_path: Any) -> None:
    import json

    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "2026-10-20-r1.json").write_text(
        json.dumps({"title": "t", "meta": {"运行": "r1"}, "summary": {"total": 1}, "cases": [{"case_id": "C001"}]}),
        encoding="utf-8",
    )
    (reports / "notes.json").write_text("{}", encoding="utf-8")  # 不符合命名规则，忽略
    (reports / "2026-10-21-bad.json").write_text("{", encoding="utf-8")  # 损坏，跳过
    app = make_app(harness, settings.model_copy(update={"eval_reports_dir": reports}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/api") as c:
        listing = (await c.get("/eval/reports")).json()
        assert [r["name"] for r in listing] == ["2026-10-20-r1"]
        assert listing[0]["summary"] == {"total": 1}
        detail = (await c.get("/eval/reports/2026-10-20-r1")).json()
        assert detail["cases"] == [{"case_id": "C001"}]
        assert (await c.get("/eval/reports/..%2F..%2Fsecrets")).status_code == 404
        assert (await c.get("/eval/reports/2026-01-01-missing")).status_code == 404
