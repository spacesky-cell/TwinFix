"""可选的 OpenTelemetry 链路追踪。

默认关闭；配置 ``TWINFIX_OTEL_ENABLED=true`` 与 OTLP/HTTP 端点后，worker 会把
Agent 运行、工具调用与 LLM 请求导出到 OTel Collector / Jaeger / Tempo 等后端。

指标走 ``/api/metrics``（Prometheus），链路走本模块：前者回答"有多少、多慢"，
后者回答"这一次事故的时间花在哪一步、哪次工具调用/LLM 请求上"。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio
from opentelemetry.trace import Span, Status, StatusCode, Tracer

log = logging.getLogger(__name__)

_provider: TracerProvider | None = None
_tracer: Tracer = trace.get_tracer("twinfix")


def _service_version() -> str:
    try:
        return version("twinfix")
    except PackageNotFoundError:
        return "dev"


def _traces_endpoint(endpoint: str) -> str:
    """接受收集器基地址或完整 /v1/traces 地址。"""
    base = endpoint.rstrip("/")
    return base if base.endswith("/v1/traces") else f"{base}/v1/traces"


def setup_tracing(
    *,
    enabled: bool,
    endpoint: str = "",
    service_name: str = "twinfix",
    sample_ratio: float = 1.0,
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """初始化全局 tracer；已初始化或未启用时返回现有/None。

    ``exporter`` 供测试注入 InMemorySpanExporter，此时不要求 endpoint。
    """
    global _provider, _tracer
    if _provider is not None:
        return _provider
    if exporter is None and not enabled:
        return None
    if exporter is None and not endpoint:
        log.warning("TWINFIX_OTEL_ENABLED=true but TWINFIX_OTEL_ENDPOINT is empty; tracing disabled")
        return None

    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            SERVICE_VERSION: _service_version(),
        }
    )
    ratio = min(max(float(sample_ratio), 0.0), 1.0)
    provider = TracerProvider(resource=resource, sampler=ParentBasedTraceIdRatio(ratio))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_traces_endpoint(endpoint))))
    _tracer = provider.get_tracer("twinfix")
    _provider = provider
    log.info(
        "OpenTelemetry tracing enabled (endpoint=%s, service=%s, sample_ratio=%.2f)",
        endpoint or "injected-exporter",
        service_name,
        ratio,
    )
    return provider


def reset_tracing() -> None:
    """关闭 provider 并回到 NoOp tracer（进程退出/测试用）。"""
    global _provider, _tracer
    if _provider is not None:
        _provider.shutdown()
    _provider = None
    _tracer = trace.get_tracer("twinfix")


def tracer() -> Tracer:
    return _tracer


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """创建一个 span；异常自动记录并标记 ERROR，不吞异常。

    ``attributes`` 使用完整键名（如 ``twinfix.tool``、``gen_ai.request.model``）。
    """
    # 异常由下面统一记录一次；关掉 SDK 的自动记录，避免同一异常出现两条 exception 事件
    with _tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise


def current_trace_id() -> str:
    """当前 span 的 trace id（32 位十六进制）；没有活动 span 时返回空串。"""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else ""
