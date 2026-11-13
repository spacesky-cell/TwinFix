"""StackObservability：Prometheus / Elasticsearch / SkyWalking 三个只读后端。

全部用 httpx.MockTransport 拦截请求，校验请求形状与响应解析，
不依赖真实监控栈（真实联调走 -m integration 的端到端用例）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from twinfix.adapters.observability import ObservabilityError, StackObservability
from twinfix.adapters.types import LogQuery, MetricQuery
from twinfix.config import Settings
from twinfix.container import build_adapters

T0 = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 20, 12, 10, 0, tzinfo=UTC)


def _handler(fn):
    return httpx.MockTransport(lambda req: httpx.Response(200, json=fn(req)))


# ---------------------------------------------------------------- Prometheus


class TestPrometheus:
    async def test_query_range_maps_points_and_labels(self) -> None:
        captured: dict = {}

        def handle(req: httpx.Request):
            captured["params"] = parse_qs(urlparse(str(req.url)).query)
            return {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"__name__": "http_requests_total", "service": "shop"},
                            "values": [[1000.0, "3.5"], [1060.0, "4.0"]],
                        }
                    ],
                },
            }

        obs = StackObservability(prometheus_url="http://prom:9090", transport=_handler(handle))
        series = await obs.query_metrics(MetricQuery(expr="http_requests_total", start=T0, end=T1, step_s=60))
        assert series.labels == {"__name__": "http_requests_total", "service": "shop"}
        assert series.points == [
            (datetime.fromtimestamp(1000, UTC), 3.5),
            (datetime.fromtimestamp(1060, UTC), 4.0),
        ]
        assert captured["params"]["query"] == ["http_requests_total"]
        assert captured["params"]["step"] == ["60"]
        assert float(captured["params"]["start"][0]) == T0.timestamp()

    async def test_empty_result(self) -> None:
        obs = StackObservability(
            prometheus_url="http://prom:9090",
            transport=_handler(lambda req: {"status": "success", "data": {"result": []}}),
        )
        series = await obs.query_metrics(MetricQuery(expr="up", start=T0, end=T1))
        assert series.points == [] and series.labels == {}

    async def test_nan_values_dropped(self) -> None:
        obs = StackObservability(
            prometheus_url="http://prom:9090",
            transport=_handler(
                lambda req: {
                    "status": "success",
                    "data": {"result": [{"metric": {}, "values": [[1.0, "NaN"], [2.0, "1.5"]]}]},
                }
            ),
        )
        series = await obs.query_metrics(MetricQuery(expr="x", start=T0, end=T1))
        assert len(series.points) == 1 and series.points[0][1] == 1.5

    async def test_promql_error_raises(self) -> None:
        obs = StackObservability(
            prometheus_url="http://prom:9090",
            transport=_handler(lambda req: {"status": "error", "error": "parse error: bad expr"}),
        )
        with pytest.raises(ObservabilityError, match="prometheus"):
            await obs.query_metrics(MetricQuery(expr="bad{", start=T0, end=T1))

    async def test_http_error_raises(self) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(503, text="unavailable"))
        obs = StackObservability(prometheus_url="http://prom:9090", transport=transport)
        with pytest.raises(ObservabilityError, match="503"):
            await obs.query_metrics(MetricQuery(expr="up", start=T0, end=T1))


# ---------------------------------------------------------------- 部署记录


class TestDeployments:
    async def test_empty_without_query(self) -> None:
        obs = StackObservability(
            prometheus_url="http://prom:9090",
            transport=_handler(lambda req: _prom_empty()),
        )
        assert await obs.recent_deploys("order", T0) == []

    async def test_timestamp_metric_maps_labels_and_sorts(self) -> None:
        captured: dict = {}

        def handle(req: httpx.Request):
            captured["params"] = parse_qs(urlparse(str(req.url)).query)
            return {
                "status": "success",
                "data": {
                    "result": [
                        {"metric": {"sha": "aaa", "env": "test"}, "values": [[T0.timestamp(), str(T1.timestamp())]]},
                        {
                            "metric": {"sha": "bbb", "env": "test"},
                            "values": [[T0.timestamp(), str((T1 + timedelta(minutes=5)).timestamp())]],
                        },
                    ]
                },
            }

        obs = StackObservability(
            prometheus_url="http://prom:9090",
            deploy_query='deploy_timestamp_seconds{service="{service}"}',
            transport=_handler(handle),
        )
        deploys = await obs.recent_deploys("order", T0)
        assert [d.sha for d in deploys] == ["bbb", "aaa"]
        assert deploys[0].deployed_at == T1 + timedelta(minutes=5)
        assert deploys[0].env == "test"
        assert captured["params"]["query"] == ['deploy_timestamp_seconds{service="order"}']
        assert "step" in captured["params"] and "start" in captured["params"]

    async def test_timestamp_metric_accepts_milliseconds(self) -> None:
        def handle(req: httpx.Request):
            return {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"sha": "abc", "env": "test"},
                            "values": [[T0.timestamp(), str(T1.timestamp() * 1000)]],
                        }
                    ]
                },
            }

        obs = StackObservability(
            prometheus_url="http://prom:9090", deploy_query='deploy_ts{service="{service}"}', transport=_handler(handle)
        )
        (deploy,) = await obs.recent_deploys("order", T0)
        assert deploy.deployed_at == T1

    async def test_info_metric_uses_first_sample_as_deploy_time(self) -> None:
        def handle(req: httpx.Request):
            return {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"sha": "abc", "env": "prod"},
                            "values": [
                                [(T0 + timedelta(seconds=60)).timestamp(), "1"],
                                [(T0 + timedelta(seconds=120)).timestamp(), "1"],
                            ],
                        }
                    ]
                },
            }

        obs = StackObservability(
            prometheus_url="http://prom:9090",
            deploy_query='deploy_info{service="{service}"}',
            transport=_handler(handle),
        )
        (deploy,) = await obs.recent_deploys("order", T0)
        assert deploy.deployed_at == T0 + timedelta(seconds=60)
        assert (deploy.sha, deploy.env) == ("abc", "prod")

    async def test_http_error_raises(self) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(503, text="unavailable"))
        obs = StackObservability(prometheus_url="http://prom:9090", deploy_query="deploy_info", transport=transport)
        with pytest.raises(ObservabilityError, match="503"):
            await obs.recent_deploys("order", T0)


def _prom_empty():
    return {"status": "success", "data": {"result": []}}


# ---------------------------------------------------------------- Elasticsearch


def _es_hit(src: dict) -> dict:
    return {"hits": {"total": {"value": 1, "relation": "eq"}, "hits": [{"_source": src}]}}


class TestElasticsearch:
    async def test_search_maps_ecs_fields(self) -> None:
        def handle(req: httpx.Request):
            assert req.url.path == "/logs-%2A/_search" or req.url.path == "/logs-*/_search"
            body = json.loads(req.content)
            assert body["size"] == 50
            return _es_hit(
                {
                    "@timestamp": "2026-10-20T12:03:00Z",
                    "message": "NullPointerException at Stock.deduct",
                    "log": {"level": "ERROR"},
                    "service": {"name": "shop"},
                    "trace": {"id": "abc123"},
                }
            )

        obs = StackObservability(
            elasticsearch_url="http://es:9200",
            es_logs_index="logs-*",
            transport=_handler(handle),
        )
        page = await obs.search_logs(LogQuery(service="shop", query="NullPointerException", start=T0, end=T1, limit=50))
        assert page.total == 1
        (line,) = page.lines
        assert line.level == "ERROR"
        assert line.service == "shop" and line.trace_id == "abc123"
        assert line.ts == datetime(2026, 10, 20, 12, 3, 0, tzinfo=UTC)
        assert "NullPointerException" in line.message

    async def test_search_maps_flat_fields(self) -> None:
        obs = StackObservability(
            elasticsearch_url="http://es:9200",
            transport=_handler(
                lambda req: _es_hit(
                    {
                        "@timestamp": "2026-10-20T12:01:00+00:00",
                        "message": "timeout",
                        "level": "WARN",
                        "service": "order",
                        "trace_id": "t1",
                    }
                )
            ),
        )
        page = await obs.search_logs(LogQuery(service=None, query="timeout", start=T0, end=T1))
        (line,) = page.lines
        assert (line.level, line.service, line.trace_id) == ("WARN", "order", "t1")

    async def test_query_builds_bool_must(self) -> None:
        captured: dict = {}

        def handle(req: httpx.Request):
            captured["body"] = json.loads(req.content)
            return {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}

        obs = StackObservability(elasticsearch_url="http://es:9200", transport=_handler(handle))
        await obs.search_logs(LogQuery(service="shop", query="error", start=T0, end=T1, trace_id="tr9", limit=10))
        must = captured["body"]["query"]["bool"]["must"]
        assert must[0] == {"range": {"@timestamp": {"gte": "2026-10-20T12:00:00Z", "lte": "2026-10-20T12:10:00Z"}}}
        assert any("query_string" in clause for clause in must)
        # service 与 trace_id 均以 bool.should 兼容两种字段命名
        service_clause = next(
            c for c in must if "bool" in c and any("service.keyword" in s["term"] for s in c["bool"]["should"])
        )
        assert service_clause["bool"]["minimum_should_match"] == 1
        trace_clause = next(
            c for c in must if "bool" in c and any("trace.id.keyword" in s["term"] for s in c["bool"]["should"])
        )
        assert trace_clause["bool"]["minimum_should_match"] == 1

    async def test_http_error_raises(self) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(401, text="unauthorized"))
        obs = StackObservability(elasticsearch_url="http://es:9200", transport=transport)
        with pytest.raises(ObservabilityError, match="elasticsearch 401"):
            await obs.search_logs(LogQuery(service=None, query="x", start=T0, end=T1))


# ---------------------------------------------------------------- SkyWalking


def _sw_trace(spans: list[dict]) -> dict:
    return {"data": {"trace": {"spans": spans}}}


class TestSkyWalking:
    async def test_trace_spans_mapped(self) -> None:
        def handle(req: httpx.Request):
            body = json.loads(req.content)
            assert body["variables"] == {"traceId": "t-1"}
            # 真实 OAP 对 "/graphql/"（带尾斜杠）返回 404
            assert req.url.path == "/graphql"
            return _sw_trace(
                [
                    {
                        "spanId": 0,
                        "parentSpanId": -1,
                        "serviceCode": "shop",
                        "endpointName": "POST /order",
                        "startTime": 1000,
                        "endTime": 1250,
                        "isError": False,
                        "tags": [{"key": "http.method", "value": "POST"}],
                    },
                    {
                        "spanId": 1,
                        "parentSpanId": 0,
                        "serviceCode": "shop",
                        "endpointName": "Stock.deduct",
                        "startTime": 1010,
                        "endTime": 1200,
                        "isError": True,
                        "tags": [],
                    },
                ]
            )

        obs = StackObservability(skywalking_url="http://oap:12800/graphql", transport=_handler(handle))
        trace = await obs.get_trace("t-1")
        assert trace.trace_id == "t-1" and len(trace.spans) == 2
        root, child = trace.spans
        assert root.parent_id is None  # parentSpanId=-1 → 根
        assert root.duration_ms == 250 and not root.error
        assert root.tags == {"http.method": "POST"}
        assert child.parent_id == "0" and child.error and child.duration_ms == 190

    async def test_graphql_errors_raise(self) -> None:
        obs = StackObservability(
            skywalking_url="http://oap:12800/graphql",
            transport=_handler(lambda req: {"errors": [{"message": "bad query"}]}),
        )
        with pytest.raises(ObservabilityError, match="skywalking"):
            await obs.get_trace("t-x")

    async def test_missing_trace_is_empty(self) -> None:
        obs = StackObservability(
            skywalking_url="http://oap:12800/graphql",
            transport=_handler(lambda req: {"data": {"trace": {"spans": []}}}),
        )
        assert (await obs.get_trace("nope")).spans == []


# ---------------------------------------------------------------- 组合与装配


class TestComposite:
    async def test_unconfigured_backends_return_empty(self) -> None:
        # 只配 ES：metrics / trace / deploys 静默返回空，不报错
        obs = StackObservability(elasticsearch_url="http://es:9200", transport=httpx.MockTransport(lambda r: _ok(r)))
        assert (await obs.query_metrics(MetricQuery(expr="x", start=T0, end=T1))).points == []
        assert (await obs.get_trace("t")).spans == []
        assert await obs.recent_deploys("shop", T0) == []

    async def test_aclose(self) -> None:
        obs = StackObservability(
            prometheus_url="http://prom:9090",
            elasticsearch_url="http://es:9200",
            skywalking_url="http://oap:12800/graphql",
            transport=httpx.MockTransport(lambda r: _ok(r)),
        )
        await obs.aclose()
        await obs.aclose()  # 幂等

    def test_build_adapters_stack(self, tmp_path) -> None:
        settings = Settings(
            data_dir=tmp_path / "data",
            observability="stack",
            prometheus_url="http://prom:9090",
            elasticsearch_url="http://es:9200",
            skywalking_url="http://oap:12800/graphql",
        )
        adapters = build_adapters(settings)
        assert isinstance(adapters.observability, StackObservability)

    def test_build_adapters_stack_requires_one_url(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="observability=stack requires"):
            build_adapters(Settings(data_dir=tmp_path / "data", observability="stack"))

    def test_build_adapters_unknown_backend(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="unknown observability backend"):
            build_adapters(Settings(data_dir=tmp_path / "data", observability="loki"))

    def test_build_adapters_default_fake(self, tmp_path) -> None:
        from twinfix.adapters.observability import FakeObservability

        adapters = build_adapters(Settings(data_dir=tmp_path / "data"))
        assert isinstance(adapters.observability, FakeObservability)


def _ok(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": {"result": []}})
