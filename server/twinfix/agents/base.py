"""Agent 契约（docs/04-interfaces.md §3）。

Agent 只做一件事：在自己的工具权限内推理，产出结构化结果。
它不改状态、不碰数据库、不决定流程走向——这些是编排器的职责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from twinfix.domain.enums import AgentName

if TYPE_CHECKING:
    from twinfix.agents.params import AgentParams
    from twinfix.gateway.gateway import ToolGateway, ToolResult
    from twinfix.tracing import Budget, RunHandle


@dataclass(frozen=True)
class IncidentView:
    """Agent 能看到的事故快照。只读；不可信内容（日志、Issue 正文）放在 signals 中，作为数据块注入。"""

    id: str
    title: str
    severity: str
    env: str
    service: str | None
    fingerprint: str
    base_commit: str | None
    target_branch: str | None
    signals: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentContext:
    incident: IncidentView
    run: RunHandle
    gateway: ToolGateway
    budget: Budget
    # 上游产物：diagnosis / repro / patch，供下游 Agent 使用
    inputs: dict[str, Any] = field(default_factory=dict)
    feedback: str | None = None  # 上一轮验证失败原因 / 人工驳回意见 / 人工指导
    attempt_seq: int | None = None
    # 本次运行生效的参数（全局默认 + 项目覆盖）；None = Agent 用自己的默认
    params: AgentParams | None = None

    async def tool(self, name: str, **args: Any) -> ToolResult:
        return await self.gateway.call(self.run, name, args)


@dataclass(frozen=True)
class AgentResult:
    outcome: str
    payload: BaseModel
    next_hint: str | None = None


class Agent(Protocol):
    name: AgentName

    async def run(self, ctx: AgentContext) -> AgentResult: ...


# 每个 Agent 允许的 outcome，编排器据此路由；返回其他值视为 Agent 缺陷
OUTCOMES: dict[AgentName, frozenset[str]] = {
    AgentName.DIAGNOSER: frozenset({"diagnosed", "not_bug", "needs_human"}),
    AgentName.REPRODUCER: frozenset({"reproduced", "cannot_reproduce"}),
    AgentName.FIXER: frozenset({"patch_ready", "cannot_fix", "needs_human"}),
    # already_fixed 仅在合并前重验（inputs["mode"] == "premerge"）时有效
    AgentName.VERIFIER: frozenset({"passed", "failed", "already_fixed"}),
    AgentName.REPORTER: frozenset({"reported"}),
}
