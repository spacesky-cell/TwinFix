"""调度实现。

InlineDispatcher：在当前进程直接驱动引擎，用于测试和单机调试。
RabbitDispatcher：生产实现，见 twinfix.worker.mq。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from twinfix.orchestrator.engine import Engine


class InlineDispatcher:
    def __init__(self) -> None:
        self.engine: Engine | None = None
        self.auto = True  # False 时只记录请求，由测试手动驱动
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def bind(self, engine: Engine) -> None:
        self.engine = engine

    async def advance(self, incident_id: str) -> None:
        self.requests.append(("advance", incident_id, None))
        if self.auto and self.engine is not None:
            await self.engine.drive(incident_id)

    async def resume(self, incident_id: str, decision: dict[str, Any]) -> None:
        self.requests.append(("resume", incident_id, decision))
        if self.auto and self.engine is not None:
            await self.engine.drive(incident_id, resume=decision)
