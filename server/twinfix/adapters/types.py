"""适配器之间共享的值对象。只描述数据，不含行为。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class Workspace:
    id: str
    repo: str
    sha: str
    root: Path  # 宿主机视角的路径（LocalSandbox）或容器内路径（DockerSandbox）
    image: str | None = None


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


# ---- 可观测 ----


@dataclass(frozen=True)
class MetricQuery:
    expr: str
    start: datetime
    end: datetime
    step_s: int = 60


@dataclass(frozen=True)
class MetricSeries:
    expr: str
    points: list[tuple[datetime, float]]
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LogQuery:
    service: str | None
    query: str
    start: datetime
    end: datetime
    trace_id: str | None = None
    limit: int = 200


@dataclass(frozen=True)
class LogLine:
    ts: datetime
    level: str
    message: str
    service: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class LogPage:
    lines: list[LogLine]
    total: int


@dataclass(frozen=True)
class Span:
    span_id: str
    parent_id: str | None
    service: str
    operation: str
    duration_ms: int
    error: bool = False
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Trace:
    trace_id: str
    spans: list[Span]


@dataclass(frozen=True)
class Deploy:
    service: str
    sha: str
    env: str
    deployed_at: datetime


# ---- 代码托管 ----


@dataclass(frozen=True)
class PRRequest:
    repo: str
    head: str
    base: str
    title: str
    body: str
    draft: bool = False


@dataclass(frozen=True)
class PRInfo:
    number: int
    url: str
    head_sha: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    conclusion: Literal["success", "failure", "neutral"]
    summary: str
    details_url: str | None = None


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    sha: str | None
    message: str = ""


@dataclass(frozen=True)
class IssueRef:
    repo: str
    number: int


@dataclass(frozen=True)
class PRRef:
    repo: str
    number: int


# ---- CI ----


@dataclass(frozen=True)
class BuildRef:
    job: str
    id: str


BuildState = Literal["queued", "running", "success", "failure", "aborted"]


@dataclass(frozen=True)
class BuildStatus:
    ref: BuildRef
    state: BuildState
    url: str | None = None


# ---- 孪生环境 ----


@dataclass(frozen=True)
class TwinSpec:
    incident_id: str
    repo: str
    sha: str
    data_snapshot: str | None = None
    ttl_s: int = 24 * 3600
    # 项目级孪生模板覆盖（project.config["twin"]），由 TwinProvider 自行解释；空表示用全局默认
    template: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TwinHandle:
    compose_project: str
    host: str
    ports: dict[str, int]
    urls: dict[str, str]
    sha: str
    # 健康检查路径（K8s 模板可自定义）；None = 默认 /actuator/health
    health_path: str | None = None


@dataclass(frozen=True)
class HttpRequest:
    service: str
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: str
    duration_ms: int


# ---- 通知 ----


@dataclass(frozen=True)
class Recipient:
    channel: str  # email / feishu / log
    address: str


@dataclass(frozen=True)
class OutgoingReport:
    id: str
    incident_id: str
    type: str
    title: str
    content: str  # Markdown


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: str | None = None
