"""HTTP 路由（docs/04-interfaces.md §5，核心范围）。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, or_, select
from sse_starlette.sse import EventSourceResponse

from twinfix.agents.params import AgentConfig
from twinfix.agents.params import defaults as agent_param_defaults
from twinfix.agents.params import resolve as resolve_agent_params
from twinfix.api.auth import (
    DUMMY_PASSWORD_HASH,
    Admin,
    CurrentPrincipal,
    Maintainer,
    hash_password,
    issue_token,
    verify_password,
)
from twinfix.api.schemas import (
    ArenaInjectIn,
    ArenaInjectOut,
    AssigneeIn,
    CancelIn,
    DecideIn,
    EvalRunIn,
    EventOut,
    GuideIn,
    HaltIn,
    IncidentCreate,
    IncidentDetail,
    IncidentPage,
    IncidentSummary,
    IngestOut,
    LoginIn,
    ProjectIn,
    ProjectOut,
    ProjectPatch,
    ResolveIn,
    SignalCreate,
    TokenOut,
    UserCreate,
    UserOut,
    UserPatch,
)
from twinfix.api.stream import event_stream
from twinfix.arena.evalruns import EvalRunManager
from twinfix.db import Store
from twinfix.domain.enums import (
    TERMINAL_STATES,
    WAITING_STATES,
    AgentName,
    ApprovalStatus,
    IncidentState,
    RunStatus,
    Severity,
    SignalSource,
    TwinStatus,
    UserRole,
)
from twinfix.domain.models import (
    Approval,
    Attempt,
    Diagnosis,
    Incident,
    IncidentEvent,
    Patch,
    Project,
    Report,
    Run,
    Signal,
    Twin,
    User,
    Verification,
)
from twinfix.domain.repo import IncidentNotFoundError, record_event
from twinfix.multirepo import repo_specs
from twinfix.multirepo import validate_config as validate_repo_config
from twinfix.orchestrator.precedents import find_precedents
from twinfix.services import ConflictError, IncidentService, NotFoundError, SignalIn

router = APIRouter(prefix="/api")

_DETAIL_RELATIONS = frozenset({"diagnosis", "attempts", "twin", "approvals", "reports", "runs", "assignee"})


def store(request: Request) -> Store:
    s: Store = request.app.state.store
    return s


def service(request: Request) -> IncidentService:
    s: IncidentService = request.app.state.service
    return s


StoreDep = Annotated[Store, Depends(store)]
ServiceDep = Annotated[IncidentService, Depends(service)]


def _translate(exc: Exception) -> HTTPException:
    # 领域层的 IncidentNotFoundError 和 services.NotFoundError 是兄弟类，两个都要映射
    if isinstance(exc, (NotFoundError, IncidentNotFoundError)):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    raise exc


# ---------------------------------------------------------------- 鉴权


@router.post("/auth/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, db: StoreDep) -> TokenOut:
    """用户名或邮箱 + 密码换 JWT。账号由 `twinfix-user create` 创建。"""
    async with db.read() as s:
        user = await s.scalar(select(User).where((User.name == body.name) | (User.email == body.name)))
    if user is None or not user.password_hash:
        verify_password(body.password, DUMMY_PASSWORD_HASH)  # 等价耗时，避免枚举账号
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    settings = request.app.state.settings
    token = issue_token(settings, user.id, user.name, user.role)
    return TokenOut(token=token, user=UserOut(id=user.id, name=user.name, role=user.role))


@router.get("/auth/me", response_model=UserOut)
async def me(user: CurrentPrincipal) -> UserOut:
    return UserOut(id=user.user_id or "local", name=user.name, role=user.role)


# ---------------------------------------------------------------- 账号（管理员）


@router.get("/users", response_model=list[UserOut])
async def list_users(db: StoreDep, user: Admin) -> list[User]:
    async with db.read() as s:
        return list((await s.scalars(select(User).order_by(User.name))).all())


@router.get("/users/assignable")
async def list_assignable_users(db: StoreDep, user: Maintainer) -> list[dict[str, Any]]:
    """可指派账号：维护者指派事故负责人时只需要 id / 名字，不暴露邮箱等账号信息。"""
    async with db.read() as s:
        rows = (await s.execute(select(User.id, User.name, User.role).order_by(User.name))).all()
    return [{"id": uid, "name": name, "role": role.value} for uid, name, role in rows]


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, db: StoreDep, user: Admin) -> User:
    async with db.tx() as tx:
        clash = await tx.session.scalar(select(User.id).where(User.name == body.name))
        if clash:
            raise HTTPException(status.HTTP_409_CONFLICT, f"user {body.name} exists")
        if body.email and await tx.session.scalar(select(User.id).where(User.email == body.email)):
            raise HTTPException(status.HTTP_409_CONFLICT, f"email {body.email} is already used")
        row = User(
            name=body.name,
            email=body.email or None,
            role=body.role,
            password_hash=hash_password(body.password),
        )
        tx.session.add(row)
    return row


@router.patch("/users/{user_id}", response_model=UserOut)
async def patch_user(user_id: str, body: UserPatch, db: StoreDep, admin: Admin) -> User:
    async with db.tx() as tx:
        row = await tx.session.get(User, user_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
        if body.role is not None:
            if row.id == admin.user_id and body.role is not UserRole.ADMIN:
                # 管理员把自己降级会直接锁死账号管理入口
                raise HTTPException(status.HTTP_409_CONFLICT, "cannot demote yourself")
            row.role = body.role
        if body.email is not None:
            if body.email and await tx.session.scalar(
                select(User.id).where(User.email == body.email, User.id != user_id)
            ):
                raise HTTPException(status.HTTP_409_CONFLICT, f"email {body.email} is already used")
            row.email = body.email or None
        if body.password:
            row.password_hash = hash_password(body.password)
    return row


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str, db: StoreDep, admin: Admin) -> None:
    if user_id == admin.user_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "cannot delete yourself")
    async with db.tx() as tx:
        row = await tx.session.get(User, user_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
        # 审批记录以 decided_by 引用账号，删掉会让审计链断掉；有历史记录时只能改角色
        decided = await tx.session.scalar(select(Approval.id).where(Approval.decided_by == user_id).limit(1))
        if decided:
            raise HTTPException(status.HTTP_409_CONFLICT, "user has approvals on record; change the role instead")
        await tx.session.delete(row)


# ---------------------------------------------------------------- 系统


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    halted = await request.app.state.control.is_halted()
    return {"status": "ok", "halted": halted, "agent_mode": request.app.state.settings.agent_mode}


@router.post("/system/halt")
async def set_halt(body: HaltIn, request: Request, user: Admin) -> dict[str, bool]:
    await request.app.state.control.set_halted(body.halted)
    return {"halted": body.halted}


@router.get("/settings/overview")
async def settings_overview(request: Request, user: CurrentPrincipal) -> dict[str, Any]:
    """只读的服务端全局配置：Agent 默认值、运行边界与已启用的适配器。

    这些值来自环境变量，控制台不能修改；项目级覆盖在 /projects/{name}/agent-config。
    不含任何密钥，供所有已登录角色查看。
    """
    from dataclasses import asdict

    s = request.app.state.settings
    return {
        "agent_defaults": asdict(agent_param_defaults(s)),
        "limits": {
            "diagnosis_confidence_threshold": s.diagnosis_confidence_threshold,
            "default_max_fix_attempts": s.default_max_fix_attempts,
            "review_timeout_s": s.review_timeout_s,
            "state_timeout_s": s.state_timeout_s,
            "step_max_retries": s.step_max_retries,
            "step_retry_delay_s": s.step_retry_delay_s,
            "lock_ttl_s": s.lock_ttl_s,
        },
        "guards": {
            "repro_required": s.repro_required,
            "repro_confirm": s.repro_confirm,
            "integrity_check": s.integrity_check,
            "precedents_enabled": s.precedents_enabled,
            "log_cluster": s.log_cluster,
        },
        "runtime": {
            "agent_mode": s.agent_mode,
            "codehost": s.codehost,
            "sandbox": s.sandbox,
            "twin_provider": s.twin_provider,
            "ci": s.ci,
            "notifier": s.notifier,
            "observability": s.observability,
            "llm_base_url": s.llm_base_url,
        },
    }


# ---------------------------------------------------------------- 项目


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(db: StoreDep, user: CurrentPrincipal) -> list[Project]:
    async with db.read() as s:
        return list((await s.scalars(select(Project).order_by(Project.name))).all())


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectIn, db: StoreDep, user: Admin) -> Project:
    errors = validate_repo_config(body.config or {})
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(errors))
    async with db.tx() as tx:
        if await tx.session.scalar(select(Project.id).where(Project.name == body.name)):
            raise HTTPException(status.HTTP_409_CONFLICT, f"project {body.name} exists")
        p = Project(**body.model_dump())
        tx.session.add(p)
    return p


@router.patch("/projects/{name}", response_model=ProjectOut)
async def patch_project(name: str, body: ProjectPatch, db: StoreDep, user: Admin) -> Project:
    """更新项目仓库映射与配置；config 整体替换（控制台提交合并后的完整对象）。"""
    if body.config is not None:
        errors = validate_repo_config(body.config)
        if errors:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(errors))
    async with db.tx() as tx:
        project = await tx.session.scalar(select(Project).where(Project.name == name).with_for_update())
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"project {name} not found")
        if body.repo is not None:
            project.repo = body.repo
        if body.language is not None:
            project.language = body.language
        if body.branch_map is not None:
            project.branch_map = body.branch_map
        if body.config is not None:
            project.config = body.config
    return project


@router.post("/projects/{name}/check")
async def check_project(name: str, request: Request, db: StoreDep, user: Maintainer) -> dict[str, Any]:
    """项目接入自检：逐项探测代码托管 / 孪生模板 / 通知渠道，返回每项结果，便于接入向导定位问题。

    只做只读探测（取分支头、读配置），不会创建分支、容器或发送通知。
    """
    async with db.read() as s:
        project = await s.scalar(select(Project).where(Project.name == name))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"project {name} not found")

    adapters = _adapters(request)
    branch = project.branch_map.get("test", "dev")
    checks: list[dict[str, Any]] = []

    async def probe(label: str, fn: Any) -> None:
        try:
            detail = await fn()
            checks.append({"name": label, "ok": True, "detail": detail})
        except Exception as exc:  # 单项失败不影响其余检查
            checks.append({"name": label, "ok": False, "detail": f"{type(exc).__name__}: {exc}"[:500]})

    async def check_codehost() -> str:
        sha = await adapters.codehost.branch_head(project.repo, branch)
        return f"{project.repo}@{branch} = {sha[:12]}"

    async def check_workspace_source() -> str:
        if adapters.workspace_manager is None:
            raise RuntimeError("codehost 不支持 remote()：孪生构建需要 codehost=local 或 github")
        return "workspace manager ready"

    async def check_twin() -> str:
        twin = adapters.twin
        services = getattr(twin, "template", None)
        if services is not None:
            return f"{type(twin).__name__}: services={list(services.services)}"
        return f"{type(twin).__name__}"

    async def check_repo_config() -> str:
        repo = str(project.repo or "")
        if "/" not in repo:
            raise RuntimeError(f"repo {repo!r} 不是 owner/name 形式")
        for env, target in project.branch_map.items():
            if not target:
                raise RuntimeError(f"branch_map[{env!r}] 为空")
        return f"branch_map={project.branch_map}"

    async def check_extra_repos() -> str:
        specs = repo_specs(project)[1:]
        if not specs:
            return "no additional repos"
        lines = []
        for spec in specs:
            target = spec.branch_map.get("test") or project.branch_map.get("test") or "dev"
            sha = await adapters.codehost.branch_head(spec.repo, target)
            lines.append(f"{spec.name}: {spec.repo}@{target}={sha[:12]}")
        return "; ".join(lines)

    await probe("repo_config", check_repo_config)
    await probe("codehost", check_codehost)
    await probe("extra_repos", check_extra_repos)
    await probe("workspace_source", check_workspace_source)
    await probe("twin_provider", check_twin)

    configured = project.config.get("twin") or {}
    checks.append(
        {
            "name": "twin_template",
            "ok": not configured or bool(configured.get("services") or _template_services(adapters.twin)),
            "detail": (
                f"project override: {configured}"
                if configured
                else f"default: services={_template_services(adapters.twin) or 'n/a'}"
            ),
        }
    )
    checks.append(
        {
            "name": "notifiers",
            "ok": bool(adapters.notifiers),
            "detail": ", ".join(n.channel for n in adapters.notifiers) or "未启用任何通知渠道",
        }
    )
    return {"project": name, "ok": all(c["ok"] for c in checks), "checks": checks}


def _template_services(twin: Any) -> list[str] | None:
    template = getattr(twin, "template", None)
    services = getattr(template, "services", None)
    return list(services) if services else None


def _adapters(request: Request) -> Any:
    """按需装配适配器并缓存（自检、统计等只读接口用；启动时不建立外部连接）。"""
    adapters = getattr(request.app.state, "adapters", None)
    if adapters is None:
        from twinfix.container import build_adapters

        adapters = build_adapters(request.app.state.settings)
        request.app.state.adapters = adapters
    return adapters


@router.get("/projects/{name}/agent-config")
async def get_agent_config(name: str, request: Request, db: StoreDep, user: CurrentPrincipal) -> dict[str, Any]:
    """项目的 Agent 参数：覆盖值 + 全局默认 + 各 Agent 最终生效值（控制台设置页展示用）。"""
    async with db.read() as s:
        project = await s.scalar(select(Project).where(Project.name == name))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"project {name} not found")
    return _agent_config_view(request.app.state.settings, project.config)


@router.put("/projects/{name}/agent-config")
async def put_agent_config(name: str, body: AgentConfig, request: Request, db: StoreDep, user: Admin) -> dict[str, Any]:
    async with db.tx() as tx:
        project = await tx.session.scalar(select(Project).where(Project.name == name).with_for_update())
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"project {name} not found")
        # 只保存显式设置的字段，留空的沿用全局默认（以后调整默认值时自动生效）
        project.config = {**project.config, "agent": body.model_dump(exclude_none=True, mode="json")}
        config = project.config
    return _agent_config_view(request.app.state.settings, config)


def _agent_config_view(settings: Any, config: dict[str, Any]) -> dict[str, Any]:
    from dataclasses import asdict

    llm_agents = (AgentName.DIAGNOSER, AgentName.REPRODUCER, AgentName.FIXER, AgentName.VERIFIER)
    return {
        "overrides": config.get("agent") or {},
        "defaults": asdict(agent_param_defaults(settings)),
        "effective": {a.value: asdict(resolve_agent_params(settings, config, a)) for a in llm_agents},
    }


# ---------------------------------------------------------------- 信号与事故


@router.post("/signals", response_model=IngestOut, status_code=202)
async def ingest_signal(body: SignalCreate, svc: ServiceDep, user: Maintainer) -> IngestOut:
    try:
        res = await svc.ingest(SignalIn(**body.model_dump()))
    except (NotFoundError, ConflictError) as exc:
        raise _translate(exc) from exc
    return IngestOut(**res.__dict__)


@router.post("/incidents", response_model=IngestOut, status_code=201)
async def create_incident(body: IncidentCreate, svc: ServiceDep, user: Maintainer) -> IngestOut:
    data = body.model_dump(exclude={"scenario"})
    meta = {"scenario": body.scenario} if body.scenario else {}
    try:
        res = await svc.ingest(SignalIn(**data, meta=meta))
    except (NotFoundError, ConflictError) as exc:
        raise _translate(exc) from exc
    return IngestOut(**res.__dict__)


@router.get("/incidents", response_model=IncidentPage)
async def list_incidents(
    db: StoreDep,
    user: CurrentPrincipal,
    state: Annotated[list[IncidentState] | None, Query()] = None,
    severity: Annotated[list[Severity] | None, Query()] = None,
    service: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    project_id: str | None = None,
    assignee_id: str | None = None,
    unassigned: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> IncidentPage:
    """事故列表。筛选与搜索都在服务端完成，控制台只取当前页。"""
    stmt = select(Incident)
    if state:
        stmt = stmt.where(Incident.state.in_(state))
    if severity:
        stmt = stmt.where(Incident.severity.in_(severity))
    if service:
        stmt = stmt.where(Incident.service.in_(service))
    if project_id:
        stmt = stmt.where(Incident.project_id == project_id)
    if assignee_id:
        stmt = stmt.where(Incident.assignee_id == assignee_id)
    if unassigned:
        stmt = stmt.where(Incident.assignee_id.is_(None))
    if q and (term := q.strip()):
        like = f"%{term}%"
        stmt = stmt.where(
            or_(
                Incident.title.ilike(like),
                Incident.id.ilike(like),
                Incident.service.ilike(like),
                Incident.fix_branch.ilike(like),
                Incident.target_branch.ilike(like),
            )
        )
    async with db.read() as s:
        total = await s.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            await s.scalars(stmt.order_by(Incident.updated_at.desc(), Incident.id.desc()).limit(limit).offset(offset))
        ).all()
    return IncidentPage(items=[IncidentSummary.model_validate(r) for r in rows], total=total or 0)


@router.get("/incidents/stats")
async def incident_stats(db: StoreDep, user: CurrentPrincipal) -> dict[str, Any]:
    """事故概览计数（整个工作区，不受列表筛选影响）与服务名清单。

    控制台的统计条与页签计数用这里的口径，避免出现“只统计当前页”的误导数字。
    """
    async with db.read() as s:
        rows = (await s.execute(select(Incident.state, func.count()).group_by(Incident.state))).all()
        high = await s.scalar(
            select(func.count())
            .select_from(Incident)
            .where(Incident.severity.in_([Severity.S1, Severity.S2]), Incident.state.notin_(TERMINAL_STATES))
        )
        services = (
            await s.scalars(
                select(Incident.service)
                .where(Incident.service.is_not(None), Incident.service != "")
                .distinct()
                .order_by(Incident.service)
            )
        ).all()
    counts = {state.value: count for state, count in rows}
    ended = sum(counts.get(state.value, 0) for state in TERMINAL_STATES)
    waiting = sum(counts.get(state.value, 0) for state in WAITING_STATES)
    total = sum(counts.values())
    return {
        "total": total,
        "ongoing": total - ended,
        "waiting": waiting,
        "high": high or 0,
        "ended": ended,
        "services": list(services),
    }


@router.get("/stats/runtime")
async def runtime_stats(request: Request, db: StoreDep, user: CurrentPrincipal) -> dict[str, Any]:
    """运行容量：各状态事故数、进行中的 Agent 运行、待审批、孪生占用与卡住的事故。

    控制台的"运行态"卡片用它判断是否需要人为干预（队列堆积、孪生占满、状态超时）。
    """
    settings = request.app.state.settings
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=settings.state_timeout_s)
    async with db.read() as s:
        by_state = (await s.execute(select(Incident.state, func.count()).group_by(Incident.state))).all()
        running = await s.scalar(select(func.count()).select_from(Run).where(Run.status == RunStatus.RUNNING))
        oldest_running = await s.scalar(select(func.min(Run.created_at)).where(Run.status == RunStatus.RUNNING))
        approvals = await s.scalar(
            select(func.count()).select_from(Approval).where(Approval.status == ApprovalStatus.PENDING)
        )
        twins = (await s.execute(select(Twin.status, func.count()).group_by(Twin.status))).all()
        # 卡住：非终态、非等待人工，且停留在当前状态超过状态超时（sweeper 下轮会处理，先暴露出来）
        stuck = await s.scalar(
            select(func.count())
            .select_from(Incident)
            .where(
                Incident.state.not_in(TERMINAL_STATES),
                Incident.state.not_in(WAITING_STATES),
                Incident.state_entered_at < stale_before,
            )
        )
    limits = {
        "twin_max_concurrent": settings.twin_max_concurrent,
        "twin_port_base": settings.twin_port_base,
        "twin_port_span": settings.twin_port_span,
        "state_timeout_s": settings.state_timeout_s,
        "review_timeout_s": settings.review_timeout_s,
        "step_max_retries": settings.step_max_retries,
        "lock_ttl_s": settings.lock_ttl_s,
    }
    return {
        "now": now,
        "incidents": {state.value: count for state, count in by_state},
        "active_runs": running or 0,
        "oldest_active_run_at": oldest_running,
        "pending_approvals": approvals or 0,
        "twins": {status.value: count for status, count in twins},
        "twin_capacity": settings.twin_max_concurrent,
        "twin_in_use": sum(count for status, count in twins if status in (TwinStatus.CREATING, TwinStatus.READY)),
        "stuck_incidents": stuck or 0,
        "limits": limits,
    }


@router.get("/stats/cost")
async def cost_stats(
    db: StoreDep,
    user: CurrentPrincipal,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    project_id: str | None = None,
) -> dict[str, Any]:
    """成本看板：近 N 天的 token / 金额 / 运行次数，按 Agent、按天、按事故聚合。"""
    since = datetime.now(UTC) - timedelta(days=days)
    scope = select(Run.id).join(Incident, Incident.id == Run.incident_id).where(Run.created_at >= since)
    if project_id:
        scope = scope.where(Incident.project_id == project_id)
    ids = scope.subquery()
    async with db.read() as s:
        totals = (
            await s.execute(
                select(
                    func.count(Run.id),
                    func.coalesce(func.sum(Run.input_tokens), 0),
                    func.coalesce(func.sum(Run.output_tokens), 0),
                    func.coalesce(func.sum(Run.cost), 0),
                    func.coalesce(func.avg(Run.latency_ms), 0),
                ).where(Run.id.in_(select(ids.c.id)))
            )
        ).one()
        by_agent = (
            await s.execute(
                select(
                    Run.agent,
                    func.count(Run.id),
                    func.coalesce(func.sum(Run.input_tokens), 0),
                    func.coalesce(func.sum(Run.output_tokens), 0),
                    func.coalesce(func.sum(Run.cost), 0),
                )
                .where(Run.id.in_(select(ids.c.id)))
                .group_by(Run.agent)
            )
        ).all()
        day = func.date_trunc("day", Run.created_at)
        daily = (
            await s.execute(
                select(
                    day,
                    func.count(Run.id),
                    func.coalesce(func.sum(Run.input_tokens + Run.output_tokens), 0),
                    func.coalesce(func.sum(Run.cost), 0),
                )
                .where(Run.id.in_(select(ids.c.id)))
                .group_by(day)
                .order_by(day)
            )
        ).all()
        top = (
            await s.execute(
                select(Incident.id, Incident.title, Incident.state, Incident.cost_tokens, Incident.cost_amount)
                .where(Incident.created_at >= since, Incident.cost_tokens > 0)
                .order_by(Incident.cost_tokens.desc())
                .limit(10)
            )
        ).all()
    return {
        "days": days,
        "since": since,
        "totals": {
            "runs": totals[0],
            "input_tokens": int(totals[1]),
            "output_tokens": int(totals[2]),
            "tokens": int(totals[1]) + int(totals[2]),
            "cost": float(totals[3]),
            "avg_latency_ms": round(float(totals[4]), 1),
        },
        "by_agent": [
            {
                "agent": agent.value,
                "runs": runs,
                "input_tokens": int(inp),
                "output_tokens": int(out),
                "tokens": int(inp) + int(out),
                "cost": float(cost),
            }
            for agent, runs, inp, out, cost in by_agent
        ],
        "daily": [
            {"date": start.strftime("%Y-%m-%d"), "runs": runs, "tokens": int(tokens), "cost": float(cost)}
            for start, runs, tokens, cost in daily
        ],
        "top_incidents": [
            {
                "id": iid,
                "title": title,
                "state": state.value,
                "cost_tokens": cost_tokens,
                "cost_amount": float(cost_amount),
            }
            for iid, title, state, cost_tokens, cost_amount in top
        ],
    }


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
async def get_incident(incident_id: str, db: StoreDep, user: CurrentPrincipal) -> IncidentDetail:
    async with db.read() as s:
        inc = await s.get(Incident, incident_id)
        if inc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
        diag = await s.scalar(select(Diagnosis).where(Diagnosis.incident_id == incident_id))
        attempts = (
            await s.scalars(select(Attempt).where(Attempt.incident_id == incident_id).order_by(Attempt.seq))
        ).all()
        attempt_ids = [a.id for a in attempts]
        patches = {
            p.attempt_id: p for p in (await s.scalars(select(Patch).where(Patch.attempt_id.in_(attempt_ids)))).all()
        }
        verifs = (
            await s.scalars(
                select(Verification).where(Verification.attempt_id.in_(attempt_ids)).order_by(Verification.created_at)
            )
        ).all()
        twin = await s.scalar(select(Twin).where(Twin.incident_id == incident_id))
        approvals = (
            await s.scalars(select(Approval).where(Approval.incident_id == incident_id).order_by(Approval.created_at))
        ).all()
        reports = (
            await s.scalars(select(Report).where(Report.incident_id == incident_id).order_by(Report.created_at))
        ).all()
        runs = (await s.scalars(select(Run).where(Run.incident_id == incident_id).order_by(Run.created_at))).all()
        # 负责人名字随详情返回：只读角色也要能看到“谁在处理”，但不能拿 /users/assignable 的账号列表
        assignee = await s.get(User, inc.assignee_id) if inc.assignee_id else None

    # 只取列字段构造，关联数据在下面单独填充（避免对已脱离会话的对象触发懒加载）
    scalar_fields = IncidentDetail.model_fields.keys() - _DETAIL_RELATIONS
    detail = IncidentDetail.model_validate({f: getattr(inc, f) for f in scalar_fields})
    if diag is not None:
        detail.diagnosis = {
            "summary": diag.summary,
            "root_cause": diag.root_cause,
            "hypotheses": diag.hypotheses,
            "suspect_locations": diag.suspect_locations,
            "confidence": diag.confidence,
            "repro": diag.repro,
        }
    detail.attempts = [
        {
            "id": a.id,
            "seq": a.seq,
            "status": a.status.value,
            "trigger": a.trigger,
            "feedback": a.feedback,
            "created_at": a.created_at,
            "patch": (
                {
                    "diff": p.diff,
                    "files_changed": p.files_changed,
                    "repos": p.repos or {},
                    "touches_tests": p.touches_tests,
                    "explanation": p.explanation,
                    "commit_sha": p.commit_sha,
                }
                if (p := patches.get(a.id))
                else None
            ),
            "verifications": [
                {
                    "checks": v.checks,
                    "passed": v.passed,
                    "commit_sha": v.commit_sha,
                    "base_sha": v.base_sha,
                    "created_at": v.created_at,
                }
                for v in verifs
                if v.attempt_id == a.id
            ],
        }
        for a in attempts
    ]
    if assignee is not None:
        detail.assignee = {"id": assignee.id, "name": assignee.name}
    if twin is not None:
        detail.twin = {
            "compose_project": twin.compose_project,
            "status": twin.status.value,
            "urls": twin.urls,
            "expires_at": twin.expires_at,
            "commit_sha": twin.commit_sha,
        }
    detail.approvals = [
        {
            "id": a.id,
            "action": a.action.value,
            "status": a.status.value,
            "comment": a.comment,
            "payload": a.payload,
            "decided_by": a.decided_by,
            "decided_at": a.decided_at,
            "expires_at": a.expires_at,
            "created_at": a.created_at,
        }
        for a in approvals
    ]
    detail.reports = [
        {
            "id": r.id,
            "type": r.type.value,
            "title": r.title,
            "content": r.content,
            "status": r.status.value,
            "error": r.error,
            "sent_at": r.sent_at,
            "created_at": r.created_at,
        }
        for r in reports
    ]
    detail.runs = [
        {
            "id": r.id,
            "agent": r.agent.value,
            "status": r.status.value,
            "outcome": r.outcome,
            "attempt_seq": r.attempt_seq,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "latency_ms": r.latency_ms,
            "error": r.error,
            "created_at": r.created_at,
        }
        for r in runs
    ]
    # meta 中的工作区路径是宿主机内部信息，不对外暴露
    detail.meta = {k: v for k, v in inc.meta.items() if k not in ("workspace", "step_failures")}
    return detail


@router.get("/incidents/{incident_id}/events", response_model=list[EventOut])
async def list_events(
    incident_id: str,
    db: StoreDep,
    user: CurrentPrincipal,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[IncidentEvent]:
    async with db.read() as s:
        q = (
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == incident_id, IncidentEvent.id > after)
            .order_by(IncidentEvent.id)
            .limit(limit)
        )
        return list((await s.scalars(q)).all())


@router.get("/incidents/{incident_id}/stream")
async def stream_incident(
    incident_id: str,
    request: Request,
    db: StoreDep,
    user: CurrentPrincipal,
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[int, Query(ge=0)] = 0,
) -> EventSourceResponse:
    start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
    return EventSourceResponse(event_stream(db, request.app.state.redis, incident_id, start))


@router.get("/stream")
async def stream_all(
    request: Request,
    db: StoreDep,
    user: CurrentPrincipal,
    last_event_id: Annotated[str | None, Header()] = None,
) -> EventSourceResponse:
    """全局事件流（事故列表页用）。新连接只推送此后的事件。"""
    if last_event_id and last_event_id.isdigit():
        start = int(last_event_id)
    else:
        async with db.read() as s:
            start = int(await s.scalar(select(func.coalesce(func.max(IncidentEvent.id), 0))) or 0)
    return EventSourceResponse(event_stream(db, request.app.state.redis, None, start))


@router.post("/incidents/{incident_id}/cancel", status_code=202)
async def cancel_incident(incident_id: str, body: CancelIn, svc: ServiceDep, user: Maintainer) -> dict[str, str]:
    try:
        await svc.cancel(incident_id, user.actor, body.reason)
    except Exception as exc:
        raise _translate(exc) from exc
    return {"status": "cancel_requested"}


@router.post("/incidents/{incident_id}/guide", status_code=202)
async def guide_incident(incident_id: str, body: GuideIn, svc: ServiceDep, user: Maintainer) -> dict[str, str]:
    try:
        restart = IncidentState(body.restart_from) if body.restart_from else None
        await svc.guide(incident_id, user.actor, body.guidance, restart)
    except Exception as exc:
        raise _translate(exc) from exc
    return {"status": "accepted"}


@router.post("/incidents/{incident_id}/resolve", status_code=202)
async def resolve_incident(incident_id: str, body: ResolveIn, svc: ServiceDep, user: Maintainer) -> dict[str, str]:
    try:
        await svc.resolve_human(incident_id, user.actor, body.verdict, body.reason)
    except Exception as exc:
        raise _translate(exc) from exc
    return {"status": "accepted"}


@router.post("/incidents/{incident_id}/assignee")
async def set_assignee(incident_id: str, body: AssigneeIn, db: StoreDep, user: Maintainer) -> dict[str, Any]:
    """指派/取消指派负责人。终态事故不再接受指派（留痕以事件为准）。"""
    async with db.tx() as tx:
        inc = await tx.session.get(Incident, incident_id, with_for_update=True)
        if inc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
        if inc.state in TERMINAL_STATES:
            raise HTTPException(status.HTTP_409_CONFLICT, f"incident already {inc.state.value}")
        assignee: User | None = None
        if body.user_id:
            assignee = await tx.session.get(User, body.user_id)
            if assignee is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"user {body.user_id} not found")
        previous = inc.assignee_id
        inc.assignee_id = assignee.id if assignee else None
        await record_event(
            tx,
            incident_id,
            "assigned",
            actor=user.actor,
            payload={"assignee_id": inc.assignee_id, "previous": previous},
        )
    return {
        "assignee_id": inc.assignee_id,
        "assignee": {"id": assignee.id, "name": assignee.name} if assignee else None,
    }


@router.get("/incidents/{incident_id}/similar")
async def similar_incidents(
    incident_id: str,
    db: StoreDep,
    user: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=10)] = 3,
) -> dict[str, Any]:
    """相似历史事故：复用诊断阶段的历史判例检索，给评审/人工处理提供参考。"""
    async with db.read() as s:
        inc = await s.get(Incident, incident_id)
        if inc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
        sigs = (await s.scalars(select(Signal).where(Signal.incident_id == incident_id))).all()
    signals = [dict(sig.raw or {}) for sig in sigs]
    return {"items": await find_precedents(db, inc, signals, limit=limit)}


@router.post("/approvals/{approval_id}/decide", status_code=202)
async def decide_approval(approval_id: str, body: DecideIn, svc: ServiceDep, user: Maintainer) -> dict[str, str]:
    try:
        approval = await svc.decide(approval_id, user.user_id, user.role, body.approve, body.comment, body.reason_kind)
    except Exception as exc:
        raise _translate(exc) from exc
    return {"status": approval.status.value}


@router.get("/approvals")
async def list_approvals(
    db: StoreDep,
    user: CurrentPrincipal,
    status: Annotated[list[ApprovalStatus] | None, Query()] = None,
    action: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """全局审批列表（审批中心）。默认全量，前端常带 status=pending 拉待办；附事故上下文。"""
    q = select(Approval, Incident).join(Incident, Incident.id == Approval.incident_id)
    if status:
        q = q.where(Approval.status.in_(status))
    if action:
        q = q.where(Approval.action.in_(action))
    async with db.read() as s:
        total = await s.scalar(select(func.count()).select_from(q.subquery()))
        rows = (await s.execute(q.order_by(Approval.created_at.desc()).limit(limit).offset(offset))).all()
    return {
        "items": [
            {
                "id": a.id,
                "action": a.action.value,
                "status": a.status.value,
                "payload": a.payload,
                "comment": a.comment,
                "decided_by": a.decided_by,
                "decided_at": a.decided_at,
                "expires_at": a.expires_at,
                "created_at": a.created_at,
                "incident": {
                    "id": i.id,
                    "title": i.title,
                    "state": i.state.value,
                    "severity": i.severity.value,
                    "service": i.service,
                    "target_branch": i.target_branch,
                },
            }
            for a, i in rows
        ],
        "total": total or 0,
    }


@router.get("/incidents/{incident_id}/evidence")
async def list_evidence(incident_id: str, db: StoreDep, user: CurrentPrincipal) -> list[dict[str, Any]]:
    """证据面板：工具结果的摘要列表（全文按 id 另取，避免一次拉取大量日志/测试输出）。"""
    from twinfix.domain.models import Evidence

    async with db.read() as s:
        rows = (
            await s.execute(
                select(Evidence.id, Evidence.kind, Evidence.summary, Evidence.tokens, Evidence.created_at, Run.agent)
                .outerjoin(Run, Run.id == Evidence.run_id)
                .where(Evidence.incident_id == incident_id)
                .order_by(Evidence.created_at)
            )
        ).all()
    return [
        {
            "id": r.id,
            "kind": r.kind.value,
            "summary": r.summary,
            "tokens": r.tokens,
            "agent": r.agent.value if r.agent else None,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.get("/evidence/{evidence_id}")
async def get_evidence(evidence_id: str, db: StoreDep, user: CurrentPrincipal) -> dict[str, Any]:
    from twinfix.domain.models import Evidence

    async with db.read() as s:
        ev = await s.get(Evidence, evidence_id)
    if ev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "evidence not found")
    # 入库前已在工具网关脱敏
    return {"id": ev.id, "kind": ev.kind.value, "summary": ev.summary, "content": ev.content or ""}


@router.get("/reports/{report_id}/download", response_class=PlainTextResponse)
async def download_report(report_id: str, db: StoreDep, user: CurrentPrincipal) -> PlainTextResponse:
    """下载报告 Markdown 源文件（文件名带事故 id 与报告类型，便于归档）。"""
    async with db.read() as s:
        report = await s.get(Report, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    filename = f"{report.incident_id}-{report.type.value}.md"
    return PlainTextResponse(
        report.content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: str, db: StoreDep, user: CurrentPrincipal) -> dict[str, Any]:
    from twinfix.domain.models import ToolCall

    async with db.read() as s:
        run = await s.get(Run, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
        calls = (await s.scalars(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at))).all()
    return {
        "id": run.id,
        "incident_id": run.incident_id,
        "agent": run.agent.value,
        "status": run.status.value,
        "outcome": run.outcome,
        "model": run.model,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "cost": str(run.cost),
        "latency_ms": run.latency_ms,
        "error": run.error,
        "created_at": run.created_at,
        "tool_calls": [
            {
                "id": c.id,
                "tool": c.tool,
                "level": c.level.value,
                "args": c.args,
                "status": c.status.value,
                "result_summary": c.result_summary,
                "result_ref": c.result_ref,
                "error": c.error,
                "latency_ms": c.latency_ms,
                "created_at": c.created_at,
            }
            for c in calls
        ],
    }


# ---------------------------------------------------------------- Arena 用例（只读）


@router.get("/arena/cases")
async def list_arena_cases(request: Request, user: CurrentPrincipal) -> list[dict[str, Any]]:
    """Arena 用例清单：只读 case.yaml 元数据，供控制台 Arena 页展示（不加载补丁/隐藏测试内容）。"""
    from twinfix.arena.cases import load_case

    root = request.app.state.settings.arena_cases_dir
    if not root.is_dir():
        return []
    out = []
    for d in sorted(p for p in root.iterdir() if (p / "case.yaml").is_file()):
        try:
            c = load_case(d)
        except Exception as exc:  # 单个用例写坏了不影响整页展示
            out.append({"id": d.name, "error": str(exc)})
            continue
        out.append(
            {
                "id": c.id,
                "title": c.title,
                "category": c.category,
                "description": c.description,
                "inject": {
                    "type": c.inject.type,
                    "file": c.inject.file,
                    "branch": c.inject.branch,
                    "services": c.inject.services,
                },
                "expect": {
                    "outcome": c.expect.outcome,
                    "root_cause_locations": c.expect.root_cause_locations,
                    "root_cause_keywords": c.expect.root_cause_keywords,
                },
                "severity": c.signal.severity.value if c.signal.severity else None,
                "hidden_tests": len(c.hidden_tests),
                "on_review": c.on_review is not None,
            }
        )
    return out


@router.post("/arena/inject", response_model=ArenaInjectOut, status_code=201)
async def inject_arena_case(
    body: ArenaInjectIn, request: Request, db: StoreDep, svc: ServiceDep, user: Maintainer
) -> ArenaInjectOut:
    """把用例的故障注入成一个新项目的本地仓库，并（默认）立即上报信号。

    只在 codehost=local 的评测/演示环境可用：需要与 worker 共享 TWINFIX_GIT_ROOT。
    """
    from twinfix.arena.cases import load_case
    from twinfix.arena.inject import inject_case

    settings = request.app.state.settings
    if settings.codehost != "local":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "arena inject requires TWINFIX_CODEHOST=local (shared TWINFIX_GIT_ROOT)"
        )
    case_dir = settings.arena_cases_dir / body.case_id
    if not (case_dir / "case.yaml").is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"arena case {body.case_id} not found")
    try:
        case = load_case(case_dir)
    except (OSError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid case: {exc}") from exc
    seed = case.seed_path(settings.arena_cases_dir.parent, settings.demo_shop_dir)
    if not seed.is_dir():
        raise HTTPException(status.HTTP_409_CONFLICT, f"arena seed not found at {seed}")

    project = body.project or f"arena-{case.id.lower()}-{datetime.now(UTC):%H%M%S}"
    if ".." in project:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid project name")
    async with db.read() as s:
        if await s.scalar(select(Project.id).where(Project.name == project)):
            raise HTTPException(status.HTTP_409_CONFLICT, f"project {project} exists")
    repo = f"arena/{project}"
    try:
        injected = await inject_case(case, seed, settings.git_root, project, repo)
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"inject failed: {exc}") from exc

    async with db.tx() as tx:
        tx.session.add(
            Project(
                name=project,
                repo=repo,
                language=case.language,
                branch_map={"test": "dev", "release": "release", "prod": "main"},
                config=dict(case.project_config),
            )
        )

    incident_id: str | None = None
    created: bool | None = None
    if body.trigger:
        sig = case.signal
        res = await svc.ingest(
            SignalIn(
                project=project,
                source=SignalSource.ALERT,
                title=sig.title,
                service=sig.service,
                env=sig.env,
                severity=sig.severity,
                message=sig.message,
                stacktrace=sig.stacktrace,
                # 每次注入都是独立实验：不加时间戳的话，同用例重复注入会归并到上一次的事故
                fingerprint=f"arena-inject:{project}",
                meta={"arena_case": case.id},
            )
        )
        incident_id, created = res.incident_id, res.created
    return ArenaInjectOut(
        case_id=injected.case_id,
        project=project,
        repo=repo,
        branch=injected.branch,
        sha=injected.sha,
        incident_id=incident_id,
        created=created,
    )


# ---------------------------------------------------------------- 评测报告（只读）

_REPORT_NAME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[A-Za-z0-9_-]{1,40}$")


@router.get("/eval/reports")
async def list_eval_reports(request: Request, user: CurrentPrincipal) -> list[dict[str, Any]]:
    root = request.app.state.settings.eval_reports_dir
    if not root.is_dir():
        return []
    out = []
    for f in sorted(root.glob("*.json"), reverse=True):
        if not _REPORT_NAME.match(f.stem):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"name": f.stem, **{k: data.get(k) for k in ("title", "meta", "summary")}})
    return out


@router.get("/eval/reports/{name}")
async def get_eval_report(name: str, request: Request, user: CurrentPrincipal) -> dict[str, Any]:
    # 名字白名单校验，杜绝路径穿越
    if not _REPORT_NAME.match(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    path = request.app.state.settings.eval_reports_dir / f"{name}.json"
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


# ---------------------------------------------------------------- 评测任务（控制台发起）


def _eval_runs(request: Request) -> EvalRunManager:
    manager: EvalRunManager | None = getattr(request.app.state, "eval_runs", None)
    if manager is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "eval runner is not configured")
    return manager


@router.post("/eval/runs", status_code=202)
async def start_eval_run(body: EvalRunIn, request: Request, user: Maintainer) -> dict[str, Any]:
    """异步发起一轮评测；子进程与 CLI 同路径，状态和日志落 data_dir/eval_runs。"""
    manager = _eval_runs(request)
    token = ""
    if not request.app.state.settings.auth_disabled:
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    try:
        run = await manager.start(
            cases=body.cases,
            label=body.label,
            model=body.model,
            effort=body.effort,
            timeout_s=body.timeout_s,
            token=token,
        )
    except ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return run.model_dump(mode="json")


@router.get("/eval/runs")
async def list_eval_runs(request: Request, user: CurrentPrincipal) -> list[dict[str, Any]]:
    return [run.model_dump(mode="json") for run in _eval_runs(request).list_runs()]


@router.get("/eval/runs/{run_id}")
async def get_eval_run(run_id: str, request: Request, user: CurrentPrincipal) -> dict[str, Any]:
    manager = _eval_runs(request)
    try:
        run = manager.get(run_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "eval run not found") from exc
    data = run.model_dump(mode="json")
    report: dict[str, Any] | None = None
    if run.report:
        path = request.app.state.settings.eval_reports_dir / f"{run.report}.json"
        if path.is_file():
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report = None
    return {"run": data, "report": report}
