"""JenkinsCI 的 REST 行为：队列 -> build number -> 状态映射 / 日志 / 任务白名单。

不依赖真实 Jenkins：httpx.MockTransport 模拟 API，路由覆盖 crumb issuer、
buildWithParameters、queue item、build api/json 与 progressiveText。
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from twinfix.adapters.ci import JobNotAllowedError
from twinfix.adapters.jenkins import JenkinsCI, JenkinsError

BASE = "http://jenkins.test"


def _fake_jenkins(
    *,
    crumb: bool = True,
    build_number: int = 7,
    result: str | None = "SUCCESS",
    building: bool = False,
    queue_delay: int = 0,
):
    """返回一个 MockTransport，模拟最小可用的 Jenkins API。queue_delay>0 时队列条目
    前几次查询还不带 executable（模拟排队等待）。"""
    state = {"queue_queries": 0, "posts": [], "queue_cancelled": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path
        if path == "/crumbIssuer/api/json":
            if not crumb:
                return httpx.Response(404)
            return httpx.Response(200, json={"crumbRequestField": "Jenkins-Crumb", "crumb": "c123"})
        if path.endswith("/buildWithParameters"):
            state["posts"].append(request)
            return httpx.Response(201, headers={"Location": f"{BASE}/queue/item/99/"})
        if path == "/queue/item/99/api/json":
            state["queue_queries"] += 1
            if state["queue_cancelled"]:
                return httpx.Response(200, json={"cancelled": True})
            if state["queue_queries"] <= queue_delay:
                return httpx.Response(200, json={"why": "Waiting for executor", "executable": None})
            return httpx.Response(200, json={"executable": {"number": build_number}})
        if path == f"/job/twinfix-twin-deploy/{build_number}/api/json":
            return httpx.Response(
                200,
                json={"building": building, "result": result, "url": f"{BASE}/job/x/{build_number}/"},
            )
        if path.endswith("/logText/progressiveText"):
            return httpx.Response(200, text="line1\nline2\nline3\n")
        return httpx.Response(404, text=f"no route {path}")

    return state, httpx.MockTransport(handler)


def _ci(transport, **kw) -> JenkinsCI:
    return JenkinsCI(base_url=BASE, username="bot", api_token="tok", transport=transport, **kw)


class TestTrigger:
    async def test_rejects_non_twinfix_job(self) -> None:
        ci = _ci(httpx.MockTransport(lambda r: httpx.Response(500)))
        with pytest.raises(JobNotAllowedError):
            await ci.trigger("prod-deploy", {})

    async def test_trigger_returns_queue_ref(self) -> None:
        state, transport = _fake_jenkins()
        ci = _ci(transport)
        ref = await ci.trigger("twinfix-twin-deploy", {"REPO": "acme/shop", "SHA": "abc"})
        assert ref.job == "twinfix-twin-deploy" and ref.id.startswith("queue/")
        # crumb 已随请求发送，参数作为表单字段
        req = state["posts"][0]
        assert req.headers.get("Jenkins-Crumb") == "c123"
        form = parse_qs(req.content.decode())
        assert form["REPO"] == ["acme/shop"] and form["SHA"] == ["abc"]

    async def test_non_string_params_are_json_encoded(self) -> None:
        state, transport = _fake_jenkins()
        ci = _ci(transport)
        await ci.trigger("twinfix-twin-deploy", {"PORTS": [31000, 31001]})
        form = parse_qs(state["posts"][0].content.decode())
        assert json.loads(form["PORTS"][0]) == [31000, 31001]

    async def test_crumb_disabled_server(self) -> None:
        # Jenkins 未开 crumb issuer（404）时照常触发，不带 crumb 头
        state, transport = _fake_jenkins(crumb=False)
        ci = _ci(transport)
        ref = await ci.trigger("twinfix-twin-deploy", {})
        assert ref.id.startswith("queue/")
        assert "Jenkins-Crumb" not in state["posts"][0].headers

    async def test_requires_credentials(self) -> None:
        with pytest.raises(ValueError, match="requires base_url"):
            JenkinsCI(base_url="", username="u", api_token="t")


class TestStatus:
    async def test_success(self) -> None:
        _, transport = _fake_jenkins()
        ci = _ci(transport)
        ref = await ci.trigger("twinfix-twin-deploy", {})
        st = await ci.status(ref)
        assert st.state == "success" and st.ref.id == "7" and st.url is not None

    async def test_running_while_building(self) -> None:
        _, transport = _fake_jenkins(building=True, result=None)
        ci = _ci(transport)
        st = await ci.status(await ci.trigger("twinfix-twin-deploy", {}))
        assert st.state == "running"

    async def test_failure_and_unstable(self) -> None:
        for result in ("FAILURE", "UNSTABLE"):
            _, transport = _fake_jenkins(result=result)
            ci = _ci(transport)
            st = await ci.status(await ci.trigger("twinfix-twin-deploy", {}))
            assert st.state == "failure", result

    async def test_aborted(self) -> None:
        _, transport = _fake_jenkins(result="ABORTED")
        ci = _ci(transport)
        st = await ci.status(await ci.trigger("twinfix-twin-deploy", {}))
        assert st.state == "aborted"

    async def test_still_queued(self) -> None:
        # 队列条目还没有 executable：status 只查一次，报 queued（轮询交给上层）
        _, transport = _fake_jenkins(queue_delay=1)
        ci = _ci(transport)
        st = await ci.status(await ci.trigger("twinfix-twin-deploy", {}))
        assert st.state == "queued"
        # 第二次查询拿到 executable 了
        st = await ci.status(st.ref)
        assert st.state == "success" and st.ref.id == "7"

    async def test_queue_item_cancelled(self) -> None:
        state, transport = _fake_jenkins()
        state["queue_cancelled"] = True
        ci = _ci(transport)
        st = await ci.status(await ci.trigger("twinfix-twin-deploy", {}))
        assert st.state == "aborted" and st.ref.id == "cancelled"


class TestLogs:
    async def test_tail(self) -> None:
        _, transport = _fake_jenkins()
        ci = _ci(transport)
        ref = await ci.trigger("twinfix-twin-deploy", {})
        assert await ci.logs(ref, tail=2) == "line2\nline3"

    async def test_logs_before_start(self) -> None:
        _, transport = _fake_jenkins(queue_delay=10**9)
        ci = _ci(transport)
        ref = await ci.trigger("twinfix-twin-deploy", {})
        # build 还没拿到 number，logs 返回占位文案而不是阻塞
        text = await ci.logs(ref)
        assert "not started" in text


class TestErrors:
    async def test_missing_queue_location(self) -> None:
        transport = httpx.MockTransport(
            lambda r: (
                httpx.Response(200, json={"crumbRequestField": "c", "crumb": "x"})
                if r.url.path == "/crumbIssuer/api/json"
                else httpx.Response(201)
            )  # 无 Location
        )
        ci = _ci(transport)
        with pytest.raises(JenkinsError):
            await ci.trigger("twinfix-twin-deploy", {})

    async def test_http_error_raises_jenkins_error(self) -> None:
        def handler(r: httpx.Request) -> httpx.Response:
            if r.url.path == "/crumbIssuer/api/json":
                return httpx.Response(404)
            return httpx.Response(500, text="boom")

        ci = _ci(httpx.MockTransport(handler))
        with pytest.raises(JenkinsError) as exc:
            await ci.trigger("twinfix-twin-deploy", {})
        assert exc.value.status == 500
