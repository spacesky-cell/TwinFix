"""Run 用量增量落库：超时/取消的 Run 也能看到已花费 token，且 finish 不重复计入事故总额。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import Harness
from twinfix.domain.enums import AgentName, RunStatus, SignalSource
from twinfix.domain.models import Incident, Run
from twinfix.services import SignalIn
from twinfix.tracing import Budget, RunHandle, RunTracker

pytestmark = pytest.mark.integration


async def _incident(h: Harness) -> str:
    project = await h.project()
    res = await h.service.ingest(
        SignalIn(project=project.name, source=SignalSource.MANUAL, title="用量落库", service="order")
    )
    assert res.created
    return res.incident_id


async def _load(h: Harness, run_id: str, incident_id: str) -> tuple[Run, Incident]:
    async with h.rt.store.read() as s:
        run = await s.get(Run, run_id)
        inc = await s.get(Incident, incident_id)
    assert run is not None and inc is not None
    return run, inc


async def test_usage_persists_before_finish_and_is_not_double_counted(harness: Harness) -> None:
    iid = await _incident(harness)
    tracker = RunTracker(harness.rt.store)
    handle = await tracker.start(
        iid, AgentName.DIAGNOSER, Budget(max_tokens=10_000, max_tool_calls=10, max_seconds=60), model="m"
    )

    handle.add_usage(100, 20, Decimal("0.010"))
    await handle.persist_usage()
    run, inc = await _load(harness, handle.id, iid)
    assert (run.input_tokens, run.output_tokens) == (100, 20)
    assert run.cost == Decimal("0.010")
    assert inc.cost_tokens == 120
    assert inc.cost_amount == Decimal("0.010")
    # 每次落库都发一条 usage 事件，详情页据此刷新 Token 消耗
    usage = [e for e in harness.publisher.events if e["kind"] == "usage"]
    assert [e["payload"]["cost_tokens"] for e in usage] == [120]

    handle.add_usage(50, 10, Decimal("0.005"))
    await tracker.finish(handle, RunStatus.CANCELLED, error="timeout")
    run, inc = await _load(harness, handle.id, iid)
    assert (run.input_tokens, run.output_tokens) == (150, 30)
    assert run.cost == Decimal("0.015")
    assert run.status is RunStatus.CANCELLED
    # finish 只结算剩余增量，事故总额不会翻倍
    assert inc.cost_tokens == 180
    assert inc.cost_amount == Decimal("0.015")
    assert [e["payload"]["cost_tokens"] for e in harness.publisher.events if e["kind"] == "usage"] == [120, 180]


async def test_abandon_running_closes_runs_left_by_a_dead_process(harness: Harness) -> None:
    iid = await _incident(harness)
    tracker = RunTracker(harness.rt.store)
    budget = Budget(max_tokens=10_000, max_tool_calls=10, max_seconds=60)
    orphan = await tracker.start(iid, AgentName.VERIFIER, budget)
    done = await tracker.start(iid, AgentName.FIXER, budget)
    await tracker.finish(done, RunStatus.SUCCEEDED, outcome="patch_ready")

    assert await tracker.abandon_running(iid, reason="进程中断，运行被系统收尾") == 1

    run, _ = await _load(harness, orphan.id, iid)
    assert run.status is RunStatus.FAILED
    assert run.error == "进程中断，运行被系统收尾"
    assert run.finished_at is not None
    # 已经结束的 Run 不受影响
    finished, _ = await _load(harness, done.id, iid)
    assert finished.status is RunStatus.SUCCEEDED
    # 事件流里补一条 run_finished，控制台的运行列表才会跟着刷新
    abandoned = [
        e for e in harness.publisher.events if e["kind"] == "run_finished" and e["payload"]["run_id"] == orphan.id
    ]
    assert [e["payload"]["status"] for e in abandoned] == ["failed"]

    # 没有残留时什么也不做，也不再发事件
    assert await tracker.abandon_running(iid, reason="again") == 0
    again = [e for e in harness.publisher.events if e["kind"] == "run_finished" and e["payload"]["run_id"] == orphan.id]
    assert again == abandoned


async def test_persist_usage_without_tracker_is_a_noop() -> None:
    handle = RunHandle(id="r", incident_id="i", agent=AgentName.DIAGNOSER, budget=Budget(1, 1, 1))
    handle.add_usage(10, 5)
    await handle.persist_usage()  # 没挂回调时不应报错
    assert handle.input_tokens == 10
