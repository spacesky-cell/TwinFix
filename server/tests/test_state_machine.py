"""状态机规则的穷举测试：每一对 (from, to) 都有明确的期望。"""

from __future__ import annotations

import itertools

import pytest

from twinfix.domain.enums import TERMINAL_STATES, IncidentState
from twinfix.domain.state_machine import (
    TRANSITIONS,
    InvalidTransitionError,
    assert_transition,
    can_transition,
    reachable_from,
)

S = IncidentState

# 与 docs/03-domain-model.md 状态图逐条对应
EXPECTED_FLOW = {
    (S.NEW, S.TRIAGED),
    (S.NEW, S.NOT_BUG),
    (S.TRIAGED, S.DIAGNOSING),
    (S.DIAGNOSING, S.REPRODUCING),
    (S.DIAGNOSING, S.NOT_BUG),
    (S.REPRODUCING, S.FIXING),
    (S.REPRODUCING, S.CANNOT_REPRODUCE),
    (S.FIXING, S.VERIFYING),
    (S.VERIFYING, S.AWAITING_REVIEW),
    (S.VERIFYING, S.FIXING),
    (S.AWAITING_REVIEW, S.MERGING),
    (S.AWAITING_REVIEW, S.FIXING),
    (S.AWAITING_REVIEW, S.DIAGNOSING),
    (S.MERGING, S.MERGED),
    (S.MERGING, S.AWAITING_REVIEW),
    (S.MERGED, S.OBSERVING),
    (S.OBSERVING, S.CLOSED),
    (S.NEEDS_HUMAN, S.DIAGNOSING),
    (S.NEEDS_HUMAN, S.REPRODUCING),
    (S.NEEDS_HUMAN, S.FIXING),
    (S.NEEDS_HUMAN, S.NOT_BUG),
}
UNIVERSAL = {S.ALREADY_FIXED, S.CANNOT_FIX, S.CANCELLED, S.NEEDS_HUMAN}
ACTIVE = [s for s in S if s not in TERMINAL_STATES]


def expected(src: IncidentState, dst: IncidentState) -> bool:
    if src in TERMINAL_STATES or src == dst:
        return False
    if (src, dst) in EXPECTED_FLOW:
        return True
    return src is not S.MERGED and dst in UNIVERSAL


@pytest.mark.parametrize(("src", "dst"), list(itertools.product(S, S)), ids=lambda s: s.value)
def test_every_pair(src: IncidentState, dst: IncidentState) -> None:
    assert can_transition(src, dst) is expected(src, dst)


def test_terminal_states_have_no_exits() -> None:
    for s in TERMINAL_STATES:
        assert TRANSITIONS[s] == frozenset()


def test_no_self_loops() -> None:
    for s in S:
        assert s not in TRANSITIONS[s]


def test_every_active_state_can_reach_a_terminal_state() -> None:
    for s in ACTIVE:
        assert reachable_from(s) & TERMINAL_STATES, s


def test_every_state_is_reachable_from_new() -> None:
    assert reachable_from(S.NEW) == set(S)


def test_merged_only_enters_observation() -> None:
    # 代码已入库后不能再被判为"无法修复"或"取消"，只能进入观察期
    assert TRANSITIONS[S.MERGED] == frozenset({S.OBSERVING})


def test_cannot_skip_reproduction() -> None:
    # 安全约束：没有复现就不能修复
    assert not can_transition(S.DIAGNOSING, S.FIXING)
    assert not can_transition(S.TRIAGED, S.FIXING)


def test_cannot_merge_without_review() -> None:
    for s in S:
        if s is not S.AWAITING_REVIEW:
            assert not can_transition(s, S.MERGING), s


def test_assert_transition_raises() -> None:
    with pytest.raises(InvalidTransitionError) as exc:
        assert_transition(S.NEW, S.MERGED)
    assert exc.value.from_state is S.NEW
    assert exc.value.to_state is S.MERGED
