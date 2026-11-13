"""评测运行器：逐个用例 注入 → 上报信号 → 等事故收敛 → 隐藏测试判分 → 清理。

每个用例使用独立的项目与本地裸仓库（arena/<case>-<run>），互不污染，可重复运行。
运行器通过 HTTP 驱动 TwinFix（与真实使用路径一致），通过共享的 git_root 注入故障并读取修复分支，
因此要求 TwinFix 以 codehost=local 运行且与运行器看到同一个 git_root。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from twinfix.adapters.gitutil import GitError, git
from twinfix.adapters.localgit import LocalGitCodeHost
from twinfix.arena.cases import Case
from twinfix.arena.grading import CaseScore, HiddenTestResult, score
from twinfix.arena.hidden import CSharp, Go, Maven, Node, Pytest
from twinfix.arena.inject import create_repo_from_case
from twinfix.arena.testrunners import TestRunner, runner_for
from twinfix.arena.validate import apply_patch

# 运行器停下来判分的状态：交付评审、需要人、或终态
SETTLED = {
    "AWAITING_REVIEW",
    "MERGED",
    # 观察期也是"修复已交付"的收敛点：评测不该为它多等 30 分钟
    "OBSERVING",
    "NEEDS_HUMAN",
    "NOT_BUG",
    "CANNOT_REPRODUCE",
    "CANNOT_FIX",
    "ALREADY_FIXED",
    "CANCELLED",
    "CLOSED",
}
_CANCELLABLE = {"AWAITING_REVIEW", "NEEDS_HUMAN", "OBSERVING"}

Log = Callable[[str], None]


@dataclass(frozen=True)
class RunConfig:
    # arena/ 目录：各用例按 case.seed 从这里取基线仓库（demo-shop / demo-python ...）
    arena_root: Path
    git_root: Path
    run_id: str
    timeout_s: float = 3600
    poll_s: float = 2.0
    cleanup: bool = True
    # 本次评测的模型 / 推理强度覆盖（写到每个用例项目的 agent-config）；空 = 用全局默认
    model: str = ""
    effort: str = ""


class TwinFixAPI:
    """评测用到的那一小部分 HTTP API。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.c = client

    async def ensure_project(self, name: str, repo: str, case: Case) -> None:
        projects = (await self.c.get("/projects")).raise_for_status().json()
        if not any(p["name"] == name for p in projects):
            body = {
                "name": name,
                "repo": repo,
                "language": case.language,
                "branch_map": {"test": "dev", "release": "release", "prod": "main"},
                # 评测判的是"修复合并进分支"，不需要合并后再盯 30 分钟观察期：
                # 观察期会把每例多拖 30 分钟，还会白白占住孪生名额
                "config": {"observe_s": 0, **case.project_config},
            }
            (await self.c.post("/projects", json=body)).raise_for_status()

    async def set_agent_config(self, name: str, *, model: str = "", effort: str = "") -> None:
        """把本次评测的模型 / 推理强度写进项目 agent-config。

        每次评测都按 (用例, run_id) 新建项目，覆盖不会串到别的运行；用 API 设置而不是改
        .env 重启服务，同一台机器上就能跑模型对比。
        """
        current = (await self.c.get(f"/projects/{name}/agent-config")).raise_for_status().json()["overrides"]
        changes = {k: v for k, v in {"model": model, "reasoning_effort": effort}.items() if v}
        (await self.c.put(f"/projects/{name}/agent-config", json={**current, **changes})).raise_for_status()

    async def signal(self, body: dict[str, Any]) -> str:
        r = (await self.c.post("/signals", json=body)).raise_for_status()
        return str(r.json()["incident_id"])

    async def detail(self, incident_id: str) -> dict[str, Any]:
        data: dict[str, Any] = (await self.c.get(f"/incidents/{incident_id}")).raise_for_status().json()
        return data

    async def approve_merge(self, incident_id: str, comment: str, wait_s: float = 60) -> bool:
        # 进入 AWAITING_REVIEW 后，审批在下一步才创建，稍等片刻
        deadline = time.monotonic() + wait_s
        while True:
            detail = await self.detail(incident_id)
            approvals = detail.get("approvals") or []
            pending = [a for a in approvals if a["action"] == "merge" and a["status"] == "pending"]
            if pending or time.monotonic() > deadline:
                break
            await asyncio.sleep(1)
        if not pending:
            return False
        body = {"approve": True, "comment": comment}
        (await self.c.post(f"/approvals/{pending[0]['id']}/decide", json=body)).raise_for_status()
        return True

    async def cancel(self, incident_id: str, reason: str) -> None:
        await self.c.post(f"/incidents/{incident_id}/cancel", json={"reason": reason})


async def wait_settled(
    api: TwinFixAPI,
    incident_id: str,
    timeout_s: float,
    poll_s: float,
    sleep: Callable[[float], Awaitable[None]],
    settled: frozenset[str] | set[str] = SETTLED,
    *,
    log: Log | None = None,
    label: str = "",
    progress_s: float = 60.0,
) -> dict[str, Any]:
    """轮询到事故收敛。长跑用例每 progress_s 打一行进度，避免日志里几十分钟没有任何输出。"""
    started = time.monotonic()
    deadline = started + timeout_s
    detail = await api.detail(incident_id)
    last_report = started
    while detail["state"] not in settled and time.monotonic() < deadline:
        await sleep(poll_s)
        detail = await api.detail(incident_id)
        now = time.monotonic()
        if log is not None and now - last_report >= progress_s:
            last_report = now
            tokens = detail.get("cost_tokens", 0)
            log(f"  [{label or incident_id}] {detail['state']} {now - started:.0f}s tokens={tokens}")
    return detail


def latest_patch(detail: dict[str, Any]) -> str | None:
    for attempt in reversed(detail.get("attempts") or []):
        patch = attempt.get("patch") or {}
        if patch.get("diff"):
            return str(patch["diff"])
    return None


class ArenaRunner:
    def __init__(
        self,
        api: TwinFixAPI,
        maven: Maven,
        cfg: RunConfig,
        log: Log = print,
        pytest: Pytest | None = None,
        go_runner: Go | None = None,
        node_runner: Node | None = None,
        csharp_runner: CSharp | None = None,
    ) -> None:
        self.api = api
        self.maven = maven
        self.pytest = pytest or Pytest()
        self.go_runner = go_runner or Go()
        self.node_runner = node_runner or Node()
        self.csharp_runner = csharp_runner or CSharp()
        self.cfg = cfg
        self.host = LocalGitCodeHost(cfg.git_root)
        self.log = log
        self._testers: dict[str, TestRunner] = {}
        # 每次启动一个独立的 fingerprint 前缀：同一 run_id 重跑（上一次中断/报错后接着跑）时，
        # 上次留下的未终结事故会抢走这次的同 fingerprint 信号，用例开局就落到旧事故上
        self.attempt = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")

    def tester(self, case: Case) -> TestRunner:
        if case.language not in self._testers:
            self._testers[case.language] = runner_for(
                case.language,
                maven=self.maven,
                pytest=self.pytest,
                go_runner=self.go_runner,
                node_runner=self.node_runner,
                csharp_runner=self.csharp_runner,
            )
        return self._testers[case.language]

    def repo_name(self, case: Case) -> str:
        return f"arena/{case.id.lower()}-{self.cfg.run_id}"

    def fingerprint(self, case: Case) -> str:
        return f"arena-{case.id}-{self.cfg.run_id}-{self.attempt}"

    async def run(self, cases: list[Case]) -> list[CaseScore]:
        scores = []
        for case in cases:
            t0 = time.monotonic()
            try:
                s = await self.run_case(case)
            except (httpx.HTTPError, GitError, OSError) as exc:
                s = CaseScore(case.id, case.category, case.expect.outcome, "ERROR", False, error=str(exc)[:300])
            self.log(f"{case.id} {s.final_state:<16} correct={s.outcome_correct} ({time.monotonic() - t0:.0f}s)")
            sys.stdout.flush()  # 评测常跑数十分钟、输出重定向到文件：逐个用例落盘便于观察进度
            scores.append(s)
        return scores

    async def inject(self, case: Case) -> str:
        repo = self.repo_name(case)
        await create_repo_from_case(self.host, case, case.seed_path(self.cfg.arena_root), repo)
        return repo

    async def run_case(self, case: Case) -> CaseScore:
        repo = await self.inject(case)
        project = f"arena-{case.id.lower()}-{self.cfg.run_id}"
        await self.api.ensure_project(project, repo, case)
        if self.cfg.model or self.cfg.effort:
            await self.api.set_agent_config(project, model=self.cfg.model, effort=self.cfg.effort)
        sig = case.signal
        incident_id = await self.api.signal(
            {
                "project": project,
                "source": "alert",
                "title": sig.title,
                "service": sig.service,
                "env": sig.env.value,
                "severity": sig.severity.value if sig.severity else None,
                "stacktrace": sig.stacktrace,
                "message": sig.message,
                "fingerprint": self.fingerprint(case),
            }
        )
        started = time.monotonic()
        deadline = started + self.cfg.timeout_s
        detail = await wait_settled(
            self.api, incident_id, self.cfg.timeout_s, self.cfg.poll_s, asyncio.sleep, log=self.log, label=case.id
        )
        if case.on_review is not None and detail["state"] == "AWAITING_REVIEW":
            detail = await self._play_review(case, repo, incident_id, deadline - time.monotonic())
        waited = time.monotonic() - started  # 判分（Maven）耗时不算进事故等待时间
        hidden = await self.grade_patch(case, repo, detail)
        result = score(case, detail, hidden)
        if detail["state"] not in SETTLED:
            elapsed = waited
            result.duration_s = elapsed  # 超时也是真实成本，别在耗时列显示 "-"
            result.notes.append(
                f"timeout in {detail['state']} after {elapsed:.0f}s (raise --timeout; high reasoning needs more)"
            )
        if self.cfg.cleanup and (detail["state"] in _CANCELLABLE or detail["state"] not in SETTLED):
            await self.api.cancel(incident_id, "arena run finished")
        return result

    async def _play_review(self, case: Case, repo: str, incident_id: str, remaining_s: float) -> dict[str, Any]:
        script = case.on_review
        assert script is not None
        if script.push_patch:
            patch = (case.directory / script.push_patch).read_text(encoding="utf-8")
            await self.host.commit_patch(repo, case.inject.branch, patch, script.push_message)
        if script.approve:
            await self.api.approve_merge(incident_id, "arena: approve after review script")
        # 批准后事故会离开 AWAITING_REVIEW（进入 MERGING → 合并 / ALREADY_FIXED / 人工）
        return await wait_settled(
            self.api,
            incident_id,
            max(remaining_s, 60),
            self.cfg.poll_s,
            asyncio.sleep,
            SETTLED - {"AWAITING_REVIEW"},
            log=self.log,
            label=case.id,
        )

    async def grade_patch(self, case: Case, repo: str, detail: dict[str, Any]) -> HiddenTestResult | None:
        """优先用推送的修复分支；没有分支时把最后一次补丁应用到注入后的分支上。

        Fixer 可能只推分支、没在结构化输出里带 diff（diff 字段为空），此时仍应按分支判分。
        """
        branch = detail.get("fix_branch")
        diff = latest_patch(detail)
        if not case.hidden_tests or (not diff and not branch):
            return None
        with tempfile.TemporaryDirectory(prefix=f"twinfix-arena-grade-{case.id}-", ignore_cleanup_errors=True) as tmp:
            work = Path(tmp) / "w"
            await git("clone", "-q", "--branch", case.inject.branch, self.host.url(repo), str(work))
            if branch and await _has_remote_branch(work, branch):
                await git("checkout", "-q", "-B", "graded", f"origin/{branch}", cwd=work)
            elif diff and (err := await apply_patch(work, diff)):
                return HiddenTestResult(ran=True, passed=False, output=f"patch does not apply: {err[:300]}")
            elif not diff:
                # fix 分支在 DIAGNOSING 就会分配，不代表真的推过；没有可判的修复
                return None
            tester = self.tester(case)
            names = tester.install(work, case.module, case.hidden_paths())
            hidden = await tester.test(work, case.module, names)
            regression = await tester.test(work, case.module) if hidden.passed else None
            return HiddenTestResult(
                ran=True,
                passed=hidden.passed,
                regression_passed=bool(regression and regression.passed),
                output=hidden.summary if not hidden.passed else (regression.summary if regression else ""),
            )


async def _has_remote_branch(work: Path, branch: str) -> bool:
    out = await git("branch", "-r", "--list", f"origin/{branch}", cwd=work)
    return bool(out.strip())
