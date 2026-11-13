"""用例自检：注入后 CI 仍绿（bug 潜伏）、隐藏测试能抓住 bug、参考修复能通过隐藏测试与回归。

新增或修改用例后必须跑一遍；判分是否可信取决于这里。
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from twinfix.adapters.gitutil import GitError, git
from twinfix.arena.cases import Case
from twinfix.arena.hidden import CSharp, Go, Maven, Node, Pytest, normalize_line_endings
from twinfix.arena.testrunners import TestRunner, runner_for


@dataclass
class Validation:
    case_id: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(p for _, p, _ in self.checks)

    def add(self, name: str, passed: bool, detail: str = "") -> bool:
        self.checks.append((name, passed, detail))
        return passed


@dataclass
class RepeatResult:
    """同一用例重复自检 N 次的结果；状态不一致即为不稳定用例。"""

    case_id: str
    runs: list[Validation]

    @property
    def passed_runs(self) -> int:
        return sum(r.ok for r in self.runs)

    @property
    def status(self) -> str:
        passed = self.passed_runs
        if passed == len(self.runs):
            return "PASS"
        return "FLAKY" if passed else "FAIL"

    @property
    def ok(self) -> bool:
        return self.status == "PASS"

    @property
    def first_failure(self) -> Validation | None:
        return next((r for r in self.runs if not r.ok), None)


async def prepare_repo(source: Path, dest: Path) -> None:
    """把种子仓库拷成一个干净的 git 仓库（LF、单个 baseline 提交）。"""
    shutil.copytree(
        source,
        dest,
        ignore=shutil.ignore_patterns(
            "target",
            ".git",
            ".idea",
            "*.log",
            "__pycache__",
            ".pytest_cache",
            ".venv",
            ".mypy_cache",
            "bin",
            "obj",
            "TestResults",
            ".vs",
        ),
    )
    normalize_line_endings(dest)
    await git("init", "-q", "-b", "main", cwd=dest)
    await git("add", "-A", cwd=dest)
    await git("commit", "-q", "-m", "baseline", cwd=dest)


async def apply_patch(repo: Path, patch: str, message: str | None = None) -> str | None:
    """应用 patch；失败返回错误信息。message 非空时提交。"""
    try:
        await git("apply", "--whitespace=nowarn", "-", cwd=repo, input_text=patch)
    except GitError as exc:
        return str(exc)
    if message:
        await git("add", "-A", cwd=repo)
        await git("commit", "-q", "-m", message, cwd=repo)
    return None


async def validate_case(
    case: Case,
    arena_root: Path,
    maven: Maven,
    pytest: Pytest | None = None,
    go_runner: Go | None = None,
    node_runner: Node | None = None,
    csharp_runner: CSharp | None = None,
) -> Validation:
    v = Validation(case.id)
    tester = runner_for(
        case.language,
        maven=maven,
        pytest=pytest,
        go_runner=go_runner,
        node_runner=node_runner,
        csharp_runner=csharp_runner,
    )
    seed = case.seed_path(arena_root)
    if not seed.is_dir():
        v.add("seed exists", False, str(seed))
        return v
    if not case.has_patch:
        return await _validate_no_patch(case, seed, tester, v)
    if case.expect.outcome != "fixed" and not case.hidden_tests:
        return await _validate_inject_only(case, seed, tester, v)
    solution = case.directory / "solution.patch"
    hidden = case.hidden_paths()
    missing = [str(p.name) for p in [*hidden, solution] if not p.is_file()]
    if not v.add("files present", bool(hidden) and not missing, ", ".join(missing)):
        return v

    with tempfile.TemporaryDirectory(prefix=f"twinfix-validate-{case.id}-", ignore_cleanup_errors=True) as tmp:
        repo = Path(tmp) / "repo"
        await prepare_repo(seed, repo)

        err = await apply_patch(repo, case.patch_text(), case.inject.message)
        if not v.add("inject applies", err is None, err or ""):
            return v
        for svc in case.inject.services:
            r = await tester.test(repo, svc)
            v.add(f"CI green after inject ({svc})", r.passed, r.summary)

        names = tester.install(repo, case.module, hidden)
        r = await tester.test(repo, case.module, names)
        v.add("hidden tests fail on bug", not r.passed, "hidden tests passed on buggy code" if r.passed else "")

        err = await apply_patch(repo, solution.read_text(encoding="utf-8"))
        if not v.add("solution applies", err is None, err or ""):
            return v
        r = await tester.test(repo, case.module, names)
        v.add("hidden tests pass with fix", r.passed, r.summary)
        r = await tester.test(repo, case.module)
        v.add("module suite passes with fix", r.passed, r.summary)
    return v


async def _validate_no_patch(case: Case, seed: Path, tester: TestRunner, v: Validation) -> Validation:
    """无注入用例（诊断类）：只确认基线本身是健康的，结论由信号与代码决定。"""
    v.add("no hidden tests for verdict case", not case.hidden_tests and case.expect.outcome != "fixed")
    with tempfile.TemporaryDirectory(prefix=f"twinfix-validate-{case.id}-", ignore_cleanup_errors=True) as tmp:
        repo = Path(tmp) / "repo"
        await prepare_repo(seed, repo)
        for svc in case.inject.services:
            r = await tester.test(repo, svc)
            v.add(f"baseline green ({svc})", r.passed, r.summary)
    return v


async def _validate_inject_only(case: Case, seed: Path, tester: TestRunner, v: Validation) -> Validation:
    """注入了代码但期望结论不是修复（如需改受保护配置 → NEEDS_HUMAN）：只确认注入可用且 CI 仍绿。"""
    with tempfile.TemporaryDirectory(prefix=f"twinfix-validate-{case.id}-", ignore_cleanup_errors=True) as tmp:
        repo = Path(tmp) / "repo"
        await prepare_repo(seed, repo)
        err = await apply_patch(repo, case.patch_text(), case.inject.message)
        if not v.add("inject applies", err is None, err or ""):
            return v
        for svc in case.inject.services:
            r = await tester.test(repo, svc)
            v.add(f"CI green after inject ({svc})", r.passed, r.summary)
        if case.on_review and case.on_review.push_patch:
            text = (case.directory / case.on_review.push_patch).read_text(encoding="utf-8")
            err = await apply_patch(repo, text, case.on_review.push_message)
            v.add("review-time push applies", err is None, err or "")
    return v
