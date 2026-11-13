"""OTel 链路导出：开关、span 属性/异常，以及 Agent / 工具 / LLM 三处接线。"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.trace import StatusCode

from tests.conftest import Harness
from twinfix import otel
from twinfix.adapters.llm import Message, ScriptedLLM
from twinfix.agents.base import AgentContext, IncidentView
from twinfix.agents.llm import LLMDiagnoser
from twinfix.config import Settings
from twinfix.domain.enums import AgentName, SignalSource, ToolCallStatus
from twinfix.gateway.gateway import ToolResult
from twinfix.services import SignalIn
from twinfix.tracing import Budget, RunHandle


def _noop_gateway() -> Any:
    class _Gateway:
        def tools_for(self, run: RunHandle) -> list[Any]:
            return []

        async def call(self, run: RunHandle, name: str, args: dict[str, Any], **_: Any) -> ToolResult:
            return ToolResult(status=ToolCallStatus.DENIED, content="no tools")

    return _Gateway()


def test_disabled_by_default() -> None:
    assert otel.setup_tracing(enabled=False) is None
    assert otel.current_trace_id() == ""


def test_enabled_without_endpoint_stays_disabled() -> None:
    assert otel.setup_tracing(enabled=True, endpoint="") is None


def test_endpoint_accepts_base_or_full_path() -> None:
    assert otel._traces_endpoint("http://127.0.0.1:4318") == "http://127.0.0.1:4318/v1/traces"
    assert otel._traces_endpoint("http://127.0.0.1:4318/") == "http://127.0.0.1:4318/v1/traces"
    assert otel._traces_endpoint("http://collector/v1/traces") == "http://collector/v1/traces"


def test_span_attributes_and_trace_id(otel_exporter: Any) -> None:
    with otel.span("unit.test", **{"twinfix.key": "v", "skipped": None}) as current:
        trace_id = otel.current_trace_id()
        current.set_attribute("twinfix.late", 7)

    assert len(trace_id) == 32
    (span,) = otel_exporter.get_finished_spans()
    assert span.name == "unit.test"
    assert span.attributes["twinfix.key"] == "v"
    assert span.attributes["twinfix.late"] == 7
    assert "skipped" not in span.attributes
    assert format(span.context.trace_id, "032x") == trace_id
    # 未出错时不要误标 ERROR
    assert span.status.status_code is StatusCode.UNSET


def test_span_records_exception_once(otel_exporter: Any) -> None:
    with pytest.raises(RuntimeError, match="boom"), otel.span("unit.fail"):
        raise RuntimeError("boom")

    (span,) = otel_exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert "RuntimeError: boom" in (span.status.description or "")
    # SDK 自动记录已关闭，异常事件只应有一条
    assert len([e for e in span.events if e.name == "exception"]) == 1


async def test_llm_chat_span(otel_exporter: Any, tmp_path: Any) -> None:
    llm = ScriptedLLM([Message(role="assistant", content='{"outcome": "not_bug", "reason": "业务拒绝"}')])
    settings = Settings(data_dir=tmp_path / "data")
    run = RunHandle(id="r1", incident_id="inc1", agent=AgentName.DIAGNOSER, budget=Budget(100_000, 50, 600))
    ctx = AgentContext(
        incident=IncidentView(
            id="inc1",
            title="t",
            severity="S3",
            env="test",
            service="shop",
            fingerprint="fp",
            base_commit=None,
            target_branch=None,
        ),
        run=run,
        gateway=_noop_gateway(),
        budget=run.budget,
    )

    result = await LLMDiagnoser(llm, settings).run(ctx)

    assert result.outcome == "not_bug"
    (span,) = [s for s in otel_exporter.get_finished_spans() if s.name.startswith("chat ")]
    attrs = span.attributes
    assert attrs is not None
    assert attrs["gen_ai.request.model"] == settings.llm_model
    assert attrs["gen_ai.response.model"] == "scripted"
    assert attrs["gen_ai.usage.input_tokens"] == 100
    assert attrs["gen_ai.usage.output_tokens"] == 20
    assert attrs["twinfix.agent"] == "diagnoser"
    assert attrs["twinfix.run_id"] == "r1"
    assert attrs["twinfix.llm.turn"] == 1


@pytest.mark.integration
async def test_tool_call_span(otel_exporter: Any, harness: Harness) -> None:
    project = await harness.project()
    harness.dispatcher.auto = False
    res = await harness.service.ingest(SignalIn(project=project.name, source=SignalSource.MANUAL, title="otel tool"))
    run = await harness.rt.tracker.start(
        res.incident_id, AgentName.DIAGNOSER, Budget(10_000, 10, 600), repo=project.repo
    )

    result = await harness.rt.gateway.call(run, "query_metrics", {"expr": "up"})

    assert result.status is ToolCallStatus.OK
    (span,) = [s for s in otel_exporter.get_finished_spans() if s.name == "tool.call"]
    attrs = span.attributes
    assert attrs is not None
    assert attrs["twinfix.tool"] == "query_metrics"
    assert attrs["twinfix.status"] == "ok"
    assert attrs["twinfix.run_id"] == run.id
    assert attrs["twinfix.incident_id"] == res.incident_id
    assert attrs["twinfix.agent"] == "diagnoser"


@pytest.mark.integration
async def test_agent_run_span(otel_exporter: Any, harness: Harness) -> None:
    project = await harness.project()
    harness.dispatcher.auto = False
    res = await harness.service.ingest(
        SignalIn(
            project=project.name,
            source=SignalSource.MANUAL,
            title="otel agent",
            meta={"scenario": "happy"},
        )
    )
    inc = await harness.rt.steps._load(res.incident_id)

    result = await harness.rt.steps._run_agent(inc, AgentName.DIAGNOSER)

    assert result.outcome == "diagnosed"
    (span,) = [s for s in otel_exporter.get_finished_spans() if s.name == "agent.run"]
    attrs = span.attributes
    assert attrs is not None
    assert attrs["twinfix.agent"] == "diagnoser"
    assert attrs["twinfix.outcome"] == "diagnosed"
    assert attrs["twinfix.incident_id"] == res.incident_id
    assert attrs["twinfix.run_id"]
    # stub Agent 照常经过网关，工具 span 应挂在同一条 trace 下
    tool_spans = [s for s in otel_exporter.get_finished_spans() if s.name == "tool.call"]
    assert tool_spans
    assert all(s.context.trace_id == span.context.trace_id for s in tool_spans)
