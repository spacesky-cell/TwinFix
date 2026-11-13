"""WorkspaceManager：工作区与代码托管之间的代码流转。

沙箱容器里没有凭据，也访问不到宿主机上的仓库，所以代码进出都由 TwinFix 进程搬运，统一用 git bundle：
  检出：宿主机从托管方取到目标提交 → 打 bundle → 放进工作区 → 工作区里 fetch bundle 并检出
  推送：工作区里把 fix 分支打 bundle → 取回宿主机 → 宿主机 fetch 后推到托管方
这样 LocalGitCodeHost 与 GitHubAppCodeHost 走同一条路径，凭据只出现在宿主机侧。

工作区内两个标签：
  twinfix-base   修复起点（bug 所在的提交）
  twinfix-repro  Reproducer 提交的复现用例（Fixer 在其之上修改）
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from twinfix.adapters.codehost import assert_fix_branch
from twinfix.adapters.gitutil import GitError, git
from twinfix.adapters.sandbox import BASE_TAG, REPRO_TAG, SandboxProvider
from twinfix.adapters.types import Workspace

BUNDLE_PATH = ".twinfix/transfer.bundle"


class RemoteSource(Protocol):
    """宿主机可访问的仓库：地址 + 访问它所需的 git 环境变量（凭据只经环境传递，不进 URL 与命令行）。"""

    async def remote(self, repo: str) -> tuple[str, dict[str, str]]: ...


@dataclass(frozen=True)
class RebaseResult:
    ok: bool
    new_base: str
    conflicts: list[str]
    message: str = ""


class WorkspaceManager:
    def __init__(self, sandbox: SandboxProvider, source: RemoteSource, cache_dir: Path) -> None:
        self.sandbox = sandbox
        self.source = source
        self.cache_dir = cache_dir.resolve()

    async def _mirror(self, repo: str) -> Path:
        """宿主机侧的镜像仓库：只增量 fetch，避免每次全量克隆。"""
        path = self.cache_dir / repo.replace("/", "__")
        url, env = await self.source.remote(repo)
        if not (path / "HEAD").exists():
            path.mkdir(parents=True, exist_ok=True)
            await git("init", "-q", "--bare", str(path))
        await git("fetch", "-q", "--prune", url, "+refs/heads/*:refs/heads/*", cwd=path, env=env)
        return path

    async def checkout(self, ws: Workspace, repo: str, ref: str) -> str:
        """把 repo@ref 放进工作区，打上 twinfix-base 标签。返回解析后的完整 sha。"""
        mirror = await self._mirror(repo)
        sha = await git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=mirror)
        with tempfile.TemporaryDirectory(prefix="twinfix-bundle-", ignore_cleanup_errors=True) as tmp:
            bundle = Path(tmp) / "b.bundle"
            # bundle 需要一个引用名：用临时分支指向目标提交
            await git("branch", "-f", "twinfix-transfer", sha, cwd=mirror)
            await git("bundle", "create", "-q", str(bundle), "twinfix-transfer", cwd=mirror)
            await self.sandbox.put_bytes(ws, BUNDLE_PATH, bundle.read_bytes())
        await self._git(ws, "init", "-q", "-b", "twinfix-work")
        await self._git(ws, "fetch", "-q", BUNDLE_PATH, "twinfix-transfer:refs/remotes/bundle/transfer")
        await self._git(ws, "checkout", "-q", "-B", "twinfix-work", sha)
        await self._git(ws, "tag", "-f", BASE_TAG, sha)
        await self._exclude(ws)
        return sha

    async def checkout_into(self, ws: Workspace, repo: str, ref: str, subdir: str) -> str:
        """把附加仓库检出到工作区的 subdir 下，供跨仓库诊断与修复使用。

        与主仓库不同：不切换工作区根；附加仓库有自己的 git 目录和 twinfix-base 标签，
        提交/推送都要带 cwd=subdir。是否允许修复由调用方（orchestrator）按 fixable 配置过滤。
        """
        mirror = await self._mirror(repo)
        sha = await git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=mirror)
        with tempfile.TemporaryDirectory(prefix="twinfix-bundle-", ignore_cleanup_errors=True) as tmp:
            bundle = Path(tmp) / "b.bundle"
            await git("branch", "-f", "twinfix-transfer", sha, cwd=mirror)
            await git("bundle", "create", "-q", str(bundle), "twinfix-transfer", cwd=mirror)
            await self.sandbox.put_bytes(ws, f"{subdir}/{BUNDLE_PATH}", bundle.read_bytes())
        # git init 对已存在的仓库是幂等的；不先探测目录是否存在，LocalSandbox 的 cwd 不存在会直接抛异常
        await self._git(ws, "init", "-q", "-b", "twinfix-ref", subdir)
        await self._git(ws, "fetch", "-q", BUNDLE_PATH, "twinfix-transfer:refs/remotes/bundle/transfer", cwd=subdir)
        await self._git(ws, "checkout", "-q", "-B", "twinfix-ref", sha, cwd=subdir)
        await self._git(ws, "tag", "-f", BASE_TAG, sha, cwd=subdir)
        await self._exclude(ws, cwd=subdir)
        return sha

    async def commit_all(self, ws: Workspace, message: str, *, tag: str | None = None) -> str | None:
        """提交工作区的全部改动。没有改动时返回 None。"""
        await self._git(ws, "add", "-A")
        status = await self._git(ws, "status", "--porcelain")
        if not status.strip():
            sha = None
        else:
            await self._git(ws, "commit", "-q", "-m", message)
            sha = await self._git(ws, "rev-parse", "HEAD")
        if tag:
            await self._git(ws, "tag", "-f", tag, "HEAD")
        return sha

    async def commit_paths(self, ws: Workspace, paths: list[str], message: str, cwd: str | None = None) -> str | None:
        """只提交 paths 里的改动，其余改动留在工作区。没有改动时返回 None。

        提交内容必须与人工评审看到的补丁一致：Fixer 自验时新建的临时文件不应随修复合并。
        """
        await self._git(ws, "reset", "-q", cwd=cwd)
        if not paths:
            return None
        await self._git(ws, "add", "-A", "--", *paths, cwd=cwd)
        if not (await self._git(ws, "status", "--porcelain", "--", *paths, cwd=cwd)).strip():
            return None
        await self._git(ws, "commit", "-q", "-m", message, cwd=cwd)
        return await self._git(ws, "rev-parse", "HEAD", cwd=cwd)

    async def tracked_at(self, ws: Workspace, ref: str = BASE_TAG, cwd: str | None = None) -> set[str]:
        """ref 上就已存在（受版本控制）的文件：区分"改了既有文件"和"新建文件"。"""
        out = await self._git(ws, "ls-tree", "-r", "--name-only", ref, cwd=cwd)
        return {line for line in out.splitlines() if line}

    async def reset_to(self, ws: Workspace, ref: str) -> None:
        """丢弃 ref 之后的全部改动（新一轮修复从复现提交重新开始）。"""
        await self._git(ws, "reset", "-q", "--hard", ref)
        await self._git(ws, "clean", "-q", "-fd", "-e", ".twinfix")

    async def changed_files(self, ws: Workspace, since: str = BASE_TAG, cwd: str | None = None) -> list[str]:
        await self._git(ws, "add", "-A", "-N", ".", cwd=cwd)
        out = await self._git(ws, "diff", "--name-only", since, cwd=cwd)
        return [f for f in out.splitlines() if f]

    async def diff(
        self,
        ws: Workspace,
        since: str = BASE_TAG,
        paths: list[str] | None = None,
        cwd: str | None = None,
    ) -> str:
        await self._git(ws, "add", "-A", "-N", ".", cwd=cwd)
        return await self._git(ws, "diff", "--no-color", since, "--", *(paths or []), cwd=cwd)

    async def head(self, ws: Workspace, cwd: str | None = None) -> str:
        return await self._git(ws, "rev-parse", "HEAD", cwd=cwd)

    async def has_tag(self, ws: Workspace, tag: str) -> bool:
        res = await self.sandbox.git(ws, ["rev-parse", "-q", "--verify", f"refs/tags/{tag}"], 30)
        return res.ok

    async def push(self, ws: Workspace, repo: str, branch: str, cwd: str | None = None) -> str:
        """把工作区 HEAD 推到托管方的 fix 分支。返回推送的 sha。"""
        assert_fix_branch(branch)  # 与托管方的分支保护双保险
        sha = await self._git(ws, "rev-parse", "HEAD", cwd=cwd)
        await self._git(ws, "branch", "-f", "twinfix-push", "HEAD", cwd=cwd)
        await self._git(ws, "bundle", "create", "-q", BUNDLE_PATH, f"{BASE_TAG}..twinfix-push", cwd=cwd)
        data = await self.sandbox.get_bytes(ws, f"{cwd}/{BUNDLE_PATH}" if cwd else BUNDLE_PATH)
        mirror = await self._mirror(repo)
        with tempfile.TemporaryDirectory(prefix="twinfix-bundle-", ignore_cleanup_errors=True) as tmp:
            bundle = Path(tmp) / "b.bundle"
            bundle.write_bytes(data)
            await git("fetch", "-q", str(bundle), f"+twinfix-push:refs/heads/{branch}", cwd=mirror)
        url, env = await self.source.remote(repo)
        # fix 分支归 TwinFix 所有，rebase 后需要覆盖
        await git("push", "-q", "--force", url, f"refs/heads/{branch}:refs/heads/{branch}", cwd=mirror, env=env)
        return sha

    async def _fetch_target(self, ws: Workspace, repo: str, branch: str) -> tuple[str, str]:
        """把目标分支最新提交取进工作区；返回 (new_base, old_base)。"""
        mirror = await self._mirror(repo)
        new_base = await git("rev-parse", f"refs/heads/{branch}", cwd=mirror)
        old_base = await self._git(ws, "rev-parse", BASE_TAG)
        if new_base == old_base:
            return new_base, old_base
        with tempfile.TemporaryDirectory(prefix="twinfix-bundle-", ignore_cleanup_errors=True) as tmp:
            bundle = Path(tmp) / "b.bundle"
            await git("branch", "-f", "twinfix-transfer", new_base, cwd=mirror)
            try:
                await git("bundle", "create", "-q", str(bundle), f"{old_base}..twinfix-transfer", cwd=mirror)
            except GitError:
                await git("bundle", "create", "-q", str(bundle), "twinfix-transfer", cwd=mirror)
            await self.sandbox.put_bytes(ws, BUNDLE_PATH, bundle.read_bytes())
        await self._git(ws, "fetch", "-q", BUNDLE_PATH, "+twinfix-transfer:refs/remotes/bundle/target")
        return new_base, old_base

    async def rebase_onto(self, ws: Workspace, repo: str, branch: str) -> RebaseResult:
        """把修复（twinfix-base..HEAD）变基到目标分支最新提交。冲突时中止并返回冲突文件。"""
        new_base, old_base = await self._fetch_target(ws, repo, branch)
        if new_base == old_base:
            return RebaseResult(True, new_base, [], "target branch unchanged")
        res = await self.sandbox.git(ws, ["rebase", "-q", "--onto", new_base, old_base], 300)
        if not res.ok:
            conflicts = (await self._git(ws, "diff", "--name-only", "--diff-filter=U")).splitlines()
            await self.sandbox.git(ws, ["rebase", "--abort"], 60)
            return RebaseResult(False, new_base, conflicts, res.stderr.strip()[:2000])
        await self._git(ws, "tag", "-f", BASE_TAG, new_base)
        return RebaseResult(True, new_base, [])

    async def stage_target_with(self, ws: Workspace, repo: str, branch: str, keep: list[str]) -> tuple[str, str] | None:
        """把工作区切到目标分支最新提交（不含修复），只带上 keep 中的文件（复现用例）。

        用于判断"目标分支是否已被别人修好"。返回 (target_sha, 原 HEAD) 以便 restore；
        目标分支未变化或带不过去文件时返回 None（工作区保持原样）。
        """
        new_base, old_base = await self._fetch_target(ws, repo, branch)
        if new_base == old_base:
            return None
        fix_head = await self.head(ws)
        await self._git(ws, "checkout", "-q", "--detach", new_base)
        res = await self.sandbox.git(ws, ["checkout", fix_head, "--", *keep], 60)
        if not res.ok:
            await self.restore(ws, fix_head)
            return None
        return new_base, fix_head

    async def restore(self, ws: Workspace, sha: str) -> None:
        await self._git(ws, "checkout", "-q", "-f", "-B", "twinfix-work", sha)
        await self._git(ws, "clean", "-q", "-fd", "-e", ".twinfix")

    async def _exclude(self, ws: Workspace, cwd: str | None = None) -> None:
        # 传输用的 bundle、构建产物、语言运行时缓存与附加仓库都不进入主仓库提交。
        # Python 的 __pycache__/.pytest_cache 会落在 tests 目录下，不排除会被当成复现测试文件提交。
        base = f"{cwd}/" if cwd else ""
        excluded = (
            ".twinfix/\ntarget/\n_repos/\n__pycache__/\n.pytest_cache/\n.mypy_cache/\n.venv/\n*.pyc\n"
            "bin/\nobj/\nTestResults/\n.vs/\n*.user\n"
        )
        await self.sandbox.put_bytes(ws, f"{base}.git/info/exclude", excluded.encode())

    async def _git(self, ws: Workspace, *args: str, cwd: str | None = None) -> str:
        res = await self.sandbox.git(ws, list(args), 300, cwd=cwd)
        if not res.ok:
            raise GitError(list(args), res.exit_code, res.stderr)
        return res.stdout.strip()


__all__ = ["BASE_TAG", "REPRO_TAG", "RebaseResult", "RemoteSource", "WorkspaceManager"]
