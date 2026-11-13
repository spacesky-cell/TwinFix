"""运行时装配：API、worker、测试共用同一套组装逻辑，避免各处装配不一致。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from twinfix.adapters.llm import LLMClient, OpenAICompatibleLLM
from twinfix.agents.base import Agent
from twinfix.agents.llm import LLMDiagnoser, LLMFixer, LLMReproducer, LLMVerifier
from twinfix.agents.reporter import Reporter
from twinfix.agents.stub import StubDiagnoser, StubFixer, StubReproducer, StubVerifier
from twinfix.config import Settings
from twinfix.container import Adapters, build_adapters
from twinfix.control import ControlPlane
from twinfix.db import Store, make_engine
from twinfix.domain.enums import AgentName
from twinfix.events import EventPublisher
from twinfix.gateway.gateway import ToolGateway
from twinfix.gateway.tools import build_registry
from twinfix.orchestrator.engine import Engine
from twinfix.orchestrator.steps import Steps
from twinfix.tracing import RunTracker

log = logging.getLogger(__name__)


def build_agents(settings: Settings, llm: LLMClient | None = None) -> tuple[dict[AgentName, Agent], LLMClient | None]:
    """返回 (agents, llm)；llm 仅 agent_mode="llm" 时非 None，由 Runtime 持有以便统一关闭。"""
    if settings.agent_mode == "stub":
        d = settings.stub_delay_s
        return (
            {
                AgentName.DIAGNOSER: StubDiagnoser(d),
                AgentName.REPRODUCER: StubReproducer(d),
                AgentName.FIXER: StubFixer(d),
                AgentName.VERIFIER: StubVerifier(d),
                AgentName.REPORTER: Reporter(),
            },
            llm,
        )
    if settings.agent_mode == "llm":
        if llm is None:
            api_key = settings.llm_api_key.get_secret_value()
            if not api_key:
                raise ValueError("agent_mode='llm' requires TWINFIX_LLM_API_KEY")
            llm = OpenAICompatibleLLM(
                base_url=settings.llm_base_url,
                api_key=api_key,
                default_model=settings.llm_model,
                timeout_s=settings.llm_timeout_s,
                reasoning_effort=settings.llm_reasoning_effort or None,
            )
        return (
            {
                AgentName.DIAGNOSER: LLMDiagnoser(llm, settings),
                AgentName.REPRODUCER: LLMReproducer(llm, settings),
                AgentName.FIXER: LLMFixer(llm, settings),
                AgentName.VERIFIER: LLMVerifier(llm, settings),
                AgentName.REPORTER: Reporter(),  # 报告分发保持确定性，不交给 LLM
            },
            llm,
        )
    raise ValueError(f"unknown agent_mode: {settings.agent_mode!r}")


@dataclass
class Runtime:
    settings: Settings
    store: Store
    control: ControlPlane
    adapters: Adapters
    gateway: ToolGateway
    tracker: RunTracker
    steps: Steps
    engine: Engine
    pool: AsyncConnectionPool[Any]
    llm: LLMClient | None = None

    async def close(self) -> None:
        # 持有网络客户端的适配器暴露 aclose()（JenkinsCI/GitHubAppCodeHost 的 httpx、
        # DockerSandbox 的 docker client、FeishuNotifier 的 httpx）；关闭失败不阻塞
        # 其余资源释放，逐个 try 保证全部走到
        for target in [
            self.llm,
            *self.adapters.notifiers,
            self.adapters.ci,
            self.adapters.codehost,
            self.adapters.sandbox,
            self.adapters.twin,
            self.adapters.observability,
        ]:
            aclose = getattr(target, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    log.warning("adapter %r aclose failed", type(target).__name__, exc_info=True)
        await self.pool.close()
        await self.store.close()


async def build_runtime(
    settings: Settings,
    *,
    publisher: EventPublisher,
    control: ControlPlane,
    adapters: Adapters | None = None,
    agents: dict[AgentName, Agent] | None = None,
    llm: LLMClient | None = None,
) -> Runtime:
    store = Store(make_engine(settings.database_url), publisher)
    adapters = adapters or build_adapters(settings)
    gateway = ToolGateway(store, build_registry(), adapters, control, log_cluster=settings.log_cluster)
    tracker = RunTracker(store)
    if agents is not None:
        agent_map, owned_llm = agents, llm
    else:
        agent_map, owned_llm = build_agents(settings, llm)
    steps = Steps(
        store=store,
        gateway=gateway,
        tracker=tracker,
        adapters=adapters,
        agents=agent_map,
        control=control,
        settings=settings,
    )
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        settings.checkpoint_dsn,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()  # LangGraph 自管的 checkpoint 表，幂等
    engine = Engine(steps, checkpointer)
    return Runtime(settings, store, control, adapters, gateway, tracker, steps, engine, pool, owned_llm)
