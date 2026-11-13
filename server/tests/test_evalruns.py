"""控制台发起的评测任务：子进程路径、状态落盘、互斥与前提检查。

runner 注入成内存实现，不真的跑 Maven / LLM；子进程参数在这里核对。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from twinfix.arena.evalruns import EvalRun, EvalRunManager
from twinfix.config import Settings
from twinfix.services import ConflictError


@pytest.fixture(autouse=True)
def _maven_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """precheck 会查 mvn；单测不依赖评测机装没装 Maven。"""
    monkeypatch.setattr("twinfix.arena.evalruns.shutil.which", lambda _name: "/usr/bin/mvn")


def _settings(tmp_path: Path, **update: object) -> Settings:
    cases = tmp_path / "arena" / "cases"
    (cases / "c001").mkdir(parents=True, exist_ok=True)
    (cases / "c001" / "case.yaml").write_text("id: c001\n", encoding="utf-8")
    (tmp_path / "arena" / "demo-shop").mkdir(exist_ok=True)
    (tmp_path / "reports").mkdir(exist_ok=True)
    base: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "eval_reports_dir": tmp_path / "reports",
        "arena_cases_dir": cases,
        "agent_mode": "llm",
        "codehost": "local",
    }
    return Settings(**{**base, **update})  # type: ignore[arg-type]


def _add_python_case(tmp_path: Path) -> None:
    d = tmp_path / "arena" / "cases" / "p001"
    d.mkdir(parents=True)
    (d / "case.yaml").write_text("id: p001\nlanguage: python\nseed: demo-python\n", encoding="utf-8")
    (tmp_path / "arena" / "demo-python").mkdir(exist_ok=True)


def _add_go_case(tmp_path: Path) -> None:
    d = tmp_path / "arena" / "cases" / "g001"
    d.mkdir(parents=True)
    (d / "case.yaml").write_text("id: g001\nlanguage: go\nseed: demo-go\n", encoding="utf-8")
    (tmp_path / "arena" / "demo-go").mkdir(exist_ok=True)


def _runner(settings: Settings, *, rc: int = 0, write_report: bool = True, calls: list | None = None):
    async def run(run: EvalRun, cmd: list[str], log_path: Path) -> int:
        if calls is not None:
            calls.append((run, cmd, log_path))
        log_path.write_text("fake eval log\n", encoding="utf-8")  # noqa: ASYNC240
        if write_report:
            (settings.eval_reports_dir / f"{run.id}.md").write_text("# report\n", encoding="utf-8")
        return rc

    return run


async def test_success_persists_report_and_command(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    calls: list = []
    manager = EvalRunManager(settings, runner=_runner(settings, calls=calls))
    run = await manager.start(cases=["c001"], label="smoke", model="m1", effort="high", timeout_s=120)
    assert run.status in ("queued", "running")
    await manager.wait()

    done = manager.get(run.id)
    assert done.status == "succeeded" and done.returncode == 0 and done.error is None
    assert done.report == run.id
    _, cmd, log_path = calls[0]
    assert cmd[1:4] == ["-m", "twinfix.arena.cli", "eval"]
    assert "c001" in cmd and "--run-id" in cmd and cmd[cmd.index("--run-id") + 1] == run.id
    assert cmd[cmd.index("--model") + 1] == "m1" and cmd[cmd.index("--effort") + 1] == "high"
    assert cmd[cmd.index("--timeout") + 1] == "120.0"
    assert log_path.name == f"{run.id}.log" and log_path.is_file()

    # 状态落盘：换一个 manager（模拟 API 重启）还能读回来
    saved = json.loads((manager.dir / f"{run.id}.json").read_text(encoding="utf-8"))
    assert saved["status"] == "succeeded" and saved["report"] == run.id
    assert EvalRunManager(settings).get(run.id).status == "succeeded"


@pytest.mark.parametrize(
    ("rc", "write_report", "hint"),
    [(0, False, "没有生成报告"), (2, True, "退出码 2")],
)
async def test_failures(tmp_path: Path, rc: int, write_report: bool, hint: str) -> None:
    settings = _settings(tmp_path)
    manager = EvalRunManager(settings, runner=_runner(settings, rc=rc, write_report=write_report))
    run = await manager.start(cases=["c001"])
    await manager.wait()
    done = manager.get(run.id)
    assert done.status == "failed" and done.error is not None and hint in done.error


async def test_only_one_run_at_a_time(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    gate = asyncio.Event()

    async def slow(run: EvalRun, cmd: list[str], log_path: Path) -> int:
        await gate.wait()
        return 0

    manager = EvalRunManager(settings, runner=slow)
    first = await manager.start(cases=["c001"])
    with pytest.raises(ConflictError):
        await manager.start(cases=["c001"])
    await asyncio.sleep(0)  # 让 _execute 把状态推到 running
    assert manager.get(first.id).status == "running"
    gate.set()
    await manager.wait()
    assert manager.get(first.id).status == "failed"  # 没写报告 → 失败而不是 succeeded
    # 上一个结束后可以再发起
    await manager.start(cases=["c001"])
    await manager.wait()


async def test_stop_marks_interrupted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    gate = asyncio.Event()

    async def slow(run: EvalRun, cmd: list[str], log_path: Path) -> int:
        await gate.wait()
        return 0

    manager = EvalRunManager(settings, runner=slow)
    run = await manager.start(cases=["c001"])
    await asyncio.sleep(0)  # 让 _execute 跑起来
    await manager.stop()
    assert manager.get(run.id).status == "interrupted"


async def test_stale_running_marked_interrupted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = EvalRunManager(settings, runner=_runner(settings))
    stale = EvalRun(id="2026-01-01-010101-abcd", status="running")
    (manager.dir / f"{stale.id}.json").write_text(stale.model_dump_json(), encoding="utf-8")
    loaded = EvalRunManager(settings).get(stale.id)
    assert loaded.status == "interrupted" and loaded.error


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"agent_mode": "stub"}, "AGENT_MODE"),
        ({"codehost": "fake"}, "CODEHOST"),
    ],
)
async def test_preconditions(tmp_path: Path, update: dict, message: str) -> None:
    settings = _settings(tmp_path, **update)
    manager = EvalRunManager(settings, runner=_runner(settings))
    with pytest.raises(ConflictError, match=message):
        await manager.start(cases=["c001"])


async def test_missing_demo_shop_and_unknown_case(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "arena" / "demo-shop").rmdir()
    manager = EvalRunManager(settings, runner=_runner(settings))
    with pytest.raises(ConflictError, match="demo"):
        await manager.start()

    settings2 = _settings(tmp_path)  # demo-shop 由 _settings 重新建出来
    with pytest.raises(ConflictError, match="未知用例"):
        await EvalRunManager(settings2, runner=_runner(settings2)).start(cases=["c999"])


async def test_maven_missing_fails_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("twinfix.arena.evalruns.shutil.which", lambda _name: None)
    settings = _settings(tmp_path)
    manager = EvalRunManager(settings, runner=_runner(settings))
    with pytest.raises(ConflictError, match="Maven"):
        await manager.start(cases=["c001"])


async def test_python_case_does_not_require_maven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_python_case(tmp_path)
    monkeypatch.setattr("twinfix.arena.evalruns.shutil.which", lambda _name: None)
    settings = _settings(tmp_path)
    manager = EvalRunManager(settings, runner=_runner(settings))
    await manager.start(cases=["p001"])
    await manager.wait()
    assert manager.list_runs()[0].status == "succeeded"


async def test_python_case_requires_pytest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_python_case(tmp_path)
    monkeypatch.setattr("twinfix.arena.evalruns.importlib.util.find_spec", lambda _name: None)
    settings = _settings(tmp_path)
    with pytest.raises(ConflictError, match="pytest"):
        await EvalRunManager(settings, runner=_runner(settings)).start(cases=["p001"])


async def test_go_case_requires_go_or_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_go_case(tmp_path)

    def which(name: str) -> str | None:
        return "/usr/bin/mvn" if name in ("mvn", "mvn.cmd") else None

    monkeypatch.setattr("twinfix.arena.evalruns.shutil.which", which)
    settings = _settings(tmp_path)
    with pytest.raises(ConflictError, match="Go"):
        await EvalRunManager(settings, runner=_runner(settings)).start(cases=["g001"])


async def test_go_case_uses_configured_image_for_grading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_go_case(tmp_path)

    def which(name: str) -> str | None:
        return "/usr/bin/mvn" if name in ("mvn", "mvn.cmd") else None

    monkeypatch.setattr("twinfix.arena.evalruns.shutil.which", which)
    settings = _settings(tmp_path, eval_go_image="twinfix-sandbox-go:1.25")
    calls: list = []
    manager = EvalRunManager(settings, runner=_runner(settings, calls=calls))
    run = await manager.start(cases=["g001"])
    await manager.wait()
    assert manager.get(run.id).status == "succeeded"
    cmd = calls[0][1]
    assert cmd[cmd.index("--go-image") + 1] == "twinfix-sandbox-go:1.25"


async def test_maven_settings_and_repo_are_passed_before_subcommand(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        eval_maven_settings=Path("/deploy/maven/eval-settings.xml"),
        eval_maven_repo=Path("/data/m2/repository"),
    )
    calls: list = []
    manager = EvalRunManager(settings, runner=_runner(settings, calls=calls))
    await manager.start(cases=["c001"])
    await manager.wait()

    _, cmd, _ = calls[0]
    assert cmd[3:7] == [
        "--maven-settings",
        str(Path("/deploy/maven/eval-settings.xml")),
        "--maven-repo",
        str(Path("/data/m2/repository")),
    ]
    assert cmd[7] == "eval"  # 全局参数必须在子命令前
