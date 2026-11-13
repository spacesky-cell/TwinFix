"""编排引擎：对外只暴露"推进一步"。

消息去重靠 checkpoint：每推进一步 checkpoint_id 都会变化，worker 派发的后续消息携带当时的
checkpoint_id，若与当前不一致即为过期消息直接丢弃。这样无论 MQ 重复投递还是多个触发源
（创建、取消、超时巡检）同时发消息，同一事故最终只有一条推进链在走。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from twinfix.orchestrator.graph import build_graph
from twinfix.orchestrator.steps import Steps

log = logging.getLogger(__name__)

AdvanceStatus = Literal["advanced", "waiting", "done", "stale"]


@dataclass(frozen=True)
class AdvanceResult:
    status: AdvanceStatus
    checkpoint_id: str | None
    next_nodes: tuple[str, ...] = ()

    @property
    def should_continue(self) -> bool:
        return self.status == "advanced" and bool(self.next_nodes)


class Engine:
    def __init__(self, steps: Steps, checkpointer: BaseCheckpointSaver[Any]) -> None:
        self.steps = steps
        graph = build_graph(steps.handlers(), steps.finalize)
        # interrupt_after="*"：每个节点执行完即落 checkpoint 并返回，一次调用只推进一步
        self.app = graph.compile(checkpointer=checkpointer, interrupt_after="*")
        self.checkpointer = checkpointer

    @staticmethod
    def _config(incident_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": incident_id}}

    async def status(self, incident_id: str) -> AdvanceResult:
        st = await self.app.aget_state(self._config(incident_id))
        if not st.values:
            return AdvanceResult("advanced", None, ("new",))  # 尚未开始
        cp = st.config["configurable"].get("checkpoint_id")
        if not st.next:
            return AdvanceResult("done", cp)
        if any(t.interrupts for t in st.tasks):
            return AdvanceResult("waiting", cp, tuple(st.next))
        return AdvanceResult("advanced", cp, tuple(st.next))

    async def advance(
        self, incident_id: str, *, expected_checkpoint: str | None = None, resume: Any = None
    ) -> AdvanceResult:
        cfg = self._config(incident_id)
        before = await self.status(incident_id)

        if resume is not None:
            if before.status == "done":
                return before
            if before.status != "waiting":
                # 等待节点还没执行到 interrupt（刚转入等待状态），先跑一步让它挂起
                await self.app.ainvoke(None if before.checkpoint_id else {"incident_id": incident_id}, cfg)
                before = await self.status(incident_id)
                if before.status != "waiting":
                    log.warning("drop resume for %s: graph is not waiting (%s)", incident_id, before.next_nodes)
                    return AdvanceResult("stale", before.checkpoint_id, before.next_nodes)
            await self.app.ainvoke(Command(resume=resume), cfg)
            return await self.status(incident_id)

        if expected_checkpoint is not None and expected_checkpoint != before.checkpoint_id:
            return AdvanceResult("stale", before.checkpoint_id, before.next_nodes)
        if before.status in ("done", "waiting"):
            return before
        if before.checkpoint_id is None:
            await self.app.ainvoke({"incident_id": incident_id}, cfg)
        else:
            await self.app.ainvoke(None, cfg)
        return await self.status(incident_id)

    async def drive(self, incident_id: str, *, resume: Any = None, max_steps: int = 200) -> AdvanceResult:
        """在当前进程里一直推进，直到等待人工或结束。测试与单机调试用。"""
        result = await self.advance(incident_id, resume=resume)
        for _ in range(max_steps):
            if not result.should_continue:
                return result
            result = await self.advance(incident_id, expected_checkpoint=result.checkpoint_id)
        raise RuntimeError(f"incident {incident_id} did not settle in {max_steps} steps")

    async def forget(self, incident_id: str) -> None:
        await self.checkpointer.adelete_thread(incident_id)
