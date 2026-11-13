"""领域写操作：状态转换、事件记录。所有对 incident.state 的修改都从这里走。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from twinfix.db import Tx
from twinfix.domain.enums import TERMINAL_STATES, ApprovalAction, ApprovalStatus, IncidentState
from twinfix.domain.models import Approval, Incident, IncidentEvent, utcnow
from twinfix.domain.state_machine import assert_transition


class IncidentNotFoundError(LookupError):
    pass


async def lock_incident(tx: Tx, incident_id: str) -> Incident:
    """行锁读取。状态转换必须在锁内做校验，避免并发写出非法转换。"""
    row = await tx.session.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
    if row is None:
        raise IncidentNotFoundError(incident_id)
    return row


def event_dict(ev: IncidentEvent) -> dict[str, Any]:
    return {
        "id": ev.id,
        "incident_id": ev.incident_id,
        "kind": ev.kind,
        "from_state": ev.from_state.value if ev.from_state else None,
        "to_state": ev.to_state.value if ev.to_state else None,
        "reason": ev.reason,
        "actor": ev.actor,
        "payload": ev.payload,
        "created_at": ev.created_at.isoformat(),
    }


async def record_event(
    tx: Tx,
    incident_id: str,
    kind: str,
    *,
    actor: str = "system",
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
    from_state: IncidentState | None = None,
    to_state: IncidentState | None = None,
) -> IncidentEvent:
    ev = IncidentEvent(
        incident_id=incident_id,
        kind=kind,
        actor=actor,
        reason=reason,
        payload=payload or {},
        from_state=from_state,
        to_state=to_state,
        created_at=utcnow(),
    )
    tx.session.add(ev)
    await tx.session.flush()  # 拿到自增 id，作为 SSE 事件 id
    tx.emit(event_dict(ev))
    return ev


async def transition(
    tx: Tx,
    incident: Incident,
    to_state: IncidentState,
    *,
    reason: str | None = None,
    actor: str = "system",
    payload: dict[str, Any] | None = None,
) -> IncidentEvent:
    from_state = incident.state
    assert_transition(from_state, to_state)
    incident.state = to_state
    incident.state_entered_at = utcnow()
    if to_state in _CLOSING_REASON_STATES and reason:
        incident.closed_reason = reason
    # 状态推进后，待决审批里"还该等"的只剩当前等待状态对应的那一类：
    # 终态下全部作废；进入 NEEDS_HUMAN 时把评审期遗留的合并审批作废（此时只等转人工）。
    # 不作废的话审批中心会堆满永远批不了的僵尸待办（decide 会 409）。
    stale = _STALE_PENDING_ACTIONS.get(to_state)
    if stale:
        await tx.session.execute(
            update(Approval)
            .where(
                Approval.incident_id == incident.id,
                Approval.status == ApprovalStatus.PENDING,
                Approval.action.in_(stale),
            )
            .values(status=ApprovalStatus.EXPIRED)
        )
    return await record_event(
        tx,
        incident.id,
        "transition",
        actor=actor,
        reason=reason,
        payload=payload,
        from_state=from_state,
        to_state=to_state,
    )


_STALE_PENDING_ACTIONS: dict[IncidentState, tuple[ApprovalAction, ...]] = {
    state: (ApprovalAction.MERGE, ApprovalAction.HUMAN_TAKEOVER, ApprovalAction.TOOL) for state in TERMINAL_STATES
}
_STALE_PENDING_ACTIONS[IncidentState.NEEDS_HUMAN] = (ApprovalAction.MERGE,)

_CLOSING_REASON_STATES = frozenset(
    {
        IncidentState.NOT_BUG,
        IncidentState.CANNOT_REPRODUCE,
        IncidentState.CANNOT_FIX,
        IncidentState.ALREADY_FIXED,
        IncidentState.CANCELLED,
        IncidentState.CLOSED,
    }
)
