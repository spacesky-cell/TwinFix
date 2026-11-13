"""可观测适配器：指标 / 日志 / 链路 / 部署记录。只读。

真实实现 ``StackObservability`` 组合三个后端（docs/04 §可观测）：
- Prometheus ``/api/v1/query_range`` → ``query_metrics``
- Elasticsearch ``_search`` → ``search_logs``（兼容 ECS 与扁平字段命名）
- SkyWalking OAP GraphQL → ``get_trace``

三者按 URL 独立启用，可只接部分后端；``recent_deploys`` 暂无数据源，
v1 返回空列表（后续可用 Prometheus 部署注解指标实现）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from twinfix.adapters.types import (
    Deploy,
    LogLine,
    LogPage,
    LogQuery,
    MetricQuery,
    MetricSeries,
    Span,
    Trace,
)


class ObservabilityAdapter(Protocol):
    async def query_metrics(self, q: MetricQuery) -> MetricSeries: ...
    async def search_logs(self, q: LogQuery) -> LogPage: ...
    async def get_trace(self, trace_id: str) -> Trace: ...
    async def recent_deploys(self, service: str, since: datetime) -> list[Deploy]: ...


class FakeObservability:
    """内存实现：测试与本地演示用，数据由调用方预置。"""

    def __init__(self) -> None:
        self.metrics: dict[str, MetricSeries] = {}
        self.logs: list[LogLine] = []
        self.traces: dict[str, Trace] = {}
        self.deploys: list[Deploy] = []

    async def query_metrics(self, q: MetricQuery) -> MetricSeries:
        return self.metrics.get(q.expr, MetricSeries(expr=q.expr, points=[]))

    async def search_logs(self, q: LogQuery) -> LogPage:
        hits = [
            line
            for line in self.logs
            if q.start <= line.ts <= q.end
            and (q.service is None or line.service == q.service)
            and (q.trace_id is None or line.trace_id == q.trace_id)
            and q.query.lower() in line.message.lower()
        ]
        return LogPage(lines=hits[: q.limit], total=len(hits))

    async def get_trace(self, trace_id: str) -> Trace:
        return self.traces.get(trace_id, Trace(trace_id=trace_id, spans=[]))

    async def recent_deploys(self, service: str, since: datetime) -> list[Deploy]:
        return [d for d in self.deploys if d.service == service and d.deployed_at >= since]


class ObservabilityError(RuntimeError):
    def __init__(self, backend: str, status: int, message: str) -> None:
        super().__init__(f"{backend} {status}: {message[:300]}")
        self.backend = backend
        self.status = status


class StackObservability:
    """Prometheus + Elasticsearch + SkyWalking 组合实现。

    三个子后端按 URL 是否提供独立启用：缺 Prometheus 的 ``query_metrics``
    返回空序列、缺 ES 的 ``search_logs`` 返回空页、缺 SkyWalking 的
    ``get_trace`` 返回空 trace——让部署可以只接部分后端（如只有 ES 日志）。
    """

    def __init__(
        self,
        *,
        prometheus_url: str = "",
        elasticsearch_url: str = "",
        es_logs_index: str = "logs-*",
        es_username: str = "",
        es_password: str = "",
        skywalking_url: str = "",
        deploy_query: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._prom = (
            self._client(prometheus_url, transport=transport, headers={"Accept": "application/json"})
            if prometheus_url
            else None
        )
        self._es = (
            self._client(
                elasticsearch_url,
                auth=(es_username, es_password) if es_username else None,
                transport=transport,
                headers={"Accept": "application/json"},
            )
            if elasticsearch_url
            else None
        )
        self._es_logs_index = es_logs_index
        # 部署记录：PromQL 模板（{service} 替换），空 = 不启用
        self._deploy_query = deploy_query
        self._sw = (
            self._client(skywalking_url, transport=transport, headers={"Accept": "application/json"})
            if skywalking_url
            else None
        )
        # skywalking_url 直接指 GraphQL 端点（…/graphql）。必须用绝对 URL 发请求：
        # httpx 对空路径会补成 "…/graphql/"，而 OAP 对尾斜杠返回 404
        self._sw_endpoint = skywalking_url.rstrip("/")

    @staticmethod
    def _client(
        base_url: str,
        *,
        auth: tuple[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None,
        headers: dict[str, str],
    ) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=auth,
            timeout=30,
            transport=transport,
            headers=headers,
        )
        return client

    # ---------------------------------------------------------------- 指标（Prometheus）

    async def query_metrics(self, q: MetricQuery) -> MetricSeries:
        if self._prom is None:
            return MetricSeries(expr=q.expr, points=[])
        resp = await self._prom.get(
            "/api/v1/query_range",
            params={
                "query": q.expr,
                "start": q.start.timestamp(),
                "end": q.end.timestamp(),
                "step": q.step_s,
            },
        )
        if resp.status_code >= 400:
            raise ObservabilityError("prometheus", resp.status_code, resp.text)
        body = resp.json()
        if body.get("status") != "success":
            raise ObservabilityError("prometheus", 200, str(body.get("error") or body))
        result = body["data"].get("result") or []
        if not result:
            return MetricSeries(expr=q.expr, points=[])
        # 多序列时合并取第一条（工具侧按单序列展示），labels 附带 metric 名便于诊断
        series = result[0]
        labels = {k: str(v) for k, v in (series.get("metric") or {}).items()}
        points = [
            (datetime.fromtimestamp(float(ts), tz=UTC), float(val))
            for ts, val in series.get("values", [])
            if _is_number(val)
        ]
        return MetricSeries(expr=q.expr, points=points, labels=labels)

    # ---------------------------------------------------------------- 日志（Elasticsearch）

    async def search_logs(self, q: LogQuery) -> LogPage:
        if self._es is None:
            return LogPage(lines=[], total=0)
        must: list[dict[str, Any]] = [
            {"range": {"@timestamp": {"gte": _iso(q.start), "lte": _iso(q.end)}}},
        ]
        if q.service:
            must.append(
                {
                    "bool": {
                        "should": [
                            {"term": {"service.name.keyword": q.service}},
                            {"term": {"service.keyword": q.service}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        if q.trace_id:
            must.append(
                {
                    "bool": {
                        "should": [
                            {"term": {"trace.id.keyword": q.trace_id}},
                            {"term": {"trace_id.keyword": q.trace_id}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        if q.query.strip():
            must.append({"query_string": {"query": q.query}})
        body = {
            "size": q.limit,
            "sort": [{"@timestamp": {"order": "asc"}}],
            "track_total_hits": True,
            "query": {"bool": {"must": must}},
        }
        resp = await self._es.post(f"/{self._es_logs_index}/_search", json=body)
        if resp.status_code >= 400:
            raise ObservabilityError("elasticsearch", resp.status_code, resp.text)
        hits = resp.json().get("hits", {})
        total = hits.get("total") or {}
        lines = [_log_line(hit.get("_source") or {}) for hit in hits.get("hits", [])]
        return LogPage(lines=lines, total=int(total.get("value", len(lines))))

    # ---------------------------------------------------------------- 链路（SkyWalking GraphQL）

    _TRACE_QUERY = """
query($traceId: ID!) {
  trace: queryTrace(traceId: $traceId) {
    spans {
      spanId
      parentSpanId
      serviceCode
      type
      startTime
      endTime
      endpointName
      isError
      tags { key value }
    }
  }
}
"""

    async def get_trace(self, trace_id: str) -> Trace:
        if self._sw is None:
            return Trace(trace_id=trace_id, spans=[])
        resp = await self._sw.post(
            self._sw_endpoint,
            json={"query": self._TRACE_QUERY, "variables": {"traceId": trace_id}},
        )
        if resp.status_code >= 400:
            raise ObservabilityError("skywalking", resp.status_code, resp.text)
        body = resp.json()
        if body.get("errors"):
            raise ObservabilityError("skywalking", 200, json.dumps(body["errors"], ensure_ascii=False))
        spans = ((body.get("data") or {}).get("trace") or {}).get("spans") or []
        return Trace(trace_id=trace_id, spans=[_span(s) for s in spans])

    async def recent_deploys(self, service: str, since: datetime) -> list[Deploy]:
        """从 Prometheus 读取部署记录。

        ``TWINFIX_DEPLOY_QUERY`` 约定为一条 range query，``{service}`` 会被替换。两种常见约定都支持：
        - ``deploy_timestamp_seconds{service="{service}"}``：样本值是 Unix 部署时间；
        - ``deploy_info{service="{service}",sha=...,env=...}``：样本值非时间戳，用窗口内首个采样点近似部署时间。
        """
        if self._prom is None or not self._deploy_query.strip():
            return []
        now = datetime.now(UTC)
        window_s = max((now - since).total_seconds(), 60)
        # 最多约 288 个采样点，避免 30 天窗口把 Prometheus 打爆
        step = max(60, int(window_s / 288))
        expr = self._deploy_query.replace("{service}", service)
        resp = await self._prom.get(
            "/api/v1/query_range",
            params={"query": expr, "start": since.timestamp(), "end": now.timestamp(), "step": step},
        )
        if resp.status_code >= 400:
            raise ObservabilityError("prometheus", resp.status_code, resp.text)
        body = resp.json()
        if body.get("status") != "success":
            raise ObservabilityError("prometheus", 200, str(body.get("error") or body))
        deploys: list[Deploy] = []
        for series in body["data"].get("result") or []:
            labels = {str(k): str(v) for k, v in (series.get("metric") or {}).items()}
            samples = [
                (datetime.fromtimestamp(float(ts), tz=UTC), float(val))
                for ts, val in series.get("values", [])
                if _is_number(val)
            ]
            if not samples:
                continue
            first_ts, first_val = samples[0]
            # 1.6e9 ≈ 2020-09；更大的值视为 Unix 时间戳（兼容毫秒），否则把首个采样点当作部署时间
            deployed_at = first_ts
            if first_val > 1_600_000_000:
                seconds = first_val / 1000 if first_val > 1e12 else first_val
                try:
                    deployed_at = datetime.fromtimestamp(seconds, tz=UTC)
                except (OverflowError, OSError, ValueError):
                    deployed_at = first_ts
            if deployed_at < since:
                continue
            deploys.append(
                Deploy(
                    service=service,
                    sha=labels.get("sha") or labels.get("revision") or labels.get("commit") or "",
                    env=labels.get("env") or labels.get("environment") or "",
                    deployed_at=deployed_at,
                )
            )
        # 同一 sha/env 在窗口内可能多次出现；按时间倒序去重
        seen: set[tuple[str, str, datetime]] = set()
        unique: list[Deploy] = []
        for deploy in sorted(deploys, key=lambda d: d.deployed_at, reverse=True):
            key = (deploy.sha, deploy.env, deploy.deployed_at)
            if key in seen:
                continue
            seen.add(key)
            unique.append(deploy)
        return unique

    async def aclose(self) -> None:
        for client in (self._prom, self._es, self._sw):
            if client is not None:
                await client.aclose()


def _is_number(val: Any) -> bool:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return False
    return f == f  # 排除 NaN


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _first(src: dict[str, Any], *keys: str) -> Any:
    """扁平 / ECS 字段命名兼容：取第一个存在的键。"""
    for key in keys:
        if key in src:
            return src[key]
        # ECS 风格嵌套："service.name" -> src["service"]["name"]
        if "." in key:
            head, _, rest = key.partition(".")
            node = src.get(head)
            if isinstance(node, dict) and rest in node:
                return node[rest]
    return None


def _log_line(src: dict[str, Any]) -> LogLine:
    raw_ts = _first(src, "@timestamp", "timestamp", "time")
    try:
        ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        ts = datetime.fromtimestamp(0, tz=UTC)
    service = _first(src, "service.name", "service")
    trace_id = _first(src, "trace.id", "trace_id", "traceId")
    message = _first(src, "message", "log", "msg", "body")
    level = _first(src, "log.level", "level", "severity")
    return LogLine(
        ts=ts,
        level=str(level or ""),
        message=str(message or ""),
        service=str(service) if service else None,
        trace_id=str(trace_id) if trace_id else None,
    )


def _span(raw: dict[str, Any]) -> Span:
    start = int(raw.get("startTime") or 0)
    end = int(raw.get("endTime") or start)
    parent = raw.get("parentSpanId")
    tags = {str(t.get("key")): str(t.get("value")) for t in raw.get("tags") or [] if t.get("key")}
    return Span(
        span_id=str(raw.get("spanId", "")),
        parent_id=str(parent) if parent is not None and int(parent) >= 0 else None,
        service=str(raw.get("serviceCode") or ""),
        operation=str(raw.get("endpointName") or ""),
        duration_ms=max(0, end - start),
        error=bool(raw.get("isError")),
        tags=tags,
    )
