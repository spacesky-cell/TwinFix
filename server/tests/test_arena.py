"""基准测试评测：用例加载、判分指标、报告渲染，以及运行器的完整链路。

运行器测试用真实的本地 git（注入 / 修复分支），TwinFix API 用 httpx.MockTransport 模拟，
Maven 用假实现记录调用——不需要 JDK、Docker 或数据库。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from twinfix.arena.cases import load_case, load_cases
from twinfix.arena.grading import (
    HiddenTestResult,
    keyword_ratio,
    location_matches,
    render_report,
    report_json,
    root_cause_rank,
    score,
    summarize,
)
from twinfix.arena.hidden import (
    Go,
    Maven,
    MavenResult,
    Pytest,
    install_go_hidden_tests,
    install_hidden_tests,
    install_pytest_hidden_tests,
    summarize_go_output,
    summarize_output,
)
from twinfix.arena.runner import ArenaRunner, RunConfig, TwinFixAPI, latest_patch
from twinfix.arena.testrunners import GoRunner, MavenRunner, PytestRunner, runner_for
from twinfix.arena.validate import RepeatResult, Validation

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES = REPO_ROOT / "arena" / "cases"

CASE_YAML = """
id: X001
title: 测试用例
category: 空指针
inject: {type: patch, file: inject.patch, branch: dev, message: "feat: x", services: [app]}
signal: {title: "boom", service: app, severity: S2}
expect:
  outcome: fixed
  root_cause_locations: ["app/src/main/java/a/Calc.java#total"]
  root_cause_keywords: ["null", "空"]
hidden_tests: [hidden/CalcHiddenTest.java]
"""

INJECT = """diff --git a/app/Calc.txt b/app/Calc.txt
--- a/app/Calc.txt
+++ b/app/Calc.txt
@@ -1 +1 @@
-ok
+bug
"""

FIX = """diff --git a/app/Calc.txt b/app/Calc.txt
--- a/app/Calc.txt
+++ b/app/Calc.txt
@@ -1 +1 @@
-bug
+fixed
"""


def _make_case(root: Path) -> Path:
    d = root / "X001"
    (d / "hidden").mkdir(parents=True)
    (d / "case.yaml").write_text(CASE_YAML, encoding="utf-8")
    (d / "inject.patch").write_text(INJECT, encoding="utf-8")
    (d / "hidden" / "CalcHiddenTest.java").write_text("package a;\nclass CalcHiddenTest {}\n", encoding="utf-8")
    return d


def _detail(state: str = "AWAITING_REVIEW", **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": "inc-1",
        "state": state,
        "cost_tokens": 1200,
        "cost_amount": "0.0100",
        "created_at": "2026-01-01T00:00:00+00:00",
        "state_entered_at": "2026-01-01T00:02:00+00:00",
        "fix_branch": None,
        "diagnosis": {
            "summary": "coupon 为 null 时 NPE",
            "root_cause": "判空写在解引用之后",
            "suspect_locations": [
                {"file": "app/src/main/java/a/Other.java", "symbol": "x"},
                {"file": "src/main/java/a/Calc.java", "line": 30, "symbol": "Calc.total"},
            ],
        },
        "runs": [{"agent": "reproducer", "outcome": "reproduced"}],
        "attempts": [{"seq": 1, "patch": {"diff": FIX}, "verifications": [{"passed": True}]}],
    }
    d.update(over)
    return d


class TestCases:
    def test_repo_cases_load_and_reference_existing_files(self) -> None:
        cases = load_cases(CASES)
        assert [c.id for c in cases][:5] == ["C001", "C002", "C003", "C004", "C005"]
        assert {c.expect.outcome for c in cases} >= {"fixed", "NOT_BUG", "NEEDS_HUMAN", "ALREADY_FIXED"}
        for c in cases:
            if c.has_patch:
                assert (c.directory / c.inject.file).is_file(), c.id
            assert all(p.is_file() for p in c.hidden_paths()), c.id
            if c.expect.outcome == "fixed":
                assert (c.directory / "solution.patch").is_file(), c.id
                assert c.expect.root_cause_locations, c.id
            if c.on_review and c.on_review.push_patch:
                assert (c.directory / c.on_review.push_patch).is_file(), c.id

    def test_id_must_match_directory(self, tmp_path: Path) -> None:
        d = _make_case(tmp_path)
        wrong = tmp_path / "X002"
        d.rename(wrong)
        with pytest.raises(ValueError, match="does not match"):
            load_case(wrong)

    def test_only_filter(self, tmp_path: Path) -> None:
        _make_case(tmp_path)
        assert load_cases(tmp_path, {"NOPE"}) == []
        assert [c.id for c in load_cases(tmp_path, {"X001"})] == ["X001"]

    def test_python_cases_carry_language_seed_and_project_config(self) -> None:
        cases = load_cases(CASES, {"P001", "P002", "P003", "P004"})
        assert [c.id for c in cases] == ["P001", "P002", "P003", "P004"]
        assert {c.language for c in cases} == {"python"}
        assert {c.seed for c in cases} == {"demo-python"}
        assert cases[0].project_config["twin"]["services"] == ["orders"]
        assert cases[0].seed_path(REPO_ROOT / "arena").name == "demo-python"

    def test_go_cases_carry_language_seed_and_project_config(self) -> None:
        cases = load_cases(CASES, {"G001", "G002", "G003", "G004"})
        assert [c.id for c in cases] == ["G001", "G002", "G003", "G004"]
        assert {c.language for c in cases} == {"go"}
        assert {c.seed for c in cases} == {"demo-go"}
        assert {c.expect.outcome for c in cases} == {"fixed", "NOT_BUG", "NEEDS_HUMAN"}
        assert cases[0].project_config["twin"]["services"] == ["orders"]
        assert cases[0].seed_path(REPO_ROOT / "arena").name == "demo-go"

    def test_seed_cannot_escape_arena(self, tmp_path: Path) -> None:
        d = tmp_path / "X010"
        d.mkdir()
        (d / "case.yaml").write_text(
            "id: X010\ntitle: t\ncategory: c\nseed: ../evil\n"
            "inject: {type: none, services: [app]}\nsignal: {title: s}\nexpect: {outcome: NOT_BUG}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="seed"):
            load_case(d)

    def test_runner_selects_tester_by_language(self) -> None:
        assert isinstance(runner_for("java", maven=Maven()), MavenRunner)
        assert isinstance(runner_for("python", maven=Maven()), PytestRunner)
        assert isinstance(runner_for("go", maven=Maven()), GoRunner)


class TestGrading:
    def test_location_matching(self) -> None:
        exp = "order/src/main/java/a/Calc.java#total"
        assert location_matches(exp, {"file": "order/src/main/java/a/Calc.java", "symbol": "total"})
        assert location_matches(exp, {"file": "src/main/java/a/Calc.java", "symbol": "Calc.total"})
        assert not location_matches(exp, {"file": "src/main/java/a/Calc.java", "symbol": "other"})
        assert not location_matches(exp, {"file": "src/main/java/a/Other.java", "symbol": "total"})
        assert not location_matches(exp, {"file": ""})
        assert location_matches("a/Calc.java", {"file": "a/Calc.java"})

    def test_rank(self) -> None:
        locs = [{"file": "x/Y.java"}, {"file": "a/Calc.java", "symbol": "total"}]
        assert root_cause_rank(["a/Calc.java#total"], locs) == 2
        assert root_cause_rank(["a/Missing.java"], locs) is None

    def test_keyword_ratio(self) -> None:
        assert keyword_ratio(["null", "空", "coupon"], "Coupon is NULL") == pytest.approx(2 / 3)
        assert keyword_ratio([], "") == 1.0

    def test_successful_fix(self, tmp_path: Path) -> None:
        case = load_case(_make_case(tmp_path))
        s = score(case, _detail(), HiddenTestResult(ran=True, passed=True, regression_passed=True))
        assert s.outcome_correct and s.reproduced and s.verified and s.fix_success
        assert not s.root_cause_top1 and s.root_cause_top3
        assert s.keyword_hits == 1.0 and not s.false_fix
        assert s.duration_s == 120 and s.tokens == 1200

    def test_post_merge_states_count_as_fixed(self, tmp_path: Path) -> None:
        """合并之后（观察期 / 已关闭）也是"修复已交付"，判分不能因为它继续往后走就算错。"""
        case = load_case(_make_case(tmp_path))
        for state in ("MERGED", "OBSERVING", "CLOSED"):
            s = score(case, _detail(state), None)
            assert s.outcome_correct, state

    def test_false_fix_is_flagged(self, tmp_path: Path) -> None:
        case = load_case(_make_case(tmp_path))
        s = score(case, _detail(), HiddenTestResult(ran=True, passed=False))
        assert s.outcome_correct and s.false_fix and not s.fix_success
        assert "hidden tests failed" in s.notes

    def test_regression_failure_counts_as_false_fix(self, tmp_path: Path) -> None:
        case = load_case(_make_case(tmp_path))
        s = score(case, _detail(), HiddenTestResult(ran=True, passed=True, regression_passed=False))
        assert s.false_fix and s.hidden_passed is False

    def test_wrong_outcome(self, tmp_path: Path) -> None:
        case = load_case(_make_case(tmp_path))
        s = score(case, _detail("NEEDS_HUMAN", attempts=[], diagnosis=None, runs=[]), None)
        assert not s.outcome_correct and s.needs_human and s.hidden_passed is None and s.keyword_hits == 0

    def test_summary_and_report(self, tmp_path: Path) -> None:
        case = load_case(_make_case(tmp_path))
        good = score(case, _detail(), HiddenTestResult(ran=True, passed=True, regression_passed=True))
        bad = score(case, _detail(), HiddenTestResult(ran=True, passed=False))
        sm = summarize([good, bad])
        assert sm.total == 2 and sm.outcome_accuracy == 1.0
        assert sm.fix_rate == 0.5 and sm.false_fix_rate == 0.5 and sm.top3 == 1.0
        md = render_report("评测", [good, bad], {"运行": "r1"})
        assert "| 误修率 | 50% |" in md and "- 运行: r1" in md and md.count("| X001 |") == 2

    def test_summarize_empty(self) -> None:
        sm = summarize([])
        assert sm.total == 0 and sm.avg_duration_s is None

    def test_root_cause_not_graded_without_expected_locations(self, tmp_path: Path) -> None:
        case = load_case(_make_case(tmp_path))
        case.expect.root_cause_locations = []
        s = score(case, _detail(), None)
        assert not s.root_cause_graded and not s.root_cause_top1
        row = next(ln for ln in render_report("评测", [s], {}).splitlines() if ln.startswith("| X001 |"))
        assert row.split("|")[6].strip() == "-"


class TestRepeatValidation:
    @staticmethod
    def _run(ok: bool) -> Validation:
        v = Validation("X001")
        v.add("check", ok)
        return v

    def test_status_reflects_all_runs(self) -> None:
        assert RepeatResult("X001", [self._run(True), self._run(True)]).status == "PASS"
        assert RepeatResult("X001", [self._run(False), self._run(False)]).status == "FAIL"
        flaky = RepeatResult("X001", [self._run(True), self._run(False), self._run(True)])
        assert flaky.status == "FLAKY" and not flaky.ok and flaky.passed_runs == 2
        assert flaky.first_failure is not None and not flaky.first_failure.ok

    def test_flaky_case_is_printed_and_fails_the_command(self, capsys: pytest.CaptureFixture[str]) -> None:
        from twinfix.arena.cli import _print_result

        case = SimpleNamespace(id="X001", title="测试用例")
        result = RepeatResult("X001", [self._run(True), self._run(False)])
        _print_result(case, result, repeat=2)
        out = capsys.readouterr().out
        assert "FLAKY X001  测试用例  (1/2 runs)" in out
        assert "run 1/2: PASS" in out and "run 2/2: FAIL" in out and "[ ] check" in out


class TestHiddenHelpers:
    def test_install_by_package(self, tmp_path: Path) -> None:
        src = tmp_path / "FooHiddenTest.java"
        src.write_text("package com.acme.x;\n\nclass FooHiddenTest {}\n", encoding="utf-8")
        names = install_hidden_tests(tmp_path / "repo", "order", [src])
        assert names == ["FooHiddenTest"]
        assert (tmp_path / "repo/order/src/test/java/com/acme/x/FooHiddenTest.java").is_file()

    def test_install_requires_package(self, tmp_path: Path) -> None:
        src = tmp_path / "X.java"
        src.write_text("class X {}", encoding="utf-8")
        with pytest.raises(ValueError, match="package"):
            install_hidden_tests(tmp_path, "m", [src])

    def test_summarize_output_keeps_signal(self) -> None:
        out = "[INFO] noise\n[ERROR] Tests run: 3, Failures: 1\nexpected: 1\n but was: 2\n[ERROR] MojoFailureException"
        s = summarize_output(out)
        assert "Tests run: 3" in s and "but was" in s and "noise" not in s and "Mojo" not in s


class TestGoHelpers:
    def test_install_into_module_package(self, tmp_path: Path) -> None:
        src = tmp_path / "calc_hidden_test.go"
        src.write_text('package app\n\nimport "testing"\n\nfunc TestX(t *testing.T) {}\n', encoding="utf-8")
        names = install_go_hidden_tests(tmp_path / "repo", "app", [src])
        assert names == ["./app"]
        assert (tmp_path / "repo" / "app" / "calc_hidden_test.go").is_file()

    def test_rejects_non_test_file(self, tmp_path: Path) -> None:
        src = tmp_path / "calc.go"
        src.write_text("package app\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"_test\.go"):
            install_go_hidden_tests(tmp_path / "repo", "app", [src])

    def test_command_host_and_module_packages(self, tmp_path: Path) -> None:
        cmd = Go(executable="go").command(tmp_path, "orders", ["./orders"])
        assert Path(cmd[0]).name in ("go", "go.exe")
        assert cmd[1:3] == ["test", "-count=1"]
        assert cmd[-1] == "./orders"
        assert Go(executable="go").command(tmp_path, "orders", None)[-1] == "./..."
        selected = Go(executable="go").command(tmp_path, "orders", ["TestCreate"])
        assert selected[-3:] == ["-run", "^TestCreate$", "./orders"]

    def test_command_image_uses_docker_and_module_mount(self, tmp_path: Path) -> None:
        cmd = Go(image="twinfix-sandbox-go:1.25").command(tmp_path, "orders", ["./orders"])
        assert cmd[:3] == ["docker", "run", "--rm"]
        image_index = cmd.index("twinfix-sandbox-go:1.25")
        assert cmd[image_index + 1 : image_index + 3] == ["go", "test"]
        assert cmd[-1] == "./orders"
        assert "GOTOOLCHAIN=local" in cmd

    def test_summarize_failure_output(self) -> None:
        out = (
            "=== RUN   TestHidden\n"
            "    calc_test.go:12: got 1, want 2\n"
            "--- FAIL: TestHidden (0.00s)\n"
            "FAIL\nexit status 1\n"
        )
        summary = summarize_go_output(out)
        assert "--- FAIL: TestHidden" in summary and "calc_test.go:12" in summary


class TestPytestHelpers:
    def test_install_into_module_tests_dir(self, tmp_path: Path) -> None:
        src = tmp_path / "test_calc_hidden.py"
        src.write_text("def test_x() -> None:\n    assert True\n", encoding="utf-8")
        names = install_pytest_hidden_tests(tmp_path / "repo", "orders", [src])
        assert names == ["orders/tests/test_calc_hidden.py"]
        assert (tmp_path / "repo" / "orders" / "tests" / "test_calc_hidden.py").is_file()

    def test_command_runs_from_repo_root(self) -> None:
        cmd = Pytest(python="py").command("orders", ["orders/tests/test_x.py"])
        assert cmd[:3] == ["py", "-m", "pytest"]
        assert cmd[-1] == "orders/tests/test_x.py"
        assert Pytest(python="py").command("orders", None)[-1] == "orders"

    async def test_hidden_tests_can_run_and_fail(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / "pkg" / "tests").mkdir(parents=True)
        (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (repo / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        hidden = tmp_path / "test_calc_hidden.py"
        hidden.write_text("from pkg.calc import add\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n")
        names = install_pytest_hidden_tests(repo, "pkg", [hidden])

        result = await Pytest().test(repo, "pkg", names)
        assert result.passed, result.summary
        assert (await Pytest().test(repo, "pkg")).passed

        (repo / "pkg" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        result = await Pytest().test(repo, "pkg", names)
        assert not result.passed and "FAILED" in result.summary


def test_maven_command_container_options(tmp_path: Path) -> None:
    mvn = Maven(settings="/deploy/maven/eval-settings.xml", repo_local="/data/m2/repository")
    cmd = mvn.command(tmp_path, "order", ["CalcHiddenTest"])
    assert cmd[1:] == [
        "-B",
        "-q",
        "-s",
        "/deploy/maven/eval-settings.xml",
        "-Dmaven.repo.local=/data/m2/repository",
        "-pl",
        "order",
        "test",
        "-Dtest=CalcHiddenTest",
        "-Dsurefire.failIfNoSpecifiedTests=false",
    ]


class FakeMaven:
    def __init__(self, hidden_passes: bool) -> None:
        self.hidden_passes = hidden_passes
        self.calls: list[tuple[str, list[str] | None, str]] = []

    async def test(self, repo: Path, module: str, tests: list[str] | None = None) -> MavenResult:
        content = (repo / "app" / "Calc.txt").read_text(encoding="utf-8").strip()
        self.calls.append((module, tests, content))
        return MavenResult(passed=self.hidden_passes and content == "fixed")


class FakeTwinFix:
    """模拟 API：收到信号后，第一次查询处于 FIXING，第二次进入 AWAITING_REVIEW。"""

    def __init__(self, final: dict[str, Any]) -> None:
        self.final = final
        self.polls = 0
        self.projects: list[dict[str, Any]] = []
        self.overrides: dict[str, Any] = {}
        self.signals: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api")
        if path == "/projects" and req.method == "GET":
            return httpx.Response(200, json=self.projects)
        if path == "/projects":
            self.projects.append(json.loads(req.content))
            return httpx.Response(201, json=self.projects[-1])
        if path.endswith("/agent-config"):
            if req.method == "GET":
                return httpx.Response(200, json={"overrides": self.overrides})
            self.overrides = json.loads(req.content)
            return httpx.Response(200, json={"overrides": self.overrides})
        if path == "/signals":
            self.signals.append(json.loads(req.content))
            return httpx.Response(202, json={"incident_id": "inc-1", "signal_id": "s", "created": True})
        if path == "/incidents/inc-1":
            self.polls += 1
            return httpx.Response(200, json=self.final if self.polls > 1 else {"state": "FIXING"})
        if path == "/incidents/inc-1/cancel":
            self.cancelled.append("inc-1")
            return httpx.Response(202, json={})
        return httpx.Response(404)


def _seed(tmp_path: Path) -> Path:
    """arena 根目录：用例默认 seed=demo-shop，所以种子仓库放在 <root>/demo-shop。"""
    arena = tmp_path / "arena"
    shop = arena / "demo-shop"
    (shop / "app").mkdir(parents=True)
    (shop / "app" / "Calc.txt").write_bytes(b"ok\n")
    return arena


async def _run(
    tmp_path: Path, final: dict[str, Any], hidden_passes: bool, model: str = ""
) -> tuple[Any, FakeTwinFix, FakeMaven]:
    case = load_case(_make_case(tmp_path / "cases"))
    fake = FakeTwinFix(final)
    maven = FakeMaven(hidden_passes)
    cfg = RunConfig(arena_root=_seed(tmp_path), git_root=tmp_path / "git", run_id="t1", poll_s=0, model=model)
    async with httpx.AsyncClient(base_url="http://tf/api", transport=httpx.MockTransport(fake.handler)) as c:
        runner = ArenaRunner(TwinFixAPI(c), maven, cfg, log=lambda _: None)  # type: ignore[arg-type]
        scores = await runner.run([case])
    return scores[0], fake, maven


class TestRunner:
    async def test_full_flow_with_patch_from_detail(self, tmp_path: Path) -> None:
        s, fake, maven = await _run(tmp_path, _detail(), hidden_passes=True)
        assert s.final_state == "AWAITING_REVIEW" and s.outcome_correct and s.fix_success
        assert fake.projects[0]["repo"] == "arena/x001-t1"
        # 评测项目关掉观察期：判的是"修复合并进分支"，不该为观察再多等 30 分钟
        assert fake.projects[0]["config"] == {"observe_s": 0}
        assert fake.signals[0]["fingerprint"].startswith("arena-X001-t1-")
        assert fake.signals[0]["severity"] == "S2"
        # 隐藏测试 + 模块回归，都在补丁应用后的代码上运行
        assert maven.calls == [("app", ["CalcHiddenTest"], "fixed"), ("app", None, "fixed")]
        assert fake.cancelled == ["inc-1"]

    async def test_timeout_records_elapsed_and_hint(self, tmp_path: Path) -> None:
        case = load_case(_make_case(tmp_path / "cases"))
        fake = FakeTwinFix(_detail())

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/incidents/inc-1"):
                return httpx.Response(200, json={"state": "REPRODUCING", "cost_tokens": 123})
            return fake.handler(req)

        cfg = RunConfig(arena_root=_seed(tmp_path), git_root=tmp_path / "git", run_id="t1", poll_s=0, timeout_s=0.01)
        async with httpx.AsyncClient(base_url="http://tf/api", transport=httpx.MockTransport(handler)) as c:
            s = (await ArenaRunner(TwinFixAPI(c), FakeMaven(True), cfg, log=lambda _: None).run([case]))[0]  # type: ignore[arg-type]
        assert s.final_state == "REPRODUCING"
        # 超时也记录了真实耗时，且备注告诉人该调 --timeout
        assert s.duration_s is not None
        assert any("timeout in REPRODUCING" in n and "--timeout" in n for n in s.notes)
        assert s.tokens == 123
        assert fake.cancelled == ["inc-1"]

    async def test_injection_lands_on_dev_only(self, tmp_path: Path) -> None:
        from twinfix.adapters.gitutil import git

        await _run(tmp_path, _detail(), hidden_passes=True)
        bare = tmp_path / "git" / "arena" / "x001-t1.git"
        assert await git("show", "dev:app/Calc.txt", cwd=bare) == "bug"
        assert await git("show", "main:app/Calc.txt", cwd=bare) == "ok"

    async def test_unappliable_patch_is_a_failed_hidden_run(self, tmp_path: Path) -> None:
        bad = _detail(attempts=[{"patch": {"diff": FIX.replace("-bug", "-nope")}, "verifications": [{"passed": True}]}])
        s, _, maven = await _run(tmp_path, bad, hidden_passes=True)
        assert s.hidden_passed is False and s.false_fix and maven.calls == []

    async def test_grade_patch_uses_pushed_branch_without_diff(self, tmp_path: Path) -> None:
        """Fixer 只推了分支、结构化输出里 diff 为空时，仍应按分支判分（真实 T001 套件回归）。"""
        from twinfix.adapters.gitutil import git
        from twinfix.adapters.localgit import LocalGitCodeHost

        case = load_case(_make_case(tmp_path / "cases"))
        cfg = RunConfig(arena_root=_seed(tmp_path), git_root=tmp_path / "git", run_id="g1", poll_s=0)
        host = LocalGitCodeHost(cfg.git_root)
        seed = tmp_path / "seed"
        (seed / "app").mkdir(parents=True)
        (seed / "app" / "Calc.txt").write_text("bug\n", encoding="utf-8")
        await host.create_from_dir("arena/x001-g1", seed)
        work = tmp_path / "work"
        await git("clone", "-q", "--branch", "dev", host.url("arena/x001-g1"), str(work))
        (work / "app" / "Calc.txt").write_text("fixed\n", encoding="utf-8")
        await git("add", "-A", cwd=work)
        await git("commit", "-q", "-m", "fix", cwd=work)
        await git("push", "-q", "origin", "HEAD:fix/x001", cwd=work)

        maven = FakeMaven(hidden_passes=True)
        transport = httpx.MockTransport(lambda _req: httpx.Response(404))
        async with httpx.AsyncClient(base_url="http://tf/api", transport=transport) as c:
            runner = ArenaRunner(TwinFixAPI(c), maven, cfg, log=lambda _: None)  # type: ignore[arg-type]
            result = await runner.grade_patch(
                case, "arena/x001-g1", {"fix_branch": "fix/x001", "attempts": [{"patch": {"diff": ""}}]}
            )
        assert result is not None and result.ran and result.passed and result.regression_passed
        assert maven.calls and maven.calls[0][2] == "fixed"

    async def test_not_bug_without_patch_skips_hidden_tests(self, tmp_path: Path) -> None:
        s, fake, maven = await _run(tmp_path, _detail("NOT_BUG", attempts=[]), hidden_passes=True)
        assert s.hidden_passed is None and not s.outcome_correct and maven.calls == []
        assert fake.cancelled == []  # 终态无需取消

    async def test_model_override_is_written_to_case_project(self, tmp_path: Path) -> None:
        _, fake, _ = await _run(tmp_path, _detail(), hidden_passes=True, model="model-b")
        assert fake.overrides == {"model": "model-b"}

    async def test_no_model_override_keeps_project_defaults(self, tmp_path: Path) -> None:
        _, fake, _ = await _run(tmp_path, _detail(), hidden_passes=True)
        assert fake.overrides == {}

    def test_latest_patch_prefers_last_attempt(self) -> None:
        d = {"attempts": [{"patch": {"diff": "a"}}, {"patch": None}, {"patch": {"diff": "c"}}]}
        assert latest_patch(d) == "c"
        assert latest_patch({"attempts": []}) is None

    async def test_each_launch_gets_a_fresh_fingerprint(self, tmp_path: Path) -> None:
        """同一 run_id 重跑时，不能复用上次事故的 fingerprint：否则未终结的旧事故会吞掉新信号。"""
        _, fake1, _ = await _run(tmp_path / "a", _detail(), hidden_passes=True)
        _, fake2, _ = await _run(tmp_path / "b", _detail(), hidden_passes=True)
        assert fake1.signals[0]["fingerprint"] != fake2.signals[0]["fingerprint"]


class TestVerdictCases:
    def test_fixed_requires_patch_and_hidden_tests(self, tmp_path: Path) -> None:
        d = tmp_path / "X009"
        d.mkdir()
        (d / "case.yaml").write_text(
            "id: X009\ntitle: t\ncategory: c\ninject: {type: none, services: [app]}\n"
            "signal: {title: s}\nexpect: {outcome: fixed}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="requires a patch"):
            load_case(d)

    async def test_none_inject_leaves_dev_untouched_and_skips_grading(self, tmp_path: Path) -> None:
        from twinfix.adapters.gitutil import git

        d = tmp_path / "cases" / "X010"
        d.mkdir(parents=True)
        (d / "case.yaml").write_text(
            "id: X010\ntitle: t\ncategory: c\ninject: {type: none, services: [app]}\n"
            "signal: {title: s}\nexpect: {outcome: NOT_BUG}\n",
            encoding="utf-8",
        )
        case = load_case(d)
        fake = FakeTwinFix(_detail("NOT_BUG", attempts=[], fix_branch="fix/twinfix-x"))
        maven = FakeMaven(True)
        cfg = RunConfig(arena_root=_seed(tmp_path), git_root=tmp_path / "git", run_id="t1", poll_s=0)
        async with httpx.AsyncClient(base_url="http://tf/api", transport=httpx.MockTransport(fake.handler)) as c:
            s = (await ArenaRunner(TwinFixAPI(c), maven, cfg, log=lambda _: None).run([case]))[0]  # type: ignore[arg-type]
        assert s.outcome_correct and s.hidden_passed is None and maven.calls == []
        bare = tmp_path / "git" / "arena" / "x010-t1.git"
        assert await git("show", "dev:app/Calc.txt", cwd=bare) == "ok"

    async def test_on_review_pushes_then_approves(self, tmp_path: Path) -> None:
        from twinfix.adapters.gitutil import git

        d = _make_case(tmp_path / "cases")
        (d / "teammate.patch").write_text(FIX, encoding="utf-8")
        text = (
            (d / "case.yaml")
            .read_text(encoding="utf-8")
            .replace(
                "expect:\n  outcome: fixed",
                "on_review: {push_patch: teammate.patch}\nexpect:\n  outcome: ALREADY_FIXED",
            )
        )
        (d / "case.yaml").write_text(text.replace("hidden_tests: [hidden/CalcHiddenTest.java]\n", ""), encoding="utf-8")
        case = load_case(d)
        decided: list[str] = []
        review = _detail("AWAITING_REVIEW", approvals=[{"id": "ap1", "action": "merge", "status": "pending"}])

        def handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path.removeprefix("/api")
            if path == "/approvals/ap1/decide":
                decided.append(json.loads(req.content)["comment"])
                return httpx.Response(202, json={"status": "approved"})
            if path == "/incidents/inc-1":
                body = _detail("ALREADY_FIXED", attempts=[]) if decided else review
                return httpx.Response(200, json=body)
            return fake.handler(req)

        fake = FakeTwinFix(review)
        cfg = RunConfig(arena_root=_seed(tmp_path), git_root=tmp_path / "git", run_id="t1", poll_s=0)
        async with httpx.AsyncClient(base_url="http://tf/api", transport=httpx.MockTransport(handler)) as c:
            s = (await ArenaRunner(TwinFixAPI(c), FakeMaven(True), cfg, log=lambda _: None).run([case]))[0]  # type: ignore[arg-type]
        assert decided and s.final_state == "ALREADY_FIXED" and s.outcome_correct
        bare = tmp_path / "git" / "arena" / "x001-t1.git"
        assert await git("show", "dev:app/Calc.txt", cwd=bare) == "fixed"


def test_report_json_round_trips(tmp_path: Path) -> None:
    from twinfix.arena.grading import report_json

    case = load_case(_make_case(tmp_path))
    s = score(case, _detail(), HiddenTestResult(ran=True, passed=True, regression_passed=True))
    data = json.loads(json.dumps(report_json("t", [s], {"运行": "r"})))
    assert data["summary"]["total"] == 1 and data["summary"]["fix_rate"] == 1.0
    assert data["cases"][0]["case_id"] == "X001" and data["cases"][0]["fix_success"] is True


class TestComparison:
    def test_compares_only_common_cases(self, tmp_path: Path) -> None:
        from twinfix.arena.grading import render_comparison

        case = load_case(_make_case(tmp_path))
        good = score(case, _detail(), HiddenTestResult(ran=True, passed=True, regression_passed=True))
        bad = score(case, _detail("NEEDS_HUMAN", attempts=[], diagnosis=None, runs=[]), None)
        a = report_json("a", [good], {"Agent": "model-a"})
        b = report_json("b", [bad], {"Agent": "model-b"})
        extra = dict(a["cases"][0], case_id="X999")
        b["cases"].append(extra)  # 只在 b 中出现的用例不参与对比
        text = render_comparison([a, b])
        assert "共同用例 1 个：X001" in text and "X999" not in text
        assert "| 结论正确率 | 100% | 0% |" in text
        assert "| X001 | ✅ AWAITING_REVIEW | ❌ NEEDS_HUMAN |" in text


class TestFastSuite:
    def test_suite_cases_exist_and_are_stable_choices(self) -> None:
        from twinfix.arena.cli import SUITES

        fast = load_cases(CASES, set(SUITES["fast"]))
        core = load_cases(CASES, set(SUITES["core"]))
        assert [c.id for c in fast] == ["C001", "C002", "C011", "C014", "C018", "C024"]
        assert set(SUITES["fast"]) < set(SUITES["core"])
        assert [c.id for c in core] == ["C001", "C002", "C009", "C011", "C012", "C014", "C018", "C024"]

    def test_gate_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from twinfix.arena import cli

        report = tmp_path / "report.json"
        report.write_text(
            json.dumps(
                {
                    "summary": {
                        "total": 6,
                        "outcome_accuracy": 0.8333,
                        "fix_rate": 0.5,
                        "false_fix_rate": 0.0,
                        "needs_human_rate": 0.1667,
                        "avg_duration_s": 2800,
                    }
                }
            ),
            encoding="utf-8",
        )
        assert cli.main(["gate", str(report)]) == 0
        assert "gate: PASS" in capsys.readouterr().out

    def test_gate_fails_on_regression(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from twinfix.arena import cli

        report = tmp_path / "report.json"
        report.write_text(
            json.dumps(
                {
                    "summary": {
                        "total": 6,
                        "outcome_accuracy": 0.5,
                        "fix_rate": 0.33,
                        "false_fix_rate": 0.1667,
                        "needs_human_rate": 0.5,
                        "avg_duration_s": 3600,
                    }
                }
            ),
            encoding="utf-8",
        )
        assert cli.main(["gate", str(report), "--max-avg-duration", "3000", "--min-fix-rate", "0.5"]) == 1
        out = capsys.readouterr().out
        assert out.count("[FAIL]") == 4 and "gate: FAIL" in out

    def test_suite_cannot_be_combined_with_explicit_cases(self) -> None:
        from twinfix.arena import cli

        with pytest.raises(SystemExit):
            cli.main(["eval", "--suite", "fast", "C001"])
