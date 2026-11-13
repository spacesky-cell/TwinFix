"""DockerSandbox 的容器恢复：宿主机/Docker 重启后工作区容器停在 exited，应按需拉起。

不依赖本机 Docker：用假的 client/container 覆盖状态判断与异常分支。
"""

from __future__ import annotations

import docker
import pytest
from docker.errors import NotFound

from twinfix.adapters.docker_sandbox import DockerSandbox


class _FakeContainer:
    def __init__(self, status: str, fail_start: Exception | None = None) -> None:
        self.status = status
        self.started = 0
        self.reloaded = 0
        self._fail_start = fail_start

    def start(self) -> None:
        self.started += 1
        if self._fail_start is not None:
            raise self._fail_start
        self.status = "running"

    def reload(self) -> None:
        self.reloaded += 1


class _FakeContainers:
    def __init__(self, container: _FakeContainer | None) -> None:
        self._container = container

    def get(self, name: str) -> _FakeContainer:
        if self._container is None:
            raise NotFound(f"no such container {name}")
        return self._container


def _sandbox(container: _FakeContainer | None) -> DockerSandbox:
    sandbox = object.__new__(DockerSandbox)
    sandbox.client = type("Client", (), {"containers": _FakeContainers(container)})()
    return sandbox


def test_running_container_is_used_without_restart() -> None:
    container = _FakeContainer("running")
    assert _sandbox(container)._container("ws") is container
    assert container.started == 0


def test_exited_container_is_started() -> None:
    container = _FakeContainer("exited")
    assert _sandbox(container)._container("ws") is container
    assert container.started == 1
    assert container.reloaded == 1


def test_container_that_cannot_start_raises() -> None:
    container = _FakeContainer("exited", fail_start=docker.errors.APIError("boom"))
    with pytest.raises(RuntimeError, match="cannot be started"):
        _sandbox(container)._container("ws")


def test_missing_container_raises() -> None:
    with pytest.raises(RuntimeError, match="not found"):
        _sandbox(None)._container("ws")
