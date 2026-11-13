"""本地 git 代码托管：用裸仓库实现真实的分支、推送、PR、Check、合并。

基准测试和离线体验的默认实现，与 GitHubAppCodeHost 行为一致：
- 只允许推 fix/twinfix-*；合并要求 expected_sha 与 PR 头一致（防并发）。
- 合并方式 squash：在临时工作树里把 PR 改动压成一个提交推到目标分支。
PR / Check / 评论元数据存在裸仓库旁的 twinfix.json 中。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from twinfix.adapters.codehost import assert_fix_branch
from twinfix.adapters.gitutil import GitError, git, git_ok
from twinfix.adapters.types import CheckResult, IssueRef, MergeResult, PRInfo, PRRef, PRRequest, Workspace


@dataclass(frozen=True)
class LocalPR:
    number: int
    head: str
    base: str
    title: str
    body: str
    state: str  # open / merged / closed
    head_sha: str
    merge_sha: str | None = None


class LocalGitCodeHost:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()  # git 命令会在临时目录里执行，必须是绝对路径
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------------------------------------------------------------- 仓库管理

    def repo_path(self, repo: str) -> Path:
        owner, _, name = repo.partition("/")
        if not owner or not name or ".." in repo or "\\" in repo:
            raise ValueError(f"invalid repo name {repo!r}")
        return self.root / owner / f"{name}.git"

    def url(self, repo: str) -> str:
        return self.repo_path(repo).resolve().as_uri()

    async def remote(self, repo: str) -> tuple[str, dict[str, str]]:
        """供 WorkspaceManager 使用：本地仓库无需凭据。"""
        return self.url(repo), {}

    async def exists(self, repo: str) -> bool:
        return (self.repo_path(repo) / "HEAD").exists()

    async def create_from_dir(self, repo: str, source: Path, branches: tuple[str, ...] = ("main", "dev")) -> str:
        """用目录内容初始化仓库（用例种子）。返回初始提交 sha。

        幂等：同名仓库已存在时整个重建。上一次评测中断会留下只推了一半分支的裸仓库，
        `git init` 不会清掉旧 refs，接着 push 会被 "fetch first" 拒绝（评测因此整批判 ERROR）。
        """
        bare = self.repo_path(repo)
        bare.parent.mkdir(parents=True, exist_ok=True)
        if bare.exists():
            _rmtree(bare)
        await git("init", "--bare", "-b", branches[0], str(bare))
        with tempfile.TemporaryDirectory(prefix="twinfix-seed-", ignore_cleanup_errors=True) as tmp:
            work = Path(tmp) / "w"
            shutil.copytree(source, work, ignore=shutil.ignore_patterns("target", ".git", "*.log"))
            await git("init", "-b", branches[0], cwd=work)
            await git("add", "-A", cwd=work)
            await git("commit", "-q", "-m", "chore: initial import", cwd=work)
            for b in branches[1:]:
                await git("branch", b, cwd=work)
            await git("push", "-q", str(bare), *[f"{b}:{b}" for b in branches], cwd=work)
            return await git("rev-parse", "HEAD", cwd=work)

    async def commit_patch(self, repo: str, branch: str, patch: str, message: str) -> str:
        """在分支上应用 patch 并提交（Arena 注入故障用）。返回新提交 sha。"""
        with tempfile.TemporaryDirectory(prefix="twinfix-patch-", ignore_cleanup_errors=True) as tmp:
            work = Path(tmp) / "w"
            await git("clone", "-q", "--branch", branch, self.url(repo), str(work))
            await git("apply", "--whitespace=nowarn", "-", cwd=work, input_text=patch)
            await git("add", "-A", cwd=work)
            await git("commit", "-q", "-m", message, cwd=work)
            await git("push", "-q", "origin", f"HEAD:{branch}", cwd=work)
            return await git("rev-parse", "HEAD", cwd=work)

    # ---------------------------------------------------------------- CodeHost 协议

    async def clone(self, repo: str, ref: str, dest: Path) -> None:
        await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
        await git("init", "-q", "-b", "twinfix-base", cwd=dest)
        await git("remote", "add", "origin", self.url(repo), cwd=dest)
        await git("fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*", cwd=dest)
        sha = await self._resolve(dest, ref)
        await git("checkout", "-q", "-B", "twinfix-base", sha, cwd=dest)

    async def push_branch(self, ws: Workspace, branch: str) -> str:
        # 实际推送由 WorkspaceManager 经 remote() 完成；这里只做分支规则校验并返回远端头
        assert_fix_branch(branch)
        return await self.branch_head(ws.repo, branch)

    async def open_pr(self, req: PRRequest) -> PRInfo:
        async with self._lock(req.repo):
            meta = self._load(req.repo)
            for pr in meta["prs"].values():
                if pr["head"] == req.head and pr["base"] == req.base and pr["state"] == "open":
                    return self._info(req.repo, pr)  # 幂等：同一分支只有一个打开的 PR
            head_sha = await self.branch_head(req.repo, req.head)
            number = meta["next"]
            meta["next"] += 1
            meta["prs"][str(number)] = {
                "number": number,
                "head": req.head,
                "base": req.base,
                "title": req.title,
                "body": req.body,
                "state": "open",
                "head_sha": head_sha,
                "merge_sha": None,
                "created_at": _now(),
            }
            self._save(req.repo, meta)
            return self._info(req.repo, meta["prs"][str(number)])

    async def set_check(self, repo: str, sha: str, check: CheckResult) -> None:
        async with self._lock(repo):
            meta = self._load(repo)
            meta["checks"].setdefault(sha, {})[check.name] = {
                "conclusion": check.conclusion,
                "summary": check.summary,
                "at": _now(),
            }
            self._save(repo, meta)

    async def merge_pr(self, repo: str, number: int, method: str, expected_sha: str) -> MergeResult:
        async with self._lock(repo):
            meta = self._load(repo)
            pr = meta["prs"].get(str(number))
            if pr is None:
                return MergeResult(False, None, "pr not found")
            if pr["state"] != "open":
                return MergeResult(False, None, f"pr is {pr['state']}")
            head = await self.branch_head(repo, pr["head"])
            if head != expected_sha:
                return MergeResult(False, None, "head sha changed")
            try:
                merge_sha = await self._squash(repo, pr, head)
            except GitError as exc:
                return MergeResult(False, None, f"merge conflict: {exc.stderr.strip()[:500]}")
            pr.update(state="merged", merge_sha=merge_sha, head_sha=head, merged_at=_now())
            self._save(repo, meta)
            return MergeResult(True, merge_sha)

    async def comment(self, target: IssueRef | PRRef, body: str) -> None:
        async with self._lock(target.repo):
            meta = self._load(target.repo)
            kind = "issue" if isinstance(target, IssueRef) else "pr"
            meta["comments"].append({"kind": kind, "number": target.number, "body": body, "at": _now()})
            self._save(target.repo, meta)

    async def branch_head(self, repo: str, branch: str) -> str:
        out = await git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", cwd=self.repo_path(repo))
        return out

    # ---------------------------------------------------------------- 查询（控制台 / 测试用）

    async def get_pr(self, repo: str, number: int) -> LocalPR | None:
        pr = self._load(repo)["prs"].get(str(number))
        if pr is None:
            return None
        return LocalPR(**{k: pr[k] for k in LocalPR.__dataclass_fields__})

    def checks(self, repo: str, sha: str) -> dict[str, Any]:
        result: dict[str, Any] = self._load(repo)["checks"].get(sha, {})
        return result

    # ---------------------------------------------------------------- 内部

    async def _squash(self, repo: str, pr: dict[str, Any], head: str) -> str:
        with tempfile.TemporaryDirectory(prefix="twinfix-merge-", ignore_cleanup_errors=True) as tmp:
            work = Path(tmp) / "w"
            await git("clone", "-q", "--branch", pr["base"], self.url(repo), str(work))
            await git("fetch", "-q", "origin", pr["head"], cwd=work)
            await git("merge", "-q", "--squash", head, cwd=work)
            if not await git("status", "--porcelain", cwd=work):
                raise GitError(["merge"], 1, "nothing to merge: changes already in base")
            await git("commit", "-q", "-m", f"{pr['title']} (#{pr['number']})\n\n{pr['body']}", cwd=work)
            await git("push", "-q", "origin", f"HEAD:{pr['base']}", cwd=work)
            return await git("rev-parse", "HEAD", cwd=work)

    async def _resolve(self, cwd: Path, ref: str) -> str:
        for candidate in (ref, f"origin/{ref}"):
            if await git_ok("rev-parse", "--verify", "-q", f"{candidate}^{{commit}}", cwd=cwd):
                return await git("rev-parse", f"{candidate}^{{commit}}", cwd=cwd)
        raise GitError(["rev-parse", ref], 1, f"unknown ref {ref}")

    def _lock(self, repo: str) -> asyncio.Lock:
        return self._locks.setdefault(repo, asyncio.Lock())

    def _meta_path(self, repo: str) -> Path:
        return self.repo_path(repo) / "twinfix.json"

    def _load(self, repo: str) -> dict[str, Any]:
        path = self._meta_path(repo)
        if not path.exists():
            return {"next": 1, "prs": {}, "checks": {}, "comments": []}
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def _save(self, repo: str, meta: dict[str, Any]) -> None:
        path = self._meta_path(repo)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)  # 原子替换，进程崩溃不会留下半个文件

    def _info(self, repo: str, pr: dict[str, Any]) -> PRInfo:
        return PRInfo(number=pr["number"], url=f"local://{repo}/pull/{pr['number']}", head_sha=pr["head_sha"])


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _rmtree(path: Path) -> None:
    """删裸仓库。Windows 上 git 留下的是只读对象文件，shutil.rmtree 会 PermissionError，先清只读位。"""

    def onerror(func: Any, name: str, exc: Any) -> None:
        Path(name).chmod(0o700)
        func(name)

    shutil.rmtree(path, onerror=onerror)
