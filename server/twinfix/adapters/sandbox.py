"""沙箱适配器：Agent 生成的命令只在 Workspace 内执行。

真实实现为 DockerSandbox（无宿主机挂载、无 Docker socket、限资源、限出网）。
LocalSandbox 只用于本地开发与测试：文件操作严格限制在工作区目录内，但命令在宿主机执行，
因此默认关闭 exec，绝不能用于运行不可信的 Agent 命令。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

from twinfix.adapters.types import CommandResult, Workspace
from twinfix.ids import new_id


class PathEscapeError(PermissionError):
    pass


class SandboxProvider(Protocol):
    async def create_workspace(self, repo: str, sha: str, image: str | None = None) -> Workspace: ...
    async def exec(self, ws: Workspace, cmd: list[str], timeout: int, cwd: str | None = None) -> CommandResult: ...
    async def read_file(self, ws: Workspace, path: str) -> str: ...
    async def write_file(self, ws: Workspace, path: str, content: str) -> None: ...
    async def list_dir(self, ws: Workspace, path: str = ".") -> list[str]: ...
    async def search(self, ws: Workspace, pattern: str, glob: str = "**/*") -> list[tuple[str, int, str]]: ...
    async def read_sources(
        self,
        ws: Workspace,
        *,
        suffixes: tuple[str, ...] = (".java",),
        exclude_dirs: tuple[str, ...] = (".git", "target", "node_modules"),
        max_files: int = 2000,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> tuple[dict[str, str], bool]: ...
    async def diff(self, ws: Workspace) -> str: ...
    async def destroy(self, ws: Workspace) -> None: ...
    # ---- 系统操作（不对 Agent 暴露）：git 与二进制文件传输，供 WorkspaceManager 使用
    async def git(
        self, ws: Workspace, args: list[str], timeout: int = 300, cwd: str | None = None
    ) -> CommandResult: ...
    async def put_bytes(self, ws: Workspace, path: str, data: bytes) -> None: ...
    async def get_bytes(self, ws: Workspace, path: str) -> bytes: ...


def resolve_in(root: Path, rel: str) -> Path:
    """把相对路径解析到工作区内；任何 `..`、绝对路径、符号链接逃逸都会被拒绝。"""
    root = root.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        raise PathEscapeError(f"path escapes workspace: {rel!r}")
    return target


class LocalSandbox:
    def __init__(self, base_dir: Path, *, allow_exec: bool = False) -> None:
        self.base_dir = base_dir
        self.allow_exec = allow_exec

    async def create_workspace(self, repo: str, sha: str, image: str | None = None) -> Workspace:
        ws_id = new_id()
        root = self.base_dir / ws_id
        root.mkdir(parents=True, exist_ok=False)
        return Workspace(id=ws_id, repo=repo, sha=sha, root=root, image=image)

    async def exec(self, ws: Workspace, cmd: list[str], timeout: int, cwd: str | None = None) -> CommandResult:
        if not self.allow_exec:
            raise PermissionError("LocalSandbox exec is disabled; use DockerSandbox")
        root = resolve_in(ws.root, cwd) if cwd else ws.root
        return await asyncio.to_thread(_run, cmd, root, timeout)

    async def read_file(self, ws: Workspace, path: str) -> str:
        return resolve_in(ws.root, path).read_bytes().decode("utf-8", errors="replace")

    async def write_file(self, ws: Workspace, path: str, content: str) -> None:
        target = resolve_in(ws.root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 按字节写：write_text 在 Windows 上会把 \n 转成 \r\n，导致整文件 diff
        target.write_bytes(content.encode("utf-8"))

    async def list_dir(self, ws: Workspace, path: str = ".") -> list[str]:
        target = resolve_in(ws.root, path)
        root = ws.root.resolve()
        return sorted(p.relative_to(root).as_posix() for p in target.iterdir())

    async def search(self, ws: Workspace, pattern: str, glob: str = "**/*") -> list[tuple[str, int, str]]:
        root = ws.root.resolve()
        hits: list[tuple[str, int, str]] = []
        for file in root.glob(glob):
            if not file.is_file() or ".git" in file.parts:
                continue
            try:
                lines = file.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for no, line in enumerate(lines, 1):
                if pattern in line:
                    hits.append((file.relative_to(root).as_posix(), no, line.strip()))
        return hits

    async def read_sources(
        self,
        ws: Workspace,
        *,
        suffixes: tuple[str, ...] = (".java",),
        exclude_dirs: tuple[str, ...] = (".git", "target", "node_modules"),
        max_files: int = 2000,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> tuple[dict[str, str], bool]:
        return await asyncio.to_thread(_read_sources_from_tree, ws.root, suffixes, exclude_dirs, max_files, max_bytes)

    async def diff(self, ws: Workspace) -> str:
        if not (ws.root / ".git").exists():
            return ""
        res = await self.git(ws, ["add", "-A", "-N", "."], 60)
        res = await self.git(ws, ["diff", "--no-color", BASE_TAG], 60)
        return res.stdout

    async def destroy(self, ws: Workspace) -> None:
        await asyncio.to_thread(shutil.rmtree, ws.root, True)

    async def git(self, ws: Workspace, args: list[str], timeout: int = 300, cwd: str | None = None) -> CommandResult:
        # git 是系统操作（固定参数、禁用 hooks），与 Agent 的 exec 分开，不受 allow_exec 限制
        from twinfix.adapters.gitutil import git_argv, git_env

        root = resolve_in(ws.root, cwd) if cwd else ws.root
        return await asyncio.to_thread(_run, git_argv(args), root, timeout, git_env())

    async def put_bytes(self, ws: Workspace, path: str, data: bytes) -> None:
        target = resolve_in(ws.root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def get_bytes(self, ws: Workspace, path: str) -> bytes:
        return resolve_in(ws.root, path).read_bytes()


# 修复起点（Reproducer 写用例之前的代码）与复现提交，WorkspaceManager 维护这两个标签
BASE_TAG = "twinfix-base"
REPRO_TAG = "twinfix-repro"

# 代码索引只读源码文件；单文件上限防止把巨大的生成代码读进内存
_MAX_SOURCE_FILE_BYTES = 512 * 1024


def _read_sources_from_tree(
    root: Path,
    suffixes: tuple[str, ...],
    exclude_dirs: tuple[str, ...],
    max_files: int,
    max_bytes: int,
) -> tuple[dict[str, str], bool]:
    root = root.resolve()
    excluded = set(exclude_dirs)
    files: dict[str, str] = {}
    total = 0
    truncated = False
    # 主仓库优先：附加仓库不应把 max_files 名额挤掉
    paths = sorted(
        root.rglob("*"),
        key=lambda p: (p.relative_to(root).as_posix().startswith("_repos/"), p.as_posix()),
    )
    for path in paths:
        if len(files) >= max_files:
            truncated = True
            break
        if not path.is_file() or path.suffix not in suffixes:
            continue
        rel = path.relative_to(root).as_posix()
        if excluded.intersection(Path(rel).parts[:-1]):
            continue
        try:
            if not path.resolve().is_relative_to(root):
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > _MAX_SOURCE_FILE_BYTES:
            continue
        if total + len(data) > max_bytes:
            truncated = True
            break
        files[rel] = data.decode("utf-8", errors="replace")
        total += len(data)
    return files, truncated


def _run(cmd: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None) -> CommandResult:
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            exit_code=-1,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            duration_ms=int((time.monotonic() - start) * 1000),
            timed_out=True,
        )
    return CommandResult(
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_ms=int((time.monotonic() - start) * 1000),
    )


def _text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    return data.decode(errors="replace") if isinstance(data, bytes) else data
