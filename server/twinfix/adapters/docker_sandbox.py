"""DockerSandbox：每个 Workspace 是一个长驻容器，Agent 的命令只在容器内执行。

隔离（docs/05-safety.md §2）
- 非 root 用户运行，去掉全部 capability，no-new-privileges，pids/CPU/内存受限。
- 不挂载宿主机目录、不挂 Docker socket；代码在命名卷里，由 TwinFix 进程经 docker cp 写入。
- 网络默认 none；需要下载依赖时接入出口受限的网络（只允许依赖镜像站）。
- 依赖缓存：共享卷只读挂载，作为 Maven 的 tail 仓库；容器写入自己的私有缓存，互不污染。

文件操作（读/写/列/搜/diff）同样在容器内执行，路径先在 TwinFix 侧做规范化与逃逸检查。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import tarfile
import time
from pathlib import PurePosixPath
from typing import Any

import docker
from docker.errors import NotFound
from docker.models.containers import Container

from twinfix.adapters.gitutil import BOT_EMAIL, BOT_NAME
from twinfix.adapters.sandbox import _MAX_SOURCE_FILE_BYTES, BASE_TAG, PathEscapeError
from twinfix.adapters.types import CommandResult, Workspace
from twinfix.ids import new_id, short_id

WORKDIR = "/workspace"
SANDBOX_UID = "10001:10001"
LABEL = "dev.twinfix.workspace"
_MAX_OUTPUT = 200_000


def _container_path(rel: str) -> str:
    p = PurePosixPath(rel.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        raise PathEscapeError(f"path escapes workspace: {rel!r}")
    return str(PurePosixPath(WORKDIR) / p)


class DockerSandbox:
    def __init__(
        self,
        *,
        image: str,
        network: str | None,
        dependency_volume: str | None,
        maven_settings: str | None = None,
        cpus: float = 2.0,
        memory_mb: int = 2048,
        base_url: str | None = None,
    ) -> None:
        self.client = docker.DockerClient(base_url=base_url) if base_url else docker.from_env()
        self.image = image
        self.network = network
        self.dependency_volume = dependency_volume
        self.maven_settings = maven_settings
        self.cpus = cpus
        self.memory_mb = memory_mb

    # ---------------------------------------------------------------- 生命周期

    async def create_workspace(self, repo: str, sha: str, image: str | None = None) -> Workspace:
        ws_id = new_id()
        name = f"twinfix-ws-{short_id(ws_id)}"
        await asyncio.to_thread(self._start, name, image or self.image, ws_id)
        return Workspace(id=ws_id, repo=repo, sha=sha, root=PurePosixPath(WORKDIR), image=name)  # type: ignore[arg-type]

    def _start(self, name: str, image: str, ws_id: str) -> None:
        volumes: dict[str, dict[str, str]] = {f"{name}-code": {"bind": WORKDIR, "mode": "rw"}}
        if self.dependency_volume:
            volumes[self.dependency_volume] = {"bind": "/deps", "mode": "ro"}
        env = {
            "HOME": "/tmp/home",  # noqa: S108 - 容器内路径，非宿主机临时目录
            # 私有可写仓库 + 只读共享仓库（tail）：命中共享缓存时不出网，新依赖只写入私有缓存
            "MAVEN_ARGS": " ".join(
                ["-B", "-Dmaven.repo.local=/tmp/m2", "-Dmaven.repo.local.tail=/deps/repository"]
                + (["-s", "/tmp/home/.m2/settings.xml"] if self.maven_settings else [])  # noqa: S108
                + ([] if self.network else ["-o"])  # 无网络时离线构建，只用共享缓存里的依赖
            ),
            # 沙箱用户在镜像里没有 passwd 条目，显式指定 user.home
            "MAVEN_OPTS": "-Duser.home=/tmp/home",
            "GIT_AUTHOR_NAME": BOT_NAME,
            "GIT_AUTHOR_EMAIL": BOT_EMAIL,
            "GIT_COMMITTER_NAME": BOT_NAME,
            "GIT_COMMITTER_EMAIL": BOT_EMAIL,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        # 代码卷初始属主是 root：先用一次性 root 容器把卷交给沙箱用户
        self.client.containers.run(
            image,
            ["chown", SANDBOX_UID, WORKDIR],
            remove=True,
            network_mode="none",
            volumes={f"{name}-code": {"bind": WORKDIR, "mode": "rw"}},
            user="0:0",
            entrypoint=[],
        )
        container = self.client.containers.run(
            image,
            ["sleep", "infinity"],
            name=name,
            detach=True,
            entrypoint=[],
            user=SANDBOX_UID,
            # 不设 working_dir：Docker 29 会把作为工作目录的卷挂载点属主重置为 root，
            # 覆盖上面的 chown。每次 exec 都显式传 workdir。
            environment=env,
            volumes=volumes,
            network_mode=self.network or "none",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            pids_limit=512,
            nano_cpus=int(self.cpus * 1e9),
            mem_limit=f"{self.memory_mb}m",
            tmpfs={"/tmp": "rw,exec,size=1g"},  # noqa: S108 - 容器内路径
            labels={LABEL: ws_id},
        )
        # HOME=/tmp/home 必须真实存在：.NET 找不到 home 会直接拒绝启动，
        # Maven 的 settings.xml 也需要这个父目录。tmpfs 会遮住镜像里的 /tmp，
        # 所以每次创建容器后显式补一次。
        container.exec_run(["mkdir", "-p", "/tmp/home"], user=SANDBOX_UID)  # noqa: S108 - 容器内路径
        if self.maven_settings:
            self._put(name, "/tmp/home/.m2/settings.xml", self.maven_settings.encode(), mode=0o644)  # noqa: S108

    async def destroy(self, ws: Workspace) -> None:
        await asyncio.to_thread(self._destroy, self._name(ws))

    def _destroy(self, name: str) -> None:
        with contextlib.suppress(NotFound):
            self.client.containers.get(name).remove(force=True)
        with contextlib.suppress(NotFound):
            self.client.volumes.get(f"{name}-code").remove(force=True)

    # ---------------------------------------------------------------- 执行

    async def exec(self, ws: Workspace, cmd: list[str], timeout: int, cwd: str | None = None) -> CommandResult:
        # 超时由容器内的 timeout 命令执行：杀掉的是沙箱里的进程树，而不只是 TwinFix 侧的等待
        wrapped = ["timeout", "--kill-after=10", str(timeout), *cmd]
        workdir = str(PurePosixPath(WORKDIR) / cwd) if cwd else WORKDIR
        return await asyncio.to_thread(self._exec, self._name(ws), wrapped, workdir=workdir)

    def _exec(self, name: str, cmd: list[str], *, workdir: str = WORKDIR) -> CommandResult:
        start = time.monotonic()
        container = self._container(name)
        result = container.exec_run(cmd, workdir=workdir, demux=True, user=SANDBOX_UID)
        out, err = result.output if isinstance(result.output, tuple) else (result.output, b"")
        code = int(result.exit_code if result.exit_code is not None else -1)
        return CommandResult(
            exit_code=code,
            stdout=_decode(out),
            stderr=_decode(err),
            duration_ms=int((time.monotonic() - start) * 1000),
            timed_out=code in (124, 137),
        )

    # ---------------------------------------------------------------- 文件

    async def read_file(self, ws: Workspace, path: str) -> str:
        res = await asyncio.to_thread(self._exec, self._name(ws), ["cat", "--", _container_path(path)])
        if res.exit_code != 0:
            raise FileNotFoundError(res.stderr.strip() or path)
        return res.stdout

    async def write_file(self, ws: Workspace, path: str, content: str) -> None:
        target = _container_path(path)
        await asyncio.to_thread(self._put, self._name(ws), target, content.encode("utf-8"))

    def _put(self, name: str, target: str, data: bytes, mode: int = 0o644) -> None:
        container = self._container(name)
        parent = str(PurePosixPath(target).parent)
        mk = container.exec_run(["mkdir", "-p", parent], user=SANDBOX_UID)
        if mk.exit_code != 0:
            raise PermissionError(f"cannot create {parent}: {_decode(mk.output)}")
        # 写入后再校验：拒绝通过已存在的符号链接写到工作区外
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(PurePosixPath(target).name)
            info.size = len(data)
            info.mode = mode
            # 必须设当前时间：默认 mtime=0 会让 Maven 增量编译认为源码比 .class 旧而跳过重编译
            info.mtime = int(time.time())
            info.uid, info.gid = 10001, 10001
            tar.addfile(info, io.BytesIO(data))
        container.put_archive(parent, buf.getvalue())
        real = container.exec_run(["realpath", "-m", target], user=SANDBOX_UID)
        resolved = _decode(real.output).strip()
        if target.startswith(WORKDIR) and not resolved.startswith(WORKDIR):
            container.exec_run(["rm", "-f", target], user=SANDBOX_UID)
            raise PathEscapeError(f"path escapes workspace via symlink: {target}")

    async def list_dir(self, ws: Workspace, path: str = ".") -> list[str]:
        target = _container_path(path)
        res = await asyncio.to_thread(
            self._exec, self._name(ws), ["find", target, "-mindepth", "1", "-maxdepth", "1", "-not", "-name", ".git"]
        )
        if res.exit_code != 0:
            raise FileNotFoundError(res.stderr.strip() or path)
        prefix = WORKDIR + "/"
        return sorted(line.removeprefix(prefix) for line in res.stdout.splitlines() if line)

    async def search(self, ws: Workspace, pattern: str, glob: str = "**/*") -> list[tuple[str, int, str]]:
        include = glob.rsplit("/", 1)[-1] if glob not in ("**/*", "*") else "*"
        cmd = [
            "grep",
            "-rnIF",
            "--exclude-dir=.git",
            "--exclude-dir=target",
            f"--include={include}",
            "-e",
            pattern,
            ".",
        ]
        res = await asyncio.to_thread(self._exec, self._name(ws), cmd)
        hits: list[tuple[str, int, str]] = []
        for line in res.stdout.splitlines():
            file, _, rest = line.partition(":")
            no, _, text = rest.partition(":")
            if no.isdigit():
                hits.append((file.removeprefix("./"), int(no), text.strip()))
        if glob not in ("**/*", "*"):
            from twinfix.gateway.paths import matches

            hits = [h for h in hits if matches(h[0], [glob])]
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
        return await asyncio.to_thread(self._read_sources, self._name(ws), suffixes, exclude_dirs, max_files, max_bytes)

    def _read_sources(
        self,
        name: str,
        suffixes: tuple[str, ...],
        exclude_dirs: tuple[str, ...],
        max_files: int,
        max_bytes: int,
    ) -> tuple[dict[str, str], bool]:
        import re

        for suffix in suffixes:
            if not re.fullmatch(r"\.[A-Za-z0-9]+", suffix):
                raise ValueError(f"invalid source suffix: {suffix!r}")
        for directory in exclude_dirs:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", directory):
                raise ValueError(f"invalid exclude dir: {directory!r}")
        names = " -o ".join(f"-name '*{suffix}'" for suffix in suffixes)
        excludes = "".join(f" -not -path './{d}/*'" for d in exclude_dirs)
        # 一次 exec 打包整棵源码树：逐文件 cat 在真实仓库上会产生上千次 exec。
        # 主仓库先打包，再打包 _repos/，保证 max_files 名额优先给主仓库。
        primary = f"find . -path './_repos' -prune -o -type f \\( {names} \\){excludes} -size -512k -print0"
        extras = f"find ./_repos -type f \\( {names} \\) -size -512k -print0 2>/dev/null || true"
        script = f"set -e; {{ {primary}; {extras}; }} | head -z -n {int(max_files)} | tar --null -T - -cf -"
        container = self._container(name)
        # demux=True：stdout 是 tar 二进制，stderr 单独取；合并会把警告混进归档导致解析失败
        result = container.exec_run(["sh", "-c", script], workdir=WORKDIR, demux=True, user=SANDBOX_UID)
        raw_out, raw_err = result.output if isinstance(result.output, tuple) else (result.output, b"")
        data = raw_out or b""
        if result.exit_code != 0 and not data:
            if b"Cowardly" in (raw_err or b"") or b"empty archive" in (raw_err or b""):
                return {}, False
            raise RuntimeError(f"cannot read source tree: {_decode(raw_err)}")
        files, truncated = _read_tar_sources(data, suffixes, max_files, max_bytes)
        # head -n 截断发生在容器内，本地无法知道后面还有没有文件；命中上限就保守标记
        if len(files) >= max_files:
            truncated = True
        return files, truncated

    async def diff(self, ws: Workspace) -> str:
        # 与修复起点比较，包含已提交与未提交的改动以及新文件
        await self.git(ws, ["add", "-A", "-N", "."], 60)
        res = await self.git(ws, ["diff", "--no-color", BASE_TAG], 60)
        return res.stdout

    # ---------------------------------------------------------------- 系统操作（不对 Agent 暴露）

    async def git(self, ws: Workspace, args: list[str], timeout: int = 300, cwd: str | None = None) -> CommandResult:
        cmd = ["timeout", str(timeout), "git", "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false", *args]
        workdir = str(PurePosixPath(WORKDIR) / cwd) if cwd else WORKDIR
        return await asyncio.to_thread(self._exec, self._name(ws), cmd, workdir=workdir)

    async def put_bytes(self, ws: Workspace, path: str, data: bytes) -> None:
        await asyncio.to_thread(self._put, self._name(ws), _container_path(path), data)

    async def get_bytes(self, ws: Workspace, path: str) -> bytes:
        return await asyncio.to_thread(self._get_file, self._name(ws), _container_path(path))

    def _get_file(self, name: str, path: str) -> bytes:
        stream, _ = self._container(name).get_archive(path)
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
            member = tar.next()
            f = tar.extractfile(member) if member else None
            if f is None:
                raise FileNotFoundError(path)
            return f.read()

    # ---------------------------------------------------------------- 内部

    def _container(self, name: str) -> Container:
        try:
            container = self.client.containers.get(name)
        except NotFound as exc:
            raise RuntimeError(f"workspace container {name} not found") from exc
        if container.status != "running":
            # 宿主机/Docker 重启后容器会停在 exited，命名卷里的代码还在；直接拉起继续用
            try:
                container.start()
            except docker.errors.APIError as exc:
                raise RuntimeError(f"workspace container {name} cannot be started: {exc}") from exc
            container.reload()
            if container.status != "running":
                raise RuntimeError(f"workspace container {name} is {container.status}, expected running")
        return container

    @staticmethod
    def _name(ws: Workspace) -> str:
        if not ws.image or not ws.image.startswith("twinfix-ws-"):
            raise RuntimeError("workspace is not a docker workspace")
        return ws.image

    def gc(self, keep: set[str]) -> list[str]:
        """回收不再被任何事故引用的工作区容器（进程崩溃遗留）。"""
        removed = []
        for c in self.client.containers.list(all=True, filters={"label": LABEL}):
            ws_id: Any = c.labels.get(LABEL)
            if ws_id not in keep:
                self._destroy(c.name)
                removed.append(c.name)
        return removed


def _read_tar_sources(
    data: bytes, suffixes: tuple[str, ...], max_files: int, max_bytes: int
) -> tuple[dict[str, str], bool]:
    files: dict[str, str] = {}
    total = 0
    truncated = False
    if not data:
        return files, truncated
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            for member in tar:
                if not member.isfile() or not member.name.endswith(suffixes):
                    continue
                if len(files) >= max_files:
                    truncated = True
                    break
                if member.size > _MAX_SOURCE_FILE_BYTES:
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                chunk = handle.read()
                if total + len(chunk) > max_bytes:
                    truncated = True
                    break
                files[member.name.removeprefix("./")] = chunk.decode("utf-8", errors="replace")
                total += len(chunk)
    except tarfile.TarError as exc:
        raise RuntimeError(f"cannot parse source archive: {exc}") from exc
    return files, truncated


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    if len(text) > _MAX_OUTPUT:
        return text[: _MAX_OUTPUT // 2] + "\n…[output truncated]…\n" + text[-_MAX_OUTPUT // 2 :]
    return text
