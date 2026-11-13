"""工具网关：Agent 与外部世界之间唯一的出口。

执行顺序（docs/04-interfaces.md §4）：
  急停/取消 → 授权（白名单 + 级别）→ 参数 schema → 策略（路径、频率、预算）
  → 审批 → 执行（超时）→ 结果脱敏/截断/存证据 → 审计（只追加）+ 事件
任何一步拒绝都会写审计，Agent 收到的是一条可读的拒绝原因，而不是异常。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import select

from twinfix.domain.enums import TERMINAL_STATES, ApprovalStatus, EvidenceKind, ToolCallStatus
from twinfix.domain.models import Approval, Evidence, Incident, ToolCall
from twinfix.domain.repo import record_event
from twinfix.gateway import paths
from twinfix.gateway.policy import POLICIES
from twinfix.gateway.redact import Redactor, default_redactor
from twinfix.gateway.registry import Tool, ToolEnv, ToolOutput, ToolRegistry
from twinfix.otel import span
from twinfix.tracing import BudgetExceededError, HaltedError, IncidentCancelledError, RunHandle

if TYPE_CHECKING:
    from twinfix.container import Adapters
    from twinfix.control import ControlPlane
    from twinfix.db import Store

log = logging.getLogger(__name__)


class ToolError(Exception):
    """工具执行中可预期的失败（编译失败不算，那是正常结果）。消息会返回给 Agent。"""


@dataclass(frozen=True)
class ToolResult:
    status: ToolCallStatus
    content: str  # 给 LLM 的文本：已脱敏、已截断
    data: Any = None
    evidence_id: str | None = None
    tool_call_id: str | None = None
    approval_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is ToolCallStatus.OK


class _Denied(Exception):
    def __init__(self, reason: str, status: ToolCallStatus = ToolCallStatus.DENIED) -> None:
        super().__init__(reason)
        self.status = status


class ToolGateway:
    def __init__(
        self,
        store: Store,
        registry: ToolRegistry,
        adapters: Adapters,
        control: ControlPlane,
        redactor: Redactor = default_redactor,
        log_cluster: bool = True,
    ) -> None:
        self.store = store
        self.registry = registry
        self.adapters = adapters
        self.control = control
        self.redactor = redactor
        self.log_cluster = log_cluster

    def tools_for(self, run: RunHandle) -> list[Tool]:
        policy = POLICIES[run.agent]
        names = set(policy.tools)
        # JVM 探针只对带运行时探针的语言插件有意义；Python/Go/TypeScript 项目不再向模型露出
        profile = getattr(run, "profile", None) or getattr(self.adapters, "profile", None)
        probe = getattr(profile, "runtime_probe", None)
        if callable(probe) and probe() is None:
            names.discard("jvm_probe")
        return [t for name in sorted(names) if (t := self.registry.get(name)) is not None]

    async def call(
        self, run: RunHandle, tool_name: str, args: dict[str, Any], *, approval_id: str | None = None
    ) -> ToolResult:
        """对外入口：包一层 tool.call span，记录工具、状态与关联 id；授权/审计逻辑在 _call。"""
        with span(
            "tool.call",
            **{
                "twinfix.tool": tool_name,
                "twinfix.run_id": run.id,
                "twinfix.incident_id": run.incident_id,
                "twinfix.agent": run.agent.value,
            },
        ) as current:
            result = await self._call(run, tool_name, args, approval_id=approval_id)
            current.set_attribute("twinfix.status", result.status.value)
            if result.evidence_id:
                current.set_attribute("twinfix.evidence_id", result.evidence_id)
            if result.tool_call_id:
                current.set_attribute("twinfix.tool_call_id", result.tool_call_id)
            if result.approval_id:
                current.set_attribute("twinfix.approval_id", result.approval_id)
            return result

    async def _call(
        self, run: RunHandle, tool_name: str, args: dict[str, Any], *, approval_id: str | None = None
    ) -> ToolResult:
        await self._check_cancelled(run)  # 取消/急停是硬中断，直接抛出，不当作普通拒绝
        started = time.monotonic()
        tool = self.registry.get(tool_name)
        safe_args = self.redactor.value(args)
        try:
            tool = self._authorize(run, tool_name, tool)
            parsed = self._validate(tool, args)
            self._check_policy(run, tool, parsed)
            if tool.approval is not None:
                await self._check_approval(run, tool, safe_args, approval_id)
        except _Denied as denied:
            pending = denied.approval_id if isinstance(denied, _PendingApproval) else approval_id
            return await self._audit(
                run,
                tool_name,
                tool,
                safe_args,
                denied.status,
                started,
                content=str(denied),
                error=str(denied),
                approval_id=pending,
            )

        run.tool_counts[tool.name] = run.tool_counts.get(tool.name, 0) + 1
        run.budget.tool_calls += 1
        env = ToolEnv(run=run, adapters=self.adapters, log_cluster=self.log_cluster)
        try:
            output = await asyncio.wait_for(tool.handler(env, parsed), timeout=tool.timeout_s)
        except TimeoutError:
            msg = f"tool {tool.name} timed out after {tool.timeout_s}s"
            return await self._audit(
                run, tool.name, tool, safe_args, ToolCallStatus.TIMEOUT, started, content=msg, error=msg
            )
        except PermissionError as exc:
            return await self._audit(
                run, tool.name, tool, safe_args, ToolCallStatus.DENIED, started, content=str(exc), error=str(exc)
            )
        except ToolError as exc:
            return await self._audit(
                run, tool.name, tool, safe_args, ToolCallStatus.ERROR, started, content=f"error: {exc}", error=str(exc)
            )
        except Exception as exc:
            log.exception("tool %s crashed", tool.name)
            msg = f"internal error in tool {tool.name}: {type(exc).__name__}"
            return await self._audit(
                run, tool.name, tool, safe_args, ToolCallStatus.ERROR, started, content=msg, error=repr(exc)
            )

        return await self._audit(
            run, tool.name, tool, safe_args, ToolCallStatus.OK, started, output=output, approval_id=approval_id
        )

    # ---- 检查 ----

    async def _check_cancelled(self, run: RunHandle) -> None:
        if await self.control.is_halted():
            raise HaltedError("system halted")
        async with self.store.read() as s:
            row = (
                await s.execute(select(Incident.cancel_requested, Incident.state).where(Incident.id == run.incident_id))
            ).one_or_none()
        # 进入终态后的收尾（发终态报告）不受取消标记影响，只中断仍在进行中的工作
        if row is not None and row.cancel_requested and row.state not in TERMINAL_STATES:
            raise IncidentCancelledError("incident cancel requested")

    def _authorize(self, run: RunHandle, name: str, tool: Tool | None) -> Tool:
        policy = POLICIES[run.agent]
        if tool is None or name not in policy.tools:
            raise _Denied(f"tool {name!r} is not available to {run.agent.value}")
        if tool.level.rank > policy.max_level.rank:
            raise _Denied(
                f"tool {name!r} level {tool.level.value} exceeds {run.agent.value} max {policy.max_level.value}"
            )
        return tool

    def _validate(self, tool: Tool, args: dict[str, Any]) -> Any:
        try:
            return tool.args_model.model_validate(args)
        except ValidationError as exc:
            raise _Denied(f"invalid arguments for {tool.name}: {exc.errors(include_url=False)}") from exc

    def _check_policy(self, run: RunHandle, tool: Tool, parsed: Any) -> None:
        if run.tool_counts.get(tool.name, 0) >= tool.max_calls_per_run:
            raise _Denied(f"rate limit: {tool.name} called {tool.max_calls_per_run} times in this run")
        try:
            run.budget.tool_calls += 1
            run.budget.check()
        except BudgetExceededError as exc:
            raise _Denied(str(exc)) from exc
        finally:
            run.budget.tool_calls -= 1

        for field_name in (*tool.read_path_args, *tool.write_path_args):
            value = getattr(parsed, field_name)
            if paths.escapes(value):
                raise _Denied(f"path {value!r} is outside the workspace")

        scope = POLICIES[run.agent].write_scope
        for field_name in tool.write_path_args:
            value = getattr(parsed, field_name)
            if scope == "none":
                raise _Denied(f"{run.agent.value} may not write files")
            if paths.matches(value, run.protected_paths):
                raise _Denied(f"path {value!r} is protected by project config")
            is_test = paths.matches(value, run.test_paths)
            if scope == "tests" and not is_test:
                raise _Denied(
                    f"{run.agent.value} may only write test files; {value!r} is not under {list(run.test_paths)}"
                )

    async def _check_approval(
        self, run: RunHandle, tool: Tool, safe_args: dict[str, Any], approval_id: str | None
    ) -> None:
        async with self.store.tx() as tx:
            if approval_id is not None:
                approval = await tx.session.get(Approval, approval_id)
                if (
                    approval is not None
                    and approval.incident_id == run.incident_id
                    and approval.action == tool.approval
                    and approval.status is ApprovalStatus.APPROVED
                ):
                    return
                raise _Denied(f"approval {approval_id} is not an approved {tool.approval} for this incident")
            approval = Approval(
                incident_id=run.incident_id, action=tool.approval, payload={"tool": tool.name, "args": safe_args}
            )
            tx.session.add(approval)
            await tx.session.flush()
            await record_event(
                tx,
                run.incident_id,
                "approval_requested",
                actor=run.actor,
                payload={"approval_id": approval.id, "action": str(tool.approval), "tool": tool.name},
            )
        raise _PendingApproval(approval.id)

    # ---- 审计 ----

    async def _audit(
        self,
        run: RunHandle,
        tool_name: str,
        tool: Tool | None,
        safe_args: dict[str, Any],
        status: ToolCallStatus,
        started: float,
        *,
        output: ToolOutput | None = None,
        content: str = "",
        error: str | None = None,
        approval_id: str | None = None,
    ) -> ToolResult:
        latency_ms = int((time.monotonic() - started) * 1000)
        evidence_id: str | None = None
        data: Any = None
        summary = content
        max_chars = tool.max_result_chars if tool else 2000

        async with self.store.tx() as tx:
            if output is not None:
                data = output.data
                full = self.redactor.text(output.content)
                # evidence_content：content 是压缩视图（如聚类后的日志）时，证据库存原始全文
                stored = full if output.evidence_content is None else self.redactor.text(output.evidence_content)
                truncated = len(full) > max_chars
                if output.evidence_kind is not None or truncated:
                    ev = Evidence(
                        incident_id=run.incident_id,
                        run_id=run.id,
                        kind=output.evidence_kind or EvidenceKind.TOOL_OUTPUT,
                        summary=self.redactor.text(output.summary or full[:200]),
                        content=stored,
                        tokens=len(stored) // 4,
                    )
                    tx.session.add(ev)
                    await tx.session.flush()
                    evidence_id = ev.id
                content = full[:max_chars]
                if truncated:
                    content += f"\n…[truncated {len(full) - max_chars} chars; full text in evidence {evidence_id}]"
                elif evidence_id and output.evidence_content is not None:
                    content += f"\n[raw lines in evidence {evidence_id}]"
                summary = self.redactor.text(output.summary or full[:300])

            call = ToolCall(
                run_id=run.id,
                incident_id=run.incident_id,
                agent=run.agent,
                tool=tool_name,
                level=tool.level if tool else POLICIES[run.agent].max_level,
                args=safe_args,
                result_summary=summary[:1000],
                result_ref=evidence_id,
                status=status,
                error=error,
                latency_ms=latency_ms,
                approval_id=approval_id,
            )
            tx.session.add(call)
            await tx.session.flush()
            await record_event(
                tx,
                run.incident_id,
                "tool_call",
                actor=run.actor,
                payload={
                    "tool_call_id": call.id,
                    "run_id": run.id,
                    "tool": tool_name,
                    "status": status.value,
                    "latency_ms": latency_ms,
                    "summary": summary[:300],
                    "evidence_id": evidence_id,
                },
            )
        return ToolResult(
            status=status,
            content=content,
            data=data,
            evidence_id=evidence_id,
            tool_call_id=call.id,
            approval_id=approval_id,
        )


class _PendingApproval(_Denied):
    def __init__(self, approval_id: str) -> None:
        super().__init__(f"approval required: {approval_id}", ToolCallStatus.PENDING_APPROVAL)
        self.approval_id = approval_id
