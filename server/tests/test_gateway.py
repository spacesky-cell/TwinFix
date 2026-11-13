"""工具网关：授权、参数、路径、频率、预算、审批、脱敏、截断、审计。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from tests.conftest import Harness
from twinfix.adapters.codehost import FakeCodeHost
from twinfix.adapters.types import CommandResult
from twinfix.domain.enums import AgentName, ApprovalStatus, SignalSource, ToolCallStatus, ToolLevel
from twinfix.domain.models import Approval, Evidence, ToolCall
from twinfix.gateway.registry import Tool, ToolEnv, ToolOutput
from twinfix.orchestrator.steps import DEFAULT_PROTECTED
from twinfix.profiles.base import TestReport as ProfileTests
from twinfix.profiles.python import PythonProfile
from twinfix.services import SignalIn
from twinfix.tracing import Budget, IncidentCancelledError, RunHandle

pytestmark = pytest.mark.integration


async def make_run(h: Harness, agent: AgentName, *, max_tool_calls: int = 50, with_ws: bool = True) -> RunHandle:
    project = await h.project()
    h.dispatcher.auto = False
    res = await h.service.ingest(
        SignalIn(project=project.name, source=SignalSource.MANUAL, title=f"gw {agent}", meta={"scenario": "happy"})
    )
    ws = await h.adapters.sandbox.create_workspace(project.repo, "abc") if with_ws else None
    return await h.rt.tracker.start(
        res.incident_id,
        agent,
        Budget(10_000, max_tool_calls, 600),
        workspace=ws,
        protected_paths=DEFAULT_PROTECTED,
        test_paths=("**/src/test/**",),
        repo=project.repo,
    )


async def audit(h: Harness, run: RunHandle) -> list[ToolCall]:
    async with h.rt.store.read() as s:
        return list(
            (await s.scalars(select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.created_at))).all()
        )


async def test_agent_cannot_call_tool_outside_its_list(harness: Harness) -> None:
    run = await make_run(harness, AgentName.DIAGNOSER)
    r = await harness.rt.gateway.call(run, "write_file", {"path": "a.java", "content": "x"})
    assert r.status is ToolCallStatus.DENIED and "not available" in r.content
    r = await harness.rt.gateway.call(run, "rm_rf", {})
    assert r.status is ToolCallStatus.DENIED
    calls = await audit(harness, run)
    assert [c.status for c in calls] == [ToolCallStatus.DENIED, ToolCallStatus.DENIED]


async def test_invalid_arguments_are_rejected(harness: Harness) -> None:
    run = await make_run(harness, AgentName.DIAGNOSER)
    r = await harness.rt.gateway.call(run, "read_file", {"path": "a", "max_lines": 999999})
    assert r.status is ToolCallStatus.DENIED and "invalid arguments" in r.content


@pytest.mark.parametrize(
    ("tool", "args", "hint"),
    [
        ("read_file", {"path": "nope/Missing.java"}, "file not found in workspace"),
        ("list_dir", {"path": "nope"}, "directory not found in workspace"),
    ],
)
async def test_missing_path_is_an_actionable_error(
    harness: Harness, tool: str, args: dict[str, str], hint: str
) -> None:
    """路径写错要回一句可照着改的话，而不是 internal error（Agent 会把内部错误当成工具坏了）。"""
    run = await make_run(harness, AgentName.DIAGNOSER)
    r = await harness.rt.gateway.call(run, tool, args)
    assert r.status is ToolCallStatus.ERROR and hint in r.content and "internal error" not in r.content


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "C:/Windows/x", "src/../../x"])
async def test_path_escape_is_rejected(harness: Harness, path: str) -> None:
    run = await make_run(harness, AgentName.FIXER)
    r = await harness.rt.gateway.call(run, "write_file", {"path": path, "content": "x"})
    assert r.status is ToolCallStatus.DENIED and "outside the workspace" in r.content


@pytest.mark.parametrize(
    "path",
    ["deploy/prod.yml", ".github/workflows/ci.yml", "order/src/main/resources/application-prod.yml", "Jenkinsfile"],
)
async def test_protected_paths_are_rejected(harness: Harness, path: str) -> None:
    run = await make_run(harness, AgentName.FIXER)
    r = await harness.rt.gateway.call(run, "write_file", {"path": path, "content": "x"})
    assert r.status is ToolCallStatus.DENIED and "protected" in r.content


async def test_reproducer_may_only_write_tests(harness: Harness) -> None:
    run = await make_run(harness, AgentName.REPRODUCER)
    gw = harness.rt.gateway
    ok = await gw.call(run, "write_file", {"path": "order/src/test/java/ReproTest.java", "content": "class T {}"})
    assert ok.ok, ok.content
    denied = await gw.call(run, "write_file", {"path": "order/src/main/java/Order.java", "content": "x"})
    assert denied.status is ToolCallStatus.DENIED and "only write test files" in denied.content
    assert run.workspace is not None
    assert (run.workspace.root / "order/src/test/java/ReproTest.java").read_text() == "class T {}"
    assert not (run.workspace.root / "order/src/main/java/Order.java").exists()


def seed_extra(run: RunHandle, *, fixable: bool = True) -> None:
    assert run.workspace is not None
    root = run.workspace.root / "_repos" / "inventory"
    (root / "src" / "main").mkdir(parents=True)
    (root / "src" / "main" / "Stock.java").write_bytes(b"class Stock {}\n")
    run.repo_workspaces = {
        "inventory": {
            "repo": "acme/inventory",
            "sha": "0" * 40,
            "root": "_repos/inventory",
            "fixable": "1" if fixable else "0",
        }
    }


async def test_write_extra_repo_requires_fixer_and_fixable(harness: Harness) -> None:
    h = harness
    gw = h.rt.gateway
    args = {"repo": "inventory", "path": "src/main/Stock.java", "content": "class Stock { int qty; }\n"}

    # Reproducer 能写测试文件，但也没有资格改附加仓库源码
    reproducer = await make_run(h, AgentName.REPRODUCER)
    seed_extra(reproducer)
    r = await gw.call(
        reproducer,
        "write_file",
        {"repo": "inventory", "path": "src/test/java/com/acme/StockTest.java", "content": "class StockTest {}"},
    )
    assert r.status is ToolCallStatus.DENIED and "only the fixer" in r.content

    fixer = await make_run(h, AgentName.FIXER)
    seed_extra(fixer, fixable=False)
    r = await gw.call(fixer, "write_file", args)
    assert r.status is ToolCallStatus.DENIED and "read-only" in r.content
    assert not (fixer.workspace.root / "_repos/inventory/src/main/Stock.java").read_text().endswith("qty; }\n")

    # 不能从主仓库根目录直接写 _repos/... 绕过 fixable 授权
    r = await gw.call(
        fixer, "write_file", {"path": "_repos/inventory/src/main/Stock.java", "content": "class Evicted {}\n"}
    )
    assert r.status is ToolCallStatus.DENIED and "written with repo=<name>" in r.content

    r = await gw.call(fixer, "write_file", {**args, "repo": "nope"})
    assert r.status is ToolCallStatus.ERROR and "repo not loaded" in r.content

    fixer.repo_workspaces["inventory"]["fixable"] = "1"
    r = await gw.call(fixer, "write_file", {**args, "repo": "inventory"})
    assert r.ok and "wrote inventory:src/main/Stock.java" in r.content
    assert (fixer.workspace.root / "_repos/inventory/src/main/Stock.java").read_text().endswith("qty; }\n")
    # 主仓库写入不受影响，且不会落进附加仓库
    primary = await gw.call(fixer, "write_file", {"path": "src/main/Order.java", "content": "class Order {}\n"})
    assert primary.ok and "wrote src/main/Order.java" in primary.content


async def test_build_and_tests_route_to_extra_repo_root(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness
    run = await make_run(h, AgentName.FIXER)
    seed_extra(run)
    seen: list[tuple[str, object, str | None]] = []

    async def fake_build(sandbox, ws, module, cwd=None):
        seen.append(("build", module, cwd))
        return CommandResult(0, "ok", "", 1)

    async def fake_tests(sandbox, ws, selector, cwd=None):
        seen.append(("tests", selector.classes, cwd))
        return ProfileTests(CommandResult(0, "ok", "", 1), [])

    monkeypatch.setattr(h.adapters.profile, "build", fake_build)
    monkeypatch.setattr(h.adapters.profile, "run_tests", fake_tests)
    gw = h.rt.gateway

    assert (await gw.call(run, "build", {"repo": "inventory", "module": "inventory-core"})).ok
    assert seen[-1] == ("build", "inventory-core", "_repos/inventory")
    assert (await gw.call(run, "build", {})).ok
    assert seen[-1] == ("build", None, None)
    assert (await gw.call(run, "run_tests", {"repo": "inventory", "classes": ["com.acme.StockTest"]})).ok
    assert seen[-1] == ("tests", ["com.acme.StockTest"], "_repos/inventory")
    assert (await gw.call(run, "run_tests", {})).ok
    assert seen[-1] == ("tests", [], None)


async def test_codeintel_tools_follow_run_profile(harness: Harness) -> None:
    h = harness
    run = await make_run(h, AgentName.FIXER)
    assert run.workspace is not None
    await h.adapters.sandbox.write_file(
        run.workspace,
        "src/shop/service.py",
        "class Service:\n    def execute(self, n):\n        return n\n",
    )
    run.profile = PythonProfile()
    gw = h.rt.gateway

    found = await gw.call(run, "search_symbol", {"query": "execute"})
    assert found.ok and "shop.service.Service#execute/1" in found.content
    outline = await gw.call(run, "code_outline", {"path": "src/shop/service.py"})
    assert outline.ok and "[method]" in outline.content and "execute" in outline.content
    # 没有 Python profile 时同一工作区不会索引 .py，证明工具读的是 run.profile 而不是全局默认
    run.profile = None
    missed = await gw.call(run, "search_symbol", {"query": "execute"})
    assert missed.ok and "no symbols match" in missed.content


async def test_external_tools_route_to_extra_repo(harness: Harness) -> None:
    h = harness
    run = await make_run(h, AgentName.ORCHESTRATOR)
    seed_extra(run)
    host = h.adapters.codehost
    assert isinstance(host, FakeCodeHost)
    head = "a" * 40
    host.heads[("acme/inventory", "fix/twinfix-multi")] = head
    gw = h.rt.gateway

    opened = await gw.call(
        run,
        "open_pr",
        {
            "repo": "inventory",
            "head": "fix/twinfix-multi",
            "base": "dev",
            "title": "fix(inventory)",
            "body": "cross-repo",
        },
    )
    assert opened.ok, opened.content
    assert host.prs[opened.data.number].req.repo == "acme/inventory"

    checked = await gw.call(
        run, "set_check", {"repo": "inventory", "sha": head, "conclusion": "success", "summary": "ok"}
    )
    assert checked.ok and host.checks[-1][0] == "acme/inventory"

    commented = await gw.call(
        run, "comment", {"repo": "inventory", "kind": "pr", "number": opened.data.number, "body": "done"}
    )
    assert commented.ok and host.comments[-1][0].repo == "acme/inventory"

    # fake 托管没有 WorkspaceManager：附加仓库推送必须明确报错，不能悄悄推主仓库
    pushed = await gw.call(run, "push_branch", {"repo": "inventory", "branch": "fix/twinfix-multi"})
    assert pushed.status is ToolCallStatus.ERROR and "require a workspace manager" in pushed.content


async def test_verifier_cannot_write(harness: Harness) -> None:
    run = await make_run(harness, AgentName.VERIFIER)
    r = await harness.rt.gateway.call(run, "write_file", {"path": "src/test/X.java", "content": "x"})
    assert r.status is ToolCallStatus.DENIED


async def test_per_tool_rate_limit(harness: Harness) -> None:
    run = await make_run(harness, AgentName.DIAGNOSER)
    tool = harness.rt.gateway.registry.get("recent_deploys")
    assert tool is not None
    statuses = [
        (await harness.rt.gateway.call(run, "recent_deploys", {"service": "order"})).status
        for _ in range(tool.max_calls_per_run + 1)
    ]
    assert statuses[:-1] == [ToolCallStatus.OK] * tool.max_calls_per_run
    assert statuses[-1] is ToolCallStatus.DENIED


async def test_budget_limit(harness: Harness) -> None:
    run = await make_run(harness, AgentName.DIAGNOSER, max_tool_calls=2)
    gw = harness.rt.gateway
    s = [(await gw.call(run, "search_logs", {"query": "x"})).status for _ in range(3)]
    assert s == [ToolCallStatus.OK, ToolCallStatus.OK, ToolCallStatus.DENIED]


async def test_merge_requires_approval(harness: Harness) -> None:
    h = harness
    run = await make_run(h, AgentName.ORCHESTRATOR)
    args = {"number": 1, "expected_sha": "x"}
    r = await h.rt.gateway.call(run, "merge_pr", args)
    assert r.status is ToolCallStatus.PENDING_APPROVAL and r.approval_id
    async with h.rt.store.read() as s:
        approval = await s.get(Approval, r.approval_id)
    assert approval is not None and approval.status is ApprovalStatus.PENDING
    # 未批准的审批不能放行
    r2 = await h.rt.gateway.call(run, "merge_pr", args, approval_id=approval.id)
    assert r2.status is ToolCallStatus.DENIED
    # 批准后放行（PR 不存在，适配器返回失败 → ERROR，说明已越过审批进入执行）
    async with h.rt.store.tx() as tx:
        a = await tx.session.get(Approval, approval.id)
        assert a is not None
        a.status = ApprovalStatus.APPROVED
    r3 = await h.rt.gateway.call(run, "merge_pr", args, approval_id=approval.id)
    assert r3.status is ToolCallStatus.ERROR and "merge rejected" in r3.content


async def test_twin_request_unknown_service_is_actionable_error(harness: Harness) -> None:
    from twinfix.adapters.types import TwinHandle

    run = await make_run(harness, AgentName.REPRODUCER)
    run.twin = TwinHandle("p", "127.0.0.1", {"order": 1}, {"order": "http://127.0.0.1:1"}, "abc")
    r = await harness.rt.gateway.call(run, "twin_request", {"service": "postgres", "path": "/"})
    assert r.status is ToolCallStatus.ERROR
    assert "unknown twin service 'postgres'" in r.content and "['order']" in r.content


async def test_non_orchestrator_cannot_merge(harness: Harness) -> None:
    run = await make_run(harness, AgentName.REPORTER)
    r = await harness.rt.gateway.call(run, "merge_pr", {"number": 1, "expected_sha": "x"})
    assert r.status is ToolCallStatus.DENIED


async def test_push_only_fix_branches(harness: Harness) -> None:
    run = await make_run(harness, AgentName.ORCHESTRATOR)
    r = await harness.rt.gateway.call(run, "push_branch", {"branch": "main"})
    assert r.status is ToolCallStatus.DENIED


async def test_cancel_raises(harness: Harness) -> None:
    from twinfix.domain.models import Incident

    run = await make_run(harness, AgentName.DIAGNOSER)
    async with harness.rt.store.tx() as tx:
        inc = await tx.session.get(Incident, run.incident_id)
        assert inc is not None
        inc.cancel_requested = True
    with pytest.raises(IncidentCancelledError):
        await harness.rt.gateway.call(run, "search_logs", {"query": "x"})


async def test_secrets_are_redacted_in_audit_and_results(harness: Harness) -> None:
    h = harness
    run = await make_run(h, AgentName.FIXER)
    secret = "password=hunter2 phone 13812345678"
    r = await h.rt.gateway.call(run, "write_file", {"path": "src/main/A.java", "content": secret})
    assert r.ok
    read = await h.rt.gateway.call(run, "read_file", {"path": "src/main/A.java"})
    assert "hunter2" not in read.content and "13812345678" not in read.content
    for call in await audit(h, run):
        dumped = str(call.args) + (call.result_summary or "")
        assert "hunter2" not in dumped and "13812345678" not in dumped


async def test_large_results_are_truncated_into_evidence(harness: Harness) -> None:
    h = harness

    class Args(BaseModel):
        pass

    async def big(env: ToolEnv, a: Args) -> ToolOutput:
        return ToolOutput(content="x" * 5000)

    h.rt.gateway.registry.register(Tool("big_tool", "", ToolLevel.L0, Args, big, max_result_chars=100))
    from twinfix.gateway import policy

    original = policy.POLICIES[AgentName.DIAGNOSER]
    policy.POLICIES[AgentName.DIAGNOSER] = policy.AgentPolicy(original.tools | {"big_tool"}, original.max_level)
    try:
        run = await make_run(h, AgentName.DIAGNOSER)
        r = await h.rt.gateway.call(run, "big_tool", {})
    finally:
        policy.POLICIES[AgentName.DIAGNOSER] = original
    assert r.ok and r.evidence_id and "truncated 4900 chars" in r.content
    async with h.rt.store.read() as s:
        ev = await s.get(Evidence, r.evidence_id)
    assert ev is not None and ev.content is not None and len(ev.content) == 5000


async def test_tool_timeout(harness: Harness) -> None:
    import asyncio

    h = harness

    class Args(BaseModel):
        pass

    async def slow(env: ToolEnv, a: Args) -> ToolOutput:
        await asyncio.sleep(5)
        return ToolOutput(content="late")

    h.rt.gateway.registry.register(Tool("slow_tool", "", ToolLevel.L0, Args, slow, timeout_s=0.1))
    from twinfix.gateway import policy

    original = policy.POLICIES[AgentName.DIAGNOSER]
    policy.POLICIES[AgentName.DIAGNOSER] = policy.AgentPolicy(original.tools | {"slow_tool"}, original.max_level)
    try:
        run = await make_run(h, AgentName.DIAGNOSER)
        r = await h.rt.gateway.call(run, "slow_tool", {})
    finally:
        policy.POLICIES[AgentName.DIAGNOSER] = original
    assert r.status is ToolCallStatus.TIMEOUT


async def test_workspace_paths_are_physically_confined(tmp_path: Path) -> None:
    from twinfix.adapters.sandbox import LocalSandbox, PathEscapeError

    sb = LocalSandbox(tmp_path)
    ws = await sb.create_workspace("r", "s")
    await sb.write_file(ws, "a/b.txt", "ok")
    with pytest.raises(PathEscapeError):
        await sb.write_file(ws, "../evil.txt", "x")
    with pytest.raises(PermissionError):
        await sb.exec(ws, ["echo", "hi"], 5)


async def test_many_log_lines_are_clustered_with_raw_in_evidence(harness: Harness) -> None:
    from datetime import UTC, datetime, timedelta

    from twinfix.adapters.observability import FakeObservability
    from twinfix.adapters.types import LogLine

    obs = harness.adapters.observability
    assert isinstance(obs, FakeObservability)
    now = datetime.now(UTC)
    obs.logs = [
        LogLine(now - timedelta(seconds=i), "ERROR", f"ledger write failed for order {1000 + i}", "order")
        for i in range(120)
    ]
    run = await make_run(harness, AgentName.DIAGNOSER)
    r = await harness.rt.gateway.call(run, "search_logs", {"query": "ledger", "limit": 500})
    assert r.ok and "120 hits → 1 templates" in r.content and "[120x] ERROR" in r.content
    assert "raw lines in evidence" in r.content and r.evidence_id
    async with harness.rt.store.read() as s:
        ev = await s.get(Evidence, r.evidence_id)
    assert ev is not None and ev.content is not None and ev.content.count("ledger write failed") == 120


async def test_log_cluster_switch_off_returns_raw_lines(harness: Harness) -> None:
    """对照开关：关闭后不再聚类，直接回贴原始行（内容变长、不做模板归并）。"""
    from datetime import UTC, datetime, timedelta

    from twinfix.adapters.observability import FakeObservability
    from twinfix.adapters.types import LogLine

    obs = harness.adapters.observability
    assert isinstance(obs, FakeObservability)
    now = datetime.now(UTC)
    obs.logs = [
        LogLine(now - timedelta(seconds=i), "ERROR", f"ledger write failed for order {1000 + i}", "order")
        for i in range(30)
    ]
    harness.rt.gateway.log_cluster = False
    run = await make_run(harness, AgentName.DIAGNOSER)
    r = await harness.rt.gateway.call(run, "search_logs", {"query": "ledger", "limit": 500})
    assert r.ok
    assert "templates" not in r.content and "[30x]" not in r.content
    assert r.content.count("ledger write failed") == 30  # 原始行全在


async def test_jvm_probe_is_diagnoser_only(harness: Harness) -> None:
    """jvm_probe 是 L1 只读探针：诊断 Agent 可用，其他 Agent 的授权表里没有它。"""
    run = await make_run(harness, AgentName.REPRODUCER)
    r = await harness.rt.gateway.call(run, "jvm_probe", {"service": "gateway", "command": "thread -b"})
    assert r.status is ToolCallStatus.DENIED and "not available" in r.content


async def test_jvm_probe_hidden_for_profiles_without_runtime_probe(harness: Harness) -> None:
    """Python/Go/TypeScript 项目没有 JVM 现场，工具列表里不应再露出 jvm_probe。"""
    run = await make_run(harness, AgentName.DIAGNOSER)
    run.profile = PythonProfile()
    names = [tool.name for tool in harness.rt.gateway.tools_for(run)]
    assert "jvm_probe" not in names and "read_file" in names
    run.profile = None  # 回落到 adapters 的默认（Java）插件
    assert "jvm_probe" in [tool.name for tool in harness.rt.gateway.tools_for(run)]


async def test_jvm_probe_rejects_non_whitelisted_command(harness: Harness) -> None:
    from twinfix.adapters.types import TwinHandle

    run = await make_run(harness, AgentName.DIAGNOSER)
    run.twin = TwinHandle("p", "127.0.0.1", {"gateway": 1}, {"gateway": "http://127.0.0.1:1"}, "abc")
    r = await harness.rt.gateway.call(run, "jvm_probe", {"service": "gateway", "command": "stop"})
    assert r.status is ToolCallStatus.ERROR
    assert "not allowed" in r.content and "thread -b" in r.content
    # 归一化空白：多打几个空格不能绕过白名单，也不该被拒绝
    ok = await harness.rt.gateway.call(run, "jvm_probe", {"service": "gateway", "command": "thread    -b"})
    assert ok.ok and "[fake arthas@gateway] thread -b" in ok.content


async def test_jvm_probe_needs_twin_and_known_service(harness: Harness) -> None:
    from twinfix.adapters.types import TwinHandle

    run = await make_run(harness, AgentName.DIAGNOSER)
    r = await harness.rt.gateway.call(run, "jvm_probe", {"service": "gateway", "command": "jvm"})
    assert r.status is ToolCallStatus.ERROR and "no twin environment attached" in r.content
    run.twin = TwinHandle("p", "127.0.0.1", {"order": 1}, {"order": "http://127.0.0.1:1"}, "abc")
    r = await harness.rt.gateway.call(run, "jvm_probe", {"service": "postgres", "command": "jvm"})
    assert r.status is ToolCallStatus.ERROR
    assert "unknown twin service 'postgres'" in r.content and "['order']" in r.content


async def test_jvm_probe_thread_output_is_thread_dump_evidence(harness: Harness) -> None:
    from twinfix.adapters.types import TwinHandle
    from twinfix.domain.enums import EvidenceKind

    run = await make_run(harness, AgentName.DIAGNOSER)
    run.twin = TwinHandle("p", "127.0.0.1", {"gateway": 1}, {"gateway": "http://127.0.0.1:1"}, "abc")
    r = await harness.rt.gateway.call(run, "jvm_probe", {"service": "gateway", "command": "thread -b"})
    assert r.ok and r.evidence_id and '"main" #1' in r.content
    async with harness.rt.store.read() as s:
        ev = await s.get(Evidence, r.evidence_id)
    assert ev is not None and ev.kind is EvidenceKind.THREAD_DUMP
    # 非 thread 命令走普通工具输出证据
    r = await harness.rt.gateway.call(run, "jvm_probe", {"service": "gateway", "command": "jvm"})
    async with harness.rt.store.read() as s:
        ev = await s.get(Evidence, r.evidence_id)
    assert ev is not None and ev.kind is EvidenceKind.TOOL_OUTPUT
