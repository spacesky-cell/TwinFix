"""代码托管适配器。真实实现包括 GitHubAppCodeHost、GitLabCodeHost 与 LocalGitCodeHost。"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from twinfix.adapters.types import (
    CheckResult,
    IssueRef,
    MergeResult,
    PRInfo,
    PRRef,
    PRRequest,
    Workspace,
)

FIX_BRANCH_PREFIX = "fix/twinfix-"


class BranchNotAllowedError(PermissionError):
    pass


def assert_fix_branch(branch: str) -> None:
    # 与 GitHub 分支规则双保险：适配器本身也只允许推 fix/twinfix-*
    if not branch.startswith(FIX_BRANCH_PREFIX):
        raise BranchNotAllowedError(f"refusing to push non-fix branch {branch!r}")


class CodeHost(Protocol):
    async def clone(self, repo: str, ref: str, dest: Path) -> None: ...
    async def push_branch(self, ws: Workspace, branch: str) -> str: ...
    async def open_pr(self, req: PRRequest) -> PRInfo: ...
    async def set_check(self, repo: str, sha: str, check: CheckResult) -> None: ...
    async def merge_pr(self, repo: str, number: int, method: str, expected_sha: str) -> MergeResult: ...
    async def comment(self, target: IssueRef | PRRef, body: str) -> None: ...
    async def branch_head(self, repo: str, branch: str) -> str: ...


@dataclass
class _FakePR:
    info: PRInfo
    req: PRRequest
    merged: bool = False


@dataclass
class FakeCodeHost:
    heads: dict[tuple[str, str], str] = field(default_factory=dict)
    prs: dict[int, _FakePR] = field(default_factory=dict)
    checks: list[tuple[str, str, CheckResult]] = field(default_factory=list)
    comments: list[tuple[IssueRef | PRRef, str]] = field(default_factory=list)
    _seq: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    async def clone(self, repo: str, ref: str, dest: Path) -> None:
        await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)

    async def push_branch(self, ws: Workspace, branch: str) -> str:
        assert_fix_branch(branch)
        sha = f"{ws.sha[:7]}f{next(self._seq):07d}"
        self.heads[(ws.repo, branch)] = sha
        return sha

    async def open_pr(self, req: PRRequest) -> PRInfo:
        number = next(self._seq)
        info = PRInfo(
            number=number,
            url=f"https://github.com/{req.repo}/pull/{number}",
            head_sha=self.heads.get((req.repo, req.head), "0" * 40),
        )
        self.prs[number] = _FakePR(info=info, req=req)
        return info

    async def set_check(self, repo: str, sha: str, check: CheckResult) -> None:
        self.checks.append((repo, sha, check))

    async def merge_pr(self, repo: str, number: int, method: str, expected_sha: str) -> MergeResult:
        pr = self.prs.get(number)
        if pr is None:
            return MergeResult(merged=False, sha=None, message="pr not found")
        if pr.merged:
            return MergeResult(merged=False, sha=None, message="already merged")
        if self.heads.get((repo, pr.req.head)) != expected_sha:
            # 与 GitHub API 的 sha 参数语义一致：头提交变了就拒绝合并
            return MergeResult(merged=False, sha=None, message="head sha changed")
        pr.merged = True
        merge_sha = f"m{expected_sha[1:]}"
        self.heads[(repo, pr.req.base)] = merge_sha
        return MergeResult(merged=True, sha=merge_sha)

    async def comment(self, target: IssueRef | PRRef, body: str) -> None:
        self.comments.append((target, body))

    async def branch_head(self, repo: str, branch: str) -> str:
        return self.heads.setdefault((repo, branch), "b" * 40)
