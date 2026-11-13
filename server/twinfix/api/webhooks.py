"""Webhook 入口（docs/04-interfaces.md §5）。各自用来源的签名/令牌鉴权，不走 JWT。

GitHub：Issue 被打上触发标签（默认 `twinfix`）时上报为信号；正文视为不可信数据，原样进入 signals。
项目按 `repository.full_name` 与 Project.repo 匹配（GitHub App 模式下 repo 即 "owner/name"）。

GitLab：项目 Webhook 的 Secret token 鉴权（`X-Gitlab-Token`）。Issue Hook 按触发标签上报，
项目按 `project.path_with_namespace` 匹配（支持嵌套 group）；Merge Request Hook 的
approved / unapproved 同步为审批票/驳回。

Alertmanager / Grafana：同一份 `alerts[]` 载荷。只接 firing；项目取 `twinfix_project` / `project` 标签，
只有一个项目时直接用它。调用方用 Bearer token（`TWINFIX_ALERT_WEBHOOK_SECRET`）鉴权。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from twinfix.config import Settings
from twinfix.domain.enums import (
    ApprovalAction,
    ApprovalStatus,
    Env,
    IncidentState,
    Severity,
    SignalSource,
    TwinStatus,
    UserRole,
)
from twinfix.domain.models import Approval, Incident, Project, Twin
from twinfix.domain.repo import record_event
from twinfix.multirepo import repo_specs
from twinfix.services import ConflictError, IncidentService, NotFoundError, SignalIn

if TYPE_CHECKING:
    from twinfix.db import Store

router = APIRouter(prefix="/api/webhooks")

DEFAULT_LABEL = "twinfix"
_SEVERITY_LABEL = re.compile(r"^(?:severity[:/ ]|sev[:/ ]?)?(S[1-4])$", re.IGNORECASE)
# Issue 正文里的 ```...``` 代码块中，第一个看起来像 Java 栈的作为 stacktrace
_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_JAVA_FRAME = re.compile(r"^\s*at [\w$.]+\(.*\)\s*$", re.MULTILINE)


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def verify_token(secret: str, authorization: str | None, token_header: str | None) -> bool:
    """Alertmanager 没有 HMAC，约定用 Bearer token（或 X-TwinFix-Token）鉴权。"""
    if not secret:
        return False
    presented = (token_header or "").strip()
    if not presented and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()
    return bool(presented) and hmac.compare_digest(secret, presented)


def _labels(issue: dict[str, Any]) -> list[str]:
    return [str(lb.get("name", "")) for lb in issue.get("labels") or [] if isinstance(lb, dict)]


def extract_stacktrace(text: str) -> str | None:
    for block in _FENCE.findall(text or ""):
        if _JAVA_FRAME.search(block):
            return str(block).strip()
    return None


def issue_to_signal(project: str, payload: dict[str, Any], trigger_label: str) -> SignalIn | None:
    """把 issues 事件转换成信号；不该触发时返回 None。"""
    action = payload.get("action")
    issue = payload.get("issue") or {}
    if issue.get("pull_request") or issue.get("state") == "closed":
        return None
    labels = _labels(issue)
    if action == "labeled":
        if (payload.get("label") or {}).get("name") != trigger_label:
            return None
    elif action == "opened":
        if trigger_label not in labels:
            return None
    else:
        return None
    severity = next(
        (Severity(m.group(1).upper()) for lb in labels if (m := _SEVERITY_LABEL.match(lb))),
        None,
    )
    body = str(issue.get("body") or "")
    number = int(issue["number"])
    return SignalIn(
        project=project,
        source=SignalSource.ISSUE,
        title=str(issue.get("title") or f"issue #{number}")[:500],
        severity=severity,
        message=body[:100_000] or None,
        stacktrace=extract_stacktrace(body),
        # 同一 Issue 重复触发（反复打标签）归并到同一事故
        fingerprint=f"github-issue:{payload.get('repository', {}).get('full_name')}#{number}",
        github_issue=number,
        raw={"url": issue.get("html_url"), "author": (issue.get("user") or {}).get("login"), "labels": labels},
    )


def gitlab_issue_to_signal(project: str, payload: dict[str, Any], trigger_label: str) -> SignalIn | None:
    """GitLab Issue Hook → 信号。

    Issue Hook 的 ``object_attributes.action``：open / reopen 且带触发标签时上报；
    update 只在标签发生变化且已带触发标签时上报（普通编辑不重复触发）。
    同一 Issue 的重复事件按指纹归并到同一事故。
    """
    oa = payload.get("object_attributes") or {}
    if oa.get("state") == "closed":
        return None
    raw_labels = oa.get("labels") or payload.get("labels") or []
    labels = [str(lb.get("title", "")) for lb in raw_labels if isinstance(lb, dict)]
    action = str(oa.get("action") or payload.get("action") or "").lower()
    if action in ("open", "opened", "reopen", "reopened"):
        if trigger_label not in labels:
            return None
    elif action == "update":
        if trigger_label not in labels or not (payload.get("changes") or {}).get("labels"):
            return None
    else:
        return None
    severity = next(
        (Severity(m.group(1).upper()) for lb in labels if (m := _SEVERITY_LABEL.match(lb))),
        None,
    )
    body = str(oa.get("description") or "")
    iid = int(oa["iid"])
    path = str((payload.get("project") or {}).get("path_with_namespace") or "")
    return SignalIn(
        project=project,
        source=SignalSource.ISSUE,
        title=str(oa.get("title") or f"issue #{iid}")[:500],
        severity=severity,
        message=body[:100_000] or None,
        stacktrace=extract_stacktrace(body),
        fingerprint=f"gitlab-issue:{path}#{iid}",
        github_issue=iid,
        raw={
            "url": oa.get("url"),
            "author": (payload.get("user") or {}).get("username"),
            "labels": labels,
            "provider": "gitlab",
        },
    )


_ALERT_SEVERITY = {
    "critical": Severity.S1,
    "fatal": Severity.S1,
    "page": Severity.S1,
    "error": Severity.S2,
    "high": Severity.S2,
    "warning": Severity.S3,
    "medium": Severity.S3,
    "info": Severity.S4,
    "low": Severity.S4,
}
_ALERT_ENV = {
    "prod": Env.PROD,
    "production": Env.PROD,
    "release": Env.RELEASE,
    "staging": Env.RELEASE,
    "stage": Env.RELEASE,
    "test": Env.TEST,
    "dev": Env.TEST,
}


def _lower_map(value: Any) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _alert_severity(raw: str | None) -> Severity | None:
    if not raw:
        return None
    value = raw.strip()
    if value.upper() in Severity.__members__:
        return Severity(value.upper())
    return _ALERT_SEVERITY.get(value.lower())


def alert_to_signal(project: str, alert: dict[str, Any], group_status: str = "firing") -> SignalIn | None:
    """把 Alertmanager / Grafana 的一条 alert 转成信号；resolved / 非 firing 返回 None。"""
    if str(alert.get("status") or group_status or "firing").lower() != "firing":
        return None
    labels = _lower_map(alert.get("labels"))
    annotations = _lower_map(alert.get("annotations"))
    name = labels.get("alertname") or annotations.get("title") or "alert"
    summary = annotations.get("summary") or annotations.get("message") or name
    description = annotations.get("description") or annotations.get("message") or summary
    context = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()) if k != "alertname")
    message = f"{description}\n\nlabels: {context}" if context else description
    stacktrace = annotations.get("stacktrace") or annotations.get("trace") or extract_stacktrace(description)
    # Alertmanager 的 fingerprint 在同 label 集上稳定；Grafana 没有时按 label 集自己算，重复告警才能归并
    fp = str(alert.get("fingerprint") or "").strip()
    if not fp:
        fp = hashlib.sha256(json.dumps(labels, sort_keys=True).encode()).hexdigest()[:16]
    return SignalIn(
        project=project,
        source=SignalSource.ALERT,
        title=summary[:500],
        service=labels.get("service") or labels.get("app") or labels.get("job") or None,
        env=_ALERT_ENV.get(labels.get("env") or labels.get("environment") or "", Env.TEST),
        severity=_alert_severity(labels.get("severity")),
        message=message[:100_000] or None,
        stacktrace=stacktrace,
        fingerprint=f"alert:{project}:{fp}",
        raw={
            "alertname": name,
            "labels": labels,
            "annotations": annotations,
            "starts_at": alert.get("startsAt"),
            "generator_url": alert.get("generatorURL") or alert.get("dashboardURL") or alert.get("panelURL"),
        },
    )


async def _match_alert_project(store: Store, payload: dict[str, Any]) -> Project | None:
    """项目取标签 `twinfix_project` / `project`；没有标签且全库只有一个项目时用它。"""
    labels = _lower_map(payload.get("commonLabels"))
    alerts = payload.get("alerts")
    if isinstance(alerts, list) and alerts and isinstance(alerts[0], dict):
        labels = {**_lower_map(alerts[0].get("labels")), **labels}
    name = labels.get("twinfix_project") or labels.get("project")
    async with store.read() as s:
        if name:
            return await s.scalar(select(Project).where(Project.name == name))
        projects = (await s.scalars(select(Project))).all()
        return projects[0] if len(projects) == 1 else None


@router.post("/alertmanager", status_code=202)
@router.post("/grafana", status_code=202)
async def alertmanager_webhook(
    request: Request,
    authorization: str = Header(""),
    x_twinfix_token: str = Header(""),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    secret = settings.alert_webhook_secret.get_secret_value()
    if not verify_token(secret, authorization, x_twinfix_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing alert webhook token")
    try:
        payload = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON") from exc
    alerts = payload.get("alerts") if isinstance(payload, dict) else None
    if not isinstance(alerts, list):
        return {"status": "ignored", "reason": "payload has no alerts[]"}

    project = await _match_alert_project(request.app.state.store, payload)
    if project is None:
        return {"status": "ignored", "reason": "no matching project (set label twinfix_project or project)"}

    svc: IncidentService = request.app.state.service
    group_status = str(payload.get("status") or "firing")
    accepted: list[dict[str, Any]] = []
    ignored = 0
    for alert in alerts:
        sig = alert_to_signal(project.name, alert if isinstance(alert, dict) else {}, group_status)
        if sig is None:
            ignored += 1
            continue
        try:
            res = await svc.ingest(sig)
        except (NotFoundError, ConflictError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        accepted.append({"incident_id": res.incident_id, "created": res.created, "fingerprint": sig.fingerprint})
    return {
        "status": "accepted" if accepted else "ignored",
        "project": project.name,
        "accepted": accepted,
        "ignored": ignored,
    }


@router.post("/jenkins", status_code=202)
async def jenkins_webhook(
    request: Request,
    authorization: str = Header(""),
    x_twinfix_token: str = Header(""),
) -> dict[str, Any]:
    """孪生部署任务回调（deploy/jenkins/twinfix-twin-deploy.Jenkinsfile 的 post.always）。

    载荷：``{"twin": "twinfix-abc123", "sha": "...", "status": "ready"|"failed", "build": "..."}``，
    鉴权用 ``Authorization: Bearer $TWINFIX_JENKINS_WEBHOOK_SECRET``。按 compose 项目名找到 Twin，
    更新状态并记一条事件；重复回调幂等（``updated=false``）。
    """
    settings: Settings = request.app.state.settings
    secret = settings.jenkins_webhook_secret.get_secret_value()
    if not verify_token(secret, authorization, x_twinfix_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing jenkins webhook token")
    try:
        payload = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "payload must be an object")
    twin_name = str(payload.get("twin") or "").strip()
    status_text = str(payload.get("status") or "").strip().lower()
    if not twin_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing twin")
    if status_text not in ("ready", "failed"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "status must be ready or failed")
    sha = str(payload.get("sha") or "").strip()
    build = str(payload.get("build") or "").strip()
    urls = payload.get("urls")
    if urls is not None and not isinstance(urls, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "urls must be an object")

    async with request.app.state.store.tx() as tx:
        twin = await tx.session.scalar(select(Twin).where(Twin.compose_project == twin_name).with_for_update())
        if twin is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown twin {twin_name}")
        target = TwinStatus.READY if status_text == "ready" else TwinStatus.FAILED
        new_urls = {str(k): str(v) for k, v in urls.items()} if urls else None
        updated = (
            twin.status is not target
            or bool(sha and twin.commit_sha != sha)
            or bool(new_urls is not None and new_urls != twin.urls)
        )
        if status_text == "ready":
            twin.status = TwinStatus.READY
            if sha:
                twin.commit_sha = sha
            if new_urls is not None:
                twin.urls = new_urls
        else:
            twin.status = TwinStatus.FAILED
        if updated:
            await record_event(
                tx,
                twin.incident_id,
                "twin_ready" if status_text == "ready" else "twin_failed",
                actor="jenkins",
                payload={"compose_project": twin_name, "sha": sha, "build": build, "via": "jenkins"},
            )
    return {"status": status_text, "twin": twin_name, "incident_id": twin.incident_id, "updated": updated}


async def _project_for_repo(session: Any, repo: str) -> tuple[Project | None, str | None]:
    """按主仓库或项目声明的附加仓库匹配项目；第二个返回值是命中的附加仓库名。

    PR 评审不走这里：TwinFix 只会在主仓库开 PR，附加仓库的 PR 编号不能拿来关联事故。
    """
    project: Project | None = await session.scalar(select(Project).where(Project.repo == repo))
    if project is not None:
        return project, None
    candidates: list[Project] = list((await session.scalars(select(Project))).all())
    for candidate in candidates:
        for spec in repo_specs(candidate):
            if spec.repo == repo:
                return candidate, spec.name
    return None, None


async def _apply_review_decision(
    request: Request,
    *,
    repo: str,
    number: int,
    approve: bool,
    comment: str | None,
    actor: str,
    reviewer: str,
) -> dict[str, Any]:
    """GitHub / GitLab 共用的评审回调：按 repo + PR 编号找等待评审的事故与合并审批。

    审批仍走 IncidentService.decide，因此受同样的状态校验、分支保护与多人审批规则约束。
    """
    store: Store = request.app.state.store
    async with store.read() as s:
        project = await s.scalar(select(Project).where(Project.repo == repo))
        if project is None:
            return {"status": "ignored", "reason": f"no project for repository {repo!r}"}
        incident = await s.scalar(
            select(Incident)
            .where(
                Incident.project_id == project.id,
                Incident.pr_number == number,
                Incident.state == IncidentState.AWAITING_REVIEW,
            )
            .order_by(Incident.created_at.desc())
        )
        if incident is None:
            return {"status": "ignored", "reason": f"no incident awaiting review for PR #{number}"}
        approval = await s.scalar(
            select(Approval)
            .where(
                Approval.incident_id == incident.id,
                Approval.action == ApprovalAction.MERGE,
                Approval.status == ApprovalStatus.PENDING,
            )
            .order_by(Approval.created_at.desc())
        )
    if approval is None:
        return {"status": "ignored", "reason": "no pending merge approval"}

    svc: IncidentService = request.app.state.service
    try:
        await svc.decide(
            approval.id,
            None,
            UserRole.MAINTAINER,
            approve=approve,
            comment=comment,
            actor=actor,
        )
    except (ConflictError, NotFoundError, PermissionError) as exc:
        return {"status": "ignored", "reason": str(exc)}
    return {
        "status": "accepted",
        "incident_id": incident.id,
        "approval_id": approval.id,
        "review": "approved" if approve else "changes_requested",
        "reviewer": reviewer,
    }


async def _handle_pr_review(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """GitHub PR review → 审批票：approve 计一票，changes_requested 直接驳回（附 review 正文）。

    TwinFix 自己开的 PR 与事故通过 project.repo + incident.pr_number 关联。
    """
    review = payload.get("review") or {}
    pr = payload.get("pull_request") or {}
    state = str(review.get("state") or "").lower()
    if state not in ("approved", "changes_requested"):
        return {"status": "ignored", "reason": f"review state {state!r}"}
    number = pr.get("number")
    if not isinstance(number, int):
        return {"status": "ignored", "reason": "pull_request.number missing"}
    repo = str((payload.get("repository") or {}).get("full_name") or "")
    login = str((review.get("user") or {}).get("login") or "github")
    return await _apply_review_decision(
        request,
        repo=repo,
        number=number,
        approve=state == "approved",
        comment=str(review.get("body") or "")[:5000] or None,
        actor=f"github:{login}",
        reviewer=login,
    )


@router.post("/github", status_code=202)
async def github_webhook(
    request: Request,
    x_github_event: str = Header(""),
    x_hub_signature_256: str | None = Header(None),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    body = await request.body()
    secret = settings.github_webhook_secret.get_secret_value()
    if not verify_signature(secret, body, x_hub_signature_256):
        # 未配置密钥也拒绝：webhook 是公网入口，不能因为本地关了 JWT 就裸奔
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing webhook signature")
    if x_github_event == "ping":
        return {"status": "pong"}
    if x_github_event == "pull_request_review":
        return await _handle_pr_review(request, json.loads(body))
    if x_github_event != "issues":
        return {"status": "ignored", "reason": f"event {x_github_event!r}"}

    payload = json.loads(body)
    repo = str((payload.get("repository") or {}).get("full_name") or "")
    async with request.app.state.store.read() as s:
        project, source_repo = await _project_for_repo(s, repo)
    if project is None:
        return {"status": "ignored", "reason": f"no project for repository {repo!r}"}

    label = str(project.config.get("issue_label") or DEFAULT_LABEL)
    sig = issue_to_signal(project.name, payload, label)
    if sig is None:
        return {"status": "ignored", "reason": "issue not labeled for TwinFix"}
    if source_repo:
        # 附加仓库来的 Issue：明确告诉编排器要加载哪个仓库，而不只靠 service 映射
        sig = replace(sig, meta={**(sig.meta or {}), "repos": [source_repo]})
    svc: IncidentService = request.app.state.service
    try:
        res = await svc.ingest(sig)
    except (NotFoundError, ConflictError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"status": "accepted", **res.__dict__}


async def _handle_gitlab_mr_event(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """GitLab Merge Request Hook → 审批票。

    - ``approved`` / ``approval``：计一票；
    - ``unapproved`` / ``unapproval``：驳回当前合并审批。GitLab 事件不携带附言，而驳回无意见会
      直接转人工（NEEDS_HUMAN）；GitLab 在推送新提交后也可能自动重置审批并触发该事件，
      因此宁可停下来等人确认，也不在审批被撤回后继续自动合并。
    """
    oa = payload.get("object_attributes") or {}
    action = str(oa.get("action") or "").lower()
    if action not in ("approved", "approval", "unapproved", "unapproval"):
        return {"status": "ignored", "reason": f"merge request action {action!r}"}
    iid = oa.get("iid")
    if not isinstance(iid, int):
        return {"status": "ignored", "reason": "object_attributes.iid missing"}
    repo = str((payload.get("project") or {}).get("path_with_namespace") or "")
    username = str((payload.get("user") or {}).get("username") or "gitlab")
    return await _apply_review_decision(
        request,
        repo=repo,
        number=iid,
        approve=action in ("approved", "approval"),
        comment=None,
        actor=f"gitlab:{username}",
        reviewer=username,
    )


@router.post("/gitlab", status_code=202)
async def gitlab_webhook(
    request: Request,
    x_gitlab_event: str = Header(""),
    x_gitlab_token: str = Header(""),
) -> dict[str, Any]:
    """GitLab 项目 Webhook 入口：Issue Hook 上报信号，Merge Request Hook 同步审批。

    鉴权用 GitLab Webhook 的 Secret token（请求头 ``X-Gitlab-Token``）；未配置密钥一律 401。
    Issue 正文同样视为不可信数据，原样进入 signals。
    """
    settings: Settings = request.app.state.settings
    secret = settings.gitlab_webhook_secret.get_secret_value()
    if not secret or not x_gitlab_token or not hmac.compare_digest(secret, x_gitlab_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing gitlab webhook token")
    try:
        payload = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "payload must be an object")

    kind = str(payload.get("object_kind") or "")
    if x_gitlab_event:
        # 事件头存在时以它为准，不采信载荷里的 object_kind 自报
        is_mr, is_issue = x_gitlab_event == "Merge Request Hook", x_gitlab_event == "Issue Hook"
    else:
        is_mr, is_issue = kind == "merge_request", kind == "issue"
    if is_mr:
        return await _handle_gitlab_mr_event(request, payload)
    if not is_issue:
        return {"status": "ignored", "reason": f"event {x_gitlab_event or kind!r}"}

    repo = str((payload.get("project") or {}).get("path_with_namespace") or "")
    async with request.app.state.store.read() as s:
        project, source_repo = await _project_for_repo(s, repo)
    if project is None:
        return {"status": "ignored", "reason": f"no project for repository {repo!r}"}

    label = str(project.config.get("issue_label") or DEFAULT_LABEL)
    sig = gitlab_issue_to_signal(project.name, payload, label)
    if sig is None:
        return {"status": "ignored", "reason": "issue not labeled for TwinFix"}
    if source_repo:
        sig = replace(sig, meta={**(sig.meta or {}), "repos": [source_repo]})
    svc: IncidentService = request.app.state.service
    try:
        res = await svc.ingest(sig)
    except (NotFoundError, ConflictError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"status": "accepted", **res.__dict__}
