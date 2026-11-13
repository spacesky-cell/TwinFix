"""事故编排图（LangGraph）。

设计要点
- 图的节点与事故状态一一对应；节点内只做"执行该状态的工作 + 决定下一个状态"。
- 业务数据的权威来源是数据库，图状态只保存 incident_id 等少量路由信息，
  因此 checkpoint 很小，重放安全。
- 以 `interrupt_after="*"` 编译：每次只推进一个节点即落 checkpoint 并返回，
  由 worker 通过 MQ 派发下一步。这样每一步都可独立重试、超时、限流，进程崩溃后从 checkpoint 继续。
- 等待人工的状态（AWAITING_REVIEW / NEEDS_HUMAN）用 `interrupt()` 挂起，
  审批 / 驳回 / 指导 / 取消 都以 `Command(resume=...)` 恢复。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from twinfix.domain.enums import IncidentState

S = IncidentState


class GraphState(TypedDict, total=False):
    incident_id: str


Step = Callable[[str], Awaitable["StepDecision"]]
Wait = Callable[[str], Awaitable["StepDecision"]]


class StepDecision:
    """节点执行结果：下一个状态（None 表示留在当前节点，例如急停后重试）。"""

    __slots__ = ("next_state",)

    def __init__(self, next_state: IncidentState | None) -> None:
        self.next_state = next_state


# 节点名就是状态名（小写）；终态统一汇入 finalize 节点
WORK_STATES: tuple[IncidentState, ...] = (
    S.NEW,
    S.TRIAGED,
    S.DIAGNOSING,
    S.REPRODUCING,
    S.FIXING,
    S.VERIFYING,
    S.AWAITING_REVIEW,
    S.MERGING,
    S.MERGED,
    S.OBSERVING,
    S.NEEDS_HUMAN,
)
FINALIZE = "finalize"


def node_name(state: IncidentState) -> str:
    if state in WORK_STATES:
        return state.value.lower()
    return FINALIZE


def build_graph(handlers: dict[IncidentState, Step], finalize: Step) -> StateGraph[GraphState, Any, Any, Any]:
    g: StateGraph[GraphState, Any, Any, Any] = StateGraph(GraphState)
    destinations = (*sorted({node_name(s) for s in IncidentState}), END)

    def make(state: IncidentState) -> Callable[[GraphState], Awaitable[Command[str]]]:
        handler = handlers[state]

        async def node(gs: GraphState) -> Command[str]:
            decision = await handler(gs["incident_id"])
            target = node_name(decision.next_state) if decision.next_state else node_name(state)
            return Command(goto=target)

        node.__name__ = f"node_{state.value.lower()}"
        return node

    # LangGraph 的 add_node 重载对 Command 返回类型的推断过严，这里显式按 Any 传入
    for state in WORK_STATES:
        action: Any = make(state)
        g.add_node(node_name(state), action, destinations=destinations)

    async def finalize_node(gs: GraphState) -> Command[str]:
        await finalize(gs["incident_id"])
        return Command(goto=END)

    final_action: Any = finalize_node
    g.add_node(FINALIZE, final_action, destinations=(END,))
    g.add_edge(START, node_name(S.NEW))
    return g
