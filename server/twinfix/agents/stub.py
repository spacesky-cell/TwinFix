"""内置模拟 Agent（stub）：不调用 LLM，按事故上的场景（incident.meta["scenario"]）给出确定性结论。

它们照常经由网关调用工具，所以审计、事件流、权限校验与真实 Agent 完全一致，
用来端到端验证状态机的每一条分支。

场景：
  happy               全流程成功
  not_bug             诊断判定非 bug
  low_confidence      诊断置信度不足 → 转人工
  cannot_reproduce    无法复现
  cannot_fix          判定超出能力
  fixer_needs_human   修复中需要人工
  verify_fail_once    第 1 次验证失败，第 2 次通过
  verify_fail_always  每次验证都失败（耗尽次数 → 转人工）
  already_fixed       合并前发现目标分支已修复
  merge_conflict      合并前重验失败（语义冲突）
"""

from __future__ import annotations

import asyncio

from twinfix.agents.base import AgentContext, AgentResult
from twinfix.domain.enums import AgentName
from twinfix.domain.schemas import (
    Check,
    CodeLocation,
    DiagnosisResult,
    Hypothesis,
    PatchResult,
    ReproResult,
    Verdict,
    VerificationResult,
)

REPRO_TEST = "src/test/java/twinfix/TwinfixReproTest.java"
FIX_FILE = "src/main/java/twinfix/Fixed.java"


def _scenario(ctx: AgentContext) -> str:
    return str(ctx.incident.meta.get("scenario", "happy"))


class _Stub:
    name: AgentName

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s

    async def pause(self) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)


class StubDiagnoser(_Stub):
    name = AgentName.DIAGNOSER

    async def run(self, ctx: AgentContext) -> AgentResult:
        scenario = _scenario(ctx)
        await ctx.tool("search_logs", query=ctx.incident.title[:60] or "error", service=ctx.incident.service)
        await self.pause()
        stack = next((s.get("stacktrace") for s in ctx.incident.signals if s.get("stacktrace")), None)
        if stack:
            await ctx.tool("parse_stacktrace", text=stack)
        await ctx.tool("record_hypothesis", id="h1", statement="并发下库存扣减非原子", confidence=0.8)
        await ctx.tool("record_hypothesis", id="h2", statement="缓存与数据库不一致", confidence=0.2, status="rejected")
        await self.pause()

        if scenario == "not_bug":
            return AgentResult("not_bug", Verdict(reason="异常为业务校验失败（预期行为），非缺陷"))
        confidence = 0.3 if scenario == "low_confidence" else 0.85
        return AgentResult(
            "diagnosed",
            DiagnosisResult(
                summary=f"[stub] {ctx.incident.title}",
                root_cause="库存扣减为先读后写，并发请求下出现超卖",
                hypotheses=[
                    Hypothesis(id="h1", statement="并发下库存扣减非原子", confidence=0.8, status="confirmed"),
                    Hypothesis(id="h2", statement="缓存与数据库不一致", confidence=0.2, status="rejected"),
                ],
                suspect_locations=[CodeLocation(file=FIX_FILE, line=1, symbol="twinfix.Fixed")],
                confidence=confidence,
            ),
        )


class StubReproducer(_Stub):
    name = AgentName.REPRODUCER

    async def run(self, ctx: AgentContext) -> AgentResult:
        await self.pause()
        if _scenario(ctx) == "cannot_reproduce":
            await ctx.tool("twin_request", service="gateway", method="POST", path="/api/orders")
            return AgentResult("cannot_reproduce", Verdict(reason="孪生环境中 20 次并发请求均未触发"))
        await ctx.tool("write_file", path=REPRO_TEST, content="class TwinfixReproTest { /* stub */ }\n")
        await ctx.tool("twin_request", service="gateway", method="POST", path="/api/orders")
        await self.pause()
        return AgentResult(
            "reproduced",
            ReproResult(
                test_file=REPRO_TEST,
                test_selector="TwinfixReproTest",
                command="./mvnw -B test -Dtest=TwinfixReproTest",
                failure_output="expected stock >= 0 but was -3",
                failing_runs=1,  # 与 Reproducer 提示词约定一致：一次因缺陷失败即可
            ),
        )


class StubFixer(_Stub):
    name = AgentName.FIXER

    async def run(self, ctx: AgentContext) -> AgentResult:
        scenario = _scenario(ctx)
        await ctx.tool("read_file", path=REPRO_TEST)
        await self.pause()
        if scenario == "cannot_fix":
            return AgentResult("cannot_fix", Verdict(reason="根因在第三方 SDK 内部，超出可修改范围"))
        if scenario == "fixer_needs_human":
            return AgentResult("needs_human", Verdict(reason="修复涉及数据库表结构变更，需要人工决策"))
        note = f"attempt {ctx.attempt_seq}" + (f", feedback: {ctx.feedback}" if ctx.feedback else "")
        await ctx.tool("write_file", path=FIX_FILE, content=f"class Fixed {{ /* stub fix, {note} */ }}\n")
        await self.pause()
        return AgentResult(
            "patch_ready",
            PatchResult(
                diff=f"--- a/{FIX_FILE}\n+++ b/{FIX_FILE}\n@@\n-// read-then-write\n+// atomic update ({note})\n",
                files_changed=[FIX_FILE],
                touches_tests=False,
                explanation="改为 UPDATE ... WHERE stock >= ? 的条件原子扣减",
            ),
        )


class StubVerifier(_Stub):
    name = AgentName.VERIFIER

    async def run(self, ctx: AgentContext) -> AgentResult:
        scenario = _scenario(ctx)
        await ctx.tool("read_file", path=FIX_FILE)
        await ctx.tool("twin_logs", service="gateway")
        await self.pause()

        if ctx.inputs.get("mode") == "premerge":
            if scenario == "already_fixed":
                return AgentResult("already_fixed", Verdict(reason="复现用例在目标分支最新提交上已通过（未应用补丁）"))
            if scenario == "merge_conflict":
                return AgentResult("failed", _verification(False, "rebase 后回归测试失败：与目标分支改动冲突"))
            return AgentResult("passed", _verification(True))

        fail = scenario == "verify_fail_always" or (scenario == "verify_fail_once" and ctx.attempt_seq == 1)
        if fail:
            return AgentResult("failed", _verification(False, "复现用例仍失败：并发 50 时库存为 -1"))
        return AgentResult("passed", _verification(True))


def _verification(passed: bool, feedback: str | None = None) -> VerificationResult:
    repro = "passed" if passed else "failed"
    return VerificationResult(
        checks=[
            Check(name="build", status="passed"),
            Check(name="repro", status=repro, detail=feedback or "2/2 runs passed"),
            Check(name="regression", status="passed", detail="128 tests"),
            Check(name="twin_errors", status="passed", detail="no new ERROR logs"),
            Check(name="test_integrity", status="passed", detail="no existing tests modified"),
        ],
        passed=passed,
        feedback=feedback,
    )
