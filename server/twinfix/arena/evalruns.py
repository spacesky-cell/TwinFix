"""控制台发起的评测任务：管理工作目录、子进程和状态文件（POST/GET /api/eval/runs）。

评测是"独立进程"而不是塞进 API 事件循环：一个 24 例全量要跑数小时，API 重启/热更新不能带走它，
Maven 判分也不该和 HTTP 服务抢线程。子进程通过 ``TWINFIX_API_SELF_URL`` 回连 API，与手工执行
``twinfix-arena eval`` 完全同一条路径；日志与状态落在 ``data_dir/eval_runs/``，重启后还能看到。

前提（不满足直接 409，避免跑出一堆必然 ERROR 的用例）：
- ``TWINFIX_AGENT_MODE=llm``：模拟 Agent（stub）不会真的修 bug；
- ``TWINFIX_CODEHOST=local``：评测机和 TwinFix 必须共享 ``TWINFIX_GIT_ROOT``；
- ``arena/demo-shop`` 存在（可用 ``TWINFIX_ARENA_DEMO_SHOP_DIR`` 指定）。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import os
import secrets
import shutil
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from twinfix.config import Settings
from twinfix.services import ConflictError

log = logging.getLogger("twinfix.evalruns")

# 子进程的 cwd：仓库根，`python -m twinfix.arena.cli` 才能按包导入
_REPO_ROOT = Path(__file__).resolve().parents[3]

EvalStatus = Literal["queued", "running", "succeeded", "failed", "interrupted"]
_ACTIVE = ("queued", "running")
_REPORT_SUFFIX = ".md"

# runner 注入点：测试里替换成内存实现，生产走子进程
Runner = Callable[["EvalRun", list[str], Path], Awaitable[int]]


class EvalRun(BaseModel):
    id: str
    status: EvalStatus = "queued"
    label: str = ""
    model: str = ""
    effort: str = ""
    cases: list[str] = Field(default_factory=list)
    timeout_s: float = 3600
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    returncode: int | None = None
    error: str | None = None
    report: str | None = None  # eval/reports 下的报告名（不含扩展名）
    log_file: str = ""


class EvalRunManager:
    def __init__(self, settings: Settings, *, runner: Runner | None = None) -> None:
        self.settings = settings
        self.dir = settings.data_dir / "eval_runs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, EvalRun] = {}
        self._task: asyncio.Task[None] | None = None
        self._runner = runner or self._spawn
        self._load()

    # ---------------------------------------------------------------- 查询

    # 不叫 list：类体内会把内建 list 遮住，注解里的 list[str] 全部失效
    def list_runs(self) -> list[EvalRun]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)

    def get(self, run_id: str) -> EvalRun:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    # ---------------------------------------------------------------- 启动

    async def start(
        self,
        *,
        cases: list[str] | None = None,
        label: str = "",
        model: str = "",
        effort: str = "",
        timeout_s: float = 3600,
        token: str = "",
    ) -> EvalRun:
        self._precheck(cases)
        if any(r.status in _ACTIVE for r in self._runs.values()):
            raise ConflictError("已有评测在运行；等它结束或先停掉再发起")
        now = datetime.now(UTC)
        run = EvalRun(
            id=f"{now:%Y-%m-%d}-{now:%H%M%S}-{secrets.token_hex(2)}",
            label=label,
            model=model,
            effort=effort,
            cases=list(cases or []),
            timeout_s=timeout_s,
            log_file="",
        )
        run.log_file = f"{run.id}.log"
        self._runs[run.id] = run
        self._save(run)
        self._task = asyncio.create_task(self._execute(run, token))
        return run

    def _precheck(self, cases: list[str] | None) -> None:
        if self.settings.agent_mode != "llm":
            raise ConflictError("评测需要 TWINFIX_AGENT_MODE=llm（模拟 Agent（stub）不会真的修复缺陷）")
        if self.settings.codehost != "local":
            raise ConflictError("评测需要 TWINFIX_CODEHOST=local，且与 TwinFix 共享 TWINFIX_GIT_ROOT")
        specs = self._case_specs(cases)
        # 判分要跑 Maven / pytest / go / node / dotnet；缺工具时每个用例都会在跑完全流程后才 ERROR（白烧数小时）
        if any(lang == "java" for _, lang, _ in specs) and (
            shutil.which("mvn") is None and shutil.which("mvn.cmd") is None
        ):
            raise ConflictError("评测判分需要 Maven（mvn 不在 PATH）；容器镜像应自带 JDK 21 + Maven")
        if any(lang == "python" for _, lang, _ in specs) and importlib.util.find_spec("pytest") is None:
            raise ConflictError("Python 用例判分需要 pytest；容器镜像应通过 `uv sync --extra eval` 安装")
        if any(lang == "go" for _, lang, _ in specs) and not self.settings.eval_go_image and shutil.which("go") is None:
            raise ConflictError(
                "Go 用例判分需要 Go 工具链（go 不在 PATH）；可设置 TWINFIX_EVAL_GO_IMAGE 用 docker 判分"
            )
        if (
            any(lang == "typescript" for _, lang, _ in specs)
            and not self.settings.eval_node_image
            and shutil.which("node") is None
        ):
            raise ConflictError(
                "TypeScript 用例判分需要 Node 22+（node 不在 PATH）；可设置 TWINFIX_EVAL_NODE_IMAGE 用 docker 判分"
            )
        if (
            any(lang == "csharp" for _, lang, _ in specs)
            and not self.settings.eval_csharp_image
            and shutil.which("dotnet") is None
        ):
            raise ConflictError(
                "C# 用例判分需要 .NET SDK（dotnet 不在 PATH）；可设置 TWINFIX_EVAL_CSHARP_IMAGE 用 docker 判分"
            )
        for case_id, _, seed in specs:
            demo = self.settings.demo_shop_dir
            if seed != "demo-shop":
                demo = self.settings.arena_cases_dir.parent / seed
            if not demo.is_dir():
                raise ConflictError(
                    f"找不到示例服务源码目录 {demo}（用例 {case_id}）；可用 TWINFIX_ARENA_DEMO_SHOP_DIR 指定"
                )

    def _case_specs(self, cases: list[str] | None) -> list[tuple[str, str, str]]:
        """(用例 id, 语言, 种子仓库)。只读 yaml 的轻量字段，完整校验留给评测子进程。"""
        selected = cases or sorted(
            d.name for d in self.settings.arena_cases_dir.iterdir() if (d / "case.yaml").is_file()
        )
        missing = [c for c in selected if not (self.settings.arena_cases_dir / c / "case.yaml").is_file()]
        if missing:
            raise ConflictError(f"未知用例：{', '.join(missing)}")
        specs = []
        for case_id in selected:
            path = self.settings.arena_cases_dir / case_id / "case.yaml"
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                raw = {}
            specs.append((case_id, str(raw.get("language") or "java").lower(), str(raw.get("seed") or "demo-shop")))
        return specs

    async def _execute(self, run: EvalRun, token: str) -> None:
        run.status = "running"
        run.started_at = datetime.now(UTC)
        self._save(run)
        log_path = self.dir / run.log_file
        try:
            cmd = self._command(run, token)
            rc = await self._runner(run, cmd, log_path)
        except asyncio.CancelledError:
            self._finish(run, "interrupted", error="API 关闭，评测进程状态未知")
            raise
        except Exception as exc:  # 启动失败（找不到 python / 权限）也要落终态
            log.exception("eval run %s failed to start", run.id)
            self._finish(run, "failed", error=str(exc)[:500])
            return
        report = self.settings.eval_reports_dir / f"{run.id}{_REPORT_SUFFIX}"
        error = None
        if rc != 0:
            error = f"评测进程退出码 {rc}；详见日志 {run.log_file}"
        elif not report.is_file():
            error = f"评测进程成功退出但没有生成报告 {report.name}"
        if report.is_file():
            run.report = run.id
        self._finish(run, "succeeded" if rc == 0 and report.is_file() else "failed", rc=rc, error=error)

    def _finish(self, run: EvalRun, status: EvalStatus, *, rc: int | None = None, error: str | None = None) -> None:
        run.status = status
        run.finished_at = datetime.now(UTC)
        if rc is not None:
            run.returncode = rc
        if error:
            run.error = error
        self._save(run)

    def _command(self, run: EvalRun, token: str) -> list[str]:
        report = self.settings.eval_reports_dir / f"{run.id}{_REPORT_SUFFIX}"
        cmd = [
            sys.executable,
            "-m",
            "twinfix.arena.cli",
        ]
        # 全局 Maven 参数必须放在子命令之前，argparse 才会交给主解析器
        if self.settings.eval_maven_settings:
            cmd += ["--maven-settings", str(self.settings.eval_maven_settings)]
        if self.settings.eval_maven_repo:
            cmd += ["--maven-repo", str(self.settings.eval_maven_repo)]
        if self.settings.eval_go_image:
            cmd += ["--go-image", self.settings.eval_go_image]
        if self.settings.eval_node_image:
            cmd += ["--node-image", self.settings.eval_node_image]
        if self.settings.eval_csharp_image:
            cmd += ["--csharp-image", self.settings.eval_csharp_image]
        cmd += [
            "eval",
            *run.cases,
            "--api",
            self.settings.api_self_url,
            "--git-root",
            str(self.settings.git_root),
            "--run-id",
            run.id,
            "--timeout",
            str(run.timeout_s),
            "--out",
            str(report),
        ]
        if run.label:
            cmd += ["--label", run.label]
        if run.model:
            cmd += ["--model", run.model]
        if run.effort:
            cmd += ["--effort", run.effort]
        if token:
            cmd += ["--token", token]
        return cmd

    async def _spawn(self, run: EvalRun, cmd: list[str], log_path: Path) -> int:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "TWINFIX_GIT_ROOT": str(self.settings.git_root)}
        with log_path.open("w", encoding="utf-8") as out:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=out,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=str(_REPO_ROOT),
            )
            return await proc.wait()

    # ---------------------------------------------------------------- 生命周期 / 持久化

    async def stop(self) -> None:
        """API 关闭：标记未完成的任务（子进程继续跑也无妨，报告仍会落盘）。"""
        for run in self._runs.values():
            if run.status in _ACTIVE:
                self._finish(run, "interrupted", error="API 关闭，评测进程状态未知")
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def wait(self) -> None:
        """测试/运维用：等当前任务结束。"""
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def _path(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.json"

    def _save(self, run: EvalRun) -> None:
        path = self._path(run.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(run.model_dump_json(indent=1), encoding="utf-8")
        tmp.replace(path)  # 原子替换：命令行/控制台同时读也不会看到半个文件

    def _load(self) -> None:
        for path in sorted(self.dir.glob("*.json")):
            try:
                run = EvalRun.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if run.status in _ACTIVE:
                # 上次 API 退出时没来得及收尾：子进程可能还在跑，状态只能标成未知
                run.status = "interrupted"
                run.error = run.error or "API 重启前的任务状态未知"
                self._save(run)
            self._runs[run.id] = run
