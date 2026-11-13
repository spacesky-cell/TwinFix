"""孪生环境适配器。真实实现为 ComposeTwinProvider / KubernetesTwinProvider。"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from twinfix.adapters.types import HttpRequest, HttpResponse, TwinHandle, TwinSpec
from twinfix.ids import short_id


def compose_project_name(incident_id: str) -> str:
    return f"twinfix-{short_id(incident_id)}"


class TwinProvider(Protocol):
    async def create(self, spec: TwinSpec) -> TwinHandle: ...
    async def wait_ready(self, twin: TwinHandle, timeout: int) -> bool: ...
    async def exec_request(self, twin: TwinHandle, req: HttpRequest) -> HttpResponse: ...
    async def logs(self, twin: TwinHandle, service: str, since: datetime) -> str: ...
    async def probe(self, twin: TwinHandle, service: str, command: str, timeout_s: int) -> str: ...
    async def destroy(self, twin: TwinHandle) -> None: ...


@dataclass
class FakeTwinProvider:
    port_base: int = 31000
    live: dict[str, TwinHandle] = field(default_factory=dict)
    destroyed: list[str] = field(default_factory=list)
    _seq: itertools.count[int] = field(default_factory=lambda: itertools.count(0))

    async def create(self, spec: TwinSpec) -> TwinHandle:
        name = compose_project_name(spec.incident_id)
        port = self.port_base + next(self._seq) * 10
        handle = TwinHandle(
            compose_project=name,
            host="localhost",
            ports={"gateway": port},
            urls={"gateway": f"http://localhost:{port}"},
            sha=spec.sha,
        )
        self.live[name] = handle
        return handle

    async def wait_ready(self, twin: TwinHandle, timeout: int) -> bool:
        return twin.compose_project in self.live

    async def exec_request(self, twin: TwinHandle, req: HttpRequest) -> HttpResponse:
        return HttpResponse(status=200, headers={}, body="{}", duration_ms=1)

    async def logs(self, twin: TwinHandle, service: str, since: datetime) -> str:
        return ""

    async def probe(self, twin: TwinHandle, service: str, command: str, timeout_s: int) -> str:
        return f'[fake arthas@{service}] {command}\n"main" #1 prio=5 tid=0x1 nid=NA waiting\n'

    async def destroy(self, twin: TwinHandle) -> None:
        if self.live.pop(twin.compose_project, None) is not None:
            self.destroyed.append(twin.compose_project)
