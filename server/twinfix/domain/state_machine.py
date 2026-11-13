"""事故状态机：唯一的转换规则表。

编排层（LangGraph）决定"什么时候"转换，这里决定"能不能"转换。
任何写 incident.state 的代码都必须经过 `assert_transition`。
"""

from __future__ import annotations

from collections import deque

from twinfix.domain.enums import TERMINAL_STATES, IncidentState

S = IncidentState

# 正常流程中的转换（不含下方的通用出口）
_FLOW: dict[IncidentState, frozenset[IncidentState]] = {
    S.NEW: frozenset({S.TRIAGED, S.NOT_BUG}),
    S.TRIAGED: frozenset({S.DIAGNOSING}),
    S.DIAGNOSING: frozenset({S.REPRODUCING, S.NOT_BUG}),
    S.REPRODUCING: frozenset({S.FIXING, S.CANNOT_REPRODUCE}),
    S.FIXING: frozenset({S.VERIFYING}),
    # 验证失败且次数未满 → 回到 FIXING
    S.VERIFYING: frozenset({S.AWAITING_REVIEW, S.FIXING}),
    # 驳回且附带意见 → 回到 FIXING；判定根因不对时直接回 DIAGNOSING
    S.AWAITING_REVIEW: frozenset({S.MERGING, S.FIXING, S.DIAGNOSING}),
    # 合并前重验超时/中断 → 回到评审重新确认，而不是卡死在 MERGING
    S.MERGING: frozenset({S.MERGED, S.AWAITING_REVIEW}),
    S.MERGED: frozenset({S.OBSERVING}),
    # 观察期满无复发 → 归档；观察期内出现同指纹信号 → 通用出口 NEEDS_HUMAN
    S.OBSERVING: frozenset({S.CLOSED}),
    # 人工介入后可从任一阶段重新开始，或直接给出结论
    S.NEEDS_HUMAN: frozenset({S.DIAGNOSING, S.REPRODUCING, S.FIXING, S.NOT_BUG}),
}

# 任意活跃状态都可以：判定已修复 / 无法修复 / 被取消 / 转人工（含超时）
_UNIVERSAL_EXITS: frozenset[IncidentState] = frozenset({S.ALREADY_FIXED, S.CANNOT_FIX, S.CANCELLED, S.NEEDS_HUMAN})

# MERGED 之后代码已入库，只剩收尾，不再接受通用出口
_NO_UNIVERSAL_EXITS: frozenset[IncidentState] = frozenset({S.MERGED})


def _build() -> dict[IncidentState, frozenset[IncidentState]]:
    table: dict[IncidentState, frozenset[IncidentState]] = {}
    for state in IncidentState:
        if state in TERMINAL_STATES:
            table[state] = frozenset()
            continue
        targets = set(_FLOW.get(state, frozenset()))
        if state not in _NO_UNIVERSAL_EXITS:
            targets |= _UNIVERSAL_EXITS
        targets.discard(state)
        table[state] = frozenset(targets)
    return table


TRANSITIONS: dict[IncidentState, frozenset[IncidentState]] = _build()


class InvalidTransitionError(Exception):
    def __init__(self, from_state: IncidentState, to_state: IncidentState) -> None:
        super().__init__(f"illegal transition {from_state} -> {to_state}")
        self.from_state = from_state
        self.to_state = to_state


def allowed_targets(state: IncidentState) -> frozenset[IncidentState]:
    return TRANSITIONS[state]


def can_transition(from_state: IncidentState, to_state: IncidentState) -> bool:
    return to_state in TRANSITIONS[from_state]


def assert_transition(from_state: IncidentState, to_state: IncidentState) -> None:
    if not can_transition(from_state, to_state):
        raise InvalidTransitionError(from_state, to_state)


def is_terminal(state: IncidentState) -> bool:
    return state in TERMINAL_STATES


def reachable_from(start: IncidentState) -> set[IncidentState]:
    seen = {start}
    queue = deque([start])
    while queue:
        for nxt in TRANSITIONS[queue.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen
