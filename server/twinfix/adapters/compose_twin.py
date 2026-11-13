"""ComposeTwinProvider：用 docker compose 为每个事故拉起一份独立的孪生环境（docs/05-safety.md §4）。

- 项目名 twinfix-<短id>，独立网络与数据库；端口从配置的端口段按槽位分配，全局并发上限。
- 构建上下文 = 指定提交的源码（从宿主机镜像仓库 git archive 导出）+ 模板目录里的 Dockerfile 等文件。
- 端口映射写在 compose 模板里，只绑 127.0.0.1。
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from twinfix.adapters.arthas import check_probe_output, clean_telnet, telnet_command
from twinfix.adapters.gitutil import git, git_bytes
from twinfix.adapters.twin import compose_project_name
from twinfix.adapters.types import HttpRequest, HttpResponse, TwinHandle, TwinSpec
from twinfix.workspace import RemoteSource


class TwinCapacityError(RuntimeError):
    pass


# 每次装配上下文时 compose 文件都落成这个名字，与模板里的原始文件名无关
_COMPOSE_FILE = "compose.twin.yml"


@dataclass(frozen=True)
class TwinTemplate:
    """孪生环境模板：compose 文件 + 需要随源码一起放进构建上下文的文件；services 为对外暴露的服务。"""

    directory: Path
    compose_file: str = "compose.twin.yml"
    services: tuple[str, ...] = ("order", "inventory")
    extra_files: tuple[Path, ...] = ()


class ComposeTwinProvider:
    def __init__(
        self,
        *,
        template: TwinTemplate,
        source: RemoteSource,
        work_dir: Path,
        mirror_dir: Path,
        port_base: int,
        port_span: int,
        max_concurrent: int,
        host: str = "127.0.0.1",
        service_mem_limit: str = "768m",
        service_cpus: float = 1.0,
        db_mem_limit: str = "256m",
        template_resolver: Callable[[dict[str, Any]], TwinTemplate] | None = None,
    ) -> None:
        self.template = template
        self.source = source
        self.work_dir = work_dir
        self.mirror_dir = mirror_dir
        self.port_base = port_base
        self.port_span = port_span
        self.max_concurrent = max_concurrent
        self.host = host
        self.service_mem_limit = service_mem_limit
        self.service_cpus = service_cpus
        self.db_mem_limit = db_mem_limit
        # 把 TwinSpec.template（项目配置）翻译成模板；缺省回落到全局模板
        self.template_resolver = template_resolver
        self._slots_lock = asyncio.Lock()

    # ---------------------------------------------------------------- 生命周期

    async def create(self, spec: TwinSpec) -> TwinHandle:
        template = self._template_for(spec)
        name = compose_project_name(spec.incident_id)
        ctx = self.work_dir / name
        async with self._slots_lock:
            slot = self._claim_slot(name)
        ports = {svc: self.port_base + slot * self.port_span + i for i, svc in enumerate(template.services)}
        await self._prepare_context(ctx, spec, template)
        (ctx / "ports.env").write_text("".join(f"PORT_{k.upper()}={v}\n" for k, v in ports.items()), encoding="utf-8")
        try:
            res = await self._compose(ctx, name, ports, "up", "-d", "--build", "--wait", "--wait-timeout", "600")
        except Exception:
            # 启动过程本身失败（docker 不可用 / 超时）也要释放槽位，否则每失败一次就永久少一个并发名额
            await asyncio.to_thread(shutil.rmtree, ctx, True)
            raise
        if res[0] != 0:
            logs = await self._compose(ctx, name, ports, "logs", "--tail", "80")
            await self._compose(ctx, name, ports, "down", "-v", "--remove-orphans")
            await asyncio.to_thread(shutil.rmtree, ctx, True)
            raise RuntimeError(f"twin {name} failed to start: {res[2][-3000:]}\n{logs[1][-3000:]}")
        return TwinHandle(
            compose_project=name,
            host=self.host,
            ports=ports,
            urls={svc: f"http://{self.host}:{p}" for svc, p in ports.items()},
            sha=spec.sha,
        )

    async def wait_ready(self, twin: TwinHandle, timeout: int) -> bool:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
            while time.monotonic() < deadline:
                try:
                    statuses = [(await http.get(f"{url}/actuator/health")).status_code for url in twin.urls.values()]
                    if all(s == 200 for s in statuses):
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2)
        return False

    async def exec_request(self, twin: TwinHandle, req: HttpRequest) -> HttpResponse:
        base = twin.urls.get(req.service)
        if base is None:
            raise ValueError(f"unknown twin service {req.service!r}; available: {sorted(twin.urls)}")
        start = time.monotonic()
        async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as http:
            body = req.body if isinstance(req.body, dict | list) else None
            content = req.body if isinstance(req.body, str) else None
            resp = await http.request(req.method, req.path, headers=req.headers, json=body, content=content)
        return HttpResponse(
            status=resp.status_code,
            headers=dict(resp.headers),
            body=resp.text[:20000],
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    async def logs(self, twin: TwinHandle, service: str, since: datetime) -> str:
        ctx = self.work_dir / twin.compose_project
        _, out, err = await self._compose(
            ctx, twin.compose_project, twin.ports, "logs", "--no-color", "--since", since.isoformat(), service
        )
        return out or err

    async def destroy(self, twin: TwinHandle) -> None:
        ctx = self.work_dir / twin.compose_project
        if ctx.exists():
            await self._compose(
                ctx, twin.compose_project, twin.ports, "down", "-v", "--remove-orphans", "--rmi", "local"
            )
            await asyncio.to_thread(shutil.rmtree, ctx, True)

    async def probe(self, twin: TwinHandle, service: str, command: str, timeout_s: int) -> str:
        """在孪生容器内用 Arthas telnet 客户端执行一条只读命令（premain 方式，端口只绑容器内 127.0.0.1）。"""
        if service not in twin.ports:
            raise ValueError(f"unknown twin service {service!r}; available: {sorted(twin.ports)}")
        container = f"{twin.compose_project}-{service}-1"
        cmd = [
            "docker",
            "exec",
            container,
            *telnet_command(command, timeout_s),
        ]
        # 同 _compose：线程里跑同步子进程（Windows 上 asyncio 子进程在 SelectorEventLoop 下不可用）
        proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, check=False)
        out = clean_telnet(proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace"))
        return check_probe_output(proc.returncode, out, command)

    # ---------------------------------------------------------------- 内部

    def _claim_slot(self, name: str) -> int:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        used = {}
        for d in self.work_dir.iterdir():
            slot_file = d / "slot"
            if slot_file.exists():
                used[int(slot_file.read_text())] = d.name
        for slot, owner in used.items():
            if owner == name:
                return slot
        for slot in range(self.max_concurrent):
            if slot not in used:
                (self.work_dir / name).mkdir(parents=True, exist_ok=True)
                (self.work_dir / name / "slot").write_text(str(slot))
                return slot
        # 所有槽位都被占：进程被强杀 / 失败构建可能留下“有 slot 文件但没有容器”的目录，
        # 回收这类槽位，避免一次中断就把并发名额永久吃掉。
        for slot, owner in sorted(used.items()):
            if not self._project_exists(owner):
                shutil.rmtree(self.work_dir / owner, True)
                (self.work_dir / name).mkdir(parents=True, exist_ok=True)
                (self.work_dir / name / "slot").write_text(str(slot))
                return slot
        raise TwinCapacityError(f"twin capacity reached ({self.max_concurrent})")

    @staticmethod
    def _project_exists(name: str) -> bool:
        """compose 项目还有运行中的容器就认为槽位仍被占用；docker 不可用时保守保留。"""
        docker = shutil.which("docker") or "docker"
        try:
            proc = subprocess.run(  # noqa: S603 - docker 路径来自 which，参数为常量
                [
                    docker,
                    "ps",
                    "--filter",
                    f"label=com.docker.compose.project={name}",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        return bool(proc.stdout.strip())

    async def _prepare_context(self, ctx: Path, spec: TwinSpec, template: TwinTemplate) -> None:
        src = ctx / "src"
        if src.exists():
            await asyncio.to_thread(shutil.rmtree, src)
        src.mkdir(parents=True)
        mirror = await self._mirror(spec.repo)
        archive = await git_bytes("archive", "--format=tar", spec.sha, cwd=mirror)
        await asyncio.to_thread(_extract, archive, src)
        for f in template.directory.iterdir():
            if f.is_file():
                shutil.copy2(f, ctx / f.name)
        for f in template.extra_files:
            shutil.copy2(f, ctx / f.name)
        # 模板文件统一落成固定名字：后续 logs/destroy 只凭 compose_project 就能找到它，
        # 不依赖创建时用的是项目模板还是默认模板（事故重放时会重新装配上下文）
        compose_src = template.directory / template.compose_file
        compose_dst = ctx / _COMPOSE_FILE
        if compose_src.resolve() != compose_dst.resolve():
            shutil.copy2(compose_src, compose_dst)

    def _template_for(self, spec: TwinSpec) -> TwinTemplate:
        if self.template_resolver is None or not spec.template:
            return self.template
        return self.template_resolver(spec.template)

    async def _mirror(self, repo: str) -> Path:
        path = self.mirror_dir / repo.replace("/", "__")
        url, env = await self.source.remote(repo)
        if not (path / "HEAD").exists():
            path.mkdir(parents=True, exist_ok=True)
            await git("init", "-q", "--bare", str(path))
        await git("fetch", "-q", "--prune", url, "+refs/heads/*:refs/heads/*", cwd=path, env=env)
        return path

    async def _compose(self, ctx: Path, name: str, ports: dict[str, int], *args: str) -> tuple[int, str, str]:
        build_context = await asyncio.to_thread(lambda: str(ctx.resolve()))
        env = {
            **os.environ,
            "COMPOSE_PROJECT_NAME": name,
            "BUILD_CONTEXT": build_context,
            "TWIN_SERVICE_MEM_LIMIT": self.service_mem_limit,
            "TWIN_SERVICE_CPUS": str(self.service_cpus),
            "TWIN_DB_MEM_LIMIT": self.db_mem_limit,
            **{f"PORT_{k.upper()}": str(v) for k, v in ports.items()},
        }
        cmd = ["docker", "compose", "-f", str(ctx / _COMPOSE_FILE), "-p", name, *args]
        # 线程里跑同步子进程：Windows 上 psycopg 要求 SelectorEventLoop，而它不支持 asyncio 子进程
        proc = await asyncio.to_thread(
            subprocess.run,  # argv 列表，无 shell
            cmd,
            env=env,
            capture_output=True,
            check=False,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def _extract(archive: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")  # data 过滤器拒绝绝对路径、.. 与设备文件
