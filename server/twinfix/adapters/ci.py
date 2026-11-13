"""CI 适配器。真实实现为 JenkinsCI 与 GitLabCI，只能触发 twinfix-twin-* 任务。"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Protocol

from twinfix.adapters.types import BuildRef, BuildState, BuildStatus

ALLOWED_JOB_PREFIX = "twinfix-twin-"


class JobNotAllowedError(PermissionError):
    pass


def assert_allowed_job(job: str) -> None:
    if not job.startswith(ALLOWED_JOB_PREFIX):
        raise JobNotAllowedError(f"refusing to trigger job {job!r}")


class CIProvider(Protocol):
    async def trigger(self, job: str, params: dict[str, Any]) -> BuildRef: ...
    async def status(self, ref: BuildRef) -> BuildStatus: ...
    async def logs(self, ref: BuildRef, tail: int = 500) -> str: ...


@dataclass
class FakeCI:
    """触发即成功。`outcome` 可在测试中改为 failure 模拟部署失败。"""

    outcome: BuildState = "success"
    builds: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    _seq: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    async def trigger(self, job: str, params: dict[str, Any]) -> BuildRef:
        assert_allowed_job(job)
        ref = BuildRef(job=job, id=str(next(self._seq)))
        self.builds[ref.id] = (job, params)
        return ref

    async def status(self, ref: BuildRef) -> BuildStatus:
        return BuildStatus(ref=ref, state=self.outcome)

    async def logs(self, ref: BuildRef, tail: int = 500) -> str:
        job, params = self.builds.get(ref.id, ("?", {}))
        return f"[fake-ci] {job} #{ref.id} params={params} -> {self.outcome}"
