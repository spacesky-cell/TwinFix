"""通知适配器。报告以 Markdown 为源，各渠道自行渲染。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import smtplib
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, ClassVar, Protocol

import httpx

from twinfix.adapters.types import OutgoingReport, Recipient, SendResult

log = logging.getLogger(__name__)


class Notifier(Protocol):
    channel: str

    async def send(self, report: OutgoingReport, recipients: list[Recipient]) -> SendResult: ...


@dataclass
class LogNotifier:
    """开发环境默认渠道：写日志，内容同时在控制台的报告页可见。"""

    channel: str = "log"
    sent: list[tuple[OutgoingReport, list[Recipient]]] = field(default_factory=list)

    async def send(self, report: OutgoingReport, recipients: list[Recipient]) -> SendResult:
        self.sent.append((report, recipients))
        log.info("report %s [%s] %s", report.id, report.type, report.title)
        return SendResult(ok=True)


@dataclass
class EmailNotifier:
    host: str
    port: int
    sender: str
    username: str | None = None
    password: str | None = None
    use_tls: bool = True
    channel: str = "email"

    async def send(self, report: OutgoingReport, recipients: list[Recipient]) -> SendResult:
        to = [r.address for r in recipients if r.channel == self.channel]
        if not to:
            return SendResult(ok=True)
        msg = EmailMessage()
        msg["Subject"] = f"[TwinFix] {report.title}"
        msg["From"] = self.sender
        msg["To"] = ", ".join(to)
        # 当前发送 Markdown 纯文本；HTML 模板待实现
        msg.set_content(report.content)
        try:
            await asyncio.to_thread(self._deliver, msg)
        except (smtplib.SMTPException, OSError) as exc:
            return SendResult(ok=False, error=str(exc))
        return SendResult(ok=True)

    def _deliver(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            if self.use_tls:
                smtp.starttls()
            if self.username and self.password:
                smtp.login(self.username, self.password)
            smtp.send_message(msg)


class FeishuNotifier:
    """飞书自定义机器人：按群 webhook 推送 interactive 卡片（docs/04 §通知）。

    - recipients 里 channel="feishu" 的 address 必须是完整 webhook URL
      （机器人绑定的是群而不是人，无法从一个全局 webhook 推导收件人）。
    - 配置了 sign_secret 时按飞书规则做 timestamp + HMAC-SHA256 签名校验。
    - 报告是 Markdown，转成一个 lark_md 卡片元素；飞书 lark_md 支持大部分
      常用语法，少数扩展语法（如 task list）会原样展示，可接受。
    """

    channel: str = "feishu"
    _MAX_ELEMENT = 20000  # lark_md 单元素内容上限附近，留余量截断

    # 卡片头颜色：待评审 / 需人工醒目，终态结论用中性色
    _TEMPLATE: ClassVar[dict[str, str]] = {
        "fixed": "orange",
        "needs_human": "red",
        "cannot_fix": "red",
        "merged": "green",
        "already_fixed": "green",
        "not_bug": "grey",
        "cannot_reproduce": "grey",
        "cancelled": "grey",
    }

    def __init__(
        self,
        *,
        sign_secret: str | None = None,
        console_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._sign_secret = sign_secret
        self._console = console_url.rstrip("/") if console_url else None
        self._http = httpx.AsyncClient(timeout=30, transport=transport)

    async def send(self, report: OutgoingReport, recipients: list[Recipient]) -> SendResult:
        urls = [r.address for r in recipients if r.channel == self.channel]
        if not urls:
            return SendResult(ok=True)
        body = self._payload(report)
        errors: list[str] = []
        for url in urls:
            try:
                resp = await self._http.post(url, json=body)
            except httpx.HTTPError as exc:
                errors.append(str(exc))
                continue
            # 飞书机器人返回 {"code":0,...}；非 0 即失败（签名错/限流等）
            if resp.status_code != 200:
                errors.append(f"http {resp.status_code}: {resp.text[:200]}")
                continue
            try:
                data = resp.json()
            except ValueError:
                errors.append(f"invalid response: {resp.text[:200]}")
                continue
            if data.get("code") != 0:
                errors.append(f"code={data.get('code')}: {data.get('msg', '')[:200]}")
        return SendResult(ok=not errors, error="; ".join(errors) or None)

    def _payload(self, report: OutgoingReport) -> dict[str, Any]:
        elements: list[dict[str, Any]] = [{"tag": "markdown", "content": report.content[: self._MAX_ELEMENT]}]
        if self._console:
            elements += [{"tag": "hr"}, {"tag": "action", "actions": self._actions(report)}]
        card: dict[str, Any] = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": self._TEMPLATE.get(report.type, "blue"),
                "title": {"tag": "plain_text", "content": f"[TwinFix] {report.title}"},
            },
            "elements": elements,
        }
        body: dict[str, Any] = {"msg_type": "interactive", "card": card}
        if self._sign_secret:
            ts = str(int(time.time()))
            body["timestamp"] = ts
            body["sign"] = self._sign(ts)
        return body

    def _actions(self, report: OutgoingReport) -> list[dict[str, Any]]:
        """审批按钮只跳转到控制台对应页面，由登录后的人在控制台操作（docs/05-safety.md §7）。

        机器人卡片的回调没有用户身份与签名校验，不能直接执行合并/驳回。
        """
        page = f"{self._console}/incidents/{report.incident_id}"

        def button(text: str, url: str, kind: str = "default") -> dict[str, Any]:
            return {"tag": "button", "text": {"tag": "plain_text", "content": text}, "type": kind, "url": url}

        if report.type == "fixed":
            return [
                button("审阅并批准合并", f"{page}?action=approve", "primary"),
                button("驳回并附意见", f"{page}?action=reject", "danger"),
                button("查看详情", page),
            ]
        if report.type == "needs_human":
            return [button("去处理", page, "primary")]
        return [button("查看详情", page)]

    def _sign(self, timestamp: str) -> str:
        # 官方算法：以 f"{ts}\n{secret}" 为 key 对空串做 HMAC-SHA256，再 base64
        digest = hmac.new(f"{timestamp}\n{self._sign_secret}".encode(), b"", hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    async def aclose(self) -> None:
        await self._http.aclose()
