"""管理员账号、Arena 注入、控制台评测任务：路由行为与防护。"""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select

from tests.conftest import Harness
from twinfix.api.auth import hash_password, issue_token, verify_password
from twinfix.api.routes import router
from twinfix.arena.evalruns import EvalRun, EvalRunManager
from twinfix.config import Settings
from twinfix.domain.enums import ApprovalAction, ApprovalStatus, SignalSource, UserRole
from twinfix.domain.models import Approval, Project, User
from twinfix.services import IncidentService, SignalIn

pytestmark = pytest.mark.integration


def _app(h: Harness, settings: Settings, eval_runs: EvalRunManager | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.store = h.rt.store
    app.state.redis = None  # SSE 退化为数据库轮询
    app.state.control = h.control
    app.state.service = IncidentService(h.rt.store, h.dispatcher)
    if eval_runs is not None:
        app.state.eval_runs = eval_runs
    app.include_router(router)
    return app


@pytest.fixture
def make_client(harness: Harness) -> Any:
    @asynccontextmanager
    async def _make(settings: Settings, eval_runs: EvalRunManager | None = None) -> AsyncIterator[httpx.AsyncClient]:
        app = _app(harness, settings, eval_runs)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/api") as c:
            yield c

    return _make


@pytest.fixture
async def client(harness: Harness, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(harness, settings)), base_url="http://t/api"
    ) as c:
        yield c


_seq = itertools.count()


async def _create_user(c: httpx.AsyncClient, **body: object) -> httpx.Response:
    name = body.pop("name", None) or f"user{next(_seq)}"
    payload = {"name": name, "password": "pass1234", "role": "viewer", **body}
    return await c.post("/users", json=payload)


class TestUsers:
    async def test_crud_and_guards(self, client: httpx.AsyncClient) -> None:
        alice = await _create_user(client, name="alice-chem", email="alice@example.com", role="viewer")
        assert alice.status_code == 201, alice.text
        row = alice.json()
        assert row["role"] == "viewer" and row["email"] == "alice@example.com" and row["created_at"]

        # 重名 / 重复邮箱 / 弱密码 / 非法用户名
        assert (await _create_user(client, name="alice-chem")).status_code == 409
        assert (await _create_user(client, name="bob-chem", email="alice@example.com")).status_code == 409
        assert (await _create_user(client, name="weak-chem", password="short")).status_code == 422
        assert (await _create_user(client, name="a b")).status_code == 422

        # 改角色 + 邮箱 + 密码
        r = await client.patch(
            f"/users/{row['id']}", json={"role": "maintainer", "email": "alice2@example.com", "password": "newpass99"}
        )
        assert r.status_code == 200 and r.json()["role"] == "maintainer"
        assert r.json()["email"] == "alice2@example.com"
        # 邮箱冲突（排除自己）
        bob = (await _create_user(client, name="bob-chem", email="bob@example.com")).json()
        assert (await client.patch(f"/users/{bob['id']}", json={"email": "alice2@example.com"})).status_code == 409
        assert (await client.get("/users")).json()[-1]["name"] in {"alice-chem", "bob-chem"}


async def test_password_hash_written(client: httpx.AsyncClient, harness: Harness) -> None:
    uid = (await _create_user(client, name="pw-check", password="pass1234")).json()["id"]
    async with harness.rt.store.read() as s:
        user = await s.get(User, uid)
    assert user is not None and user.password_hash and verify_password("pass1234", user.password_hash)


async def test_delete_user_blocked_by_approvals(client: httpx.AsyncClient, harness: Harness) -> None:
    harness.dispatcher.auto = False
    uid = (await _create_user(client, name="approver-1", role="maintainer")).json()["id"]
    project = await harness.project()
    res = await harness.service.ingest(SignalIn(project=project.name, source=SignalSource.ALERT, title="审批历史"))
    async with harness.rt.store.tx() as tx:
        tx.session.add(
            Approval(
                incident_id=res.incident_id,
                action=ApprovalAction.MERGE,
                status=ApprovalStatus.APPROVED,
                decided_by=uid,
            )
        )
    r = await client.delete(f"/users/{uid}")
    assert r.status_code == 409 and "approvals" in r.json()["detail"]
    # 改角色仍然可以
    assert (await client.patch(f"/users/{uid}", json={"role": "viewer"})).status_code == 200


async def test_self_demote_and_delete_blocked(harness: Harness, settings: Settings) -> None:
    settings = settings.model_copy(update={"auth_disabled": False, "jwt_secret": SecretStr("k" * 32)})
    async with harness.rt.store.tx() as tx:
        row = User(name="root-admin", role=UserRole.ADMIN, password_hash=hash_password("admin1234"))
        tx.session.add(row)
        await tx.session.flush()
        uid = row.id
    token = issue_token(settings, uid, "root-admin", UserRole.ADMIN)
    app = _app(harness, settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t/api", headers={"Authorization": f"Bearer {token}"}
    ) as c:
        assert (await c.patch(f"/users/{uid}", json={"role": "viewer"})).status_code == 409
        assert (await c.delete(f"/users/{uid}")).status_code == 409


# ---------------------------------------------------------------- 全局配置


async def test_settings_overview_is_read_only_and_secretless(client: httpx.AsyncClient, settings: Settings) -> None:
    r = await client.get("/settings/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_defaults"]["max_tokens"] == settings.budget_max_tokens
    assert body["agent_defaults"]["model"] == settings.llm_model
    assert body["runtime"]["agent_mode"] == settings.agent_mode
    assert body["runtime"]["codehost"] == settings.codehost
    assert body["limits"]["review_timeout_s"] == settings.review_timeout_s
    # 只读接口，不能夹带任何密钥
    flat = json.dumps(body).lower()
    assert "jwt" not in flat and "secret" not in flat and "password" not in flat


# ---------------------------------------------------------------- Arena 注入


def _case_dir(tmp_path: Path) -> Path:
    cases = tmp_path / "cases"
    d = cases / "X001"
    d.mkdir(parents=True)
    (d / "case.yaml").write_text(
        "id: X001\ntitle: 空指针\ncategory: 空指针\n"
        "inject: {type: none, services: [app]}\n"
        "signal: {title: boom, service: app, severity: S2}\n"
        "expect: {outcome: NOT_BUG}\n",
        encoding="utf-8",
    )
    return cases


def _demo_shop(tmp_path: Path) -> Path:
    d = tmp_path / "demo-shop"
    (d / "app").mkdir(parents=True)
    (d / "app" / "Calc.txt").write_text("ok\n", encoding="utf-8")
    return d


def _local_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={
            "codehost": "local",
            "arena_cases_dir": _case_dir(tmp_path),
            "arena_demo_shop_dir": _demo_shop(tmp_path),
            "git_root": tmp_path / "git",
        }
    )


async def test_arena_inject_requires_local_codehost(client: httpx.AsyncClient) -> None:
    r = await client.post("/arena/inject", json={"case_id": "X001"})
    assert r.status_code == 409 and "CODEHOST" in r.json()["detail"]


async def test_arena_inject_unknown_case(make_client: Any, settings: Settings, tmp_path: Path) -> None:
    async with make_client(_local_settings(settings, tmp_path)) as c:
        assert (await c.post("/arena/inject", json={"case_id": "NOPE"})).status_code == 404
        # 路径穿越式项目名（schema 已限字符，这里再兜一层）
        assert (await c.post("/arena/inject", json={"case_id": "X001", "project": "..evil"})).status_code in (409, 422)


async def test_arena_inject_creates_repo_and_incident(
    make_client: Any, harness: Harness, settings: Settings, tmp_path: Path
) -> None:
    harness.dispatcher.auto = False
    cfg = _local_settings(settings, tmp_path)
    async with make_client(cfg) as c:
        r = await c.post("/arena/inject", json={"case_id": "X001", "project": "arena-demo"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["project"] == "arena-demo" and body["repo"] == "arena/arena-demo"
        assert body["branch"] == "dev" and body["sha"]
        assert body["incident_id"] and body["created"] is True
        assert (tmp_path / "git" / "arena" / "arena-demo.git").is_dir()

        async with harness.rt.store.read() as s:
            project = await s.scalar(select(Project).where(Project.name == "arena-demo"))
        assert project is not None and project.repo == "arena/arena-demo"

        # 同名项目再注入 → 409
        assert (await c.post("/arena/inject", json={"case_id": "X001", "project": "arena-demo"})).status_code == 409
        # trigger=false：只建仓库，不建事故
        r2 = await c.post("/arena/inject", json={"case_id": "X001", "project": "arena-quiet", "trigger": False})
        assert r2.status_code == 201 and r2.json()["incident_id"] is None


# ---------------------------------------------------------------- 评测任务


def _eval_settings(settings: Settings, tmp_path: Path) -> Settings:
    cfg = _local_settings(settings, tmp_path)
    return cfg.model_copy(
        update={
            "agent_mode": "llm",
            "data_dir": tmp_path / "data",
            "eval_reports_dir": tmp_path / "reports",
        }
    )


async def test_eval_runs_route(make_client: Any, settings: Settings, tmp_path: Path) -> None:
    cfg = _eval_settings(settings, tmp_path)
    cfg.eval_reports_dir.mkdir(parents=True)

    async def runner(run: EvalRun, cmd: list[str], log_path: Path) -> int:
        log_path.write_text("log\n", encoding="utf-8")  # noqa: ASYNC240
        (cfg.eval_reports_dir / f"{run.id}.md").write_text("# report\n", encoding="utf-8")
        (cfg.eval_reports_dir / f"{run.id}.json").write_text(
            json.dumps({"title": "t", "meta": {}, "summary": {}, "cases": []}), encoding="utf-8"
        )
        return 0

    manager = EvalRunManager(cfg, runner=runner)
    async with make_client(cfg, manager) as c:
        # 不满足前提的用例 → 409；合法请求 → 202
        assert (await c.post("/eval/runs", json={"cases": ["NOPE"]})).status_code == 409
        r = await c.post("/eval/runs", json={"cases": ["X001"], "label": "smoke", "effort": "low"})
        assert r.status_code == 202, r.text
        run_id = r.json()["id"]

        await manager.wait()
        detail = (await c.get(f"/eval/runs/{run_id}")).json()
        assert detail["run"]["status"] == "succeeded" and detail["run"]["report"] == run_id
        assert detail["report"] is not None and detail["report"]["title"] == "t"
        assert [x["id"] for x in (await c.get("/eval/runs")).json()] == [run_id]
        assert (await c.get("/eval/runs/nope")).status_code == 404

        # 坏请求：effort 不在允许值 / 用例 id 带路径
        assert (await c.post("/eval/runs", json={"effort": "ultra"})).status_code == 422
        assert (await c.post("/eval/runs", json={"cases": ["../x"]})).status_code == 422


async def test_eval_runs_unavailable_without_manager(client: httpx.AsyncClient) -> None:
    assert (await client.post("/eval/runs", json={})).status_code == 503
    assert (await client.get("/eval/runs")).status_code == 503
