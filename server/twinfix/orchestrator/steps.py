"""每个事故状态的工作。

约定
- 每个 handler 接收 incident_id，返回下一个状态；状态转换统一由 `_guard` 落库。
- handler 必须可重放：崩溃后同一步骤会被重新执行，所以创建类操作都先查后建
  （attempt、report、approval、twin、PR 都是幂等的）。
- 等待人工的 handler 调用 `interrupt()`；恢复时 LangGraph 会从头重跑该 handler，
  因此 interrupt 之前的代码同样必须幂等。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from twinfix.adapters.twin import compose_project_name
from twinfix.adapters.types import TwinHandle, TwinSpec, Workspace
from twinfix.agents.base import OUTCOMES, Agent, AgentContext, AgentResult, IncidentView
from twinfix.agents.params import resolve as resolve_params
from twinfix.domain.enums import (
    TERMINAL_STATES,
    AgentName,
    ApprovalAction,
    ApprovalStatus,
    AttemptStatus,
    IncidentState,
    ReportStatus,
    ReportType,
    RunStatus,
    TwinStatus,
)
from twinfix.domain.models import (
    Approval,
    Attempt,
    Diagnosis,
    Incident,
    Patch,
    Project,
    Report,
    Signal,
    Twin,
    Verification,
    utcnow,
)
from twinfix.domain.repo import lock_incident, record_event, transition
from twinfix.domain.schemas import Check, DiagnosisResult, PatchResult, ReproResult, Verdict, VerificationResult
from twinfix.gateway import paths
from twinfix.ids import short_id
from twinfix.multirepo import involved_specs, repo_root_prefix
from twinfix.notify_routing import route_report
from twinfix.orchestrator.graph import StepDecision
from twinfix.orchestrator.integrity import IntegrityReport, check_patch
from twinfix.orchestrator.precedents import find_precedents
from twinfix.orchestrator.reporting import TERMINAL_REPORT, render
from twinfix.otel import span
from twinfix.profiles.base import LanguageProfile, TestSelector
from twinfix.profiles.factory import get_profile
from twinfix.services import required_merge_approvals
from twinfix.tracing import Budget, HaltedError, IncidentCancelledError, RunHandle, RunTracker
from twinfix.triage.rules import classify

if TYPE_CHECKING:
    from twinfix.config import Settings
    from twinfix.container import Adapters
    from twinfix.control import ControlPlane
    from twinfix.db import Store
    from twinfix.gateway.gateway import ToolGateway

log = logging.getLogger(__name__)
S = IncidentState

# 工作状态：有超时；等待状态（AWAITING_REVIEW / NEEDS_HUMAN）的超时由 sweeper 以 resume 方式处理
AUTO_APPROVE = "auto_merge_dev"

TIMED_STATES = frozenset({S.TRIAGED, S.DIAGNOSING, S.REPRODUCING, S.FIXING, S.VERIFYING, S.MERGING})

# 合并后默认观察期：期间同指纹再出现信号即转人工（项目配置 observe_s 覆盖，0 表示关闭）
DEFAULT_OBSERVE_S = 30 * 60

# 孪生记录停在 CREATING 超过这个时长即视为创建失败（进程崩溃/容器起了一半），由 sweeper 回收
TWIN_CREATE_GRACE_S = 3600


def _approval_votes(approval: Approval) -> list[dict[str, Any]]:
    votes = (approval.payload or {}).get("approvals")
    return [v for v in votes if isinstance(v, dict)] if isinstance(votes, list) else []


class StepFailedError(RuntimeError):
    """可重试的步骤失败：worker 会延迟重试，超过次数后由 _guard 转人工。"""


class IncidentBudgetExhausted(RuntimeError):
    """整个事故的累计预算（project.config["budget_per_incident_tokens"]）已用尽。

    与单次 Run 的 BudgetExceededError 不同：这不是某个 Agent 跑超了，而是整张事故单
    已经花掉太多钱，继续重试没有意义，必须由人决定是否追加预算。
    """


class Steps:
    def __init__(
        self,
        *,
        store: Store,
        gateway: ToolGateway,
        tracker: RunTracker,
        adapters: Adapters,
        agents: dict[AgentName, Agent],
        control: ControlPlane,
        settings: Settings,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.tracker = tracker
        self.adapters = adapters
        self.agents = agents
        self.control = control
        self.settings = settings

    def handlers(self) -> dict[IncidentState, Callable[[str], Awaitable[StepDecision]]]:
        table: dict[IncidentState, Callable[[Incident], Awaitable[Move]]] = {
            S.NEW: self.new,
            S.TRIAGED: self.triaged,
            S.DIAGNOSING: self.diagnosing,
            S.REPRODUCING: self.reproducing,
            S.FIXING: self.fixing,
            S.VERIFYING: self.verifying,
            S.AWAITING_REVIEW: self.awaiting_review,
            S.MERGING: self.merging,
            S.MERGED: self.merged,
            S.OBSERVING: self.observing,
            S.NEEDS_HUMAN: self.needs_human,
        }
        return {state: self._guard(state, fn) for state, fn in table.items()}

    # ------------------------------------------------------------------ 守卫

    def _guard(
        self, state: IncidentState, fn: Callable[[Incident], Awaitable[Move]]
    ) -> Callable[[str], Awaitable[StepDecision]]:
        async def run(incident_id: str) -> StepDecision:
            inc = await self._load(incident_id)
            if inc.state in TERMINAL_STATES:
                return StepDecision(inc.state)
            if inc.state is not state:
                # 只会在人工改库等异常情况下出现：以数据库为准重新对齐图
                log.warning("graph/db state mismatch for %s: node=%s db=%s", incident_id, state, inc.state)
                return StepDecision(inc.state)
            if inc.cancel_requested and state not in (S.AWAITING_REVIEW, S.NEEDS_HUMAN):
                return await self._apply(
                    inc.id, state, Move(S.CANCELLED, _cancel_reason(inc), actor=_cancel_actor(inc))
                )
            if await self.control.is_halted():
                raise HaltedError("system halted")
            if state in TIMED_STATES and _elapsed(inc) > self.settings.state_timeout_s:
                # MERGING 卡住通常是合并前重验期间进程中断：回到评审让操作人重新确认，
                # 比直接转人工更好恢复（审批通过后重跑合并前重验即可）。
                if state is S.MERGING:
                    return await self._apply(inc.id, state, Move(S.AWAITING_REVIEW, "合并前重验超时，回到评审重新确认"))
                return await self._apply(inc.id, state, Move(S.NEEDS_HUMAN, f"{state} 超时"))

            try:
                move = await fn(inc)
            except IncidentBudgetExhausted as exc:
                # 预算用尽不是暂时性失败，重试只会继续烧钱：直接转人工
                return await self._apply(
                    incident_id, state, Move(S.NEEDS_HUMAN, str(exc), payload={"budget_exhausted": True})
                )
            except IncidentCancelledError:
                inc = await self._load(incident_id)
                return await self._apply(
                    inc.id, state, Move(S.CANCELLED, _cancel_reason(inc), actor=_cancel_actor(inc))
                )
            except (HaltedError, GraphInterrupt):
                # GraphInterrupt 是 interrupt() 的挂起信号，必须交还给 LangGraph
                raise
            except Exception as exc:
                failures = await self._count_failure(incident_id, state, exc)
                if failures >= self.settings.step_max_retries:
                    return await self._apply(
                        incident_id, state, Move(S.NEEDS_HUMAN, f"{state} 连续失败 {failures} 次：{exc}")
                    )
                raise StepFailedError(f"{state} failed ({failures}/{self.settings.step_max_retries}): {exc}") from exc
            return await self._apply(incident_id, state, move)

        return run

    async def _apply(self, incident_id: str, expected: IncidentState, move: Move) -> StepDecision:
        if move.to is None:
            return StepDecision(None)
        async with self.store.tx() as tx:
            inc = await lock_incident(tx, incident_id)
            if inc.state is not expected:
                return StepDecision(inc.state)
            meta = dict(inc.meta)
            meta.pop("step_failures", None)
            meta.update(move.meta)
            inc.meta = meta
            await transition(tx, inc, move.to, reason=move.reason, actor=move.actor, payload=move.payload)
        return StepDecision(move.to)

    async def _count_failure(self, incident_id: str, state: IncidentState, exc: Exception) -> int:
        log.warning("step %s failed for %s: %r", state, incident_id, exc)
        async with self.store.tx() as tx:
            inc = await lock_incident(tx, incident_id)
            meta = dict(inc.meta)
            failures = dict(meta.get("step_failures", {}))
            failures[state.value] = failures.get(state.value, 0) + 1
            meta["step_failures"] = failures
            inc.meta = meta
            await record_event(
                tx,
                incident_id,
                "step_failed",
                reason=f"{type(exc).__name__}: {exc}"[:2000],
                payload={"state": state.value, "failures": failures[state.value]},
            )
        return int(failures[state.value])

    async def _load(self, incident_id: str) -> Incident:
        async with self.store.read() as s:
            inc = await s.get(Incident, incident_id)
        if inc is None:
            raise LookupError(incident_id)
        return inc

    # ------------------------------------------------------------------ 各状态

    async def new(self, inc: Incident) -> Move:
        project, signals = await self._project_and_signals(inc)
        decision = classify(title=inc.title, env=inc.env, severity=inc.severity, signals=signals, config=project.config)
        if not decision.is_bug:
            return Move(S.NOT_BUG, decision.reason, payload={"stage": "triage"})
        async with self.store.tx() as tx:
            row = await lock_incident(tx, inc.id)
            row.severity = decision.severity
        return Move(S.TRIAGED, decision.reason, payload={"severity": decision.severity.value})

    async def triaged(self, inc: Incident) -> Move:
        project, _ = await self._project_and_signals(inc)
        target = inc.target_branch or project.branch_map.get(inc.env.value) or "dev"
        base = inc.base_commit
        if base is None and inc.service:
            deploys = await self.adapters.observability.recent_deploys(inc.service, utcnow() - timedelta(days=30))
            deploys = [d for d in deploys if d.env == inc.env.value]
            if deploys:
                base = max(deploys, key=lambda d: d.deployed_at).sha
        if base is None:
            base = await self.adapters.codehost.branch_head(project.repo, target)
        async with self.store.tx() as tx:
            row = await lock_incident(tx, inc.id)
            row.target_branch = target
            row.base_commit = base
            row.fix_branch = row.fix_branch or f"fix/twinfix-{short_id(inc.id)}"
        return Move(S.DIAGNOSING, f"base {base[:10]} → {target}")

    async def diagnosing(self, inc: Incident) -> Move:
        await self._ensure_workspace(inc)
        # 诊断阶段就要能读运行时现场（线程栈、JVM 状态），所以孪生在这里拉起而不是等到复现。
        # 起不来不阻断诊断：诊断仍可基于日志/指标/代码完成，只是少了 jvm_probe。
        try:
            await self._ensure_twin(inc)
        except (HaltedError, IncidentCancelledError):
            raise
        except Exception as exc:
            log.warning("twin unavailable during diagnosis of %s: %r", inc.id, exc)
            async with self.store.tx() as tx:
                await record_event(tx, inc.id, "twin_unavailable", payload={"error": str(exc)[:500]})
        inputs: dict[str, Any] = {}
        if self.settings.precedents_enabled:
            _, signals = await self._project_and_signals(inc)
            if found := await find_precedents(self.store, inc, signals):
                inputs["precedents"] = found
                async with self.store.tx() as tx:
                    await record_event(tx, inc.id, "precedents", payload={"ids": [p["incident_id"] for p in found]})
        # 评审驳回时标注"根因不对"→ 重新诊断，把人工意见作为 feedback 带进去
        feedback = str(inc.meta.get("diagnosis_feedback") or "").strip() or None
        result = await self._run_agent(inc, AgentName.DIAGNOSER, inputs=inputs or None, feedback=feedback)
        if feedback:
            await self._consume_meta(inc.id, "diagnosis_feedback")
        if result.outcome == "not_bug":
            return Move(S.NOT_BUG, _reason(result), actor="agent:diagnoser")
        if result.outcome == "needs_human":
            return Move(S.NEEDS_HUMAN, _reason(result), actor="agent:diagnoser")
        diag = _as(result.payload, DiagnosisResult)
        async with self.store.tx() as tx:
            row = await tx.session.scalar(select(Diagnosis).where(Diagnosis.incident_id == inc.id))
            if row is None:
                row = Diagnosis(incident_id=inc.id)
                tx.session.add(row)
            row.summary = diag.summary
            row.root_cause = diag.root_cause
            row.hypotheses = [h.model_dump() for h in diag.hypotheses]
            row.suspect_locations = [loc.model_dump() for loc in diag.suspect_locations]
            row.confidence = diag.confidence
            row.repro = None
        payload = {"confidence": diag.confidence}
        if diag.confidence < self.settings.diagnosis_confidence_threshold:
            return Move(
                S.NEEDS_HUMAN,
                f"诊断置信度 {diag.confidence:.2f} 低于阈值 {self.settings.diagnosis_confidence_threshold}",
                actor="agent:diagnoser",
                payload=payload,
            )
        return Move(S.REPRODUCING, diag.summary[:300], actor="agent:diagnoser", payload=payload)

    async def reproducing(self, inc: Incident) -> Move:
        if not self.settings.repro_required:
            # 对照实验：去掉"先复现"约束——不写复现用例，直接进入修复。
            # 状态机仍保留 DIAGNOSING → REPRODUCING → FIXING 的轨迹（"没有复现就不能修复"
            # 是硬约束，对照实验只改变"复现阶段做什么"，不改状态机）；孪生环境照建，Verifier 的
            # twin_errors 检查依赖它。
            await self._ensure_workspace(inc)
            await self._ensure_twin(inc)
            async with self.store.tx() as tx:
                await record_event(tx, inc.id, "repro_skipped", payload={"reason": "repro_required=false"})
            return Move(
                S.FIXING,
                "跳过复现（对照实验），直接修复",
                meta={"next_attempt": {"trigger": "initial", "feedback": None}},
            )
        await self._ensure_workspace(inc)
        await self._ensure_twin(inc)
        diag = await self._diagnosis(inc.id)
        project = await self._project(inc)
        profile = self._profile_of(project)
        result = await self._run_agent(
            inc,
            AgentName.REPRODUCER,
            inputs={
                "diagnosis": diag,
                # 语言插件给出的用例骨架与测试目录：少走"先探测项目怎么写测试"的弯路
                "repro_guide": {
                    "language": profile.name,
                    "test_paths": profile.test_paths(),
                    "template": profile.repro_test_template(),
                },
            },
        )
        if result.outcome == "cannot_reproduce":
            return Move(S.CANNOT_REPRODUCE, _reason(result), actor="agent:reproducer")
        repro = _as(result.payload, ReproResult)
        if self.settings.repro_confirm and self.settings.sandbox != "local":
            # 稳定性由系统确认，不采信 Agent 自报的 failing_runs：在沙箱里独立重跑复现用例
            confirmed, output = await self._confirm_repro(inc, repro)
            if confirmed < _REPRO_CONFIRM_RUNS:
                return Move(
                    S.CANNOT_REPRODUCE,
                    f"复现不稳定：系统重跑 {_REPRO_CONFIRM_RUNS} 次仅 {confirmed} 次失败。{output[:300]}",
                    actor="agent:reproducer",
                )
            repro = repro.model_copy(
                update={"failing_runs": confirmed, "failure_output": output or repro.failure_output}
            )
        elif repro.failing_runs < 1:
            # 未启用系统确认（本地沙箱 / 对照实验）：退回 Agent 自报。
            # 门槛与 Reproducer 提示词一致（"一次因缺陷失败即可"）；要求 2 次会让 Agent 按提示词
            # 报的 failing_runs=1 全部判成 CANNOT_REPRODUCE，对照实验测到的就是门槛错配而非机制本身。
            return Move(S.CANNOT_REPRODUCE, "复现用例未失败（Agent 自报 0 次）", actor="agent:reproducer")
        # 只提交 Reproducer 明确声明的测试文件：临时探索用例不能随修复合并
        repro_files = _declared_repro_files(repro)
        async with self.store.tx() as tx:
            row = await tx.session.scalar(select(Diagnosis).where(Diagnosis.incident_id == inc.id))
            if row is None:
                raise StepFailedError("diagnosis missing")
            row.repro = {**repro.model_dump(), "test_files": repro_files}
        return Move(
            S.FIXING,
            f"复现用例 {repro.test_selector}",
            actor="agent:reproducer",
            meta={"next_attempt": {"trigger": "initial", "feedback": None}},
        )

    async def fixing(self, inc: Incident) -> Move:
        attempt = await self._current_or_new_attempt(inc)
        diag = await self._diagnosis(inc.id)
        result = await self._run_agent(
            inc,
            AgentName.FIXER,
            inputs={"diagnosis": diag, "repro": diag.get("repro")},
            attempt_seq=attempt.seq,
            feedback=attempt.feedback,
        )
        if result.outcome in ("cannot_fix", "needs_human"):
            await self._finish_attempt(attempt.id, AttemptStatus.ABORTED)
            target = S.CANNOT_FIX if result.outcome == "cannot_fix" else S.NEEDS_HUMAN
            return Move(target, _reason(result), actor="agent:fixer")
        patch = _as(result.payload, PatchResult)
        # 只接受已加载且标记 fixable 的附加仓库；其余条目视为模型臆造，直接丢弃
        allowed = {
            name for name, info in _repo_workspaces(inc).items() if info.get("fixable") == "1" and info.get("root")
        }
        ignored = sorted(set(patch.repos) - allowed)
        repos_payload = {
            name: {
                "diff": repo_patch.diff,
                "files_changed": [_norm_path(f) for f in repo_patch.files_changed],
            }
            for name, repo_patch in patch.repos.items()
            if name in allowed
        }
        if ignored:
            async with self.store.tx() as tx:
                await record_event(tx, inc.id, "fix_repos_ignored", payload={"repos": ignored})
        # touches_tests 由系统按语言插件的测试路径判定，不采信 Agent 自报
        test_globs = self._profile_for_inc(inc).test_paths()
        primary_files = [_norm_path(f) for f in patch.files_changed]
        touches_tests = any(paths.matches(f, test_globs) for f in primary_files) or any(
            paths.matches(f, test_globs) for p in repos_payload.values() for f in p["files_changed"]
        )
        async with self.store.tx() as tx:
            row = await tx.session.scalar(select(Patch).where(Patch.attempt_id == attempt.id))
            if row is None:
                row = Patch(attempt_id=attempt.id)
                tx.session.add(row)
            row.diff = patch.diff
            row.files_changed = primary_files
            row.repos = repos_payload
            row.touches_tests = touches_tests
            row.explanation = patch.explanation
        scope = ", ".join(primary_files) or "主仓库无改动"
        if repos_payload:
            scope += "；附加仓库：" + ", ".join(sorted(repos_payload))
        return Move(
            S.VERIFYING,
            f"第 {attempt.seq} 次修复：{scope}",
            actor="agent:fixer",
            payload={"attempt_seq": attempt.seq, "touches_tests": touches_tests, "repos": sorted(repos_payload)},
        )

    async def verifying(self, inc: Incident) -> Move:
        attempt = await self._latest_attempt(inc.id)
        if attempt is None:
            raise StepFailedError("no attempt to verify")
        diag = await self._diagnosis(inc.id)
        patch = await self._patch(attempt.id)
        repro = diag.get("repro") or {}
        repro_files = [f for f in [repro.get("test_file"), *(repro.get("test_files") or [])] if f]
        await self._deploy_fix(inc, attempt.seq, patch=patch, repro_files=repro_files)
        ver_inputs: dict[str, Any] = {"mode": "verify", "repro": diag.get("repro"), "patch": patch}
        if not diag.get("repro"):
            # 对照实验（repro_required=false）：没有复现用例，Verifier 只能按根因 + 回归 + 孪生错误判断
            ver_inputs["diagnosis"] = diag
        result = await self._run_agent(
            inc,
            AgentName.VERIFIER,
            inputs=ver_inputs,
            attempt_seq=attempt.seq,
        )
        if result.outcome == "already_fixed":
            raise StepFailedError("verifier returned already_fixed outside premerge mode")
        ver = self._check_integrity(_as(result.payload, VerificationResult), patch, inc)
        await self._save_verification(attempt.id, ver, base_sha=inc.base_commit)

        if not ver.passed:
            await self._finish_attempt(attempt.id, AttemptStatus.FAILED)
            feedback = ver.feedback or _failed_checks(ver)
            failed_streak = await self._verify_failure_streak(inc.id)
            limit = int(
                (await self._project(inc)).config.get("max_fix_attempts", self.settings.default_max_fix_attempts)
            )
            if failed_streak >= limit:
                return Move(S.NEEDS_HUMAN, f"连续 {failed_streak} 次验证失败：{feedback}", actor="agent:verifier")
            return Move(
                S.FIXING,
                f"验证失败，交回修复：{feedback}",
                actor="agent:verifier",
                meta={"next_attempt": {"trigger": "verify_failed", "feedback": feedback}},
            )

        await self._finish_attempt(attempt.id, AttemptStatus.PASSED)
        sha, pr = await self._publish_fix(inc, attempt, ver)
        return Move(S.AWAITING_REVIEW, f"验证通过，PR #{pr}", actor="agent:verifier", payload={"sha": sha, "pr": pr})

    async def awaiting_review(self, inc: Incident) -> Move:
        project = await self._project(inc)
        test_branch = project.branch_map.get("test", "dev")
        # 自动合并只允许到测试分支，main / release 永远需要人工（docs/05-safety.md §1）
        auto = bool(project.config.get("auto_merge_dev")) and inc.target_branch == test_branch
        approval = await self._ensure_approval(inc, ApprovalAction.MERGE, auto_approve=auto)
        await self._report(inc, ReportType.FIXED, key=f"fixed:{inc.state_entered_at.timestamp()}")
        required = required_merge_approvals(project.config)
        votes = _approval_votes(approval)

        # 结论已经落库（服务层在收齐审批票时把审批置为 APPROVED / REJECTED）：
        # 直接按库里的结果推进，节点重放也幂等，不会因为缺少 resume 值而卡住
        if approval.status is ApprovalStatus.APPROVED:
            actor = votes[-1]["actor"] if votes else f"user:{approval.decided_by or 'local'}"
            reason = "项目配置：验证通过后自动合并到测试分支" if approval.comment == AUTO_APPROVE else "人工确认合并"
            return Move(
                S.MERGING,
                reason,
                actor=actor,
                payload={"approval_id": approval.id, "approvals": len(votes)},
            )
        if approval.status is ApprovalStatus.REJECTED:
            return self._review_rejected(approval, actor=f"user:{approval.decided_by or 'local'}")

        # 等待审批：多人审批配置下，票数不够时继续挂起，直到收齐或有人驳回
        while True:
            decision: dict[str, Any] = interrupt(
                {
                    "waiting": "review",
                    "approval_id": approval.id,
                    "required": required,
                    "approved": len(votes),
                }
            )
            action = decision.get("action")
            actor = decision.get("actor", "system")
            if action == "approve":
                fresh = await self._reload_approval(approval.id)
                if fresh is None or fresh.status is ApprovalStatus.PENDING:
                    # 还差审批人：记下这一票（服务层已写库），重新挂起等待下一位
                    continue
                if fresh.status is ApprovalStatus.APPROVED:
                    votes = _approval_votes(fresh)
                    return Move(
                        S.MERGING,
                        "人工确认合并",
                        actor=votes[-1]["actor"] if votes else actor,
                        payload={"approval_id": fresh.id, "approvals": len(votes)},
                    )
                return self._review_rejected(fresh, actor=actor)
            if action == "reject":
                fresh = await self._reload_approval(approval.id)
                return self._review_rejected(fresh or approval, actor=actor)
            if action == "cancel":
                return Move(S.CANCELLED, decision.get("reason") or "人工取消", actor=actor)
            if action == "timeout":
                return Move(S.NEEDS_HUMAN, "等待评审超时")
            raise StepFailedError(f"unknown review decision {decision!r}")

    def _review_rejected(self, approval: Approval, *, actor: str) -> Move:
        actor = (approval.payload or {}).get("decided_actor") or actor
        comment = (approval.comment or "").strip()
        kind = (approval.payload or {}).get("reason_kind")
        if comment:
            # 根因判断错误 → 回到诊断；补丁/断言问题 → 回到修复
            target = S.DIAGNOSING if kind == "root_cause" else S.FIXING
            meta: dict[str, Any] = {"next_attempt": {"trigger": "review_rejected", "feedback": comment}}
            if target is S.DIAGNOSING:
                meta["diagnosis_feedback"] = comment
            return Move(
                target,
                f"驳回（{'根因不对' if target is S.DIAGNOSING else '修复待改'}）：{comment}",
                actor=actor,
                meta=meta,
            )
        return Move(S.NEEDS_HUMAN, "驳回（未附意见）", actor=actor)

    async def _reload_approval(self, approval_id: str) -> Approval | None:
        async with self.store.read() as s:
            return await s.get(Approval, approval_id)

    async def merging(self, inc: Incident) -> Move:
        project = await self._project(inc)
        approval = await self._approved(inc.id, ApprovalAction.MERGE)
        if approval is None:
            return Move(S.NEEDS_HUMAN, "缺少有效的合并审批")
        attempt = await self._latest_attempt(inc.id)
        last_ver = await self._latest_verification(attempt.id) if attempt else None
        if attempt is None or last_ver is None or inc.pr_number is None:
            return Move(S.NEEDS_HUMAN, "缺少验证记录或 PR")

        branch = inc.target_branch or "dev"
        diag = await self._diagnosis(inc.id)
        patch = await self._patch(attempt.id)
        repo_prs = inc.meta.get("repo_prs") or {}
        # 合并前预检（方案 B）：在目标分支最新 head 上重验；预检到真正合并之间 head 又前进时，
        # 最多再预检一轮，仍不稳定就交人工。代码托管的 merge 只校验修复分支头（expected_sha），
        # 不校验目标分支，这一步把竞态窗口收窄到最后一次读取 head 与 merge 调用之间。
        precheck_rounds = 0
        while True:
            head = await self.adapters.codehost.branch_head(project.repo, branch)
            # 目标分支在评审期间有新提交：先由系统判断是否已被别人修好，再把修复变基上去
            if head != last_ver.base_sha and (
                moved := await self._sync_with_target(inc, project.repo, diag.get("repro") or {})
            ):
                return moved
            extra_heads = await self._extra_heads(inc, repo_prs, branch)
            # 合并前在目标分支最新 head 上做最终验证；head 未变时 Verifier 可走快速路径
            result = await self._run_agent(
                inc,
                AgentName.VERIFIER,
                inputs={
                    "mode": "premerge",
                    "repro": diag.get("repro"),
                    "head": head,
                    "head_changed": head != last_ver.base_sha,
                },
                attempt_seq=attempt.seq,
            )
            if result.outcome == "already_fixed":
                return Move(S.ALREADY_FIXED, _reason(result), actor="agent:verifier")
            # 合并前重验同样过一遍系统侧完整性检查：评审期间补丁可能被改过
            ver = self._check_integrity(_as(result.payload, VerificationResult), patch, inc)
            await self._save_verification(attempt.id, ver, base_sha=head)
            if not ver.passed:
                return Move(
                    S.NEEDS_HUMAN,
                    f"合并前重验失败：{ver.feedback or _failed_checks(ver)}",
                    actor="agent:verifier",
                )

            # 预检完成后再读一次目标 head：期间有新提交说明刚才的结论已经过期
            final_head = await self.adapters.codehost.branch_head(project.repo, branch)
            final_extra = await self._extra_heads(inc, repo_prs, branch)
            if final_head == head and final_extra == extra_heads:
                break
            precheck_rounds += 1
            async with self.store.tx() as tx:
                await record_event(
                    tx,
                    inc.id,
                    "premerge_head_moved",
                    payload={
                        "branch": branch,
                        "verified": head,
                        "current": final_head,
                        "extra_repos": {
                            name: {"verified": extra_heads[name], "current": final_extra.get(name, "")}
                            for name in extra_heads
                            if extra_heads[name] != final_extra.get(name)
                        },
                        "round": precheck_rounds,
                    },
                )
            if final_extra != extra_heads:
                return Move(
                    S.NEEDS_HUMAN,
                    "附加仓库目标分支在合并前预检期间前进，无法自动变基重验；请人工确认后重试",
                    actor="system",
                )
            if precheck_rounds >= 2:
                return Move(
                    S.NEEDS_HUMAN,
                    f"目标分支 {branch} 在合并前预检期间连续前进（{head[:10]} → {final_head[:10]}），请人工确认后重试",
                    actor="system",
                )
            # 回到循环开头：重新同步/变基，并在新的 head 上再预检一轮

        run = await self._system_run(inc, attempt.seq)
        method = project.config.get("merge_method", "squash")
        merged_repos = list(inc.meta.get("merged_repos") or [])
        try:
            sha = await self._push(run, inc)
            await self.gateway.call(
                run, "set_check", {"sha": sha, "conclusion": "success", "summary": _check_summary(ver)}
            )
            # 附加仓库先合：主仓库 PR 是整套修复的提交点，主仓库合并失败时人工可以只处理剩余部分
            for name, info in repo_prs.items():
                if name in merged_repos or not isinstance(info, dict):
                    continue
                merged_extra = await self.gateway.call(
                    run,
                    "merge_pr",
                    {
                        "number": int(info["pr_number"]),
                        "expected_sha": str(info["commit_sha"]),
                        "method": method,
                        "repo": name,
                    },
                    approval_id=approval.id,
                )
                if not merged_extra.ok:
                    return Move(
                        S.NEEDS_HUMAN,
                        f"附加仓库 {name} 合并失败（主仓库尚未合并）：{merged_extra.content}",
                        actor="system",
                    )
                merged_repos.append(name)
                async with self.store.tx() as tx:
                    row = await lock_incident(tx, inc.id)
                    meta = dict(row.meta)
                    meta["merged_repos"] = merged_repos
                    row.meta = meta
                inc.meta = row.meta
            merged = await self.gateway.call(
                run,
                "merge_pr",
                {"number": inc.pr_number, "expected_sha": sha, "method": method},
                approval_id=approval.id,
            )
        finally:
            await self.tracker.finish(run, RunStatus.SUCCEEDED)
        if not merged.ok:
            raise StepFailedError(f"merge failed: {merged.content}")
        return Move(S.MERGED, f"已合并到 {inc.target_branch}", payload={"merge": merged.content})

    async def _extra_heads(self, inc: Incident, repo_prs: dict[str, Any], branch: str) -> dict[str, str]:
        """附加仓库目标分支 head；用于确认预检结论没有因为目标前进而过期（附加仓库不自动变基）。"""
        heads: dict[str, str] = {}
        workspaces = _repo_workspaces(inc)
        for name in repo_prs:
            repo = (workspaces.get(name) or {}).get("repo")
            if not repo:
                continue
            heads[name] = await self.adapters.codehost.branch_head(repo, branch)
        return heads

    async def _sync_with_target(self, inc: Incident, repo: str, repro: dict[str, Any]) -> Move | None:
        """目标分支前进后：复现用例在不带补丁的最新 head 上已通过 → ALREADY_FIXED；
        否则把修复变基到最新 head（冲突 → NEEDS_HUMAN）。返回 None 表示继续合并前重验。"""
        wm = self.adapters.workspace_manager
        ws = _workspace(inc)
        branch = inc.target_branch or "dev"
        if wm is None or ws is None:
            return None  # fake 代码托管：交给 Verifier 判断（stub 场景）
        test_file, selector = repro.get("test_file"), repro.get("test_selector")
        if test_file and selector:
            staged = await wm.stage_target_with(ws, repo, branch, [test_file])
            if staged is not None:
                target_sha, fix_head = staged
                profile = self._profile_for_inc(inc)
                try:
                    cls, _, method = str(selector).partition("#")
                    sel = TestSelector(methods=[selector]) if method else TestSelector(classes=[cls])
                    report = await profile.run_tests(self.adapters.sandbox, ws, sel)
                finally:
                    await wm.restore(ws, fix_head)
                ran = [c for c in report.cases if profile.case_matches(str(selector), c.name)]
                fixed_upstream = bool(ran) and not [c for c in ran if c.status in ("failed", "error")]
                async with self.store.tx() as tx:
                    await record_event(
                        tx, inc.id, "upstream_check", payload={"head": target_sha, "repro_passes": fixed_upstream}
                    )
                if fixed_upstream:
                    return Move(
                        S.ALREADY_FIXED,
                        f"目标分支 {branch}@{target_sha[:10]} 未应用补丁时复现用例 {selector} 已通过：评审期间已被修复",
                    )
        rebased = await wm.rebase_onto(ws, repo, branch)
        if not rebased.ok:
            files = "、".join(rebased.conflicts) or rebased.message[:200]
            return Move(S.NEEDS_HUMAN, f"修复与 {branch} 最新提交冲突，需要人工处理：{files}")
        return None

    async def merged(self, inc: Incident) -> Move:
        await self._cleanup(inc)
        project = await self._project(inc)
        try:
            observe_s = int(project.config.get("observe_s", DEFAULT_OBSERVE_S))
        except (TypeError, ValueError):
            observe_s = DEFAULT_OBSERVE_S
        if observe_s <= 0:
            # 观察期关闭：仍记一条 OBSERVING 轨迹，observing() 会立即归档
            observe_s = 0
        until = utcnow() + timedelta(seconds=observe_s)
        observation = {
            "until": until.isoformat(),
            "observe_s": observe_s,
            "signals_at_merge": inc.signal_count,
        }
        reason = "已合并，进入观察期" if observe_s > 0 else "已合并，环境已回收"
        return Move(
            S.OBSERVING,
            reason,
            meta={"observation": observation},
            payload={"observation": observation},
        )

    async def observing(self, inc: Incident) -> Move:
        """合并后观察期：等同指纹新信号或观察期满。未到期时留在本状态，由 sweeper 到点推进。"""
        observation = dict(inc.meta.get("observation") or {})
        until_raw = observation.get("until")
        if not until_raw:
            # 旧数据/人工改库：没有观察记录就直接归档
            return Move(S.CLOSED, "已合并，观察期已结束")
        if inc.signal_count > int(observation.get("signals_at_merge", 0)):
            meta = {"observation": {**observation, "result": "recurred", "recurred_at": utcnow().isoformat()}}
            return Move(
                S.NEEDS_HUMAN,
                "观察期内出现同指纹新信号，修复可能未生效",
                meta=meta,
                payload={"observation": meta["observation"]},
            )
        try:
            until = datetime.fromisoformat(until_raw)
        except ValueError:
            return Move(S.CLOSED, "已合并，观察期已结束")
        if utcnow() < until:
            # 观察期未到：挂起等待，sweeper 到点 / 复发信号 / 人工取消用 resume 唤醒
            decision = interrupt({"waiting": "observation", "until": until_raw})
            if isinstance(decision, dict) and decision.get("action") == "cancel":
                return Move(
                    S.CANCELLED,
                    decision.get("reason") or "人工取消",
                    actor=decision.get("actor", "system"),
                )
            # 唤醒后重新判断是否出现复发（inc 会重新读取，signal_count 可能已增加）
            return await self.observing(inc)
        meta = {"observation": {**observation, "result": "no_recurrence"}}
        return Move(
            S.CLOSED,
            "观察期结束，未出现复发",
            meta=meta,
            payload={"observation": meta["observation"]},
        )

    async def needs_human(self, inc: Incident) -> Move:
        approval = await self._ensure_approval(inc, ApprovalAction.HUMAN_TAKEOVER)
        await self._report(inc, ReportType.NEEDS_HUMAN, key=f"needs_human:{inc.state_entered_at.timestamp()}")

        decision: dict[str, Any] = interrupt({"waiting": "human", "approval_id": approval.id})
        await self._close_approval(approval.id, decision)
        action = decision.get("action")
        actor = decision.get("actor", "system")
        guidance = (decision.get("guidance") or "").strip() or None
        if action == "retry":
            target = S(decision.get("from", "DIAGNOSING"))
            if target not in (S.DIAGNOSING, S.REPRODUCING, S.FIXING, S.MERGING):
                raise StepFailedError(f"cannot restart from {target}")
            meta: dict[str, Any] = {"guidance": guidance}
            if target is S.FIXING:
                meta["next_attempt"] = {"trigger": "human_guided", "feedback": guidance}
            return Move(
                target,
                f"人工指导后从 {target} 重新开始" + (f"：{guidance}" if guidance else ""),
                actor=actor,
                meta=meta,
            )
        verdicts = {
            "not_bug": S.NOT_BUG,
            "cannot_fix": S.CANNOT_FIX,
            "already_fixed": S.ALREADY_FIXED,
            "cancel": S.CANCELLED,
        }
        if action in verdicts:
            return Move(verdicts[action], decision.get("reason") or f"人工判定 {action}", actor=actor)
        if action == "timeout":
            return Move(None, "")  # 超时只提醒，仍等待人工
        raise StepFailedError(f"unknown human decision {decision!r}")

    async def finalize(self, incident_id: str) -> StepDecision:
        inc = await self._load(incident_id)
        await self._cleanup(inc)
        report_type = TERMINAL_REPORT.get(inc.state)
        if report_type is not None:
            await self._report(inc, report_type, key=f"final:{inc.state.value}")
        async with self.store.tx() as tx:
            await record_event(tx, inc.id, "finalized", payload={"state": inc.state.value})
        return StepDecision(None)

    # ------------------------------------------------------------------ Agent 执行

    async def _run_agent(
        self,
        inc: Incident,
        agent_name: AgentName,
        *,
        inputs: dict[str, Any] | None = None,
        attempt_seq: int | None = None,
        feedback: str | None = None,
    ) -> AgentResult:
        project, signals = await self._project_and_signals(inc)
        guidance = inc.meta.get("guidance")
        if guidance and agent_name is not AgentName.REPORTER:
            feedback = f"{feedback}\n\n人工指导：{guidance}" if feedback else f"人工指导：{guidance}"
        await self._check_incident_budget(inc, project, agent_name)
        params = resolve_params(self.settings, project.config, agent_name)
        budget = Budget(params.max_tokens, params.max_tool_calls, params.max_seconds)
        profile = self._profile_of(project)
        run = await self.tracker.start(
            inc.id,
            agent_name,
            budget,
            attempt_seq=attempt_seq,
            model=params.model if self.settings.agent_mode == "llm" else None,
            workspace=_workspace(inc),
            twin=await self._twin_handle(inc.id),
            protected_paths=tuple(project.config.get("protected_paths", DEFAULT_PROTECTED)),
            test_paths=tuple(profile.test_paths()),
            repo=project.repo,
            profile=profile,
            repo_workspaces=_repo_workspaces(inc),
        )
        ctx = AgentContext(
            incident=_view(inc, signals),
            run=run,
            gateway=self.gateway,
            budget=budget,
            inputs=inputs or {},
            feedback=feedback,
            attempt_seq=attempt_seq,
            params=params,
        )
        try:
            with span(
                "agent.run",
                **{
                    "twinfix.incident_id": inc.id,
                    "twinfix.run_id": run.id,
                    "twinfix.agent": agent_name.value,
                    "twinfix.attempt_seq": attempt_seq,
                    "gen_ai.request.model": run.model,
                },
            ) as current:
                result = await self.agents[agent_name].run(ctx)
                if result.outcome not in OUTCOMES[agent_name]:
                    raise StepFailedError(f"{agent_name} returned unknown outcome {result.outcome!r}")
                current.set_attribute("twinfix.outcome", result.outcome)
                current.set_attribute("gen_ai.usage.input_tokens", run.input_tokens)
                current.set_attribute("gen_ai.usage.output_tokens", run.output_tokens)
                current.set_attribute("twinfix.cost_usd", float(run.cost))
        except IncidentCancelledError:
            await self.tracker.finish(run, RunStatus.CANCELLED, error="cancelled")
            raise
        except HaltedError:
            await self.tracker.finish(run, RunStatus.CANCELLED, error="halted")
            raise
        except Exception as exc:
            await self.tracker.finish(run, RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}"[:2000])
            raise
        await self.tracker.finish(run, RunStatus.SUCCEEDED, outcome=result.outcome)
        if guidance and agent_name is not AgentName.REPORTER:
            await self._consume_guidance(inc.id, guidance)
        return result

    async def _check_incident_budget(self, inc: Incident, project: Project, agent_name: AgentName) -> None:
        """单事故累计预算：超出即停，不再开新的 Agent 运行。

        只统计整张事故单的开销（Incident.cost_tokens，由 RunTracker 按增量累加）。
        单次运行的预算与异常重试策略不受影响——这里只是给"这张单总共花多少钱"封顶。
        报告代理不参与：事故收尾报告必须发得出去。
        """
        if agent_name is AgentName.REPORTER:
            return
        limit = int(project.config.get("budget_per_incident_tokens") or 0)
        if limit <= 0:
            return
        async with self.store.read() as s:
            used = await s.scalar(select(Incident.cost_tokens).where(Incident.id == inc.id))
        if used is not None and used >= limit:
            raise IncidentBudgetExhausted(
                f"事故累计预算已用尽（{used}/{limit} tokens），已停止自动处理；确认追加预算或人工处理后重试"
            )

    def _check_integrity(
        self, ver: VerificationResult, patch: dict[str, Any] | None, inc: Incident | None = None
    ) -> VerificationResult:
        """系统侧完整性检查；关掉开关时只透传（对照实验）。"""
        if not self.settings.integrity_check:
            return ver
        profile = self._profile_for_inc(inc) if inc is not None else self.adapters.profile
        return _apply_integrity(ver, patch, profile.test_paths())

    async def _confirm_repro(self, inc: Incident, repro: ReproResult) -> tuple[int, str]:
        """在复现工作区独立运行复现用例若干次；返回复现用例失败的次数与最后一次失败输出。

        系统自身的确认动作，不经 Agent 工具网关（与验证阶段装隐藏测试同理），结果记事件留痕。
        """
        ws = _workspace(inc)
        if ws is None:
            raise StepFailedError("workspace missing for repro confirmation")
        profile = self._profile_for_inc(inc)
        _, _, method = repro.test_selector.partition("#")
        selector = (
            TestSelector(methods=[repro.test_selector]) if method else TestSelector(classes=[repro.test_selector])
        )
        failures, output = 0, ""
        for _ in range(_REPRO_CONFIRM_RUNS):
            report = await profile.run_tests(self.adapters.sandbox, ws, selector)
            failed = [c for c in report.failed if profile.case_matches(repro.test_selector, c.name)]
            if failed:
                failures += 1
                output = "\n".join(f"{c.name}: {c.message[:500]}" for c in failed)
            elif report.command.exit_code != 0 and not report.cases:
                output = (report.command.stdout + report.command.stderr)[-500:]
        async with self.store.tx() as tx:
            await record_event(
                tx, inc.id, "repro_confirmed", payload={"selector": repro.test_selector, "failures": failures}
            )
        return failures, output

    async def _system_run(self, inc: Incident, attempt_seq: int | None) -> RunHandle:
        project = await self._project(inc)
        budget = Budget(10**6, 50, 600)
        return await self.tracker.start(
            inc.id,
            AgentName.ORCHESTRATOR,
            budget,
            attempt_seq=attempt_seq,
            workspace=_workspace(inc),
            repo=project.repo,
            profile=self._profile_of(project),
            repo_workspaces=_repo_workspaces(inc),
        )

    async def _consume_guidance(self, incident_id: str, guidance: str) -> None:
        async with self.store.tx() as tx:
            row = await lock_incident(tx, incident_id)
            if row.meta.get("guidance") == guidance:
                meta = dict(row.meta)
                meta.pop("guidance")
                row.meta = meta

    async def _consume_meta(self, incident_id: str, key: str) -> None:
        async with self.store.tx() as tx:
            row = await lock_incident(tx, incident_id)
            meta = dict(row.meta)
            meta.pop(key, None)
            row.meta = meta

    # ------------------------------------------------------------------ 资源

    async def _ensure_workspace(self, inc: Incident) -> Workspace:
        if (ws := _workspace(inc)) is not None:
            return ws
        project = await self._project(inc)
        specs = involved_specs(project, inc.service, inc.meta)
        primary = specs[0]
        sha = inc.base_commit or "HEAD"
        image = self._sandbox_image(project)
        ws = await self.adapters.sandbox.create_workspace(primary.repo, sha, image=image)
        extras: dict[str, dict[str, str]] = {}
        failed: list[dict[str, str]] = []
        if self.adapters.workspace_manager is not None:
            # 沙箱里没有凭据：代码经 bundle 从宿主机侧镜像传入，而不是直接 git clone 远端
            sha = await self.adapters.workspace_manager.checkout(ws, primary.repo, sha)
            for spec in specs[1:]:
                root = repo_root_prefix(spec, primary)
                candidates = [
                    inc.target_branch,
                    spec.branch_map.get("test"),
                    project.branch_map.get("test"),
                    "main",
                    "master",
                    "dev",
                ]
                refs = list(dict.fromkeys(ref for ref in candidates if ref))
                last_error: Exception | None = None
                extra_sha: str | None = None
                for ref in refs:
                    try:
                        extra_sha = await self.adapters.workspace_manager.checkout_into(ws, spec.repo, ref, root)
                        break
                    except Exception as exc:  # 尝试下一个常见分支；全部失败才记 failed
                        last_error = exc
                if extra_sha is None:
                    log.warning("cannot load extra repo %s (%s): %s", spec.name, spec.repo, last_error)
                    failed.append({"name": spec.name, "repo": spec.repo, "error": str(last_error)[:300]})
                    continue
                extras[spec.name] = {
                    "repo": spec.repo,
                    "sha": extra_sha,
                    "root": root,
                    "fixable": "1" if spec.fixable else "0",
                }
        else:
            await self.adapters.codehost.clone(primary.repo, sha, ws.root)
        repo_workspaces = {
            primary.name: {"repo": primary.repo, "sha": sha, "root": "", "fixable": "1"},
            **extras,
        }
        async with self.store.tx() as tx:
            row = await lock_incident(tx, inc.id)
            meta = dict(row.meta)
            meta["workspace"] = {"id": ws.id, "repo": ws.repo, "sha": sha, "root": str(ws.root), "image": ws.image}
            meta["repo_workspaces"] = repo_workspaces
            meta["repos"] = [{"name": spec.name, "repo": spec.repo, "primary": spec.primary} for spec in specs]
            meta["language"] = (project.language or "java").lower()
            meta["project_packages"] = [str(p) for p in project.config.get("project_packages", ())]
            meta["commands"] = project.config.get("commands") or {}
            if (project.language or "").lower() == "python":
                meta["python_executable"] = str(project.config.get("python_executable") or "python")
            row.meta = meta
            await record_event(
                tx,
                inc.id,
                "workspace_ready",
                payload={
                    "workspace_id": ws.id,
                    "sha": sha,
                    "primary": primary.name,
                    "repos": [spec.name for spec in specs],
                    "extra_repos": sorted(extras),
                    "failed_repos": failed,
                },
            )
        inc.meta = row.meta
        return ws

    async def _ensure_twin(self, inc: Incident) -> TwinHandle:
        if (handle := await self._twin_handle(inc.id)) is not None:
            return handle
        project = await self._project(inc)
        ttl = int(project.config.get("twin_ttl_s", 24 * 3600))
        async with self.store.tx() as tx:
            twin = await tx.session.scalar(select(Twin).where(Twin.incident_id == inc.id))
            if twin is None:
                twin = Twin(
                    incident_id=inc.id, compose_project=compose_project_name(inc.id), commit_sha=inc.base_commit
                )
                tx.session.add(twin)
            twin.status = TwinStatus.CREATING
        try:
            handle = await self.adapters.twin.create(
                TwinSpec(
                    inc.id,
                    project.repo,
                    inc.base_commit or "HEAD",
                    project.config.get("data_snapshot"),
                    ttl,
                    template=dict(project.config.get("twin") or {}),
                )
            )
        except Exception:
            # 创建失败也要落终态：诊断阶段把失败当"没有孪生"放过，若这里一直停在 CREATING，
            # 复现阶段既不会重试也等不到 READY，事故会卡死在一个 24 小时的空孪生记录上。
            async with self.store.tx() as tx:
                twin = await tx.session.scalar(select(Twin).where(Twin.incident_id == inc.id))
                if twin is not None:
                    twin.status = TwinStatus.FAILED
            raise
        ready = await self.adapters.twin.wait_ready(
            handle, timeout=int(project.config.get("twin_ready_timeout_s", 600))
        )
        async with self.store.tx() as tx:
            twin = await tx.session.scalar(select(Twin).where(Twin.incident_id == inc.id))
            assert twin is not None
            twin.status = TwinStatus.READY if ready else TwinStatus.FAILED
            twin.host, twin.ports, twin.urls = handle.host, handle.ports, handle.urls
            twin.expires_at = utcnow() + timedelta(seconds=ttl)
            await record_event(
                tx,
                inc.id,
                "twin_ready" if ready else "twin_failed",
                payload={"compose_project": handle.compose_project, "urls": handle.urls},
            )
        if not ready:
            await self.adapters.twin.destroy(handle)
            raise StepFailedError("twin environment did not become ready")
        return handle

    async def _deploy_fix(
        self,
        inc: Incident,
        attempt_seq: int,
        *,
        patch: dict[str, Any] | None,
        repro_files: list[str],
    ) -> None:
        """验证前：提交复现用例 + 补丁 → 推 fix 分支 → 孪生环境切到该提交。

        孪生环境从 git 提交构建；不这样做，twin_errors 检查看到的永远是修复前的代码。
        fake 代码托管（无 WorkspaceManager）时跳过。

        只提交本次修复声明的文件：补丁的 files_changed + 复现用例，既有的受控文件改动一并保留。
        Fixer 自验时新建的临时文件（例如临时用例）不进入提交，否则会随修复一起合并上线。
        """
        wm = self.adapters.workspace_manager
        ws = _workspace(inc)
        if wm is None or ws is None or not inc.fix_branch:
            return
        declared = {_norm_path(p) for p in (patch or {}).get("files_changed", [])}
        declared |= {_norm_path(p) for p in repro_files}
        changed = await wm.changed_files(ws)
        keep, dropped = _scoped_paths(changed, declared, await wm.tracked_at(ws))
        if dropped:
            log.warning("incident %s: 未声明的临时文件不进入提交 %s", inc.id, dropped)
            async with self.store.tx() as tx:
                await record_event(tx, inc.id, "fix_files_scoped", payload={"committed": keep, "dropped": dropped})
        message = f"fix: {inc.title}"[:200] + f"\n\nTwinFix incident {inc.id}, attempt {attempt_seq}"
        await wm.commit_paths(ws, keep, message)
        extra_commits: dict[str, str] = {}
        for name, repo_patch in ((patch or {}).get("repos") or {}).items():
            info = _repo_workspaces(inc).get(name) or {}
            root = info.get("root") or ""
            if not root or info.get("fixable") != "1" or not isinstance(repo_patch, dict):
                continue
            declared_extra = {_norm_path(p) for p in repo_patch.get("files_changed") or []}
            changed_extra = await wm.changed_files(ws, cwd=root)
            keep_extra, dropped_extra = _scoped_paths(changed_extra, declared_extra, await wm.tracked_at(ws, cwd=root))
            if dropped_extra:
                async with self.store.tx() as tx:
                    await record_event(
                        tx,
                        inc.id,
                        "fix_files_scoped",
                        payload={"repo": name, "committed": keep_extra, "dropped": dropped_extra},
                    )
            committed = await wm.commit_paths(ws, keep_extra, message, cwd=root)
            if committed:
                extra_commits[name] = committed
        if extra_commits:
            async with self.store.tx() as tx:
                await record_event(tx, inc.id, "extra_repos_committed", payload=extra_commits)
        head = await wm.head(ws)
        twin = await self._twin_handle(inc.id)
        if twin is not None and twin.sha == head:
            return
        run = await self._system_run(inc, attempt_seq)
        try:
            await self._push(run, inc)
        finally:
            await self.tracker.finish(run, RunStatus.SUCCEEDED)
        if twin is not None:
            await self._redeploy_twin(inc, head)

    async def _redeploy_twin(self, inc: Incident, sha: str) -> None:
        project = await self._project(inc)
        ttl = int(project.config.get("twin_ttl_s", 24 * 3600))
        # 同一事故的 create 是幂等的：复用项目名与端口槽位，按新提交重建镜像并滚动服务
        handle = await self.adapters.twin.create(
            TwinSpec(
                inc.id,
                project.repo,
                sha,
                project.config.get("data_snapshot"),
                ttl,
                template=dict(project.config.get("twin") or {}),
            )
        )
        ready = await self.adapters.twin.wait_ready(
            handle, timeout=int(project.config.get("twin_ready_timeout_s", 600))
        )
        async with self.store.tx() as tx:
            twin = await tx.session.scalar(select(Twin).where(Twin.incident_id == inc.id))
            assert twin is not None
            twin.status = TwinStatus.READY if ready else TwinStatus.FAILED
            twin.commit_sha = sha
            twin.host, twin.ports, twin.urls = handle.host, handle.ports, handle.urls
            await record_event(
                tx,
                inc.id,
                "twin_redeployed" if ready else "twin_failed",
                payload={"compose_project": handle.compose_project, "sha": sha},
            )
        if not ready:
            raise StepFailedError(f"twin environment did not become ready on {sha[:10]}")

    async def _twin_handle(self, incident_id: str) -> TwinHandle | None:
        async with self.store.read() as s:
            twin = await s.scalar(select(Twin).where(Twin.incident_id == incident_id, Twin.status == TwinStatus.READY))
        if twin is None:
            return None
        return TwinHandle(twin.compose_project, twin.host or "", twin.ports, twin.urls, twin.commit_sha or "")

    async def _cleanup(self, inc: Incident) -> None:
        if (handle := await self._twin_handle(inc.id)) is not None:
            await self.adapters.twin.destroy(handle)
            async with self.store.tx() as tx:
                twin = await tx.session.scalar(select(Twin).where(Twin.incident_id == inc.id))
                if twin is not None:
                    twin.status = TwinStatus.DESTROYED
                await record_event(tx, inc.id, "twin_destroyed", payload={"compose_project": handle.compose_project})
        if (ws := _workspace(inc)) is not None:
            await self.adapters.sandbox.destroy(ws)
            async with self.store.tx() as tx:
                row = await lock_incident(tx, inc.id)
                meta = dict(row.meta)
                meta.pop("workspace", None)
                row.meta = meta
            inc.meta = row.meta

    # ------------------------------------------------------------------ 修复发布

    async def _publish_fix(self, inc: Incident, attempt: Attempt, ver: VerificationResult) -> tuple[str, int]:
        project = await self._project(inc)
        patch = await self._patch(attempt.id) or {}
        repo_patches: dict[str, Any] = dict(patch.get("repos") or {})
        run = await self._system_run(inc, attempt.seq)
        try:
            sha = await self._push(run, inc)
            pr_number = inc.pr_number
            if pr_number is None:
                body = await self._pr_body(inc)
                opened = await self.gateway.call(
                    run,
                    "open_pr",
                    {
                        "head": inc.fix_branch,
                        "base": inc.target_branch,
                        "title": f"fix: {inc.title}"[:200],
                        "body": body,
                    },
                )
                if not opened.ok:
                    raise StepFailedError(f"open_pr failed: {opened.content}")
                pr_number = opened.data.number
                async with self.store.tx() as tx:
                    row = await lock_incident(tx, inc.id)
                    row.pr_number = pr_number
                    meta = dict(row.meta)
                    meta["pr_url"] = opened.data.url
                    row.meta = meta
                inc.pr_number = pr_number
                inc.meta = row.meta
            await self.gateway.call(
                run, "set_check", {"sha": sha, "conclusion": "success", "summary": _check_summary(ver)}
            )
            repo_prs: dict[str, dict[str, Any]] = {}
            for name, repo_patch in repo_patches.items():
                info = _repo_workspaces(inc).get(name) or {}
                if not info.get("root") or info.get("fixable") != "1":
                    continue
                extra_sha = await self._push(run, inc, repo=name)
                extra_pr = repo_patch.get("pr_number")
                extra_url = repo_patch.get("pr_url")
                if not isinstance(extra_pr, int):
                    opened_extra = await self.gateway.call(
                        run,
                        "open_pr",
                        {
                            "head": inc.fix_branch,
                            "base": inc.target_branch,
                            "title": f"fix({name}): {inc.title}"[:200],
                            "body": await self._pr_body(inc) + f"\n\n附加仓库：`{info.get('repo')}`",
                            "repo": name,
                        },
                    )
                    if not opened_extra.ok:
                        raise StepFailedError(f"open_pr on {name} failed: {opened_extra.content}")
                    extra_pr, extra_url = opened_extra.data.number, opened_extra.data.url
                await self.gateway.call(
                    run,
                    "set_check",
                    {
                        "sha": extra_sha,
                        "conclusion": "success",
                        "summary": _check_summary(ver),
                        "repo": name,
                    },
                )
                repo_patches[name] = {
                    **repo_patch,
                    "branch": inc.fix_branch,
                    "commit_sha": extra_sha,
                    "pr_number": extra_pr,
                    "pr_url": extra_url,
                }
                repo_prs[name] = {
                    "pr_number": extra_pr,
                    "pr_url": extra_url,
                    "commit_sha": extra_sha,
                    "branch": inc.fix_branch,
                }
            if repo_prs:
                async with self.store.tx() as tx:
                    row = await lock_incident(tx, inc.id)
                    meta = dict(row.meta)
                    meta["repo_prs"] = {**(meta.get("repo_prs") or {}), **repo_prs}
                    row.meta = meta
                inc.meta = row.meta
        finally:
            await self.tracker.finish(run, RunStatus.SUCCEEDED)
        async with self.store.tx() as tx:
            row_v = await tx.session.scalar(
                select(Verification)
                .where(Verification.attempt_id == attempt.id)
                .order_by(Verification.created_at.desc())
            )
            if row_v is not None:
                row_v.commit_sha = sha
            row_p = await tx.session.scalar(select(Patch).where(Patch.attempt_id == attempt.id))
            if row_p is not None:
                row_p.commit_sha = sha
                row_p.repos = repo_patches
        log.info("published fix for %s on %s as PR #%s (%s)", inc.id, project.repo, pr_number, sha)
        return sha, pr_number

    async def _push(self, run: RunHandle, inc: Incident, repo: str | None = None) -> str:
        args: dict[str, Any] = {"branch": inc.fix_branch}
        if repo:
            args["repo"] = repo
        pushed = await self.gateway.call(run, "push_branch", args)
        if not pushed.ok:
            raise StepFailedError(f"push failed: {pushed.content}")
        return str(pushed.data)

    async def _pr_body(self, inc: Incident) -> str:
        diag = await self._diagnosis(inc.id)
        lines = [
            f"TwinFix incident `{inc.id}`",
            "",
            "### 根因",
            diag.get("root_cause", ""),
            "",
            f"控制台：{self.settings.console_url}/incidents/{inc.id}",
        ]
        if inc.github_issue:
            lines += ["", f"Fixes #{inc.github_issue}"]
        return "\n".join(lines)

    # ------------------------------------------------------------------ 审批与报告

    async def _ensure_approval(self, inc: Incident, action: ApprovalAction, *, auto_approve: bool = False) -> Approval:
        async with self.store.tx() as tx:
            existing = await tx.session.scalar(
                select(Approval)
                .where(
                    Approval.incident_id == inc.id,
                    Approval.action == action,
                    Approval.created_at >= inc.state_entered_at,
                )
                .order_by(Approval.created_at.desc())
            )
            if existing is not None:
                return existing
            approval = Approval(
                incident_id=inc.id,
                action=action,
                payload={"pr_number": inc.pr_number, "target_branch": inc.target_branch},
                expires_at=utcnow() + timedelta(seconds=self.settings.review_timeout_s),
            )
            if auto_approve:
                approval.status = ApprovalStatus.APPROVED
                approval.decided_at = utcnow()
                approval.comment = AUTO_APPROVE
            tx.session.add(approval)
            await tx.session.flush()
            await record_event(
                tx,
                inc.id,
                "approval_requested",
                payload={"approval_id": approval.id, "action": action.value, "status": approval.status.value},
            )
            return approval

    async def _approved(self, incident_id: str, action: ApprovalAction) -> Approval | None:
        async with self.store.read() as s:
            approval: Approval | None = await s.scalar(
                select(Approval)
                .where(Approval.incident_id == incident_id, Approval.action == action)
                .order_by(Approval.created_at.desc())
            )
        if approval is None or approval.status is not ApprovalStatus.APPROVED:
            return None
        return approval

    async def _close_approval(self, approval_id: str, decision: dict[str, Any]) -> None:
        async with self.store.tx() as tx:
            approval = await tx.session.get(Approval, approval_id)
            if approval is not None and approval.status is ApprovalStatus.PENDING:
                approval.status = (
                    ApprovalStatus.EXPIRED if decision.get("action") == "timeout" else ApprovalStatus.APPROVED
                )
                approval.decided_at = utcnow()
                approval.comment = decision.get("guidance") or decision.get("reason")

    async def _report(self, inc: Incident, report_type: ReportType, *, key: str) -> None:
        project = await self._project(inc)
        plan = route_report(project.config, inc.severity, utcnow())
        async with self.store.tx() as tx:
            existing = await tx.session.scalar(
                select(Report).where(Report.incident_id == inc.id, Report.dedupe_key == key)
            )
            if existing is not None and existing.status is ReportStatus.SENT:
                return
            if existing is None:
                rendered = render(report_type, await self._report_data(tx.session, inc))
                existing = Report(
                    incident_id=inc.id,
                    type=report_type,
                    channel="all",
                    dedupe_key=key,
                    recipients=list(plan.recipients),
                    title=rendered.title,
                    content=rendered.content,
                )
                tx.session.add(existing)
                try:
                    await tx.session.flush()
                except IntegrityError:
                    return
            report_id, title, content, recipients = existing.id, existing.title, existing.content, existing.recipients

        if plan.is_suppressed:
            # 静默时段：报告留在控制台，标记 suppressed，流程照常收尾
            async with self.store.tx() as tx:
                row = await tx.session.get(Report, report_id)
                assert row is not None
                row.status, row.error, row.sent_at = ReportStatus.SUPPRESSED, plan.suppressed, None
                await record_event(
                    tx,
                    inc.id,
                    "report_suppressed",
                    reason=plan.suppressed,
                    payload={"report_id": report_id, "type": report_type.value},
                )
            return

        status, error = ReportStatus.SENT, None
        try:
            await self._run_agent(
                inc,
                AgentName.REPORTER,
                inputs={
                    "report_id": report_id,
                    "type": report_type.value,
                    "title": title,
                    "content": content,
                    "recipients": recipients,
                    "github_issue": inc.github_issue,
                },
            )
        except (IncidentCancelledError, HaltedError):
            raise
        except Exception as exc:
            # 通知失败不阻塞事故流程：报告留在控制台，状态标记为 failed
            status, error = ReportStatus.FAILED, str(exc)[:2000]
        async with self.store.tx() as tx:
            row = await tx.session.get(Report, report_id)
            assert row is not None
            row.status, row.error = status, error
            row.sent_at = utcnow() if status is ReportStatus.SENT else None
            await record_event(
                tx,
                inc.id,
                "report",
                payload={"report_id": report_id, "type": report_type.value, "status": status.value},
            )

    async def _report_data(self, session: Any, inc: Incident) -> dict[str, Any]:
        fresh = await session.get(Incident, inc.id)
        incident = {
            "id": fresh.id,
            "title": fresh.title,
            "severity": fresh.severity.value,
            "env": fresh.env.value,
            "service": fresh.service,
            "target_branch": fresh.target_branch,
            "fix_branch": fresh.fix_branch,
            "closed_reason": fresh.closed_reason,
            "cost_tokens": fresh.cost_tokens,
        }
        data: dict[str, Any] = {
            "incident": incident,
            "console_url": f"{self.settings.console_url}/incidents/{inc.id}",
            "pr_url": fresh.meta.get("pr_url"),
            "repo_prs": fresh.meta.get("repo_prs") or {},
        }
        diag = await session.scalar(select(Diagnosis).where(Diagnosis.incident_id == inc.id))
        if diag is not None:
            data["diagnosis"] = {
                "summary": diag.summary,
                "root_cause": diag.root_cause,
                "confidence": diag.confidence,
                "suspect_locations": diag.suspect_locations,
            }
        attempt = await session.scalar(
            select(Attempt).where(Attempt.incident_id == inc.id).order_by(Attempt.seq.desc())
        )
        if attempt is not None:
            patch = await session.scalar(select(Patch).where(Patch.attempt_id == attempt.id))
            if patch is not None:
                data["patch"] = {
                    "diff": patch.diff,
                    "repos": patch.repos or {},
                    "explanation": patch.explanation,
                }
            ver = await session.scalar(
                select(Verification)
                .where(Verification.attempt_id == attempt.id)
                .order_by(Verification.created_at.desc())
            )
            if ver is not None:
                data["verification"] = {"checks": ver.checks, "passed": ver.passed}
        twin = await session.scalar(select(Twin).where(Twin.incident_id == inc.id))
        if twin is not None and twin.status is TwinStatus.READY:
            data["twin"] = {"compose_project": twin.compose_project, "urls": twin.urls}
        if fresh.state in TERMINAL_STATES or fresh.state is S.NEEDS_HUMAN:
            data["reason"] = fresh.closed_reason or await self._last_reason(session, inc.id)
        return data

    async def _last_reason(self, session: Any, incident_id: str) -> str | None:
        from twinfix.domain.models import IncidentEvent

        ev = await session.scalar(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == incident_id, IncidentEvent.kind == "transition")
            .order_by(IncidentEvent.id.desc())
        )
        return ev.reason if ev else None

    # ------------------------------------------------------------------ 查询

    async def _project(self, inc: Incident) -> Project:
        async with self.store.read() as s:
            project = await s.get(Project, inc.project_id)
        if project is None:
            raise LookupError(inc.project_id)
        return project

    def _sandbox_image(self, project: Project) -> str | None:
        """沙箱镜像按项目语言切换：Python 需要 pytest，Go 需要工具链，Java 用默认 Maven/JDK 镜像。"""
        configured = (project.config or {}).get("sandbox_image")
        if configured:
            return str(configured)
        language = (project.language or "java").lower()
        return {
            "python": self.settings.sandbox_image_python,
            "go": self.settings.sandbox_image_go,
            "typescript": self.settings.sandbox_image_typescript,
            "csharp": self.settings.sandbox_image_csharp,
        }.get(language)

    def _profile_of(self, project: Project) -> LanguageProfile:
        """按项目语言选择插件；与默认插件同名时复用实例（测试会替换 adapters.profile）。"""
        language = (project.language or "java").lower()
        config = project.config or {}
        if language == str(getattr(self.adapters.profile, "name", "java")) and not config.get("commands"):
            return self.adapters.profile
        packages = tuple(str(p) for p in config.get("project_packages", ()))
        return get_profile(
            language,
            packages,
            str(config.get("python_executable") or "python"),
            config.get("commands"),
        )

    def _profile_for_inc(self, inc: Incident) -> LanguageProfile:
        """工作区创建时把语言写进 meta，后续步骤不必再查 Project。"""
        meta = inc.meta or {}
        default_name = str(getattr(self.adapters.profile, "name", "java"))
        language = str(meta.get("language") or default_name).lower()
        if language == default_name and not meta.get("commands"):
            return self.adapters.profile
        packages = tuple(str(p) for p in meta.get("project_packages") or ())
        return get_profile(
            language,
            packages,
            str(meta.get("python_executable") or "python"),
            meta.get("commands"),
        )

    async def _project_and_signals(self, inc: Incident) -> tuple[Project, list[dict[str, Any]]]:
        project = await self._project(inc)
        async with self.store.read() as s:
            rows = (
                await s.scalars(
                    select(Signal).where(Signal.incident_id == inc.id).order_by(Signal.occurred_at.desc()).limit(20)
                )
            ).all()
        signals = [
            {
                "source": r.source.value,
                "service": r.service,
                "trace_id": r.trace_id,
                "occurred_at": r.occurred_at.isoformat(),
                **{k: r.raw.get(k) for k in ("message", "stacktrace", "url") if k in r.raw},
            }
            for r in rows
        ]
        return project, signals

    async def _diagnosis(self, incident_id: str) -> dict[str, Any]:
        async with self.store.read() as s:
            d = await s.scalar(select(Diagnosis).where(Diagnosis.incident_id == incident_id))
        if d is None:
            return {}
        return {
            "summary": d.summary,
            "root_cause": d.root_cause,
            "hypotheses": d.hypotheses,
            "suspect_locations": d.suspect_locations,
            "confidence": d.confidence,
            "repro": d.repro,
        }

    async def _patch(self, attempt_id: str) -> dict[str, Any] | None:
        async with self.store.read() as s:
            p = await s.scalar(select(Patch).where(Patch.attempt_id == attempt_id))
        if p is None:
            return None
        return {
            "diff": p.diff,
            "files_changed": p.files_changed,
            "repos": p.repos or {},
            "touches_tests": p.touches_tests,
            "explanation": p.explanation,
        }

    async def _latest_attempt(self, incident_id: str) -> Attempt | None:
        async with self.store.read() as s:
            return await s.scalar(
                select(Attempt).where(Attempt.incident_id == incident_id).order_by(Attempt.seq.desc())
            )

    async def _latest_verification(self, attempt_id: str) -> Verification | None:
        async with self.store.read() as s:
            return await s.scalar(
                select(Verification)
                .where(Verification.attempt_id == attempt_id)
                .order_by(Verification.created_at.desc())
            )

    async def _current_or_new_attempt(self, inc: Incident) -> Attempt:
        async with self.store.tx() as tx:
            row = await lock_incident(tx, inc.id)
            latest = await tx.session.scalar(
                select(Attempt).where(Attempt.incident_id == inc.id).order_by(Attempt.seq.desc())
            )
            if latest is not None and latest.status is AttemptStatus.RUNNING:
                return latest  # 崩溃重放：继续同一次尝试
            pending = dict(row.meta).get("next_attempt") or {"trigger": "initial", "feedback": None}
            attempt = Attempt(
                incident_id=inc.id,
                seq=(latest.seq + 1) if latest else 1,
                trigger=pending.get("trigger", "initial"),
                feedback=pending.get("feedback"),
            )
            tx.session.add(attempt)
            meta = dict(row.meta)
            meta.pop("next_attempt", None)
            row.meta = meta
            await tx.session.flush()
            await record_event(
                tx,
                inc.id,
                "attempt_started",
                payload={
                    "attempt_id": attempt.id,
                    "seq": attempt.seq,
                    "trigger": attempt.trigger,
                    "feedback": attempt.feedback,
                },
            )
            return attempt

    async def _finish_attempt(self, attempt_id: str, status: AttemptStatus) -> None:
        async with self.store.tx() as tx:
            attempt = await tx.session.get(Attempt, attempt_id)
            if attempt is not None:
                attempt.status = status

    async def _verify_failure_streak(self, incident_id: str) -> int:
        """自上一次人工介入（驳回/指导）以来，连续验证失败的次数。"""
        async with self.store.read() as s:
            attempts = (
                await s.scalars(select(Attempt).where(Attempt.incident_id == incident_id).order_by(Attempt.seq.desc()))
            ).all()
        streak = 0
        for a in attempts:
            if a.status is not AttemptStatus.FAILED:
                break
            streak += 1
            if a.trigger != "verify_failed":
                break
        return streak

    async def _save_verification(self, attempt_id: str, ver: VerificationResult, *, base_sha: str | None) -> None:
        async with self.store.tx() as tx:
            tx.session.add(
                Verification(
                    attempt_id=attempt_id,
                    commit_sha=ver.commit_sha,
                    base_sha=ver.base_sha or base_sha,
                    checks=[c.model_dump() for c in ver.checks],
                    passed=ver.passed,
                )
            )

    async def pending_timeouts(self) -> list[tuple[str, IncidentState]]:
        """sweeper 使用：返回超时的事故（工作状态与等待状态）。"""
        now = datetime.now(UTC)
        work_cutoff = now - timedelta(seconds=self.settings.state_timeout_s)
        review_cutoff = now - timedelta(seconds=self.settings.review_timeout_s)
        # 观察期到点：meta.observation.until 由 merged() 写入，格式与 now.isoformat() 一致，可直接按字符串比较
        observe_due = (Incident.state == S.OBSERVING) & (
            Incident.meta["observation"]["until"].astext <= now.isoformat()
        )
        async with self.store.read() as s:
            rows = (
                await s.execute(
                    select(Incident.id, Incident.state).where(
                        ((Incident.state.in_(TIMED_STATES)) & (Incident.state_entered_at < work_cutoff))
                        | ((Incident.state == S.AWAITING_REVIEW) & (Incident.state_entered_at < review_cutoff))
                        | observe_due
                    )
                )
            ).all()
        return [(r[0], r[1]) for r in rows]

    async def expired_twins(self, stale_create_s: int = TWIN_CREATE_GRACE_S) -> list[str]:
        """sweeper 使用：该回收的孪生（docs/05-safety.md §4 的 TTL）。

        - READY 且 expires_at 已过：事故关不掉的孪生（例如停在 NEEDS_HUMAN 等人工）到点回收。
        - CREATING 且超过宽限期仍未就绪：创建途中进程崩了，容器可能起了一半，记录也永远等不到 READY。
        """
        now = utcnow()
        async with self.store.read() as s:
            rows = await s.scalars(
                select(Twin.id).where(
                    ((Twin.status == TwinStatus.READY) & (Twin.expires_at < now))
                    | (
                        (Twin.status == TwinStatus.CREATING)
                        & (Twin.updated_at < now - timedelta(seconds=stale_create_s))
                    )
                )
            )
            return list(rows.all())

    async def reap_twin(self, twin_id: str) -> None:
        """TTL 回收：先销毁容器再落 DESTROYED。销毁失败时状态不变，下一轮 sweeper 会再试一次。"""
        async with self.store.read() as s:
            twin = await s.get(Twin, twin_id)
        if twin is None or twin.status is TwinStatus.DESTROYED:
            return
        observed = twin.updated_at
        handle = TwinHandle(twin.compose_project, twin.host or "", twin.ports, twin.urls, twin.commit_sha or "")
        await self.adapters.twin.destroy(handle)
        async with self.store.tx() as tx:
            row = await tx.session.scalar(select(Twin).where(Twin.id == twin_id).with_for_update())
            if row is None or row.updated_at != observed or row.status is TwinStatus.DESTROYED:
                return  # 销毁期间被重新拉起（例如人工恢复后重建），这条记录已经不属于我们
            row.status = TwinStatus.DESTROYED
            await record_event(
                tx,
                row.incident_id,
                "twin_destroyed",
                payload={"compose_project": handle.compose_project, "reason": "ttl"},
            )

    async def count_active(self) -> int:
        async with self.store.read() as s:
            return int(
                await s.scalar(select(func.count()).select_from(Incident).where(Incident.state.not_in(TERMINAL_STATES)))
                or 0
            )


DEFAULT_PROTECTED = (
    "**/application-prod.yml",
    "**/application-prod.yaml",
    "**/application-prod.properties",
    "deploy/**",
    ".github/**",
    "Jenkinsfile",
    "**/*.pem",
    "**/*.key",
)


def _declared_repro_files(repro: ReproResult) -> list[str]:
    """Reproducer 明确声明的测试文件；未声明的临时用例不进入修复提交。"""
    return sorted({_norm_path(p) for p in [repro.test_file, *repro.test_files] if p})


class Move:
    __slots__ = ("actor", "meta", "payload", "reason", "to")

    def __init__(
        self,
        to: IncidentState | None,
        reason: str,
        *,
        actor: str = "system",
        payload: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.to = to
        self.reason = reason
        self.actor = actor
        self.payload = payload or {}
        self.meta = meta or {}


def _elapsed(inc: Incident) -> float:
    return (datetime.now(UTC) - inc.state_entered_at).total_seconds()


def _cancel_actor(inc: Incident) -> str:
    return str(inc.meta.get("cancelled_by", "system"))


def _cancel_reason(inc: Incident) -> str:
    return str(inc.meta.get("cancel_reason") or "取消请求")


def _reason(result: AgentResult) -> str:
    payload = result.payload
    return payload.reason if isinstance(payload, Verdict) else result.outcome


def _as[T: BaseModel](payload: BaseModel, cls: type[T]) -> T:
    if not isinstance(payload, cls):
        raise StepFailedError(f"expected {cls.__name__}, got {type(payload).__name__}")
    return payload


_REPRO_CONFIRM_RUNS = 2


def _apply_integrity(
    ver: VerificationResult, patch: dict[str, Any] | None, test_globs: list[str]
) -> VerificationResult:
    """系统侧的测试完整性检查覆盖 Verifier 自报的同名检查：违规即验证失败，可疑只记录。"""
    data = patch or {}
    reports = [check_patch(data.get("diff") or "", test_globs)]
    for name, repo_patch in (data.get("repos") or {}).items():
        if not isinstance(repo_patch, dict):
            continue
        repo_report = check_patch(repo_patch.get("diff") or "", test_globs)
        reports.append(
            IntegrityReport(
                violations=[f"[{name}] {v}" for v in repo_report.violations],
                suspicious=[f"[{name}] {s}" for s in repo_report.suspicious],
            )
        )
    violations = [v for report in reports for v in report.violations]
    suspicious = [s for report in reports for s in report.suspicious]
    detail = (
        "; ".join([*(f"违规: {v}" for v in violations), *(f"可疑: {s}" for s in suspicious)])
        or "no existing tests modified; no suspicious patterns"
    )
    checks = [c for c in ver.checks if c.name != "test_integrity"]
    checks.append(Check(name="test_integrity", status="passed" if not violations else "failed", detail=detail))
    if not violations:
        return ver.model_copy(update={"checks": checks})
    feedback = "测试完整性检查未通过：" + "；".join(violations) + "。不能删除、跳过或弱化已有测试，请只修改业务代码。"
    return ver.model_copy(update={"checks": checks, "passed": False, "feedback": feedback})


def _failed_checks(ver: VerificationResult) -> str:
    return "; ".join(f"{c.name}: {c.detail}" for c in ver.checks if c.status == "failed") or "verification failed"


def _check_summary(ver: VerificationResult) -> str:
    return "\n".join(f"- {c.name}: {c.status} {c.detail}" for c in ver.checks)


def _norm_path(path: str) -> str:
    """把 Agent 报的路径归一成仓库相对路径（去掉 ./ 前缀与反斜杠）。"""
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _scoped_paths(changed: list[str], declared: set[str], tracked: set[str]) -> tuple[list[str], list[str]]:
    """把工作区改动分成"进提交"和"留在工作区"两组。

    既有受控文件的改动一律提交（修复经常顺带改到相邻文件）；新增文件必须由 Agent 声明，
    否则是自验留下的临时产物。返回 (keep, dropped)，两者都保持 changed 的顺序。
    """
    keep = [f for f in changed if f in declared or f in tracked]
    dropped = [f for f in changed if f not in declared and f not in tracked]
    return keep, dropped


def _workspace(inc: Incident) -> Workspace | None:
    raw = inc.meta.get("workspace")
    if not raw:
        return None
    from pathlib import Path

    return Workspace(id=raw["id"], repo=raw["repo"], sha=raw["sha"], root=Path(raw["root"]), image=raw.get("image"))


def _repo_workspaces(inc: Incident) -> dict[str, dict[str, str]]:
    raw = inc.meta.get("repo_workspaces")
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): {str(k): str(v) for k, v in info.items()} for name, info in raw.items() if isinstance(info, dict)
    }


def _view(inc: Incident, signals: list[dict[str, Any]]) -> IncidentView:
    raw_loaded = inc.meta.get("repo_workspaces")
    loaded: dict[str, Any] = raw_loaded if isinstance(raw_loaded, dict) else {}
    repos = [
        {
            "name": name,
            "repo": info.get("repo"),
            "primary": not info.get("root"),
            "fixable": info.get("fixable") == "1" or not info.get("root"),
        }
        for name, info in loaded.items()
        if isinstance(info, dict)
    ]
    return IncidentView(
        id=inc.id,
        title=inc.title,
        severity=inc.severity.value,
        env=inc.env.value,
        service=inc.service,
        fingerprint=inc.fingerprint,
        base_commit=inc.base_commit,
        target_branch=inc.target_branch,
        signals=signals,
        meta={
            **{k: v for k, v in inc.meta.items() if k in ("scenario", "labels", "source_url")},
            "repos": repos,
        },
    )
