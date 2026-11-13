"""ORM 实体，对应 docs/03-domain-model.md 第 2 节。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from twinfix.domain.enums import (
    AgentName,
    ApprovalAction,
    ApprovalStatus,
    AttemptStatus,
    Env,
    EvidenceKind,
    IncidentState,
    ReportStatus,
    ReportType,
    RunStatus,
    Severity,
    SignalSource,
    ToolCallStatus,
    ToolLevel,
    TwinStatus,
    UserRole,
)
from twinfix.ids import new_id

Json = dict[str, Any]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _enum(cls: type, name: str) -> Enum:
    # 存枚举值而非成员名，数据库里看到的就是 "L2-sandbox"、"test" 这样的值
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e], native_enum=True)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {Json: JSONB, datetime: DateTime(timezone=True)}


class Entity(Base):
    __abstract__ = True

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow, onupdate=utcnow)


class Project(Entity):
    __tablename__ = "project"

    name: Mapped[str] = mapped_column(Text, unique=True)
    repo: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, default="java")
    branch_map: Mapped[Json] = mapped_column(default=dict)
    config: Mapped[Json] = mapped_column(default=dict)

    services: Mapped[list[Service]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Service(Entity):
    __tablename__ = "service"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    project_id: Mapped[str] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    module_path: Mapped[str | None] = mapped_column(Text)
    compose_service: Mapped[str | None] = mapped_column(Text)
    health_url: Mapped[str | None] = mapped_column(Text)

    project: Mapped[Project] = relationship(back_populates="services")


class Incident(Entity):
    __tablename__ = "incident"
    __table_args__ = (
        # 同项目内未关闭（非终态）的事故，指纹唯一；终态后同指纹可再建（复发）
        Index(
            "uq_incident_open_fingerprint",
            "project_id",
            "fingerprint",
            unique=True,
            postgresql_where=text(
                "state NOT IN ('NOT_BUG','CANNOT_REPRODUCE','CANNOT_FIX','ALREADY_FIXED','CANCELLED','CLOSED')"
            ),
        ),
        Index("ix_incident_state", "state"),
        Index("ix_incident_project_created", "project_id", "created_at"),
    )

    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"))
    title: Mapped[str] = mapped_column(Text)
    state: Mapped[IncidentState] = mapped_column(_enum(IncidentState, "incident_state"), default=IncidentState.NEW)
    severity: Mapped[Severity] = mapped_column(_enum(Severity, "severity"), default=Severity.S3)
    env: Mapped[Env] = mapped_column(_enum(Env, "env"), default=Env.TEST)
    fingerprint: Mapped[str] = mapped_column(Text)
    signal_count: Mapped[int] = mapped_column(Integer, default=0)
    service: Mapped[str | None] = mapped_column(Text)
    base_commit: Mapped[str | None] = mapped_column(Text)
    target_branch: Mapped[str | None] = mapped_column(Text)
    fix_branch: Mapped[str | None] = mapped_column(Text)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    github_issue: Mapped[int | None] = mapped_column(Integer)
    recurrence_of: Mapped[str | None] = mapped_column(ForeignKey("incident.id"))
    # 负责人：转人工/待评审时由人指派，控制台按人筛选待办
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("user.id"))
    cost_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_amount: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal(0))
    closed_reason: Mapped[str | None] = mapped_column(Text)
    state_entered_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)
    # 控制面：取消请求由 API 置位，编排在步骤边界和每次工具调用前检查
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # 附加信息：labels、来源链接、stub 场景（内置模拟场景，用于驱动全部分支）等
    meta: Mapped[Json] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))

    project: Mapped[Project] = relationship()
    diagnosis: Mapped[Diagnosis | None] = relationship(back_populates="incident", uselist=False)
    attempts: Mapped[list[Attempt]] = relationship(back_populates="incident", order_by="Attempt.seq")
    twin: Mapped[Twin | None] = relationship(back_populates="incident", uselist=False)


class Signal(Entity):
    __tablename__ = "signal"
    __table_args__ = (Index("ix_signal_incident", "incident_id"), Index("ix_signal_fingerprint", "fingerprint"))

    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"))
    source: Mapped[SignalSource] = mapped_column(_enum(SignalSource, "signal_source"))
    raw: Mapped[Json] = mapped_column(default=dict)
    service: Mapped[str | None] = mapped_column(Text)
    env: Mapped[Env] = mapped_column(_enum(Env, "env"), default=Env.TEST)
    fingerprint: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(default=utcnow)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incident.id"))


class Diagnosis(Entity):
    __tablename__ = "diagnosis"

    incident_id: Mapped[str] = mapped_column(ForeignKey("incident.id", ondelete="CASCADE"), unique=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    root_cause: Mapped[str] = mapped_column(Text, default="")
    hypotheses: Mapped[list[Json]] = mapped_column(JSONB, default=list)
    suspect_locations: Mapped[list[Json]] = mapped_column(JSONB, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    repro: Mapped[Json | None] = mapped_column()  # Reproducer 产物：复现用例 + 命令

    incident: Mapped[Incident] = relationship(back_populates="diagnosis")


class Attempt(Entity):
    __tablename__ = "attempt"
    __table_args__ = (UniqueConstraint("incident_id", "seq"),)

    incident_id: Mapped[str] = mapped_column(ForeignKey("incident.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    status: Mapped[AttemptStatus] = mapped_column(_enum(AttemptStatus, "attempt_status"), default=AttemptStatus.RUNNING)
    feedback: Mapped[str | None] = mapped_column(Text)
    # 本次尝试的起因：initial / verify_failed / review_rejected / human_guided
    # 自动重试上限只统计自上一次人工介入以来连续的 verify_failed
    trigger: Mapped[str] = mapped_column(Text, default="initial")

    incident: Mapped[Incident] = relationship(back_populates="attempts")
    patch: Mapped[Patch | None] = relationship(back_populates="attempt", uselist=False)
    verifications: Mapped[list[Verification]] = relationship(back_populates="attempt")


class Patch(Entity):
    __tablename__ = "patch"

    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempt.id", ondelete="CASCADE"), unique=True)
    diff: Mapped[str] = mapped_column(Text)
    files_changed: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # 附加仓库补丁：{repo_name: {diff, files_changed, commit_sha?, branch?, pr?}}
    repos: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    touches_tests: Mapped[bool] = mapped_column(Boolean, default=False)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text, default="")

    attempt: Mapped[Attempt] = relationship(back_populates="patch")


class Verification(Entity):
    __tablename__ = "verification"

    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempt.id", ondelete="CASCADE"))
    commit_sha: Mapped[str | None] = mapped_column(Text)
    base_sha: Mapped[str | None] = mapped_column(Text)
    checks: Mapped[list[Json]] = mapped_column(JSONB, default=list)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)

    attempt: Mapped[Attempt] = relationship(back_populates="verifications")


class Twin(Entity):
    __tablename__ = "twin"

    incident_id: Mapped[str] = mapped_column(ForeignKey("incident.id", ondelete="CASCADE"), unique=True)
    compose_project: Mapped[str] = mapped_column(Text)
    host: Mapped[str | None] = mapped_column(Text)
    ports: Mapped[Json] = mapped_column(default=dict)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TwinStatus] = mapped_column(_enum(TwinStatus, "twin_status"), default=TwinStatus.CREATING)
    expires_at: Mapped[datetime | None] = mapped_column()
    urls: Mapped[Json] = mapped_column(default=dict)

    incident: Mapped[Incident] = relationship(back_populates="twin")


class Run(Entity):
    __tablename__ = "run"
    __table_args__ = (Index("ix_run_incident", "incident_id"),)

    incident_id: Mapped[str] = mapped_column(ForeignKey("incident.id", ondelete="CASCADE"))
    agent: Mapped[AgentName] = mapped_column(_enum(AgentName, "agent_name"))
    attempt_seq: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(Text)
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus, "run_status"), default=RunStatus.RUNNING)
    outcome: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal(0))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column()

    tool_calls: Mapped[list[ToolCall]] = relationship(back_populates="run", order_by="ToolCall.created_at")


class ToolCall(Entity):
    """审计表：只追加。数据库层由触发器禁止 UPDATE/DELETE（见迁移）。"""

    __tablename__ = "tool_call"
    __table_args__ = (Index("ix_tool_call_run", "run_id"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"))
    incident_id: Mapped[str] = mapped_column(ForeignKey("incident.id"))
    agent: Mapped[AgentName] = mapped_column(_enum(AgentName, "agent_name"))
    tool: Mapped[str] = mapped_column(Text)
    level: Mapped[ToolLevel] = mapped_column(_enum(ToolLevel, "tool_level"))
    args: Mapped[Json] = mapped_column(default=dict)  # 脱敏后
    result_summary: Mapped[str | None] = mapped_column(Text)
    result_ref: Mapped[str | None] = mapped_column(Text)  # evidence id
    status: Mapped[ToolCallStatus] = mapped_column(_enum(ToolCallStatus, "tool_call_status"))
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    approval_id: Mapped[str | None] = mapped_column(ForeignKey("approval.id"))

    run: Mapped[Run] = relationship(back_populates="tool_calls")


class Evidence(Entity):
    __tablename__ = "evidence"
    __table_args__ = (Index("ix_evidence_incident", "incident_id"),)

    incident_id: Mapped[str] = mapped_column(ForeignKey("incident.id", ondelete="CASCADE"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("run.id"))
    kind: Mapped[EvidenceKind] = mapped_column(_enum(EvidenceKind, "evidence_kind"))
    summary: Mapped[str] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)  # 小内容直接入库
    content_ref: Mapped[str | None] = mapped_column(Text)  # 大内容存文件/对象存储
    tokens: Mapped[int] = mapped_column(Integer, default=0)


class Approval(Entity):
    __tablename__ = "approval"
    __table_args__ = (Index("ix_approval_incident_status", "incident_id", "status"),)

    incident_id: Mapped[str] = mapped_column(ForeignKey("incident.id", ondelete="CASCADE"))
    action: Mapped[ApprovalAction] = mapped_column(_enum(ApprovalAction, "approval_action"))
    payload: Mapped[Json] = mapped_column(default=dict)
    status: Mapped[ApprovalStatus] = mapped_column(
        _enum(ApprovalStatus, "approval_status"), default=ApprovalStatus.PENDING
    )
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("user.id"))
    decided_at: Mapped[datetime | None] = mapped_column()
    comment: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column()
    token_hash: Mapped[str | None] = mapped_column(Text)


class Report(Entity):
    __tablename__ = "report"
    __table_args__ = (
        Index("ix_report_incident", "incident_id"),
        # 步骤重放（崩溃恢复）时不重复发同一份报告
        UniqueConstraint("incident_id", "dedupe_key"),
    )

    dedupe_key: Mapped[str] = mapped_column(Text)

    incident_id: Mapped[str] = mapped_column(ForeignKey("incident.id", ondelete="CASCADE"))
    type: Mapped[ReportType] = mapped_column(_enum(ReportType, "report_type"))
    channel: Mapped[str] = mapped_column(Text)
    recipients: Mapped[list[str]] = mapped_column(JSONB, default=list)
    title: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text)  # Markdown 源，按渠道渲染
    sent_at: Mapped[datetime | None] = mapped_column()
    status: Mapped[ReportStatus] = mapped_column(_enum(ReportStatus, "report_status"), default=ReportStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text)


class IncidentEvent(Base):
    """状态变更与过程事件流水。自增 id 作为 SSE 的 Last-Event-ID，天然有序。"""

    __tablename__ = "incident_event"
    __table_args__ = (Index("ix_incident_event_incident_id", "incident_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incident.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text, default="transition")  # transition / tool_call / run / note
    from_state: Mapped[IncidentState | None] = mapped_column(_enum(IncidentState, "incident_state"))
    to_state: Mapped[IncidentState | None] = mapped_column(_enum(IncidentState, "incident_state"))
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text, default="system")
    payload: Mapped[Json] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), default=utcnow)


class User(Entity):
    __tablename__ = "user"

    name: Mapped[str] = mapped_column(Text, unique=True)
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "user_role"), default=UserRole.VIEWER)
    password_hash: Mapped[str | None] = mapped_column(Text)
    im_ids: Mapped[Json] = mapped_column(default=dict)
