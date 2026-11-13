"""应用服务：API 与 worker 共用的用例。HTTP 层只做参数校验与鉴权，业务都在这里。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from twinfix.db import Store
from twinfix.domain.enums import (
    TERMINAL_STATES,
    WAITING_STATES,
    ApprovalAction,
    ApprovalStatus,
    Env,
    IncidentState,
    Severity,
    SignalSource,
    UserRole,
)
from twinfix.domain.models import Approval, Incident, Project, Signal, utcnow
from twinfix.domain.repo import lock_incident, record_event
from twinfix.triage.rules import fingerprint


class Dispatcher(Protocol):
    """把"推进/恢复某事故"的请求交给 worker。实现：RabbitMQ（生产）或内存直调（测试）。"""

    async def advance(self, incident_id: str) -> None: ...
    async def resume(self, incident_id: str, decision: dict[str, Any]) -> None: ...


class ConflictError(Exception):
    pass


class NotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class SignalIn:
    project: str
    source: SignalSource
    title: str
    service: str | None = None
    env: Env = Env.TEST
    severity: Severity | None = None
    message: str | None = None
    stacktrace: str | None = None
    trace_id: str | None = None
    fingerprint: str | None = None
    occurred_at: datetime | None = None
    github_issue: int | None = None
    raw: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class IngestResult:
    incident_id: str
    signal_id: str
    created: bool
    recurrence_of: str | None = None


class IncidentService:
    def __init__(self, store: Store, dispatcher: Dispatcher) -> None:
        self.store = store
        self.dispatcher = dispatcher

    async def ingest(self, sig: SignalIn) -> IngestResult:
        """信号入库并聚合：同项目内同指纹的未关闭事故只累加计数，否则新建事故（必要时标记复发）。"""
        for _ in range(3):  # 并发创建同指纹事故时，唯一索引冲突后重试一次即可归并
            try:
                result = await self._ingest_once(sig)
            except IntegrityError:
                continue
            if result.created:
                await self.dispatcher.advance(result.incident_id)
            else:
                # 观察期内同指纹信号复发：唤醒挂起的观察期节点，让它第一时间转人工
                async with self.store.read() as s:
                    state = await s.scalar(select(Incident.state).where(Incident.id == result.incident_id))
                if state is IncidentState.OBSERVING:
                    await self.dispatcher.resume(result.incident_id, {"action": "observation_recheck"})
            return result
        raise ConflictError("could not ingest signal due to concurrent writes")

    async def _ingest_once(self, sig: SignalIn) -> IngestResult:
        async with self.store.tx() as tx:
            project = await tx.session.scalar(select(Project).where(Project.name == sig.project))
            if project is None:
                raise NotFoundError(f"project {sig.project!r} not found")
            packages = tuple(project.config.get("project_packages", ()))
            fp = fingerprint(
                service=sig.service,
                title=sig.title,
                stacktrace=sig.stacktrace,
                explicit=sig.fingerprint,
                project_packages=packages,
            )
            raw = dict(sig.raw or {})
            raw.update(
                {
                    k: v
                    for k, v in {"message": sig.message, "stacktrace": sig.stacktrace, "title": sig.title}.items()
                    if v
                }
            )
            signal = Signal(
                project_id=project.id,
                source=sig.source,
                raw=raw,
                service=sig.service,
                env=sig.env,
                fingerprint=fp,
                trace_id=sig.trace_id,
                occurred_at=sig.occurred_at or utcnow(),
            )
            open_inc = await tx.session.scalar(
                select(Incident)
                .where(
                    Incident.project_id == project.id,
                    Incident.fingerprint == fp,
                    Incident.state.not_in(TERMINAL_STATES),
                )
                .with_for_update()
            )
            if open_inc is not None:
                open_inc.signal_count += 1
                signal.incident_id = open_inc.id
                tx.session.add(signal)
                await tx.session.flush()
                await record_event(
                    tx, open_inc.id, "signal", payload={"signal_id": signal.id, "count": open_inc.signal_count}
                )
                return IngestResult(open_inc.id, signal.id, created=False)

            previous = await tx.session.scalar(
                select(Incident.id)
                .where(
                    Incident.project_id == project.id,
                    Incident.fingerprint == fp,
                    Incident.state == IncidentState.CLOSED,
                )
                .order_by(Incident.created_at.desc())
            )
            inc = Incident(
                project_id=project.id,
                title=sig.title[:500],
                fingerprint=fp,
                env=sig.env,
                severity=sig.severity or Severity.S3,
                service=sig.service,
                signal_count=1,
                github_issue=sig.github_issue,
                recurrence_of=previous,
                meta=dict(sig.meta or {}),
            )
            tx.session.add(inc)
            await tx.session.flush()
            signal.incident_id = inc.id
            tx.session.add(signal)
            await tx.session.flush()
            await record_event(
                tx,
                inc.id,
                "created",
                actor=f"source:{sig.source.value}",
                payload={"signal_id": signal.id, "recurrence_of": previous, "title": inc.title},
                to_state=IncidentState.NEW,
            )
            return IngestResult(inc.id, signal.id, created=True, recurrence_of=previous)

    async def cancel(self, incident_id: str, actor: str, reason: str | None = None) -> None:
        async with self.store.tx() as tx:
            inc = await lock_incident(tx, incident_id)
            if inc.state in TERMINAL_STATES:
                raise ConflictError(f"incident already {inc.state}")
            inc.cancel_requested = True
            meta = dict(inc.meta)
            meta["cancelled_by"] = actor
            if reason:
                # 编排收尾时用这条原因作为 closed_reason，比固定的“取消请求”更有信息量
                meta["cancel_reason"] = reason
            inc.meta = meta
            state = inc.state
            await record_event(tx, incident_id, "cancel_requested", actor=actor, reason=reason)
        if state in WAITING_STATES or state is IncidentState.OBSERVING:
            await self.dispatcher.resume(
                incident_id, {"action": "cancel", "actor": actor, "reason": reason or "人工取消"}
            )
        else:
            # 运行中的 Agent 会在下一次工具调用时收到取消；同时补发一步，确保空闲中的事故也能及时收尾
            await self.dispatcher.advance(incident_id)

    async def guide(self, incident_id: str, actor: str, guidance: str, restart_from: IncidentState | None) -> None:
        """人工指导：运行中的事故把意见注入下一个 Agent；NEEDS_HUMAN 中的事故按指导重新开始。"""
        async with self.store.tx() as tx:
            inc = await lock_incident(tx, incident_id)
            if inc.state in TERMINAL_STATES:
                raise ConflictError(f"incident already {inc.state}")
            state = inc.state
            if state is not IncidentState.NEEDS_HUMAN:
                meta = dict(inc.meta)
                meta["guidance"] = guidance
                inc.meta = meta
            await record_event(tx, incident_id, "guidance", actor=actor, reason=guidance)
        if state is IncidentState.NEEDS_HUMAN:
            target = restart_from or IncidentState.DIAGNOSING
            await self.dispatcher.resume(
                incident_id, {"action": "retry", "from": target.value, "guidance": guidance, "actor": actor}
            )

    async def resolve_human(self, incident_id: str, actor: str, verdict: str, reason: str | None) -> None:
        if verdict not in ("not_bug", "cannot_fix", "already_fixed"):
            raise ConflictError(f"unknown verdict {verdict}")
        await self._require_state(incident_id, IncidentState.NEEDS_HUMAN)
        await self.dispatcher.resume(incident_id, {"action": verdict, "actor": actor, "reason": reason})

    async def decide(
        self,
        approval_id: str,
        user_id: str | None,
        role: UserRole,
        approve: bool,
        comment: str | None,
        reason_kind: str | None = None,
        actor: str | None = None,
    ) -> Approval:
        """审批一票。

        项目配置 required_merge_approvals > 1 时，同一事故需要多位不同审批人通过才置为 APPROVED；
        票数不足时审批保持 PENDING，只把这一票记进 payload.approvals。
        actor 可显式指定（GitHub PR review 等非本地账号来源），默认按登录用户生成。
        """
        if role is UserRole.VIEWER:
            raise PermissionError("viewer cannot decide approvals")
        actor = actor or (f"user:{user_id}" if user_id else "user:local")
        async with self.store.tx() as tx:
            approval = await tx.session.scalar(select(Approval).where(Approval.id == approval_id).with_for_update())
            if approval is None:
                raise NotFoundError(approval_id)
            if approval.status is not ApprovalStatus.PENDING:
                raise ConflictError(f"approval already {approval.status}")
            if approval.expires_at is not None and approval.expires_at < utcnow():
                approval.status = ApprovalStatus.EXPIRED
                raise ConflictError("approval expired")
            if approval.action is not ApprovalAction.MERGE:
                raise ConflictError("only merge approvals are decided here; use guide/resolve for human takeover")
            inc = await lock_incident(tx, approval.incident_id)
            if inc.state is not IncidentState.AWAITING_REVIEW:
                raise ConflictError(f"incident is {inc.state}, not awaiting review")
            target = (inc.target_branch or "").lower()
            if approve and target in _PROTECTED_BRANCHES and role is not UserRole.ADMIN:
                # main / release 的合并必须由管理员确认（docs/05-safety.md §1）
                raise PermissionError(f"merging into {target} requires admin")
            votes = _votes(approval)
            if any(v.get("actor") == actor for v in votes):
                raise ConflictError(f"{actor} already voted on this approval")
            payload = dict(approval.payload or {})
            required = await _required_approvals(tx.session, approval.incident_id)
            if approve:
                votes.append({"actor": actor, "user_id": user_id, "comment": comment, "at": utcnow().isoformat()})
                payload["approvals"] = votes
                if len(votes) >= required:
                    approval.status = ApprovalStatus.APPROVED
                    approval.decided_by = user_id
                    approval.decided_at = utcnow()
                    approval.comment = comment
                    payload["decided_actor"] = actor
            else:
                approval.status = ApprovalStatus.REJECTED
                approval.decided_by = user_id
                approval.decided_at = utcnow()
                approval.comment = comment
                payload["decided_actor"] = actor
                payload["reason_kind"] = reason_kind
            approval.payload = payload
            await record_event(
                tx,
                approval.incident_id,
                "approval_decided",
                actor=actor,
                reason=comment,
                payload={
                    "approval_id": approval.id,
                    "status": approval.status.value,
                    "approve": approve,
                    "votes": len(votes),
                    "required": required,
                    "reason_kind": reason_kind,
                },
            )
        await self.dispatcher.resume(
            approval.incident_id,
            {
                "action": "approve" if approve else "reject",
                "comment": comment,
                "actor": actor,
                "approval_id": approval.id,
                # 驳回原因分类：root_cause 回诊断，patch / assertion 回修复
                "reason_kind": reason_kind,
            },
        )
        return approval

    async def _require_state(self, incident_id: str, state: IncidentState) -> None:
        async with self.store.read() as s:
            current = await s.scalar(select(Incident.state).where(Incident.id == incident_id))
        if current is None:
            raise NotFoundError(incident_id)
        if current is not state:
            raise ConflictError(f"incident is {current}, expected {state}")

    async def expire_review(self, incident_id: str) -> None:
        async with self.store.tx() as tx:
            await tx.session.execute(
                update(Approval)
                .where(Approval.incident_id == incident_id, Approval.status == ApprovalStatus.PENDING)
                .values(status=ApprovalStatus.EXPIRED, decided_at=utcnow())
            )
        await self.dispatcher.resume(incident_id, {"action": "timeout"})

    async def wake_observation(self, incident_id: str) -> None:
        """sweeper 使用：观察期到点，唤醒挂起的观察期节点做最终判定。"""
        await self.dispatcher.resume(incident_id, {"action": "observation_tick"})


_PROTECTED_BRANCHES = frozenset({"main", "master", "release"})


def required_merge_approvals(config: dict[str, Any] | None) -> int:
    """合并需要几位审批人通过（项目配置 required_merge_approvals，默认 1）。"""
    try:
        return max(1, int((config or {}).get("required_merge_approvals") or 1))
    except (TypeError, ValueError):
        return 1


async def _required_approvals(session: Any, incident_id: str) -> int:
    config = await session.scalar(
        select(Project.config).join(Incident, Incident.project_id == Project.id).where(Incident.id == incident_id)
    )
    return required_merge_approvals(config)


def _votes(approval: Approval) -> list[dict[str, Any]]:
    votes = (approval.payload or {}).get("approvals")
    return [v for v in votes if isinstance(v, dict)] if isinstance(votes, list) else []
