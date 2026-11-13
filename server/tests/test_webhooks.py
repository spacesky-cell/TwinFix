"""GitHub Issue webhook：验签、触发标签、字段提取、归并。"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select

from tests.conftest import Harness
from twinfix.api.webhooks import (
    alert_to_signal,
    extract_stacktrace,
    gitlab_issue_to_signal,
    issue_to_signal,
    router,
    verify_signature,
    verify_token,
)
from twinfix.config import Settings
from twinfix.domain.enums import (
    ApprovalAction,
    ApprovalStatus,
    Env,
    IncidentState,
    Severity,
    SignalSource,
    TwinStatus,
)
from twinfix.domain.models import Approval, Incident, IncidentEvent, Project, Twin
from twinfix.ids import new_id
from twinfix.services import IncidentService, SignalIn

SECRET = "s3cret-webhook"
STACK = (
    "```\njava.lang.NullPointerException\n"
    "    at com.acme.order.domain.PriceCalculator.calculate(PriceCalculator.java:21)\n```"
)


def _payload(repo: str, action: str = "labeled", labels: tuple[str, ...] = ("twinfix", "S2"), **issue: Any) -> dict:
    return {
        "action": action,
        "label": {"name": "twinfix"},
        "repository": {"full_name": repo},
        "issue": {
            "number": 42,
            "title": "下单 500：优惠券为空",
            "body": f"不用券下单直接 500\n\n{STACK}\n",
            "state": "open",
            "html_url": f"https://github.com/{repo}/issues/42",
            "user": {"login": "alice"},
            "labels": [{"name": n} for n in labels],
            **issue,
        },
    }


class TestPure:
    def test_signature(self) -> None:
        body = b'{"a":1}'
        good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert verify_signature(SECRET, body, good)
        assert not verify_signature(SECRET, body + b" ", good)
        assert not verify_signature("", body, good)
        assert not verify_signature(SECRET, body, None)

    def test_stacktrace_extraction(self) -> None:
        assert extract_stacktrace(f"intro\n```json\n{{}}\n```\n{STACK}") is not None
        assert "PriceCalculator.java:21" in (extract_stacktrace(STACK) or "")
        assert extract_stacktrace("no code here") is None

    def test_issue_to_signal(self) -> None:
        sig = issue_to_signal("demo", _payload("acme/x"), "twinfix")
        assert sig is not None and sig.source is SignalSource.ISSUE and sig.github_issue == 42
        assert sig.severity is Severity.S2 and sig.stacktrace and "NullPointerException" in sig.stacktrace
        assert sig.fingerprint == "github-issue:acme/x#42"

    @pytest.mark.parametrize(
        ("action", "labels", "extra"),
        [
            ("labeled", ("twinfix",), {"state": "closed"}),
            ("edited", ("twinfix",), {}),
            ("opened", ("bug",), {}),
            ("labeled", ("twinfix",), {"pull_request": {"url": "x"}}),
        ],
    )
    def test_not_triggered(self, action: str, labels: tuple[str, ...], extra: dict[str, Any]) -> None:
        p = _payload("acme/x", action, labels, **extra)
        if action == "opened":
            p["label"] = None
        assert issue_to_signal("demo", p, "twinfix") is None

    def test_other_label_added_does_not_trigger(self) -> None:
        p = _payload("acme/x")
        p["label"] = {"name": "S2"}
        assert issue_to_signal("demo", p, "twinfix") is None


def _gitlab_issue(
    repo: str,
    action: str = "open",
    labels: tuple[str, ...] = ("twinfix", "S2"),
    state: str = "opened",
    changes: dict[str, Any] | None = None,
    iid: int = 42,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "object_kind": "issue",
        "user": {"username": "alice"},
        "project": {"path_with_namespace": repo},
        "object_attributes": {
            "iid": iid,
            "title": "下单 500：优惠券为空",
            "description": f"不用券下单直接 500\n\n{STACK}\n",
            "state": state,
            "action": action,
            "url": f"https://gitlab.test/{repo}/-/issues/{iid}",
            "labels": [{"title": n} for n in labels],
        },
        "labels": [{"title": n} for n in labels],
    }
    if changes is not None:
        payload["changes"] = changes
    return payload


class TestGitLabParsing:
    def test_issue_to_signal(self) -> None:
        sig = gitlab_issue_to_signal("demo", _gitlab_issue("acme/shop"), "twinfix")
        assert sig is not None and sig.source is SignalSource.ISSUE and sig.github_issue == 42
        assert sig.severity is Severity.S2 and sig.stacktrace and "NullPointerException" in sig.stacktrace
        assert sig.fingerprint == "gitlab-issue:acme/shop#42"
        assert sig.raw["provider"] == "gitlab"

    def test_update_with_new_label_triggers(self) -> None:
        p = _gitlab_issue("acme/shop", action="update", changes={"labels": {"previous": [], "current": ["twinfix"]}})
        sig = gitlab_issue_to_signal("demo", p, "twinfix")
        assert sig is not None and sig.fingerprint == "gitlab-issue:acme/shop#42"

    @pytest.mark.parametrize(
        "payload",
        [
            _gitlab_issue("acme/shop", action="open", labels=("bug",)),
            # 标签变化事件，但触发标签已被移除
            _gitlab_issue(
                "acme/shop",
                action="update",
                labels=(),
                changes={"labels": {"previous": ["twinfix"], "current": []}},
            ),
            _gitlab_issue("acme/shop", action="update"),
            _gitlab_issue("acme/shop", action="open", state="closed"),
            _gitlab_issue("acme/shop", action="close"),
        ],
    )
    def test_not_triggered(self, payload: dict[str, Any]) -> None:
        assert gitlab_issue_to_signal("demo", payload, "twinfix") is None


async def _post_gitlab(
    c: httpx.AsyncClient,
    payload: dict[str, Any],
    event: str = "Issue Hook",
    token: str = SECRET,
) -> httpx.Response:
    return await c.post(
        "/webhooks/gitlab",
        json=payload,
        headers={"X-Gitlab-Event": event, "X-Gitlab-Token": token},
    )


@pytest.mark.integration
async def test_gitlab_issue_creates_one_incident(client: httpx.AsyncClient, harness: Harness) -> None:
    harness.dispatcher.auto = False
    project = await _repo_project(harness)
    r = await _post_gitlab(client, _gitlab_issue(project.repo))
    assert r.status_code == 202 and r.json()["status"] == "accepted", r.text
    incident_id = r.json()["incident_id"]
    # 同一 Issue 的重复标签事件归并
    again = await _post_gitlab(
        client,
        _gitlab_issue(project.repo, action="update", changes={"labels": {"previous": [], "current": ["twinfix"]}}),
    )
    assert again.json()["incident_id"] == incident_id and again.json()["created"] is False
    async with harness.rt.store.read() as s:
        inc = await s.scalar(select(Incident).where(Incident.id == incident_id))
    assert inc is not None and inc.github_issue == 42
    assert inc.title.startswith("下单 500")


@pytest.mark.integration
async def test_gitlab_webhook_auth_and_ignored_events(client: httpx.AsyncClient, harness: Harness) -> None:
    project = await _repo_project(harness)
    body = json.dumps(_gitlab_issue(project.repo)).encode()
    # 缺 token / 错 token 都拒绝
    assert (await client.post("/webhooks/gitlab", content=body)).status_code == 401
    assert (await _post_gitlab(client, _gitlab_issue(project.repo), token="wrong")).status_code == 401
    assert (await _post_gitlab(client, _gitlab_issue(project.repo), event="Push Hook")).json()["status"] == "ignored"
    assert (await _post_gitlab(client, _gitlab_issue("nobody/else"))).json()["status"] == "ignored"
    assert (await _post_gitlab(client, _gitlab_issue(project.repo, labels=("bug",)))).json()["status"] == "ignored"
    bad = await client.post(
        "/webhooks/gitlab",
        content=b"not json",
        headers={"X-Gitlab-Event": "Issue Hook", "X-Gitlab-Token": SECRET},
    )
    assert bad.status_code == 400


def _gitlab_mr_payload(repo: str, iid: int, action: str = "approved", username: str = "alice") -> dict[str, Any]:
    return {
        "object_kind": "merge_request",
        "user": {"username": username},
        "project": {"path_with_namespace": repo},
        "object_attributes": {"iid": iid, "action": action},
    }


@pytest.mark.integration
async def test_gitlab_mr_approved_counts_as_merge_approval(client: httpx.AsyncClient, harness: Harness) -> None:
    project, iid, number = await _review_incident(harness)
    r = await _post_gitlab(client, _gitlab_mr_payload(project.repo, number), event="Merge Request Hook")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "accepted" and body["review"] == "approved" and body["reviewer"] == "alice"
    assert await _state(harness, iid) is IncidentState.CLOSED
    async with harness.rt.store.read() as s:
        approval = await s.get(Approval, body["approval_id"])
        merged = await s.scalar(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == iid, IncidentEvent.to_state == IncidentState.MERGING)
            .order_by(IncidentEvent.id.desc())
        )
    assert approval is not None and approval.status is ApprovalStatus.APPROVED
    assert approval.payload["decided_actor"] == "gitlab:alice"
    assert merged is not None and merged.actor == "gitlab:alice"


@pytest.mark.integration
async def test_gitlab_mr_unapproved_rejects_without_comment(client: httpx.AsyncClient, harness: Harness) -> None:
    """GitLab 撤回审批没有附言：驳回后直接转人工，不再自动进入下一轮修复。"""
    project, iid, number = await _review_incident(harness)
    r = await _post_gitlab(
        client, _gitlab_mr_payload(project.repo, number, "unapproved", "bob"), event="Merge Request Hook"
    )
    assert r.json()["status"] == "accepted", r.text
    assert await _state(harness, iid) is IncidentState.NEEDS_HUMAN
    async with harness.rt.store.read() as s:
        rejected = await s.scalar(
            select(Approval).where(Approval.incident_id == iid, Approval.status == ApprovalStatus.REJECTED)
        )
        pending = await s.scalar(
            select(Approval).where(
                Approval.incident_id == iid,
                Approval.action == ApprovalAction.MERGE,
                Approval.status == ApprovalStatus.PENDING,
            )
        )
    assert rejected is not None and rejected.payload["decided_actor"] == "gitlab:bob"
    assert pending is None


ALERT = {
    "status": "firing",
    "labels": {
        "alertname": "HighErrorRate",
        "severity": "critical",
        "service": "order",
        "env": "prod",
        "project": "demo",
    },
    "annotations": {"summary": "下单接口 5xx 升高", "description": f"错误率 12%\n\n{STACK}"},
    "startsAt": "2026-10-25T01:00:00Z",
    "fingerprint": "abc123",
}


class TestAlertParsing:
    def test_token(self) -> None:
        assert verify_token(SECRET, f"Bearer {SECRET}", None)
        assert verify_token(SECRET, None, SECRET)
        assert not verify_token(SECRET, "Bearer wrong", None)
        assert not verify_token("", "Bearer x", None)
        assert not verify_token(SECRET, "Basic x", None)

    def test_alert_to_signal(self) -> None:
        sig = alert_to_signal("demo", ALERT)
        assert sig is not None and sig.source is SignalSource.ALERT
        assert sig.severity is Severity.S1 and sig.env is Env.PROD and sig.service == "order"
        assert sig.fingerprint == "alert:demo:abc123"
        assert "labels: env=prod" in (sig.message or "")
        assert sig.stacktrace and "NullPointerException" in sig.stacktrace

    def test_resolved_is_ignored(self) -> None:
        assert alert_to_signal("demo", {**ALERT, "status": "resolved"}) is None
        assert alert_to_signal("demo", {"labels": {}}, group_status="resolved") is None

    def test_fingerprint_falls_back_to_labels(self) -> None:
        alert = {**ALERT, "fingerprint": ""}
        a = alert_to_signal("demo", alert)
        b = alert_to_signal("demo", {**alert, "annotations": {"summary": "改了文案"}})
        assert a is not None and b is not None and a.fingerprint == b.fingerprint


@pytest.fixture
async def client(harness: Harness, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.state.settings = settings.model_copy(
        update={
            "github_webhook_secret": SecretStr(SECRET),
            "gitlab_webhook_secret": SecretStr(SECRET),
            "alert_webhook_secret": SecretStr(SECRET),
            "jenkins_webhook_secret": SecretStr(SECRET),
        }
    )
    app.state.store = harness.rt.store
    app.state.service = IncidentService(harness.rt.store, harness.dispatcher)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/api") as c:
        yield c


async def _post(
    c: httpx.AsyncClient, payload: dict[str, Any], event: str = "issues", secret: str = SECRET
) -> httpx.Response:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-GitHub-Event": event, "X-Hub-Signature-256": sig, "Content-Type": "application/json"}
    return await c.post("/webhooks/github", content=body, headers=headers)


async def _repo_project(h: Harness) -> Project:
    repo = f"acme/shop-{new_id()[-6:].lower()}"
    async with h.rt.store.tx() as tx:
        p = Project(name=f"gh-{new_id()[-6:].lower()}", repo=repo, branch_map={"test": "dev"}, config={})
        tx.session.add(p)
    return p


@pytest.mark.integration
async def test_labeled_issue_creates_one_incident(client: httpx.AsyncClient, harness: Harness) -> None:
    harness.dispatcher.auto = False
    project = await _repo_project(harness)
    r = await _post(client, _payload(project.repo))
    assert r.status_code == 202 and r.json()["status"] == "accepted", r.text
    incident_id = r.json()["incident_id"]
    # 反复打标签归并到同一事故
    r2 = await _post(client, _payload(project.repo))
    assert r2.json()["incident_id"] == incident_id and r2.json()["created"] is False
    async with harness.rt.store.read() as s:
        inc = await s.scalar(select(Incident).where(Incident.id == incident_id))
    assert inc is not None and inc.github_issue == 42


@pytest.mark.integration
async def test_rejects_bad_signature_and_ignores_unknown(client: httpx.AsyncClient, harness: Harness) -> None:
    project = await _repo_project(harness)
    r = await _post(client, _payload(project.repo), secret="wrong")
    assert r.status_code == 401
    assert (await _post(client, {"zen": "hi"}, event="ping")).json()["status"] == "pong"
    assert (await _post(client, {}, event="push")).json()["status"] == "ignored"
    assert (await _post(client, _payload("nobody/else"))).json()["status"] == "ignored"


async def _post_alert(
    c: httpx.AsyncClient, payload: dict[str, Any], token: str = SECRET, path: str = "alertmanager"
) -> httpx.Response:
    return await c.post(f"/webhooks/{path}", json=payload, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.integration
async def test_alertmanager_firing_creates_and_dedupes(client: httpx.AsyncClient, harness: Harness) -> None:
    harness.dispatcher.auto = False
    project = await _repo_project(harness)
    payload = {
        "status": "firing",
        "commonLabels": {"project": project.name},
        "alerts": [ALERT],
    }
    r = await _post_alert(client, payload)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "accepted" and body["project"] == project.name
    incident_id = body["accepted"][0]["incident_id"]
    assert body["accepted"][0]["created"] is True

    # Alertmanager repeat_interval 会重复投递同一 fingerprint，归并而不是新建
    again = (await _post_alert(client, payload)).json()
    assert again["accepted"][0]["incident_id"] == incident_id
    assert again["accepted"][0]["created"] is False

    async with harness.rt.store.read() as s:
        inc = await s.scalar(select(Incident).where(Incident.id == incident_id))
    assert inc is not None
    assert inc.severity is Severity.S1 and inc.env is Env.PROD and inc.service == "order"


@pytest.mark.integration
async def test_alertmanager_grafana_alias_and_filters(client: httpx.AsyncClient, harness: Harness) -> None:
    harness.dispatcher.auto = False
    project = await _repo_project(harness)
    firing = {"status": "firing", "commonLabels": {"project": project.name}, "alerts": [ALERT]}
    # Grafana 走同一份载荷格式
    assert (await _post_alert(client, firing, path="grafana")).json()["status"] == "accepted"

    resolved = {
        "status": "resolved",
        "commonLabels": {"project": project.name},
        "alerts": [{**ALERT, "status": "resolved"}],
    }
    body = (await _post_alert(client, resolved)).json()
    assert body["status"] == "ignored" and body["ignored"] == 1

    unknown = {"status": "firing", "commonLabels": {"project": "nope"}, "alerts": [ALERT]}
    assert (await _post_alert(client, unknown)).json()["status"] == "ignored"
    assert (await _post_alert(client, {"alerts": []})).json()["status"] == "ignored"
    assert (await client.post("/webhooks/alertmanager", json=firing)).status_code == 401
    assert (await _post_alert(client, firing, token="wrong")).status_code == 401


async def _twin(harness: Harness) -> tuple[str, str]:
    """建一个事故和一条 CREATING 状态的孪生记录，返回 (incident_id, compose_project)。"""
    project = await _repo_project(harness)
    res = await harness.service.ingest(
        SignalIn(project=project.name, source=SignalSource.ALERT, title="孪生部署回调测试")
    )
    name = f"twinfix-{new_id()[-6:].lower()}"
    async with harness.rt.store.tx() as tx:
        tx.session.add(Twin(incident_id=res.incident_id, compose_project=name, status=TwinStatus.CREATING, urls={}))
    return res.incident_id, name


async def _post_twin(c: httpx.AsyncClient, payload: dict[str, Any], token: str = SECRET) -> httpx.Response:
    return await c.post("/webhooks/jenkins", json=payload, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.integration
async def test_jenkins_ready_marks_twin_and_is_idempotent(client: httpx.AsyncClient, harness: Harness) -> None:
    harness.dispatcher.auto = False
    incident_id, name = await _twin(harness)
    payload = {
        "twin": name,
        "sha": "abc123",
        "status": "ready",
        "build": "17",
        "urls": {"api": "http://127.0.0.1:18080"},
    }
    r = await _post_twin(client, payload)
    assert r.status_code == 202, r.text
    assert r.json() == {"status": "ready", "twin": name, "incident_id": incident_id, "updated": True}

    async with harness.rt.store.read() as s:
        twin = await s.scalar(select(Twin).where(Twin.compose_project == name))
        ev = await s.scalar(
            select(IncidentEvent).where(IncidentEvent.incident_id == incident_id, IncidentEvent.kind == "twin_ready")
        )
    assert twin is not None and twin.status is TwinStatus.READY
    assert twin.commit_sha == "abc123" and twin.urls == {"api": "http://127.0.0.1:18080"}
    assert ev is not None and ev.actor == "jenkins" and ev.payload["build"] == "17"

    # 重复回调（Jenkins 重试）不再产生事件
    again = (await _post_twin(client, payload)).json()
    assert again["updated"] is False


@pytest.mark.integration
async def test_jenkins_failure_and_rejections(client: httpx.AsyncClient, harness: Harness) -> None:
    harness.dispatcher.auto = False
    _, name = await _twin(harness)
    body = json.dumps({"twin": name, "status": "ready"}).encode()
    assert (await client.post("/webhooks/jenkins", content=body)).status_code == 401
    assert (await _post_twin(client, {"twin": name, "status": "ready"}, token="wrong")).status_code == 401
    assert (await _post_twin(client, {"status": "ready"})).status_code == 400
    assert (await _post_twin(client, {"twin": name, "status": "done"})).status_code == 400
    assert (await _post_twin(client, {"twin": "twinfix-nope", "status": "failed"})).status_code == 404
    bad_json = await client.post(
        "/webhooks/jenkins", content=b"not json", headers={"Authorization": f"Bearer {SECRET}"}
    )
    assert bad_json.status_code == 400
    assert (await _post_twin(client, {"twin": name, "status": "ready", "urls": "nope"})).status_code == 400

    r = await _post_twin(client, {"twin": name, "status": "failed", "build": "18"})
    assert r.status_code == 202 and r.json()["updated"] is True
    async with harness.rt.store.read() as s:
        twin = await s.scalar(select(Twin).where(Twin.compose_project == name))
    assert twin is not None and twin.status is TwinStatus.FAILED


async def _review_incident(h: Harness, config: dict[str, Any] | None = None) -> tuple[Project, str, int]:
    """跑完 stub Agent 的完整流程，拿一个停在 AWAITING_REVIEW、已有 PR 的事故。"""
    project = await h.project(**(config or {}))
    # 每个用例用独立仓库名：webhook 靠 repository.full_name 找项目
    async with h.rt.store.tx() as tx:
        row = await tx.session.get(Project, project.id)
        assert row is not None
        row.repo = f"acme/review-{new_id()[-6:].lower()}"
        project = row
    res = await h.service.ingest(
        SignalIn(
            project=project.name,
            source=SignalSource.MANUAL,
            title="PR 评审回调",
            service="order",
            stacktrace="java.lang.IllegalStateException: stock < 0\n\tat com.acme.Stock.deduct(Stock.java:42)",
        )
    )
    assert res.created
    async with h.rt.store.read() as s:
        inc = await s.get(Incident, res.incident_id)
    assert inc is not None and inc.state is IncidentState.AWAITING_REVIEW and inc.pr_number
    return project, res.incident_id, inc.pr_number


def _review_payload(repo: str, number: int, state: str = "approved", login: str = "alice", body: str = "") -> dict:
    return {
        "action": "submitted",
        "review": {"state": state, "body": body, "user": {"login": login}},
        "pull_request": {"number": number},
        "repository": {"full_name": repo},
    }


async def _state(h: Harness, incident_id: str) -> IncidentState:
    async with h.rt.store.read() as s:
        inc = await s.get(Incident, incident_id)
    assert inc is not None
    return inc.state


@pytest.mark.integration
async def test_pr_review_approved_counts_as_merge_approval(client: httpx.AsyncClient, harness: Harness) -> None:
    project, iid, number = await _review_incident(harness)
    r = await _post(client, _review_payload(project.repo, number, "approved", "alice"), event="pull_request_review")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "accepted" and body["review"] == "approved" and body["reviewer"] == "alice"
    assert await _state(harness, iid) is IncidentState.CLOSED

    async with harness.rt.store.read() as s:
        approval = await s.get(Approval, body["approval_id"])
        merged = await s.scalar(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == iid, IncidentEvent.to_state == IncidentState.MERGING)
            .order_by(IncidentEvent.id.desc())
        )
    assert approval is not None and approval.status is ApprovalStatus.APPROVED
    # 非本地账号来源没有 user_id，审计 actor 记为 github:login
    assert approval.payload["decided_actor"] == "github:alice"
    assert merged is not None and merged.actor == "github:alice"


@pytest.mark.integration
async def test_pr_review_needs_two_reviewers_and_rejects_duplicates(
    client: httpx.AsyncClient, harness: Harness
) -> None:
    project, iid, number = await _review_incident(harness, {"required_merge_approvals": 2})
    first = await _post(client, _review_payload(project.repo, number, "approved", "alice"), event="pull_request_review")
    assert first.json()["status"] == "accepted", first.text
    assert await _state(harness, iid) is IncidentState.AWAITING_REVIEW
    async with harness.rt.store.read() as s:
        approval = await s.scalar(select(Approval).where(Approval.id == first.json()["approval_id"]))
    assert approval is not None and approval.status is ApprovalStatus.PENDING
    assert [v["actor"] for v in approval.payload["approvals"]] == ["github:alice"]

    # 同一位 reviewer 再点一次 approve 不能凑票
    dup = await _post(client, _review_payload(project.repo, number, "approved", "alice"), event="pull_request_review")
    assert dup.json()["status"] == "ignored" and "already voted" in dup.json()["reason"]
    assert await _state(harness, iid) is IncidentState.AWAITING_REVIEW

    second = await _post(client, _review_payload(project.repo, number, "approved", "bob"), event="pull_request_review")
    assert second.json()["status"] == "accepted", second.text
    assert await _state(harness, iid) is IncidentState.CLOSED
    async with harness.rt.store.read() as s:
        merged = await s.scalar(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == iid, IncidentEvent.to_state == IncidentState.MERGING)
            .order_by(IncidentEvent.id.desc())
        )
    assert merged is not None and merged.actor == "github:bob"


@pytest.mark.integration
async def test_pr_review_changes_requested_sends_back_to_fixing(client: httpx.AsyncClient, harness: Harness) -> None:
    project, iid, number = await _review_incident(harness)
    async with harness.rt.store.read() as s:
        old = await s.scalar(
            select(Approval).where(Approval.incident_id == iid, Approval.status == ApprovalStatus.PENDING)
        )
    assert old is not None
    r = await _post(
        client,
        _review_payload(project.repo, number, "changes_requested", "bob", body="补丁漏了并发锁"),
        event="pull_request_review",
    )
    assert r.json()["status"] == "accepted", r.text
    # 驳回附意见 → 回修复重跑，stub 又走回评审，等待新的审批
    assert await _state(harness, iid) is IncidentState.AWAITING_REVIEW
    async with harness.rt.store.read() as s:
        rejected = await s.get(Approval, old.id)
        new = await s.scalar(
            select(Approval).where(Approval.incident_id == iid, Approval.status == ApprovalStatus.PENDING)
        )
    assert rejected is not None and rejected.status is ApprovalStatus.REJECTED
    assert rejected.comment == "补丁漏了并发锁" and rejected.payload["decided_actor"] == "github:bob"
    assert new is not None and new.id != old.id


@pytest.mark.integration
async def test_pr_review_ignores_unrelated_payloads(client: httpx.AsyncClient, harness: Harness) -> None:
    project, iid, number = await _review_incident(harness)
    # 普通评论不投票，也不打扰
    commented = _review_payload(project.repo, number, "commented", "alice")
    assert (await _post(client, commented, event="pull_request_review")).json() == {
        "status": "ignored",
        "reason": "review state 'commented'",
    }
    # 没有 number
    no_number = _review_payload(project.repo, number)
    no_number["pull_request"] = {}
    assert "number missing" in (await _post(client, no_number, event="pull_request_review")).json()["reason"]
    # 仓库没有对应项目 / PR 号对不上
    assert (
        "no project"
        in (await _post(client, _review_payload("nobody/else", number), event="pull_request_review")).json()["reason"]
    )
    assert (
        "no incident"
        in (await _post(client, _review_payload(project.repo, number + 1), event="pull_request_review")).json()[
            "reason"
        ]
    )
    assert await _state(harness, iid) is IncidentState.AWAITING_REVIEW


@pytest.mark.integration
async def test_issue_from_extra_repo_matches_project(client: httpx.AsyncClient, harness: Harness) -> None:
    harness.dispatcher.auto = False
    extra = f"acme/inventory-{new_id()[-6:].lower()}"
    project = await harness.project(repos=[{"name": "inventory", "repo": extra}])

    r = await _post(client, _payload(extra))

    assert r.status_code == 202 and r.json()["status"] == "accepted", r.text
    async with harness.rt.store.read() as s:
        inc = await s.scalar(select(Incident).where(Incident.id == r.json()["incident_id"]))
    assert inc is not None and inc.project_id == project.id
