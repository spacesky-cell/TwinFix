"""KubernetesTwinProvider：Manifest 渲染、NodePort 解析、槽位、失败清理与日志/探针。

无集群：通过注入的 kubectl runner 验证命令序列与行为；真实集群联调已在文档中标注为待办。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from twinfix.adapters.k8s_twin import K8sTwinTemplate, KubernetesTwinProvider, render_manifest
from twinfix.adapters.twin import compose_project_name
from twinfix.adapters.types import TwinSpec

MANIFEST = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: order
spec:
  template:
    spec:
      containers:
        - name: order
          image: ${IMAGE}
---
apiVersion: v1
kind: Service
metadata:
  name: order
spec:
  type: NodePort
  ports:
    - port: 8080
      nodePort: ${PORT_ORDER}
---
apiVersion: v1
kind: Service
metadata:
  name: inventory
spec:
  type: NodePort
  ports:
    - port: 8081
      nodePort: ${PORT_INVENTORY}
"""


class FakeKubectl:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.namespaces: set[str] = set()
        self.node_ports = {"order": "31234", "inventory": "31235"}
        self.apply_rc = 0
        self.delete_rc = 0
        self.logs_output = "2026-11-11T12:00:00Z INFO order started\n"
        self.probe_output = (
            "Welcome to Arthas!\r\n"
            "[arthas@1]$ thread -n 1\r\n"
            '"main" #1 prio=5 tid=0x1 nid=NA waiting\r\n'
            "[arthas@1]$ \r\n"
        )

    async def __call__(self, argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        self.calls.append((argv, env))
        args = list(argv[1:])
        if args[:2] == ["--context", "kind-test"]:
            args = args[2:]
        if args[:2] == ["create", "namespace"]:
            self.namespaces.add(args[2])
            return 0, f"namespace/{args[2]} created", ""
        if args[0] == "apply":
            return (self.apply_rc, "", "" if self.apply_rc == 0 else "apply failed")
        if args[:2] == ["get", "service"]:
            return 0, self.node_ports.get(args[2], ""), ""
        if args[:2] == ["rollout", "status"]:
            return 0, "deployment successfully rolled out", ""
        if args[0] == "logs":
            return 0, self.logs_output, ""
        if args[0] == "exec":
            return 0, self.probe_output, ""
        if args[:2] == ["delete", "namespace"]:
            if self.delete_rc != 0:
                return self.delete_rc, "", "forbidden"
            self.namespaces.discard(args[2])
            return 0, "", ""
        if args[:2] == ["get", "namespace"]:
            if args[2] in self.namespaces:
                return 0, f"namespace/{args[2]}", ""
            return 1, "", f'Error from server (NotFound): namespaces "{args[2]}" not found'
        return 1, "", "unexpected kubectl command: " + " ".join(args)


def make_provider(tmp_path: Path, runner: FakeKubectl, **kwargs: Any) -> KubernetesTwinProvider:
    template_dir = tmp_path / "template"
    template_dir.mkdir(exist_ok=True)
    (template_dir / "twin.yaml").write_text(MANIFEST, encoding="utf-8")
    params: dict[str, Any] = {
        "template": K8sTwinTemplate(directory=template_dir, services=("order", "inventory")),
        "work_dir": tmp_path / "twins",
        "node_host": "node.example",
        "port_base": 31000,
        "port_span": 10,
        "max_concurrent": 2,
        "default_image": "registry.example/twin:test",
        "kubectl": "kubectl",
        "context": "kind-test",
        "runner": runner,
    }
    params.update(kwargs)
    return KubernetesTwinProvider(**params)


def spec(**kwargs: Any) -> TwinSpec:
    params: dict[str, Any] = {"incident_id": "inc-1234", "repo": "acme/demo", "sha": "abc123"}
    params.update(kwargs)
    return TwinSpec(**params)


async def test_create_renders_manifest_and_returns_handle(tmp_path: Path) -> None:
    runner = FakeKubectl()
    provider = make_provider(tmp_path, runner)

    handle = await provider.create(spec(template={"image": "registry.example/acme:abc123"}))

    assert handle.compose_project == compose_project_name("inc-1234")
    assert handle.ports == {"order": 31234, "inventory": 31235}
    assert handle.urls["order"] == "http://node.example:31234"
    assert handle.health_path == "/actuator/health"
    rendered = (tmp_path / "twins" / handle.compose_project / "twin.rendered.yaml").read_text(encoding="utf-8")
    assert "nodePort: 31000" in rendered and "nodePort: 31001" in rendered
    assert "registry.example/acme:abc123" in rendered
    cmds = [" ".join(argv) for argv, _ in runner.calls]
    assert f"kubectl --context kind-test create namespace {handle.compose_project}" in cmds
    assert "apply -n" in " ".join(cmds)
    assert sum(cmd.startswith("kubectl --context kind-test rollout status") for cmd in cmds) == 2


async def test_kubeconfig_and_context_reach_kubectl(tmp_path: Path) -> None:
    runner = FakeKubectl()
    kubeconfig = tmp_path / "kubeconfig"
    provider = make_provider(tmp_path, runner, kubeconfig=kubeconfig)

    await provider.create(spec())

    argv, env = runner.calls[0]
    assert argv[:3] == ["kubectl", "--context", "kind-test"]
    assert env["KUBECONFIG"] == str(kubeconfig)


async def test_image_is_required_when_manifest_uses_placeholder(tmp_path: Path) -> None:
    provider = make_provider(tmp_path, FakeKubectl(), default_image="")

    with pytest.raises(ValueError, match="no image is configured"):
        await provider.create(spec())


async def test_apply_failure_cleans_up_namespace_and_slot(tmp_path: Path) -> None:
    runner = FakeKubectl()
    runner.apply_rc = 1
    provider = make_provider(tmp_path, runner)

    with pytest.raises(RuntimeError, match="apply failed"):
        await provider.create(spec())

    name = compose_project_name("inc-1234")
    assert name not in runner.namespaces
    assert any("delete namespace" in " ".join(argv) for argv, _ in runner.calls)
    assert not (tmp_path / "twins" / name).exists()


async def test_service_without_node_port_fails(tmp_path: Path) -> None:
    runner = FakeKubectl()
    runner.node_ports["order"] = ""
    provider = make_provider(tmp_path, runner)

    with pytest.raises(RuntimeError, match="has no NodePort"):
        await provider.create(spec())


async def test_destroy_deletes_namespace_and_workdir(tmp_path: Path) -> None:
    runner = FakeKubectl()
    provider = make_provider(tmp_path, runner)
    handle = await provider.create(spec())

    await provider.destroy(handle)

    assert handle.compose_project not in runner.namespaces
    assert not (tmp_path / "twins" / handle.compose_project).exists()


async def test_destroy_keeps_slot_when_namespace_delete_fails(tmp_path: Path) -> None:
    """删除失败时不能释放槽位，否则下一个孪生可能复用同一个 NodePort。"""
    runner = FakeKubectl()
    provider = make_provider(tmp_path, runner)
    handle = await provider.create(spec())
    runner.delete_rc = 1

    await provider.destroy(handle)

    assert handle.compose_project in runner.namespaces
    assert (tmp_path / "twins" / handle.compose_project / "slot").exists()


async def test_logs_use_rfc3339_since_time(tmp_path: Path) -> None:
    runner = FakeKubectl()
    provider = make_provider(tmp_path, runner)
    handle = await provider.create(spec())

    text = await provider.logs(handle, "order", datetime(2026, 11, 11, 12, 0, tzinfo=UTC))

    assert text == runner.logs_output
    logs_argv = next(argv for argv, _ in runner.calls if argv[3] == "logs")
    assert "deployment/order" in logs_argv
    assert "--since-time=2026-11-11T12:00:00Z" in logs_argv


async def test_probe_cleans_arthas_output(tmp_path: Path) -> None:
    runner = FakeKubectl()
    provider = make_provider(tmp_path, runner)
    handle = await provider.create(spec())

    out = await provider.probe(handle, "order", "thread -n 1", timeout_s=10)

    assert out == '"main" #1 prio=5 tid=0x1 nid=NA waiting'
    exec_argv = next(argv for argv, _ in runner.calls if argv[3] == "exec")
    assert exec_argv[-7:] == [
        "com.taobao.arthas.client.TelnetConsole",
        "-t",
        "10000",
        "-c",
        "thread -n 1",
        "127.0.0.1",
        "3658",
    ]


async def test_stale_slot_is_reclaimed_when_namespace_is_gone(tmp_path: Path) -> None:
    runner = FakeKubectl()
    stale = tmp_path / "twins" / "twinfix-stale"
    stale.mkdir(parents=True)
    (stale / "slot").write_text("0", encoding="utf-8")
    provider = make_provider(tmp_path, runner, max_concurrent=1)

    handle = await provider.create(spec())

    rendered = (tmp_path / "twins" / handle.compose_project / "twin.rendered.yaml").read_text(encoding="utf-8")
    assert "nodePort: 31000" in rendered
    assert not stale.exists()


def test_port_range_must_fit_node_port_range(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="NodePort range"):
        make_provider(tmp_path, FakeKubectl(), port_base=29000)


def test_render_manifest_rejects_unknown_placeholder() -> None:
    with pytest.raises(ValueError, match=r"unknown placeholder \$\{NOPE\}"):
        render_manifest("x: ${NOPE}", {})
