"""编排端到端：用 stub Agent 驱动状态机走过每一条分支（真实 Postgres + LangGraph checkpoint）。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from tests.conftest import Harness
from twinfix.domain.enums import (
    ApprovalAction,
    ApprovalStatus,
    AttemptStatus,
    IncidentState,
    ReportStatus,
    ReportType,
    SignalSource,
    ToolCallStatus,
    TwinStatus,
    UserRole,
)
from twinfix.domain.models import (
    Approval,
    Attempt,
    Incident,
    IncidentEvent,
    Patch,
    Project,
    Report,
    ToolCall,
    Twin,
    Verification,
    utcnow,
)
from twinfix.services import ConflictError, SignalIn

pytestmark = pytest.mark.integration
S = IncidentState

STACK = """java.lang.IllegalStateException: stock < 0
    at com.acme.inventory.StockService.deduct(StockService.java:42)
    at com.acme.order.OrderService.create(OrderService.java:88)
    at org.springframework.aop.Proxy.invoke(Proxy.java:10)
"""


async def open_incident(h: Harness, scenario: str = "happy", **kw: Any) -> str:
    project = await h.project(**kw.pop("config", {}))
    res = await h.service.ingest(
        SignalIn(
            project=project.name,
            source=SignalSource.MANUAL,
            title=kw.pop("title", f"超卖 {scenario}"),
            service="order",
            stacktrace=kw.pop("stacktrace", STACK),
            meta={"scenario": scenario},
            **kw,
        )
    )
    assert res.created
    return res.incident_id


async def state_of(h: Harness, incident_id: str) -> IncidentState:
    async with h.rt.store.read() as s:
        inc = await s.get(Incident, incident_id)
        assert inc is not None
        return inc.state


async def path_of(h: Harness, incident_id: str) -> list[str]:
    async with h.rt.store.read() as s:
        rows = (
            await s.scalars(
                select(IncidentEvent)
                .where(IncidentEvent.incident_id == incident_id, IncidentEvent.kind == "transition")
                .order_by(IncidentEvent.id)
            )
        ).all()
    return [r.to_state.value for r in rows if r.to_state]


async def pending_merge(h: Harness, incident_id: str) -> Approval:
    async with h.rt.store.read() as s:
        a = await s.scalar(
            select(Approval).where(
                Approval.incident_id == incident_id,
                Approval.action == ApprovalAction.MERGE,
                Approval.status == ApprovalStatus.PENDING,
            )
        )
    assert a is not None
    return a


async def reports(h: Harness, incident_id: str) -> list[Report]:
    async with h.rt.store.read() as s:
        return list(
            (await s.scalars(select(Report).where(Report.incident_id == incident_id).order_by(Report.created_at))).all()
        )


FLOW_TO_REVIEW = ["TRIAGED", "DIAGNOSING", "REPRODUCING", "FIXING", "VERIFYING", "AWAITING_REVIEW"]


async def test_project_custom_commands_reach_profile(harness: Harness) -> None:
    project = await harness.project(commands={"test": "npx vitest run", "report_format": "jest-json"})
    profile = harness.rt.steps._profile_of(project)
    assert profile.commands is not None  # type: ignore[attr-defined]
    assert profile.commands.test == "npx vitest run"  # type: ignore[attr-defined]
    assert profile.commands.report_format == "jest-json"  # type: ignore[attr-defined]


async def test_happy_path_to_closed(harness: Harness) -> None:
    h = harness
    iid = await open_incident(h)
    assert await state_of(h, iid) is S.AWAITING_REVIEW
    assert await path_of(h, iid) == FLOW_TO_REVIEW

    from twinfix.domain.models import User

    async with h.rt.store.tx() as tx:
        alice = User(name=f"alice-{iid[-6:]}", role=UserRole.MAINTAINER)
        tx.session.add(alice)
    approval = await pending_merge(h, iid)
    await h.service.decide(approval.id, alice.id, UserRole.MAINTAINER, approve=True, comment="复测通过")

    assert await state_of(h, iid) is S.CLOSED
    assert await path_of(h, iid) == [*FLOW_TO_REVIEW, "MERGING", "MERGED", "OBSERVING", "CLOSED"]
    # 审计：进入 MERGING 的转换要记在批准人名下，而不是系统自动合并
    async with h.rt.store.read() as s:
        to_merging = await s.scalar(
            select(IncidentEvent).where(IncidentEvent.incident_id == iid, IncidentEvent.to_state == S.MERGING)
        )
    assert to_merging is not None
    assert to_merging.actor == f"user:{alice.id}" and to_merging.reason == "人工确认合并"

    async with h.rt.store.read() as s:
        inc = await s.get(Incident, iid)
        twin = await s.scalar(select(Twin).where(Twin.incident_id == iid))
        calls = (await s.scalars(select(ToolCall).where(ToolCall.incident_id == iid))).all()
    assert inc is not None and inc.pr_number is not None
    assert inc.fix_branch is not None and inc.fix_branch.startswith("fix/twinfix-")
    assert inc.target_branch == "dev"
    assert "workspace" not in inc.meta  # 工作区已回收
    assert twin is not None and twin.status is TwinStatus.DESTROYED
    assert h.adapters.twin.live == {}  # type: ignore[attr-defined]

    # 每一步都经网关留痕，且全部成功
    tools = {c.tool for c in calls}
    assert {"search_logs", "write_file", "push_branch", "open_pr", "set_check", "merge_pr", "send_report"} <= tools
    assert all(c.status is ToolCallStatus.OK for c in calls), [(c.tool, c.status, c.error) for c in calls]

    pr = h.adapters.codehost.prs[inc.pr_number]  # type: ignore[attr-defined]
    assert pr.merged and pr.req.base == "dev"

    types = [r.type for r in await reports(h, iid)]
    assert types == [ReportType.FIXED, ReportType.MERGED]
    assert all(r.status is ReportStatus.SENT for r in await reports(h, iid))


@pytest.mark.parametrize(
    ("scenario", "final", "path"),
    [
        ("not_bug", S.NOT_BUG, ["TRIAGED", "DIAGNOSING", "NOT_BUG"]),
        ("cannot_reproduce", S.CANNOT_REPRODUCE, ["TRIAGED", "DIAGNOSING", "REPRODUCING", "CANNOT_REPRODUCE"]),
        ("cannot_fix", S.CANNOT_FIX, ["TRIAGED", "DIAGNOSING", "REPRODUCING", "FIXING", "CANNOT_FIX"]),
    ],
)
async def test_agent_verdicts_end_in_terminal_state(
    harness: Harness, scenario: str, final: IncidentState, path: list[str]
) -> None:
    iid = await open_incident(harness, scenario)
    assert await state_of(harness, iid) is final
    assert await path_of(harness, iid) == path
    rs = await reports(harness, iid)
    assert [r.type.value for r in rs] == [scenario]
    assert harness.adapters.twin.live == {}  # type: ignore[attr-defined]


async def test_triage_filters_business_exception(harness: Harness) -> None:
    iid = await open_incident(
        harness, stacktrace="com.acme.BizException: 余额不足\n    at com.acme.Pay.pay(Pay.java:1)\n"
    )
    assert await path_of(harness, iid) == ["NOT_BUG"]


@pytest.mark.parametrize(
    ("scenario", "path_tail"),
    [
        ("low_confidence", ["DIAGNOSING", "NEEDS_HUMAN"]),
        ("fixer_needs_human", ["FIXING", "NEEDS_HUMAN"]),
    ],
)
async def test_needs_human_branches(harness: Harness, scenario: str, path_tail: list[str]) -> None:
    iid = await open_incident(harness, scenario)
    assert await state_of(harness, iid) is S.NEEDS_HUMAN
    assert (await path_of(harness, iid))[-2:] == path_tail
    assert [r.type for r in await reports(harness, iid)] == [ReportType.NEEDS_HUMAN]

    await harness.service.resolve_human(iid, "user:alice", "cannot_fix", "确认需要改表结构")
    assert await state_of(harness, iid) is S.CANNOT_FIX


async def test_verify_fails_once_then_passes(harness: Harness) -> None:
    iid = await open_incident(harness, "verify_fail_once")
    assert await state_of(harness, iid) is S.AWAITING_REVIEW
    assert await path_of(harness, iid) == [
        "TRIAGED",
        "DIAGNOSING",
        "REPRODUCING",
        "FIXING",
        "VERIFYING",
        "FIXING",
        "VERIFYING",
        "AWAITING_REVIEW",
    ]
    async with harness.rt.store.read() as s:
        attempts = (await s.scalars(select(Attempt).where(Attempt.incident_id == iid).order_by(Attempt.seq))).all()
    assert [(a.seq, a.status, a.trigger) for a in attempts] == [
        (1, AttemptStatus.FAILED, "initial"),
        (2, AttemptStatus.PASSED, "verify_failed"),
    ]
    assert attempts[1].feedback and "仍失败" in attempts[1].feedback


async def test_verify_exhausts_attempts(harness: Harness) -> None:
    iid = await open_incident(harness, "verify_fail_always", config={"max_fix_attempts": 2})
    assert await state_of(harness, iid) is S.NEEDS_HUMAN
    async with harness.rt.store.read() as s:
        n = len((await s.scalars(select(Attempt).where(Attempt.incident_id == iid))).all())
    assert n == 2


async def test_review_reject_with_comment_goes_back_to_fixing(harness: Harness) -> None:
    h = harness
    iid = await open_incident(h)
    approval = await pending_merge(h, iid)
    await h.service.decide(approval.id, None, UserRole.MAINTAINER, approve=False, comment="不要改接口签名")
    # 驳回 → FIXING → VERIFYING → 再次等待评审，并生成新的审批
    assert await state_of(h, iid) is S.AWAITING_REVIEW
    assert (await path_of(h, iid))[-3:] == ["FIXING", "VERIFYING", "AWAITING_REVIEW"]
    async with h.rt.store.read() as s:
        attempts = (await s.scalars(select(Attempt).where(Attempt.incident_id == iid).order_by(Attempt.seq))).all()
    assert attempts[-1].trigger == "review_rejected"
    assert attempts[-1].feedback == "不要改接口签名"
    new_approval = await pending_merge(h, iid)
    assert new_approval.id != approval.id
    # 两次进入 AWAITING_REVIEW 各发一次 FIXED 报告
    assert [r.type for r in await reports(h, iid)] == [ReportType.FIXED, ReportType.FIXED]


async def test_review_reject_without_comment_needs_human(harness: Harness) -> None:
    iid = await open_incident(harness)
    approval = await pending_merge(harness, iid)
    await harness.service.decide(approval.id, None, UserRole.MAINTAINER, approve=False, comment=None)
    assert await state_of(harness, iid) is S.NEEDS_HUMAN


async def test_human_guidance_restarts_from_fixing(harness: Harness) -> None:
    h = harness
    iid = await open_incident(h, "fixer_needs_human")
    assert await state_of(h, iid) is S.NEEDS_HUMAN
    # 指导后场景改为 happy，模拟人给了正确方向
    async with h.rt.store.tx() as tx:
        inc = await tx.session.get(Incident, iid)
        assert inc is not None
        inc.meta = {**inc.meta, "scenario": "happy"}
    await h.service.guide(iid, "user:bob", "用乐观锁版本号即可，不需要改表", S.FIXING)
    assert await state_of(h, iid) is S.AWAITING_REVIEW
    async with h.rt.store.read() as s:
        last = await s.scalar(select(Attempt).where(Attempt.incident_id == iid).order_by(Attempt.seq.desc()))
        takeover = await s.scalar(
            select(Approval).where(Approval.incident_id == iid, Approval.action == ApprovalAction.HUMAN_TAKEOVER)
        )
    assert last is not None and last.trigger == "human_guided"
    assert last.feedback == "用乐观锁版本号即可，不需要改表"
    assert takeover is not None and takeover.status is ApprovalStatus.APPROVED


@pytest.mark.parametrize(("scenario", "final"), [("already_fixed", S.ALREADY_FIXED), ("merge_conflict", S.NEEDS_HUMAN)])
async def test_premerge_reverification(harness: Harness, scenario: str, final: IncidentState) -> None:
    iid = await open_incident(harness, scenario)
    approval = await pending_merge(harness, iid)
    await harness.service.decide(approval.id, None, UserRole.MAINTAINER, approve=True, comment=None)
    assert await state_of(harness, iid) is final
    async with harness.rt.store.read() as s:
        inc = await s.get(Incident, iid)
    assert inc is not None
    assert not harness.adapters.codehost.prs[inc.pr_number].merged  # type: ignore[attr-defined]


async def test_premerge_head_moved_between_check_and_merge(harness: Harness) -> None:
    """预检完成、真正合并前目标分支又前进：不能直接合并，应在新 head 上重新预检。"""
    h = harness
    iid = await open_incident(h)
    approval = await pending_merge(h, iid)
    async with h.rt.store.read() as s:
        inc = await s.get(Incident, iid)
        assert inc is not None
        project = await s.scalar(select(Project).where(Project.id == inc.project_id))
    assert project is not None
    repo, branch = project.repo, inc.target_branch or "dev"
    host = h.adapters.codehost
    initial = host.heads.setdefault((repo, branch), "b" * 40)
    moved = "c" * 40
    calls = {"n": 0}
    original = host.branch_head

    async def flaky(repo_arg: str, branch_arg: str) -> str:
        if (repo_arg, branch_arg) != (repo, branch):
            return await original(repo_arg, branch_arg)
        calls["n"] += 1
        # 第 1 次是预检前的 head；第 2 次模拟预检期间目标分支前进；之后保持新 head
        return initial if calls["n"] == 1 else moved

    host.branch_head = flaky  # type: ignore[method-assign]

    await h.service.decide(approval.id, None, UserRole.MAINTAINER, approve=True, comment=None)

    assert await state_of(h, iid) is S.CLOSED
    assert calls["n"] == 4  # 两轮预检：各读一次开始 head + 一次结束 head
    async with h.rt.store.read() as s:
        events = list(
            (
                await s.scalars(
                    select(IncidentEvent).where(
                        IncidentEvent.incident_id == iid, IncidentEvent.kind == "premerge_head_moved"
                    )
                )
            ).all()
        )
    assert len(events) == 1
    assert events[0].payload["round"] == 1
    assert events[0].payload["verified"] == initial
    assert events[0].payload["current"] == moved


async def test_extra_repo_head_moved_before_merge_needs_human(harness: Harness) -> None:
    """附加仓库不自动变基：预检期间目标 head 变化时直接转人工，而不是合并一个未在新 base 验证的补丁。"""
    h = harness
    iid = await open_incident(h)
    extra_repo = "acme/extra-lib"
    async with h.rt.store.tx() as tx:
        inc = await tx.session.get(Incident, iid)
        assert inc is not None
        meta = dict(inc.meta)
        meta["repo_workspaces"] = {
            **(meta.get("repo_workspaces") or {}),
            "extra": {"repo": extra_repo, "sha": "e" * 40, "root": "_repos/extra", "fixable": "1"},
        }
        meta["repo_prs"] = {"extra": {"pr_number": 99, "pr_url": "https://example/99", "commit_sha": "f" * 40}}
        inc.meta = meta
    approval = await pending_merge(h, iid)
    host = h.adapters.codehost
    original = host.branch_head
    calls = {"n": 0}

    async def flaky(repo_arg: str, branch_arg: str) -> str:
        if repo_arg != extra_repo:
            return await original(repo_arg, branch_arg)
        calls["n"] += 1
        return "a" * 40 if calls["n"] == 1 else "b" * 40

    host.branch_head = flaky  # type: ignore[method-assign]

    await h.service.decide(approval.id, None, UserRole.MAINTAINER, approve=True, comment=None)

    assert await state_of(h, iid) is S.NEEDS_HUMAN
    assert calls["n"] == 2
    async with h.rt.store.read() as s:
        event = await s.scalar(
            select(IncidentEvent).where(IncidentEvent.incident_id == iid, IncidentEvent.kind == "premerge_head_moved")
        )
    assert event is not None
    assert event.payload["extra_repos"] == {"extra": {"verified": "a" * 40, "current": "b" * 40}}


async def test_auto_merge_dev(harness: Harness) -> None:
    iid = await open_incident(harness, config={"auto_merge_dev": True})
    assert await state_of(harness, iid) is S.CLOSED


async def test_premerge_integrity_blocks_review_time_test_deletion(harness: Harness) -> None:
    """评审期间补丁被改成删测试：合并前重验的系统侧扫描必须拦下（不采信 Verifier 自报）。"""
    h = harness
    iid = await open_incident(h)
    # 模拟评审期间 PR 上的补丁被改：attempt 1 的 diff 换成一个删除测试文件的 diff
    async with h.rt.store.tx() as tx:
        attempt = await tx.session.scalar(
            select(Attempt).where(Attempt.incident_id == iid).order_by(Attempt.seq.desc())
        )
        assert attempt is not None
        patch = await tx.session.scalar(select(Patch).where(Patch.attempt_id == attempt.id))
        assert patch is not None
        patch.diff = (
            "diff --git a/src/test/java/twinfix/TwinfixReproTest.java b/src/test/java/twinfix/TwinfixReproTest.java\n"
            "deleted file mode 100644\n"
            "--- a/src/test/java/twinfix/TwinfixReproTest.java\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n-class TwinfixReproTest {}\n"
        )

    approval = await pending_merge(h, iid)
    await h.service.decide(approval.id, None, UserRole.MAINTAINER, approve=True, comment=None)
    assert await state_of(h, iid) is S.NEEDS_HUMAN
    async with h.rt.store.read() as s:
        inc = await s.get(Incident, iid)
        ver = await s.scalar(
            select(Verification).where(Verification.attempt_id == attempt.id).order_by(Verification.created_at.desc())
        )
    assert inc is not None and not h.adapters.codehost.prs[inc.pr_number].merged  # type: ignore[attr-defined]
    # 合并前重验的结论已经落库且带 failed 的 test_integrity（而不是被内部异常吞掉）
    assert ver is not None and not ver.passed
    assert [c["status"] for c in ver.checks if c["name"] == "test_integrity"] == ["failed"]


async def test_protected_branch_requires_admin(harness: Harness) -> None:
    from twinfix.domain.enums import Env

    iid = await open_incident(harness, env=Env.PROD)
    approval = await pending_merge(harness, iid)
    with pytest.raises(PermissionError):
        await harness.service.decide(approval.id, None, UserRole.MAINTAINER, approve=True, comment=None)
    await harness.service.decide(approval.id, None, UserRole.ADMIN, approve=True, comment=None)
    assert await state_of(harness, iid) is S.CLOSED
    async with harness.rt.store.read() as s:
        inc = await s.get(Incident, iid)
    assert inc is not None and inc.target_branch == "main" and inc.severity.value == "S2"


async def test_cancel_while_awaiting_review(harness: Harness) -> None:
    iid = await open_incident(harness)
    await harness.service.cancel(iid, "user:carol", "重复事故")
    assert await state_of(harness, iid) is S.CANCELLED
    assert [r.type for r in await reports(harness, iid)][-1] is ReportType.CANCELLED
    assert harness.adapters.twin.live == {}  # type: ignore[attr-defined]


async def test_cancel_mid_run_stops_agent(harness: Harness) -> None:
    """Agent 运行中被取消：下一次工具调用即中断，事故转 CANCELLED。"""
    h = harness
    h.dispatcher.auto = False
    iid = await open_incident(h)
    engine = h.rt.engine
    r = await engine.advance(iid)  # NEW → TRIAGED
    r = await engine.advance(iid, expected_checkpoint=r.checkpoint_id)  # → DIAGNOSING
    assert await state_of(h, iid) is S.DIAGNOSING
    async with h.rt.store.tx() as tx:  # 模拟运行中途 API 置位取消
        inc = await tx.session.get(Incident, iid)
        assert inc is not None
        inc.cancel_requested = True
    await engine.drive(iid)
    assert await state_of(h, iid) is S.CANCELLED


async def test_stale_messages_are_dropped(harness: Harness) -> None:
    h = harness
    h.dispatcher.auto = False
    iid = await open_incident(h)
    first = await h.rt.engine.advance(iid)
    again = await h.rt.engine.advance(iid, expected_checkpoint="stale-checkpoint")
    assert again.status == "stale"
    assert await path_of(h, iid) == ["TRIAGED"]
    await h.rt.engine.advance(iid, expected_checkpoint=first.checkpoint_id)
    assert await path_of(h, iid) == ["TRIAGED", "DIAGNOSING"]


async def test_halt_pauses_and_resumes(harness: Harness) -> None:
    from twinfix.tracing import HaltedError

    h = harness
    h.dispatcher.auto = False
    iid = await open_incident(h)
    r = await h.rt.engine.advance(iid)
    h.control.halted = True
    with pytest.raises(HaltedError):
        await h.rt.engine.advance(iid, expected_checkpoint=r.checkpoint_id)
    assert await state_of(h, iid) is S.TRIAGED  # 急停不改变状态
    h.control.halted = False
    await h.rt.engine.drive(iid)
    assert await state_of(h, iid) is S.AWAITING_REVIEW


async def test_step_failure_retries_then_needs_human(harness: Harness) -> None:
    from twinfix.orchestrator.steps import StepFailedError

    h = harness
    h.dispatcher.auto = False
    iid = await open_incident(h)
    h.adapters.twin.live = None  # type: ignore[attr-defined]  # 让孪生环境创建抛异常
    r = await h.rt.engine.advance(iid)
    r = await h.rt.engine.advance(iid, expected_checkpoint=r.checkpoint_id)
    r = await h.rt.engine.advance(iid, expected_checkpoint=r.checkpoint_id)  # → REPRODUCING
    assert await state_of(h, iid) is S.REPRODUCING
    with pytest.raises(StepFailedError):
        await h.rt.engine.advance(iid, expected_checkpoint=r.checkpoint_id)
    # step_max_retries=2：第二次失败即转人工
    await h.rt.engine.advance(iid, expected_checkpoint=r.checkpoint_id)
    assert await state_of(h, iid) is S.NEEDS_HUMAN
    async with h.rt.store.read() as s:
        failures = (
            await s.scalars(
                select(IncidentEvent).where(IncidentEvent.incident_id == iid, IncidentEvent.kind == "step_failed")
            )
        ).all()
    assert len(failures) == 2


async def test_failed_twin_creation_leaves_no_creating_row(harness: Harness) -> None:
    """创建孪生抛异常时记录必须落 FAILED：停在 CREATING 的行既等不到 READY，也不会被重新创建。"""
    from twinfix.orchestrator.steps import StepFailedError

    h = harness
    h.dispatcher.auto = False
    iid = await open_incident(h)
    h.adapters.twin.live = None  # type: ignore[attr-defined]  # 让孪生环境创建抛异常
    r = await h.rt.engine.advance(iid)
    r = await h.rt.engine.advance(iid, expected_checkpoint=r.checkpoint_id)
    r = await h.rt.engine.advance(iid, expected_checkpoint=r.checkpoint_id)  # → REPRODUCING
    with pytest.raises(StepFailedError):
        await h.rt.engine.advance(iid, expected_checkpoint=r.checkpoint_id)
    async with h.rt.store.read() as s:
        row = await s.scalar(select(Twin).where(Twin.incident_id == iid))
    assert row is not None and row.status is TwinStatus.FAILED


async def test_twin_ttl_reaps_stale_environments(harness: Harness) -> None:
    """TTL 回收：过期的 READY 孪生与卡在 CREATING 的孪生都被销毁；未到期的原样保留。"""
    from twinfix.orchestrator.steps import TWIN_CREATE_GRACE_S

    h = harness
    iid = await open_incident(h)
    assert h.adapters.twin.live  # 走到评审时孪生还活着
    async with h.rt.store.read() as s:
        twin = await s.scalar(select(Twin).where(Twin.incident_id == iid))
    assert twin is not None and twin.status is TwinStatus.READY

    assert twin.id not in await h.rt.steps.expired_twins()  # 24h TTL 未到，不动
    async with h.rt.store.tx() as tx:
        row = await tx.session.scalar(select(Twin).where(Twin.id == twin.id))
        assert row is not None
        row.expires_at = utcnow() - timedelta(seconds=1)
    assert await h.rt.steps.expired_twins() == [twin.id]

    await h.rt.steps.reap_twin(twin.id)
    assert h.adapters.twin.live == {}  # type: ignore[attr-defined]
    async with h.rt.store.read() as s:
        row = await s.get(Twin, twin.id)
        ev = await s.scalar(
            select(IncidentEvent).where(IncidentEvent.incident_id == iid, IncidentEvent.kind == "twin_destroyed")
        )
    assert row is not None and row.status is TwinStatus.DESTROYED
    assert ev is not None and ev.payload["reason"] == "ttl"
    assert twin.id not in await h.rt.steps.expired_twins()  # 已回收的不再重复处理

    # 创建卡死：CREATING 且超过宽限期 → 回收
    async with h.rt.store.tx() as tx:
        row = await tx.session.scalar(select(Twin).where(Twin.id == twin.id))
        assert row is not None
        row.status = TwinStatus.CREATING
        row.expires_at = None
        row.updated_at = utcnow() - timedelta(seconds=TWIN_CREATE_GRACE_S + 60)
    assert await h.rt.steps.expired_twins() == [twin.id]


async def test_signal_aggregation_and_recurrence(harness: Harness) -> None:
    h = harness
    project = await h.project()

    def sig(title: str) -> SignalIn:
        return SignalIn(
            project=project.name,
            source=SignalSource.LOG,
            title=title,
            service="order",
            stacktrace=STACK,
            meta={"scenario": "happy"},
        )

    # 标题里的订单号不同，但栈相同 → 同一指纹
    a = await h.service.ingest(sig("超卖 order#1001"))
    b = await h.service.ingest(sig("超卖 order#2002"))
    assert a.created and not b.created and a.incident_id == b.incident_id
    async with h.rt.store.read() as s:
        inc = await s.get(Incident, a.incident_id)
    assert inc is not None and inc.signal_count == 2

    approval = await pending_merge(h, a.incident_id)
    await h.service.decide(approval.id, None, UserRole.MAINTAINER, approve=True, comment=None)
    assert await state_of(h, a.incident_id) is S.CLOSED

    c = await h.service.ingest(sig("超卖 order#3003"))
    assert c.created and c.recurrence_of == a.incident_id


async def test_tool_call_audit_is_append_only(harness: Harness) -> None:
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    iid = await open_incident(harness, "not_bug")
    with pytest.raises(DBAPIError, match="append-only"):
        async with harness.rt.store.tx() as tx:
            await tx.session.execute(update(ToolCall).where(ToolCall.incident_id == iid).values(error="tampered"))


async def test_events_are_published_after_commit(harness: Harness) -> None:
    iid = await open_incident(harness, "not_bug")
    kinds = [e["kind"] for e in harness.publisher.events if e["incident_id"] == iid]
    assert kinds[0] == "created"
    assert "transition" in kinds and "tool_call" in kinds and "run_started" in kinds
    ids = [e["id"] for e in harness.publisher.events if e["incident_id"] == iid]
    assert ids == sorted(ids)


class _RecordingTwin:
    """孪生环境替身：记录每次按哪个提交创建（含验证前的重部署）。"""

    def __init__(self) -> None:
        from twinfix.adapters.twin import FakeTwinProvider

        self.inner = FakeTwinProvider()
        self.created_shas: list[str] = []

    async def create(self, spec: Any) -> Any:
        self.created_shas.append(spec.sha)
        return await self.inner.create(spec)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def test_verify_deploys_committed_fix_to_twin(settings: Any, tmp_path: Any) -> None:
    """codehost=local：验证前修复被提交并推到 fix 分支，孪生环境按该提交重建。"""
    from twinfix.adapters.gitutil import git
    from twinfix.adapters.localgit import LocalGitCodeHost
    from twinfix.agents.stub import FIX_FILE, REPRO_TEST
    from twinfix.container import build_adapters
    from twinfix.control import MemoryControlPlane
    from twinfix.dispatch import InlineDispatcher
    from twinfix.events import MemoryPublisher
    from twinfix.runtime import build_runtime
    from twinfix.services import IncidentService

    local = settings.model_copy(update={"codehost": "local", "git_root": tmp_path / "git"})
    adapters = build_adapters(local)
    twin = _RecordingTwin()
    adapters.twin = twin  # type: ignore[assignment]
    src = tmp_path / "seed"
    src.mkdir()
    (src / "README.md").write_bytes(b"seed\n")
    host = LocalGitCodeHost(tmp_path / "git")
    base = await host.create_from_dir("acme/demo-shop", src)

    dispatcher = InlineDispatcher()
    rt = await build_runtime(local, publisher=MemoryPublisher(), control=MemoryControlPlane(), adapters=adapters)
    dispatcher.bind(rt.engine)
    try:
        h = Harness(rt, IncidentService(rt.store, dispatcher), dispatcher, MemoryPublisher())
        iid = await open_incident(h)
        assert await state_of(h, iid) is S.AWAITING_REVIEW
        async with rt.store.read() as s:
            inc = await s.get(Incident, iid)
            row = await s.scalar(select(Twin).where(Twin.incident_id == iid))
        assert inc is not None and inc.fix_branch and row is not None
        head = await host.branch_head("acme/demo-shop", inc.fix_branch)
        # 首次按基线创建，验证前按修复提交重建；孪生记录与分支头一致
        assert twin.created_shas[0] == base and twin.created_shas[-1] == head != base
        assert row.commit_sha == head
        files = await git("show", "--name-only", "--format=", head, cwd=host.repo_path("acme/demo-shop"))
        assert FIX_FILE in files.splitlines() and REPRO_TEST in files.splitlines()
    finally:
        await rt.close()


async def test_nul_bytes_are_stripped_on_write(harness: Harness) -> None:
    """LLM 输出偶尔夹带 \x00，PostgreSQL text/jsonb 会拒绝；Store 在 flush 前统一清理。"""
    from twinfix.domain.models import Diagnosis

    iid = await open_incident(harness, "not_bug")
    async with harness.rt.store.tx() as tx:
        tx.session.add(
            Diagnosis(
                incident_id=iid,
                summary="a\x00b",
                root_cause="c",
                hypotheses=[{"statement": "x\x00y", "nested": {"k\x00": ["z\x00"]}}],
                suspect_locations=[],
                confidence=0.5,
            )
        )
    async with harness.rt.store.read() as s:
        row = await s.scalar(select(Diagnosis).where(Diagnosis.incident_id == iid))
    assert row is not None and row.summary == "ab"
    assert row.hypotheses == [{"statement": "xy", "nested": {"k": ["z"]}}]


class _ScriptedProfile:
    """语言插件替身：run_tests 按脚本返回复现用例失败/通过，其余委托给真实 JavaProfile。"""

    def __init__(self, inner: Any, fail_pattern: list[bool]) -> None:
        self.inner = inner
        self.fail_pattern = list(fail_pattern)
        self.selectors: list[Any] = []

    async def run_tests(self, sandbox: Any, ws: Any, selector: Any) -> Any:
        from twinfix.adapters.types import CommandResult
        from twinfix.profiles.base import TestCase, TestReport

        self.selectors.append(selector)
        fail = self.fail_pattern.pop(0) if self.fail_pattern else False
        status = "failed" if fail else "passed"
        case = TestCase("twinfix.TwinfixReproTest#reproduces", status, "expected 0.41 but was 0.40" if fail else "")
        return TestReport(CommandResult(1 if fail else 0, "", "", 5), [case])

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@pytest.mark.parametrize(("pattern", "final"), [([True, True], S.AWAITING_REVIEW), ([True, False], S.CANNOT_REPRODUCE)])
async def test_repro_is_confirmed_by_system_reruns(settings: Any, pattern: list[bool], final: IncidentState) -> None:
    """可执行沙箱下，复现稳定性由编排器独立重跑确认，不采信 Agent 自报的 failing_runs。"""
    from twinfix.container import build_adapters
    from twinfix.control import MemoryControlPlane
    from twinfix.dispatch import InlineDispatcher
    from twinfix.events import MemoryPublisher
    from twinfix.runtime import build_runtime
    from twinfix.services import IncidentService

    adapters = build_adapters(settings)
    profile = _ScriptedProfile(adapters.profile, pattern)
    adapters.profile = profile  # type: ignore[assignment]
    # 只让编排器认为沙箱可执行；Agent 仍是 stub，不会真的跑命令
    exec_settings = settings.model_copy(update={"sandbox": "docker"})
    dispatcher = InlineDispatcher()
    rt = await build_runtime(
        exec_settings, publisher=MemoryPublisher(), control=MemoryControlPlane(), adapters=adapters
    )
    dispatcher.bind(rt.engine)
    try:
        h = Harness(rt, IncidentService(rt.store, dispatcher), dispatcher, MemoryPublisher())
        iid = await open_incident(h)
        assert await state_of(h, iid) is final
        assert [sel.classes for sel in profile.selectors[:2]] == [["TwinfixReproTest"]] * 2
        async with rt.store.read() as s:
            ev = await s.scalar(
                select(IncidentEvent).where(IncidentEvent.incident_id == iid, IncidentEvent.kind == "repro_confirmed")
            )
        assert ev is not None and ev.payload["failures"] == sum(pattern)
    finally:
        await rt.close()


async def test_repro_confirm_switch_falls_back_to_agent_report(settings: Any) -> None:
    """关掉系统确认（对照实验）：不再重跑，采信 Agent 自报的 failing_runs（桩按提示词约定报 1 次）。"""
    from twinfix.container import build_adapters
    from twinfix.control import MemoryControlPlane
    from twinfix.dispatch import InlineDispatcher
    from twinfix.events import MemoryPublisher
    from twinfix.runtime import build_runtime
    from twinfix.services import IncidentService

    adapters = build_adapters(settings)
    profile = _ScriptedProfile(adapters.profile, [False, False])  # 若被调用只会判"复现失败"
    adapters.profile = profile  # type: ignore[assignment]
    exec_settings = settings.model_copy(update={"sandbox": "docker", "repro_confirm": False})
    dispatcher = InlineDispatcher()
    rt = await build_runtime(
        exec_settings, publisher=MemoryPublisher(), control=MemoryControlPlane(), adapters=adapters
    )
    dispatcher.bind(rt.engine)
    try:
        h = Harness(rt, IncidentService(rt.store, dispatcher), dispatcher, MemoryPublisher())
        iid = await open_incident(h)
        assert await state_of(h, iid) is S.AWAITING_REVIEW
        assert profile.selectors == []  # 完全没有触发系统侧重跑
    finally:
        await rt.close()


async def test_repro_required_switch_skips_reproduction(settings: Any) -> None:
    """关掉"先复现"约束（对照实验）：REPRODUCING 变空过，诊断后直接修；没有复现用例作验收标准。"""
    from twinfix.agents.base import AgentName
    from twinfix.container import build_adapters
    from twinfix.control import MemoryControlPlane
    from twinfix.dispatch import InlineDispatcher
    from twinfix.domain.models import Diagnosis, Run
    from twinfix.events import MemoryPublisher
    from twinfix.runtime import build_runtime
    from twinfix.services import IncidentService

    abl = settings.model_copy(update={"repro_required": False})
    dispatcher = InlineDispatcher()
    rt = await build_runtime(
        abl, publisher=MemoryPublisher(), control=MemoryControlPlane(), adapters=build_adapters(abl)
    )
    dispatcher.bind(rt.engine)
    try:
        h = Harness(rt, IncidentService(rt.store, dispatcher), dispatcher, MemoryPublisher())
        iid = await open_incident(h)
        assert await state_of(h, iid) is S.AWAITING_REVIEW
        # 状态机轨迹不变（"没有复现就不能修复"仍是硬约束），只是复现阶段不再跑 Agent
        assert await path_of(h, iid) == FLOW_TO_REVIEW
        async with rt.store.read() as s:
            diag = await s.scalar(select(Diagnosis).where(Diagnosis.incident_id == iid))
            runs = (await s.scalars(select(Run).where(Run.incident_id == iid))).all()
            ev = await s.scalar(
                select(IncidentEvent).where(IncidentEvent.incident_id == iid, IncidentEvent.kind == "repro_skipped")
            )
        assert diag is not None and diag.repro is None
        assert AgentName.REPRODUCER not in {r.agent for r in runs}
        assert ev is not None and ev.payload["reason"] == "repro_required=false"
        assert AgentName.VERIFIER in {r.agent for r in runs}
    finally:
        await rt.close()


class _UpstreamProfile:
    """语言插件替身：复现用例在"不含补丁的工作区"里的结果由 upstream_fixed 决定。
    判断依据是工作区里是否有修复文件（stub Fixer 写的 FIX_FILE）：没有 = 系统切到了目标分支上。
    """

    def __init__(self, inner: Any, upstream_fixed: bool) -> None:
        self.inner = inner
        self.upstream_fixed = upstream_fixed

    async def run_tests(self, sandbox: Any, ws: Any, selector: Any) -> Any:
        from twinfix.adapters.types import CommandResult
        from twinfix.agents.stub import FIX_FILE
        from twinfix.profiles.base import TestCase, TestReport

        has_fix = (ws.root / FIX_FILE).exists()
        ok = has_fix or self.upstream_fixed
        case = TestCase("twinfix.TwinfixReproTest#reproduces", "passed" if ok else "failed", "" if ok else "bug")
        return TestReport(CommandResult(0 if ok else 1, "", "", 5), [case])

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@pytest.mark.parametrize(("upstream_fixed", "final"), [(True, S.ALREADY_FIXED), (False, S.CLOSED)])
async def test_teammate_push_during_review(
    settings: Any, tmp_path: Any, upstream_fixed: bool, final: IncidentState
) -> None:
    """C009：评审期间目标分支前进。系统在不带补丁的最新 head 上跑复现用例：
    已通过 → ALREADY_FIXED（不合并）；仍失败 → 变基后正常合并。"""
    from twinfix.adapters.localgit import LocalGitCodeHost
    from twinfix.container import build_adapters
    from twinfix.control import MemoryControlPlane
    from twinfix.dispatch import InlineDispatcher
    from twinfix.events import MemoryPublisher
    from twinfix.runtime import build_runtime
    from twinfix.services import IncidentService

    local = settings.model_copy(update={"codehost": "local", "git_root": tmp_path / "git"})
    adapters = build_adapters(local)
    adapters.profile = _UpstreamProfile(adapters.profile, upstream_fixed)  # type: ignore[assignment]
    src = tmp_path / "seed"
    src.mkdir()
    (src / "README.md").write_bytes(b"seed\n")
    (src / "NOTES.md").write_bytes(b"v1\n")
    host = LocalGitCodeHost(tmp_path / "git")
    await host.create_from_dir("acme/demo-shop", src)

    dispatcher = InlineDispatcher()
    rt = await build_runtime(local, publisher=MemoryPublisher(), control=MemoryControlPlane(), adapters=adapters)
    dispatcher.bind(rt.engine)
    try:
        h = Harness(rt, IncidentService(rt.store, dispatcher), dispatcher, MemoryPublisher())
        iid = await open_incident(h)
        assert await state_of(h, iid) is S.AWAITING_REVIEW
        # 同事在评审期间往 dev 推了一个不冲突的提交
        await host.commit_patch(
            "acme/demo-shop", "dev", "--- a/NOTES.md\n+++ b/NOTES.md\n@@ -1 +1 @@\n-v1\n+v2\n", "teammate"
        )
        approval = await pending_merge(h, iid)
        await h.service.decide(approval.id, None, UserRole.MAINTAINER, approve=True, comment=None)
        assert await state_of(h, iid) is final
        async with rt.store.read() as s:
            ev = await s.scalar(
                select(IncidentEvent).where(IncidentEvent.incident_id == iid, IncidentEvent.kind == "upstream_check")
            )
        assert ev is not None and ev.payload["repro_passes"] is upstream_fixed
    finally:
        await rt.close()


async def _open_with_config(h: Harness, **config: Any) -> tuple[Any, str]:
    project = await h.project(**config)
    res = await h.service.ingest(
        SignalIn(
            project=project.name,
            source=SignalSource.MANUAL,
            title="观察期事故",
            service="order",
            stacktrace=STACK,
        )
    )
    assert res.created
    return project, res.incident_id


async def _approve(h: Harness, incident_id: str) -> None:
    from twinfix.domain.models import User

    async with h.rt.store.tx() as tx:
        alice = User(name=f"alice-{incident_id[-6:]}", role=UserRole.MAINTAINER)
        tx.session.add(alice)
    approval = await pending_merge(h, incident_id)
    await h.service.decide(approval.id, alice.id, UserRole.MAINTAINER, approve=True, comment="通过")


async def test_merge_enters_observation_then_closes(harness: Harness) -> None:
    _, iid = await _open_with_config(harness, observe_s=3600)
    await _approve(harness, iid)
    assert await state_of(harness, iid) is S.OBSERVING
    async with harness.rt.store.read() as s:
        inc = await s.get(Incident, iid)
    observation = inc.meta["observation"]  # type: ignore[index]
    assert observation["signals_at_merge"] == 1
    # 观察期未到：观察期节点挂起，推进（advance）不会让它提前结束
    await harness.dispatcher.advance(iid)
    assert await state_of(harness, iid) is S.OBSERVING
    # 把到期时间拨到过去，sweeper 的唤醒让事故归档
    async with harness.rt.store.tx() as tx:
        row = await tx.session.get(Incident, iid)
        meta = dict(row.meta)
        meta["observation"] = {**meta["observation"], "until": (utcnow() - timedelta(seconds=1)).isoformat()}
        row.meta = meta
    await harness.service.wake_observation(iid)
    assert await state_of(harness, iid) is S.CLOSED
    async with harness.rt.store.read() as s:
        inc = await s.get(Incident, iid)
    assert inc.meta["observation"]["result"] == "no_recurrence"  # type: ignore[index]


async def test_recurrence_during_observation_needs_human(harness: Harness) -> None:
    project, iid = await _open_with_config(harness, observe_s=3600)
    await _approve(harness, iid)
    assert await state_of(harness, iid) is S.OBSERVING
    # 观察期内同指纹信号再次进来：归并到同一事故并计数
    res = await harness.service.ingest(
        SignalIn(
            project=project.name,
            source=SignalSource.ALERT,
            title="观察期事故（复发）",
            service="order",
            stacktrace=STACK,
        )
    )
    assert not res.created and res.incident_id == iid
    # ingest 检测到观察期事故的复发信号会主动唤醒观察期节点，无需等待 sweeper
    assert await state_of(harness, iid) is S.NEEDS_HUMAN
    async with harness.rt.store.read() as s:
        inc = await s.get(Incident, iid)
    assert inc.meta["observation"]["result"] == "recurred"  # type: ignore[index]


async def test_cancel_during_observation_cancels_incident(harness: Harness) -> None:
    """观察期可以人工取消：取消请求唤醒挂起的观察期节点并落到 CANCELLED，而不是等观察期满。"""
    _, iid = await _open_with_config(harness, observe_s=3600)
    await _approve(harness, iid)
    assert await state_of(harness, iid) is S.OBSERVING
    await harness.service.cancel(iid, "user:alice", "线上已恢复，无需继续观察")
    assert await state_of(harness, iid) is S.CANCELLED
    async with harness.rt.store.read() as s:
        inc = await s.get(Incident, iid)
    assert inc is not None and inc.closed_reason == "线上已恢复，无需继续观察"


async def test_incident_budget_exhausted_goes_needs_human(harness: Harness) -> None:
    """单事故累计预算用尽：不再重试烧钱，直接转人工。"""
    h = harness
    iid = await open_incident(h, config={"budget_per_incident_tokens": 100})
    assert await state_of(h, iid) is S.AWAITING_REVIEW
    async with h.rt.store.tx() as tx:
        row = await tx.session.get(Incident, iid)
        row.cost_tokens = 500  # stub Agent 不花 token，直接模拟已用超限
    from twinfix.domain.models import User

    async with h.rt.store.tx() as tx:
        bob = User(name=f"bob-{iid[-6:]}", role=UserRole.MAINTAINER)
        tx.session.add(bob)
    approval = await pending_merge(h, iid)
    # 驳回回修复：下一次 Agent 运行前就会撞上预算闸门
    await h.service.decide(
        approval.id, bob.id, UserRole.MAINTAINER, approve=False, comment="再修一版", reason_kind="patch"
    )
    assert await state_of(h, iid) is S.NEEDS_HUMAN
    async with h.rt.store.read() as s:
        to_human = await s.scalar(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == iid, IncidentEvent.to_state == S.NEEDS_HUMAN)
            .order_by(IncidentEvent.id.desc())
        )
    assert to_human is not None
    assert to_human.payload.get("budget_exhausted") is True
    assert "预算已用尽" in (to_human.reason or "")


async def test_unlimited_incident_budget_by_default(harness: Harness) -> None:
    """没有配置预算时不做任何拦截（默认行为不变）。"""
    iid = await open_incident(harness, config={})
    async with harness.rt.store.tx() as tx:
        row = await tx.session.get(Incident, iid)
        row.cost_tokens = 10**9
    assert await state_of(harness, iid) is S.AWAITING_REVIEW
    approval = await pending_merge(harness, iid)
    from twinfix.domain.models import User

    async with harness.rt.store.tx() as tx:
        bob = User(name=f"bob-{iid[-6:]}", role=UserRole.MAINTAINER)
        tx.session.add(bob)
    await harness.service.decide(approval.id, bob.id, UserRole.MAINTAINER, approve=False, comment="再修一版")
    # 驳回回到 FIXING 后正常重跑，最终又回到评审
    assert await state_of(harness, iid) is S.AWAITING_REVIEW
    assert (await path_of(harness, iid)).count("FIXING") >= 2


async def test_report_suppressed_in_quiet_hours(harness: Harness) -> None:
    """静默时段命中：报告不推送，但完整保留并标记 suppressed。"""
    h = harness
    now = utcnow()
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")
    iid = await open_incident(
        h,
        config={
            "report_recipients": ["email:oncall@example.com", "feishu:https://hook"],
            "notify": {
                "channels": ["feishu"],
                "quiet_hours": {"start": start, "end": end, "timezone": "UTC"},
            },
        },
    )
    assert await state_of(h, iid) is S.AWAITING_REVIEW
    rs = await reports(h, iid)
    assert [r.type for r in rs] == [ReportType.FIXED]
    assert rs[0].status is ReportStatus.SUPPRESSED
    assert rs[0].sent_at is None
    assert "静默时段" in (rs[0].error or "")
    # 渠道白名单生效：只留下 feishu 收件人
    assert rs[0].recipients == ["feishu:https://hook"]


async def test_merge_requires_two_approvers(harness: Harness) -> None:
    """required_merge_approvals=2：第一票只记账，第二位不同审批人通过后才合并。"""
    h = harness
    iid = await open_incident(h, config={"required_merge_approvals": 2})
    assert await state_of(h, iid) is S.AWAITING_REVIEW
    from twinfix.domain.models import User

    async with h.rt.store.tx() as tx:
        alice = User(name=f"alice-{iid[-6:]}", role=UserRole.MAINTAINER)
        bob = User(name=f"bob-{iid[-6:]}", role=UserRole.MAINTAINER)
        tx.session.add_all([alice, bob])
    approval = await pending_merge(h, iid)

    await h.service.decide(approval.id, alice.id, UserRole.MAINTAINER, approve=True, comment="LGTM")
    # 一票不够：事故继续挂起在评审，审批保持 PENDING，只记下这一票
    assert await state_of(h, iid) is S.AWAITING_REVIEW
    fresh = await pending_merge(h, iid)
    assert fresh.id == approval.id and fresh.status is ApprovalStatus.PENDING
    assert [v["actor"] for v in fresh.payload["approvals"]] == [f"user:{alice.id}"]

    # 同一个人重复投票是冲突，不能自己凑够票数
    with pytest.raises(ConflictError):
        await h.service.decide(fresh.id, alice.id, UserRole.MAINTAINER, approve=True, comment="again")
    assert await state_of(h, iid) is S.AWAITING_REVIEW

    await h.service.decide(fresh.id, bob.id, UserRole.MAINTAINER, approve=True, comment="也通过")
    assert await state_of(h, iid) is S.CLOSED
    async with h.rt.store.read() as s:
        to_merging = await s.scalar(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == iid, IncidentEvent.to_state == S.MERGING)
            .order_by(IncidentEvent.id.desc())
        )
    assert to_merging is not None
    # 审计记在投出关键一票的审批人名下
    assert to_merging.actor == f"user:{bob.id}"


async def test_reject_with_root_cause_reason_restarts_diagnosis(harness: Harness) -> None:
    iid = await open_incident(harness)
    assert await state_of(harness, iid) is S.AWAITING_REVIEW
    from twinfix.domain.models import User

    async with harness.rt.store.tx() as tx:
        bob = User(name=f"bob-{iid[-6:]}", role=UserRole.MAINTAINER)
        tx.session.add(bob)
    approval = await pending_merge(harness, iid)
    await harness.service.decide(
        approval.id,
        bob.id,
        UserRole.MAINTAINER,
        approve=False,
        comment="根因不对，是缓存过期问题",
        reason_kind="root_cause",
    )
    # 驳回后回到诊断重跑，stub 会再次走到评审
    path = await path_of(harness, iid)
    assert path.count("DIAGNOSING") >= 2
    async with harness.rt.store.read() as s:
        inc = await s.get(Incident, iid)
    assert inc.meta.get("diagnosis_feedback") is None  # type: ignore[union-attr]
