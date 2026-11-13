"""Run / ToolCall 追踪与预算。

每次 Agent 执行是一个 Run；Run 下的每次工具调用是一条 ToolCall（由网关写入）。
LLM 用量、成本在 Run 上累计，结束时汇总到 Incident。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from twinfix.domain.enums import AgentName, RunStatus
from twinfix.domain.models import Incident, Run, utcnow
from twinfix.domain.repo import record_event

if TYPE_CHECKING:
    from twinfix.adapters.types import TwinHandle, Workspace
    from twinfix.db import Store
    from twinfix.profiles.base import LanguageProfile


class BudgetExceededError(RuntimeError):
    pass


class IncidentCancelledError(RuntimeError):
    """事故已被请求取消。Agent 不应捕获它；编排器会把事故转为 CANCELLED。"""


class HaltedError(RuntimeError):
    """系统急停。当前步骤中止但不改变事故状态，解除急停后从同一步骤重新开始。"""


@dataclass
class Budget:
    max_tokens: int
    max_tool_calls: int
    max_seconds: int
    tokens: int = 0
    tool_calls: int = 0
    started: float = field(default_factory=time.monotonic)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def check(self) -> None:
        if self.tokens > self.max_tokens:
            raise BudgetExceededError(f"token budget exceeded ({self.tokens}/{self.max_tokens})")
        if self.tool_calls > self.max_tool_calls:
            raise BudgetExceededError(f"tool call budget exceeded ({self.tool_calls}/{self.max_tool_calls})")
        if self.elapsed() > self.max_seconds:
            raise BudgetExceededError(f"time budget exceeded ({int(self.elapsed())}s/{self.max_seconds}s)")

    def remaining(self) -> dict[str, int]:
        return {
            "tokens": self.max_tokens - self.tokens,
            "tool_calls": self.max_tool_calls - self.tool_calls,
            "seconds": int(self.max_seconds - self.elapsed()),
        }


@dataclass
class RunHandle:
    """一次 Agent 执行的上下文与计量。网关依据其中的 agent、scope 做授权与路径约束。"""

    id: str
    incident_id: str
    agent: AgentName
    budget: Budget
    attempt_seq: int | None = None
    workspace: Workspace | None = None
    twin: TwinHandle | None = None
    protected_paths: tuple[str, ...] = ()
    test_paths: tuple[str, ...] = ()
    repo: str | None = None
    # 本次事故使用的语言插件；未设置时工具退回 adapters.profile
    profile: LanguageProfile | None = None
    # 附加仓库（只读）：name -> {repo, sha, root}；root 是工作区内的相对目录
    repo_workspaces: dict[str, dict[str, str]] = field(default_factory=dict)
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal(0)
    tool_counts: dict[str, int] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    # 已落库的用量；add_usage 只改内存，persist_usage 把增量写回数据库
    flushed_input_tokens: int = 0
    flushed_output_tokens: int = 0
    flushed_cost: Decimal = Decimal(0)
    on_usage: Callable[[RunHandle], Awaitable[None]] | None = field(default=None, repr=False)

    @property
    def actor(self) -> str:
        return f"agent:{self.agent.value}"

    def add_usage(self, input_tokens: int, output_tokens: int, cost: Decimal = Decimal(0)) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost += cost
        self.budget.tokens += input_tokens + output_tokens

    async def persist_usage(self) -> None:
        """把上次落库之后的用量增量写回 Run / Incident。

        每次 LLM 调用后调用一次：事故被取消、超时或被硬中断时，已花掉的 token 仍然落在库里，
        实时控制台和评测报告都能看到真实开销，而不是等 RunTracker.finish 才结算。
        """
        if self.on_usage is not None:
            await self.on_usage(self)


class RunTracker:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def start(
        self,
        incident_id: str,
        agent: AgentName,
        budget: Budget,
        *,
        attempt_seq: int | None = None,
        model: str | None = None,
        **scope: Any,
    ) -> RunHandle:
        async with self.store.tx() as tx:
            run = Run(incident_id=incident_id, agent=agent, attempt_seq=attempt_seq, model=model)
            tx.session.add(run)
            await tx.session.flush()
            await record_event(
                tx,
                incident_id,
                "run_started",
                actor=f"agent:{agent.value}",
                payload={"run_id": run.id, "agent": agent.value, "attempt_seq": attempt_seq},
            )
        return RunHandle(
            id=run.id,
            incident_id=incident_id,
            agent=agent,
            budget=budget,
            attempt_seq=attempt_seq,
            model=model,
            on_usage=self._flush_usage,
            **scope,
        )

    async def _flush_usage(self, handle: RunHandle) -> None:
        """只写增量，且用 SQL 自增，避免同一事故多个 Run 并发结算时互相覆盖。

        同时记一条 usage 事件：详情页靠它触发刷新，Token 消耗才能随每次模型调用更新。
        """
        in_delta = handle.input_tokens - handle.flushed_input_tokens
        out_delta = handle.output_tokens - handle.flushed_output_tokens
        cost_delta = handle.cost - handle.flushed_cost
        if not (in_delta or out_delta or cost_delta):
            return
        async with self.store.tx() as tx:
            await tx.session.execute(
                update(Run)
                .where(Run.id == handle.id)
                .values(
                    input_tokens=Run.input_tokens + in_delta,
                    output_tokens=Run.output_tokens + out_delta,
                    cost=Run.cost + cost_delta,
                )
            )
            await tx.session.execute(
                update(Incident)
                .where(Incident.id == handle.incident_id)
                .values(
                    cost_tokens=Incident.cost_tokens + in_delta + out_delta,
                    cost_amount=Incident.cost_amount + cost_delta,
                )
            )
            await record_event(
                tx,
                handle.incident_id,
                "usage",
                actor=handle.actor,
                payload={
                    "run_id": handle.id,
                    "agent": handle.agent.value,
                    "input_tokens": handle.input_tokens,
                    "output_tokens": handle.output_tokens,
                    "cost_tokens": handle.input_tokens + handle.output_tokens,
                },
            )
        handle.flushed_input_tokens += in_delta
        handle.flushed_output_tokens += out_delta
        handle.flushed_cost += cost_delta

    async def finish(
        self, handle: RunHandle, status: RunStatus, *, outcome: str | None = None, error: str | None = None
    ) -> None:
        latency_ms = int((time.monotonic() - handle.started) * 1000)
        # 先结清最后一次 LLM 调用之后的用量；之后 Run 上写的是最终总量，Incident 只累加增量
        await self._flush_usage(handle)
        tokens = handle.input_tokens + handle.output_tokens
        async with self.store.tx() as tx:
            await tx.session.execute(
                update(Run)
                .where(Run.id == handle.id)
                .values(
                    status=status,
                    outcome=outcome,
                    error=error,
                    model=handle.model,
                    input_tokens=handle.input_tokens,
                    output_tokens=handle.output_tokens,
                    cost=handle.cost,
                    latency_ms=latency_ms,
                    finished_at=utcnow(),
                )
            )
            await record_event(
                tx,
                handle.incident_id,
                "run_finished",
                actor=handle.actor,
                payload={
                    "run_id": handle.id,
                    "agent": handle.agent.value,
                    "status": status.value,
                    "outcome": outcome,
                    "error": error,
                    "latency_ms": latency_ms,
                    "tokens": tokens,
                    "tool_calls": handle.budget.tool_calls,
                },
            )

    async def abandon_running(self, incident_id: str, *, reason: str) -> int:
        """把一个事故下仍停在 running 的 Run 结清，返回处理条数。

        进程被杀（宿主机重启、容器重建）时 Run 来不及 finish，会永远停在 running，
        控制台就一直显示"运行中"。worker 重新拿到事故锁后调用这里统一收尾。
        """
        async with self.store.tx() as tx:
            rows = (
                await tx.session.execute(
                    select(Run.id, Run.agent).where(Run.incident_id == incident_id, Run.status == RunStatus.RUNNING)
                )
            ).all()
            if not rows:
                return 0
            await tx.session.execute(
                update(Run)
                .where(Run.id.in_([row.id for row in rows]))
                .values(status=RunStatus.FAILED, error=reason, finished_at=utcnow())
            )
            for row in rows:
                await record_event(
                    tx,
                    incident_id,
                    "run_finished",
                    actor=f"agent:{row.agent.value}",
                    payload={
                        "run_id": row.id,
                        "agent": row.agent.value,
                        "status": RunStatus.FAILED.value,
                        "error": reason,
                    },
                )
        return len(rows)
