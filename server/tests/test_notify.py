"""FeishuNotifier 的 webhook 行为：interactive 卡片、签名字段、错误聚合。

不依赖真实飞书：httpx.MockTransport 拦截 POST，检查 body 与状态码/返回码的处理。
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import httpx

from twinfix.adapters.notify import FeishuNotifier
from twinfix.adapters.types import OutgoingReport, Recipient

REPORT = OutgoingReport(id="r1", incident_id="i1", type="fix_ready", title="修复就绪", content="# 根因\nNPE")
WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/abc"


def _feishu(recorder: list[httpx.Request], *, code: int = 0, status: int = 200, secret=None):
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if status != 200:
            return httpx.Response(status, text="oops")
        return httpx.Response(200, json={"code": code, "msg": "x" if code else "ok"})

    return FeishuNotifier(sign_secret=secret, transport=httpx.MockTransport(handler))


def _recipients(*urls: str) -> list[Recipient]:
    return [Recipient(channel="feishu", address=u) for u in urls]


class TestSend:
    async def test_no_feishu_recipients_is_ok(self) -> None:
        requests: list[httpx.Request] = []
        notifier = _feishu(requests)
        res = await notifier.send(REPORT, [Recipient(channel="email", address="a@b.c")])
        assert res.ok and requests == []

    async def test_posts_interactive_card(self) -> None:
        requests: list[httpx.Request] = []
        notifier = _feishu(requests)
        res = await notifier.send(REPORT, _recipients(WEBHOOK))
        assert res.ok and len(requests) == 1
        import json

        body = json.loads(requests[0].content)
        assert body["msg_type"] == "interactive"
        assert body["card"]["header"]["title"]["content"] == "[TwinFix] 修复就绪"
        assert "根因" in body["card"]["elements"][0]["content"]
        # 未配签名密钥时不带 timestamp/sign
        assert "sign" not in body and "timestamp" not in body

    async def test_multiple_webhooks(self) -> None:
        requests: list[httpx.Request] = []
        notifier = _feishu(requests)
        res = await notifier.send(REPORT, _recipients(WEBHOOK, WEBHOOK + "2"))
        assert res.ok and len(requests) == 2

    async def test_nonzero_code_fails(self) -> None:
        notifier = _feishu([], code=19021)
        res = await notifier.send(REPORT, _recipients(WEBHOOK))
        assert not res.ok and "19021" in (res.error or "")

    async def test_http_error_fails(self) -> None:
        notifier = _feishu([], status=500)
        res = await notifier.send(REPORT, _recipients(WEBHOOK))
        assert not res.ok and "500" in (res.error or "")

    async def test_partial_failure_aggregates(self) -> None:
        # 第一个 webhook 成功，第二个飞书返回业务错误
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"code": 0 if calls["n"] == 1 else 9499, "msg": "bad"})

        notifier = FeishuNotifier(transport=httpx.MockTransport(handler))
        res = await notifier.send(REPORT, _recipients(WEBHOOK, WEBHOOK + "2"))
        assert not res.ok and "9499" in (res.error or "")


class TestSigning:
    async def test_signed_payload(self) -> None:
        requests: list[httpx.Request] = []
        notifier = _feishu(requests, secret="s3cret")
        res = await notifier.send(REPORT, _recipients(WEBHOOK))
        assert res.ok
        import json

        body = json.loads(requests[0].content)
        ts, sign = body["timestamp"], body["sign"]
        assert ts.isdigit()
        expected = base64.b64encode(hmac.new(f"{ts}\ns3cret".encode(), b"", hashlib.sha256).digest())
        assert sign == expected.decode()


class TestApprovalCard:
    async def _card(self, report_type: str, console: str | None = "https://twinfix.example.com/") -> dict:
        import json

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"code": 0})

        notifier = FeishuNotifier(console_url=console, transport=httpx.MockTransport(handler))
        report = OutgoingReport(id="r1", incident_id="inc1", type=report_type, title="t", content="body")
        assert (await notifier.send(report, _recipients(WEBHOOK))).ok
        card: dict = json.loads(requests[0].content)["card"]
        return card

    async def test_fixed_report_has_review_buttons_that_only_link(self) -> None:
        card = await self._card("fixed")
        assert card["header"]["template"] == "orange"
        actions = card["elements"][-1]["actions"]
        urls = [a["url"] for a in actions]
        assert urls == [
            "https://twinfix.example.com/incidents/inc1?action=approve",
            "https://twinfix.example.com/incidents/inc1?action=reject",
            "https://twinfix.example.com/incidents/inc1",
        ]
        # 只跳转：按钮不带回调 value，机器人无法替人执行合并
        assert all("value" not in a for a in actions)

    async def test_terminal_report_has_details_link_and_neutral_color(self) -> None:
        card = await self._card("not_bug")
        assert card["header"]["template"] == "grey"
        assert [a["text"]["content"] for a in card["elements"][-1]["actions"]] == ["查看详情"]

    async def test_no_console_url_means_no_buttons(self) -> None:
        card = await self._card("fixed", console=None)
        assert [e["tag"] for e in card["elements"]] == ["markdown"]
