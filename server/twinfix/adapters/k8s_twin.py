"""KubernetesTwinProvider：用 kubectl 在独立 Namespace 中为每个事故拉起孪生环境。

与 Compose 孪生的差异
- 不做源码构建：镜像必须由 CI 预先构建并推送到镜像仓库（项目配置 ``twin.image`` 或
  ``TWINFIX_K8S_DEFAULT_IMAGE``）；Manifest 里用 ``${IMAGE}`` 引用。
- 每个孪生一个 Namespace（``twinfix-<短id>``），销毁时整组资源一起删除；Service 以 NodePort
  暴露，worker 通过 ``TWINFIX_K8S_NODE_HOST:<NodePort>`` 访问。
- Manifest 是多文档 YAML，支持 ``${NAMESPACE}`` / ``${TWIN_ID}`` / ``${INCIDENT_ID}`` /
  ``${REPO}`` / ``${SHA}`` / ``${DATA_SNAPSHOT}`` / ``${IMAGE}`` / ``${PORT_<SERVICE>}``
  以及项目配置里标量字段的 ``${TWIN_<KEY>}`` 占位符；未知占位符直接报错，避免拼错后静默下发。

无集群环境下只通过注入的 runner 做单元测试；真实集群联调需配置 kubeconfig 与节点地址。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from twinfix.adapters.arthas import check_probe_output, clean_telnet, telnet_command
from twinfix.adapters.twin import compose_project_name
from twinfix.adapters.types import HttpRequest, HttpResponse, TwinHandle, TwinSpec

log = logging.getLogger(__name__)

# kubectl 调用注入点：测试用假 runner 替换，生产默认在线程里跑同步子进程
KubectlRunner = Callable[[list[str], dict[str, str]], Awaitable[tuple[int, str, str]]]

_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_RESERVED_TWIN_KEYS = frozenset({"image", "directory", "manifest_file", "services", "health_path"})


@dataclass(frozen=True)
class K8sTwinTemplate:
    """K8s 孪生模板：Manifest 文件 + 对外服务名（Service 与 Deployment 同名）。"""

    directory: Path
    manifest_file: str = "twin.yaml"
    services: tuple[str, ...] = ("order", "inventory")
    health_path: str = "/actuator/health"


def render_manifest(text: str, values: dict[str, str]) -> str:
    """替换 ``${VAR}``；未知变量报错，避免 k8s 收到字面量占位符。"""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ValueError(f"unknown placeholder ${{{key}}} in twin manifest")
        return values[key]

    return _PLACEHOLDER.sub(_sub, text)


async def _default_runner(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    # 线程里跑同步子进程：Windows 上 psycopg 要求 SelectorEventLoop，而它不支持 asyncio 子进程
    proc = await asyncio.to_thread(subprocess.run, argv, env=env, capture_output=True, check=False)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


class KubernetesTwinProvider:
    def __init__(
        self,
        *,
        template: K8sTwinTemplate,
        work_dir: Path,
        node_host: str,
        port_base: int,
        port_span: int,
        max_concurrent: int,
        default_image: str = "",
        kubectl: str = "kubectl",
        kubeconfig: Path | None = None,
        context: str = "",
        rollout_timeout_s: int = 600,
        runner: KubectlRunner | None = None,
        template_resolver: Callable[[dict[str, Any]], K8sTwinTemplate] | None = None,
    ) -> None:
        if not node_host:
            raise ValueError("twin_provider=k8s requires TWINFIX_K8S_NODE_HOST (node address reachable from worker)")
        max_port = port_base + max_concurrent * port_span
        if port_base < 30000 or max_port - 1 > 32767:
            raise ValueError(
                "twin_port_base/twin_port_span must fit Kubernetes NodePort range 30000-32767 "
                f"(got {port_base}..{max_port - 1})"
            )
        self.template = template
        self.work_dir = work_dir
        self.node_host = node_host
        self.port_base = port_base
        self.port_span = port_span
        self.max_concurrent = max_concurrent
        self.default_image = default_image
        self.kubectl = kubectl
        self.kubeconfig = kubeconfig
        self.context = context
        self.rollout_timeout_s = rollout_timeout_s
        self.template_resolver = template_resolver
        self._runner = runner or _default_runner
        self._slots_lock = asyncio.Lock()

    # ---------------------------------------------------------------- 生命周期

    async def create(self, spec: TwinSpec) -> TwinHandle:
        template = self._template_for(spec)
        name = compose_project_name(spec.incident_id)
        ctx = self.work_dir / name
        async with self._slots_lock:
            slot = await self._claim_slot(name)
        assigned = {svc: self.port_base + slot * self.port_span + i for i, svc in enumerate(template.services)}
        try:
            ctx.mkdir(parents=True, exist_ok=True)
            manifest = (template.directory / template.manifest_file).read_text(encoding="utf-8")
            image = str(spec.template.get("image") or self.default_image)
            if not image and "${IMAGE}" in manifest:
                raise ValueError(
                    f"twin manifest {template.manifest_file!r} uses ${{IMAGE}} but no image is configured "
                    "(set project.config.twin.image or TWINFIX_K8S_DEFAULT_IMAGE)"
                )
            values = self._values(spec, name, assigned, image)
            rendered = render_manifest(manifest, values)
            rendered_path = ctx / "twin.rendered.yaml"
            rendered_path.write_text(rendered, encoding="utf-8")

            await self._create_namespace(name)
            code, _, err = await self._kubectl("apply", "-n", name, "-f", str(rendered_path))
            if code != 0:
                raise RuntimeError(f"twin {name} apply failed: {err[-3000:]}")
            ports = {svc: await self._node_port(name, svc) for svc in template.services}
            for svc in template.services:
                code, _, err = await self._kubectl(
                    "rollout",
                    "status",
                    f"deployment/{svc}",
                    "-n",
                    name,
                    f"--timeout={self.rollout_timeout_s}s",
                )
                if code != 0:
                    raise RuntimeError(f"twin {name} service {svc} not ready: {err[-3000:]}")
        except Exception:
            if await self._delete_namespace(name):
                await asyncio.to_thread(shutil.rmtree, ctx, True)
            raise
        return TwinHandle(
            compose_project=name,
            host=self.node_host,
            ports=ports,
            urls={svc: f"http://{self.node_host}:{port}" for svc, port in ports.items()},
            sha=spec.sha,
            health_path=template.health_path,
        )

    async def wait_ready(self, twin: TwinHandle, timeout: int) -> bool:
        deadline = time.monotonic() + timeout
        health_path = twin.health_path or "/actuator/health"
        async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
            while time.monotonic() < deadline:
                try:
                    statuses = [(await http.get(f"{url}{health_path}")).status_code for url in twin.urls.values()]
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
        if service not in twin.ports:
            raise ValueError(f"unknown twin service {service!r}; available: {sorted(twin.ports)}")
        since_utc = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
        since_arg = since_utc.isoformat(timespec="seconds").replace("+00:00", "Z")
        _, out, err = await self._kubectl(
            "logs",
            "-n",
            twin.compose_project,
            f"deployment/{service}",
            f"--since-time={since_arg}",
            "--tail=500",
        )
        return out or err

    async def probe(self, twin: TwinHandle, service: str, command: str, timeout_s: int) -> str:
        """在 Pod 内用 Arthas telnet 客户端执行一条只读命令（与 Compose 孪生同一套白名单）。"""
        if service not in twin.ports:
            raise ValueError(f"unknown twin service {service!r}; available: {sorted(twin.ports)}")
        code, out, err = await self._kubectl(
            "exec",
            "-n",
            twin.compose_project,
            f"deployment/{service}",
            "--",
            *telnet_command(command, timeout_s),
        )
        text = clean_telnet(out + err)
        return check_probe_output(code, text, command)

    async def destroy(self, twin: TwinHandle) -> None:
        # 删除成功才回收槽位：失败时保留 slot 目录，容量核算仍认为它被占用，
        # 避免同一 NodePort 被下一个孪生复用；等 Namespace 真正消失后由容量回收逻辑清理。
        if await self._delete_namespace(twin.compose_project):
            ctx = self.work_dir / twin.compose_project
            if ctx.exists():
                await asyncio.to_thread(shutil.rmtree, ctx, True)

    # ---------------------------------------------------------------- 内部

    async def _kubectl(self, *args: str) -> tuple[int, str, str]:
        argv = [self.kubectl]
        if self.context:
            argv.extend(["--context", self.context])
        argv.extend(args)
        env = {**os.environ}
        if self.kubeconfig is not None:
            env["KUBECONFIG"] = str(self.kubeconfig)
        return await self._runner(argv, env)

    async def _create_namespace(self, name: str) -> None:
        code, _, err = await self._kubectl("create", "namespace", name)
        if code != 0 and "AlreadyExists" not in err:
            raise RuntimeError(f"failed to create namespace {name}: {err[-2000:]}")

    async def _delete_namespace(self, name: str) -> bool:
        code, _, err = await self._kubectl("delete", "namespace", name, "--ignore-not-found", "--wait=false")
        if code != 0:
            # 与 Compose 的 down 一致：销毁失败不阻塞事故收尾；槽位保留，交给集群侧 GC / 人工清理
            log.warning("failed to delete twin namespace %s: %s", name, err[-1000:])
            return False
        return True

    async def _node_port(self, namespace: str, service: str) -> int:
        code, out, err = await self._kubectl(
            "get", "service", service, "-n", namespace, "-o", "jsonpath={.spec.ports[0].nodePort}"
        )
        port = out.strip()
        if code != 0 or not port.isdigit():
            raise RuntimeError(
                f"twin service {service!r} in namespace {namespace} has no NodePort "
                f"(set spec.type=NodePort): {err[-1000:]}"
            )
        return int(port)

    def _values(self, spec: TwinSpec, name: str, ports: dict[str, int], image: str) -> dict[str, str]:
        values = {
            "NAMESPACE": name,
            "TWIN_ID": name,
            "INCIDENT_ID": spec.incident_id,
            "REPO": spec.repo,
            "SHA": spec.sha,
            "DATA_SNAPSHOT": spec.data_snapshot or "",
            "IMAGE": image,
            **{f"PORT_{svc.upper()}": str(port) for svc, port in ports.items()},
        }
        for key, raw in spec.template.items():
            if key in _RESERVED_TWIN_KEYS or not isinstance(raw, str | int | float | bool):
                continue
            values[f"TWIN_{key.upper()}"] = str(raw)
        return values

    def _template_for(self, spec: TwinSpec) -> K8sTwinTemplate:
        if self.template_resolver is None or not spec.template:
            return self.template
        return self.template_resolver(spec.template)

    async def _claim_slot(self, name: str) -> int:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        used: dict[int, str] = {}
        for d in self.work_dir.iterdir():
            slot_file = d / "slot"
            if d.is_dir() and slot_file.exists():
                used[int(slot_file.read_text(encoding="utf-8"))] = d.name
        for slot, owner in used.items():
            if owner == name:
                return slot
        for slot in range(self.max_concurrent):
            if slot not in used:
                (self.work_dir / name).mkdir(parents=True, exist_ok=True)
                (self.work_dir / name / "slot").write_text(str(slot), encoding="utf-8")
                return slot
        # 回收“有 slot 文件但 Namespace 已不存在”的槽位（进程中断/手工删除后的残留）
        for slot, owner in sorted(used.items()):
            if not await self._project_exists(owner):
                shutil.rmtree(self.work_dir / owner, True)
                (self.work_dir / name).mkdir(parents=True, exist_ok=True)
                (self.work_dir / name / "slot").write_text(str(slot), encoding="utf-8")
                return slot
        raise RuntimeError(f"twin capacity reached ({self.max_concurrent})")

    async def _project_exists(self, name: str) -> bool:
        code, out, err = await self._kubectl("get", "namespace", name, "-o", "name")
        if code == 0:
            return bool(out.strip())
        # kubectl 不可用等异常情况：保守保留槽位；只有明确 NotFound 才允许回收
        return "NotFound" not in err and "not found" not in err.lower()
