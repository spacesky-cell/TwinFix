"""HTTP 请求/响应模型。"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from twinfix.domain.enums import Env, IncidentState, Severity, SignalSource, UserRole


class LoginIn(BaseModel):
    # 用户名或邮箱
    name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1000)


class SignalCreate(BaseModel):
    project: str
    title: str = Field(min_length=1, max_length=500)
    source: SignalSource = SignalSource.MANUAL
    service: str | None = None
    env: Env = Env.TEST
    severity: Severity | None = None
    message: str | None = Field(None, max_length=100_000)
    stacktrace: str | None = Field(None, max_length=200_000)
    trace_id: str | None = None
    fingerprint: str | None = None
    occurred_at: datetime | None = None


class IncidentCreate(SignalCreate):
    github_issue: int | None = None
    # 选择内置模拟场景，用于演示和验证每条分支
    scenario: str | None = Field(None, pattern=r"^[a-z_]+$")


class IngestOut(BaseModel):
    incident_id: str
    signal_id: str
    created: bool
    recurrence_of: str | None


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(ORM):
    id: str
    name: str
    role: UserRole
    email: str | None = None
    created_at: datetime | None = None


_USER_NAME = r"^[A-Za-z0-9._-]{2,64}$"


class UserCreate(BaseModel):
    name: str = Field(pattern=_USER_NAME)
    email: str | None = Field(None, max_length=200)
    password: str = Field(min_length=8, max_length=1000)
    role: UserRole = UserRole.VIEWER


class UserPatch(BaseModel):
    email: str | None = Field(None, max_length=200)
    password: str | None = Field(None, min_length=8, max_length=1000)
    role: UserRole | None = None


class TokenOut(BaseModel):
    token: str
    user: UserOut


class IncidentSummary(ORM):
    id: str
    title: str
    state: IncidentState
    severity: Severity
    env: Env
    service: str | None
    signal_count: int
    target_branch: str | None
    fix_branch: str | None
    pr_number: int | None
    assignee_id: str | None = None
    cost_tokens: int
    created_at: datetime
    updated_at: datetime
    state_entered_at: datetime


class IncidentDetail(IncidentSummary):
    project_id: str
    fingerprint: str
    base_commit: str | None
    github_issue: int | None
    recurrence_of: str | None
    cost_amount: Decimal
    closed_reason: str | None
    cancel_requested: bool
    meta: dict[str, Any]
    diagnosis: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = []
    twin: dict[str, Any] | None = None
    assignee: dict[str, str] | None = None
    approvals: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []


class IncidentPage(BaseModel):
    items: list[IncidentSummary]
    total: int


class EventOut(ORM):
    id: int
    incident_id: str
    kind: str
    from_state: IncidentState | None
    to_state: IncidentState | None
    reason: str | None
    actor: str
    payload: dict[str, Any]
    created_at: datetime


class GuideIn(BaseModel):
    guidance: str = Field(min_length=1, max_length=5000)
    restart_from: Literal["DIAGNOSING", "REPRODUCING", "FIXING", "MERGING"] | None = None


class CancelIn(BaseModel):
    reason: str | None = Field(None, max_length=2000)


class ResolveIn(BaseModel):
    verdict: Literal["not_bug", "cannot_fix", "already_fixed"]
    reason: str | None = Field(None, max_length=2000)


class DecideIn(BaseModel):
    approve: bool
    comment: str | None = Field(None, max_length=5000)
    # 驳回原因：root_cause → 回到诊断；patch / assertion → 回到修复
    reason_kind: Literal["root_cause", "patch", "assertion"] | None = None


class AssigneeIn(BaseModel):
    # null 表示取消指派
    user_id: str | None = Field(None, max_length=26)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    language: Literal["java", "python", "go", "typescript", "csharp"] = "java"
    branch_map: dict[str, str] = Field(default_factory=lambda: {"test": "dev", "release": "release", "prod": "main"})
    config: dict[str, Any] = Field(default_factory=dict)


class ProjectPatch(BaseModel):
    """项目配置更新：只改给出的字段；config 是整体替换（控制台提交合并后的完整对象）。"""

    repo: str | None = Field(None, pattern=r"^[\w.-]+/[\w.-]+$")
    language: Literal["java", "python", "go", "typescript", "csharp"] | None = None
    branch_map: dict[str, str] | None = None
    config: dict[str, Any] | None = None


class ProjectOut(ORM):
    id: str
    name: str
    repo: str
    language: str
    branch_map: dict[str, str]
    config: dict[str, Any]
    created_at: datetime


class HaltIn(BaseModel):
    halted: bool


class ArenaInjectIn(BaseModel):
    case_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,20}$")
    # 空 = 自动生成 arena-<case>-<时分秒>
    project: str | None = Field(None, pattern=r"^[A-Za-z0-9._-]{1,100}$")
    trigger: bool = True  # 注入后立即上报用例信号，一键走完整闭环


class ArenaInjectOut(BaseModel):
    case_id: str
    project: str
    repo: str
    branch: str
    sha: str
    incident_id: str | None = None
    created: bool | None = None


_CASE_ID = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


class EvalRunIn(BaseModel):
    cases: list[str] = Field(default_factory=list, max_length=50)
    label: str = Field("", max_length=200)
    model: str = Field("", max_length=200)
    effort: Literal["", "low", "medium", "high"] = ""
    timeout_s: float = Field(3600, ge=60, le=24 * 3600)

    @field_validator("cases")
    @classmethod
    def _valid_case_ids(cls, values: list[str]) -> list[str]:
        for v in values:
            if not _CASE_ID.match(v):
                raise ValueError(f"invalid case id {v!r}")
        return values
