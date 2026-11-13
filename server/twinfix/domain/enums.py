from __future__ import annotations

from enum import StrEnum


class IncidentState(StrEnum):
    NEW = "NEW"
    TRIAGED = "TRIAGED"
    DIAGNOSING = "DIAGNOSING"
    REPRODUCING = "REPRODUCING"
    FIXING = "FIXING"
    VERIFYING = "VERIFYING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    MERGING = "MERGING"
    MERGED = "MERGED"
    # 合并后观察期：同指纹再出现信号即视为未修复；观察期满无复发才归档
    OBSERVING = "OBSERVING"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    # 终态
    NOT_BUG = "NOT_BUG"
    CANNOT_REPRODUCE = "CANNOT_REPRODUCE"
    CANNOT_FIX = "CANNOT_FIX"
    ALREADY_FIXED = "ALREADY_FIXED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


TERMINAL_STATES: frozenset[IncidentState] = frozenset(
    {
        IncidentState.NOT_BUG,
        IncidentState.CANNOT_REPRODUCE,
        IncidentState.CANNOT_FIX,
        IncidentState.ALREADY_FIXED,
        IncidentState.CANCELLED,
        IncidentState.CLOSED,
    }
)

# 等待人工输入的状态：编排在这里挂起（LangGraph interrupt），由审批/取消/超时恢复
WAITING_STATES: frozenset[IncidentState] = frozenset({IncidentState.AWAITING_REVIEW, IncidentState.NEEDS_HUMAN})


class Severity(StrEnum):
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"


class Env(StrEnum):
    TEST = "test"
    RELEASE = "release"
    PROD = "prod"


class SignalSource(StrEnum):
    ALERT = "alert"
    LOG = "log"
    ISSUE = "issue"
    MANUAL = "manual"
    IM = "im"
    FRONTEND = "frontend"


class ToolLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2_SANDBOX = "L2-sandbox"
    L2_EXTERNAL = "L2-external"
    L3 = "L3"

    @property
    def rank(self) -> int:
        return _LEVEL_RANK[self]


_LEVEL_RANK = {
    ToolLevel.L0: 0,
    ToolLevel.L1: 1,
    ToolLevel.L2_SANDBOX: 2,
    ToolLevel.L2_EXTERNAL: 3,
    ToolLevel.L3: 4,
}


class AgentName(StrEnum):
    # 编排器自身的系统操作（推分支、开 PR、合并）也走网关并留审计
    ORCHESTRATOR = "orchestrator"
    DIAGNOSER = "diagnoser"
    REPRODUCER = "reproducer"
    FIXER = "fixer"
    VERIFIER = "verifier"
    REPORTER = "reporter"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ABORTED = "aborted"


class TwinStatus(StrEnum):
    CREATING = "creating"
    READY = "ready"
    FAILED = "failed"
    DESTROYED = "destroyed"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolCallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"
    TIMEOUT = "timeout"
    PENDING_APPROVAL = "pending_approval"


class ApprovalAction(StrEnum):
    MERGE = "merge"
    HUMAN_TAKEOVER = "human_takeover"
    TOOL = "tool"  # 高级别工具调用的逐次审批


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ReportType(StrEnum):
    FIXED = "fixed"
    MERGED = "merged"
    CANNOT_REPRODUCE = "cannot_reproduce"
    CANNOT_FIX = "cannot_fix"
    NEEDS_HUMAN = "needs_human"
    NOT_BUG = "not_bug"
    ALREADY_FIXED = "already_fixed"
    CANCELLED = "cancelled"


class ReportStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    # 静默时段命中路由规则，未发送但完整保留在控制台
    SUPPRESSED = "suppressed"


class UserRole(StrEnum):
    ADMIN = "admin"
    MAINTAINER = "maintainer"
    VIEWER = "viewer"


class EvidenceKind(StrEnum):
    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    THREAD_DUMP = "thread_dump"
    CODE = "code"
    TEST_OUTPUT = "test_output"
    DEPLOY = "deploy"
    TOOL_OUTPUT = "tool_output"
