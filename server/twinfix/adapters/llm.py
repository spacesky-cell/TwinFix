"""LLM 客户端：统一为 OpenAI 兼容协议（tool calling）。按任务路由模型；失败降级待加入。"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ChatRequest:
    messages: list[Message]
    model: str | None = None
    tools: list[ToolSpec] = field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int | None = None
    # 覆盖客户端默认的 reasoning_effort（项目级配置）；None = 用客户端默认，"" = 不发送
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class ChatResponse:
    message: Message
    usage: Usage
    model: str
    latency_ms: int
    finish_reason: str | None = None


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    async def chat(self, req: ChatRequest) -> ChatResponse: ...


class OpenAICompatibleLLM:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str,
        *,
        timeout_s: float = 120,
        retries: int = 4,
        backoff_s: float = 2.0,
        reasoning_effort: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.default_model = default_model
        self.retries = retries
        self.backoff_s = backoff_s
        # 推理模型的思考深度（OpenAI 兼容字段 reasoning_effort）；空 = 不发送，用模型默认
        self.reasoning_effort = reasoning_effort
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
            transport=transport,
        )

    async def chat(self, req: ChatRequest) -> ChatResponse:
        model = req.model or self.default_model
        body: dict[str, Any] = {
            "model": model,
            "messages": [_encode_message(m) for m in req.messages],
            "temperature": req.temperature,
        }
        if req.max_tokens:
            body["max_tokens"] = req.max_tokens
        effort = self.reasoning_effort if req.reasoning_effort is None else req.reasoning_effort
        if effort:
            body["reasoning_effort"] = effort
        if req.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in req.tools
            ]
        start = time.monotonic()
        resp = await self._post_with_retry(body)
        data = resp.json()
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        return ChatResponse(
            message=_decode_message(choice["message"]),
            usage=Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
            model=data.get("model", model),
            latency_ms=int((time.monotonic() - start) * 1000),
            finish_reason=choice.get("finish_reason"),
        )

    async def _post_with_retry(self, body: dict[str, Any]) -> httpx.Response:
        """网关抖动（429/5xx、连接错误）指数退避重试；4xx 等确定性错误立即失败。

        一轮 Agent 循环可能已累积几十轮上下文，单次抖动让整步重来代价很高。
        """
        for attempt in range(self.retries + 1):
            try:
                resp = await self._http.post("/chat/completions", json=body)
                if resp.status_code not in _RETRYABLE or attempt == self.retries:
                    resp.raise_for_status()
                    return resp
            except httpx.HTTPStatusError as exc:
                raise LLMError(f"llm request failed: {exc}") from exc
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise LLMError(f"llm request failed: {exc}") from exc
            await asyncio.sleep(self.backoff_s * 2**attempt)
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        await self._http.aclose()


# 520-524：Cloudflare 等前置代理的源站错误/超时（长推理时常见）
_RETRYABLE = frozenset({429, 500, 502, 503, 504, 520, 522, 524})


def _encode_message(m: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in m.tool_calls
        ]
    if m.tool_call_id:
        out["tool_call_id"] = m.tool_call_id
    return out


def _decode_message(raw: dict[str, Any]) -> Message:
    calls = []
    for c in raw.get("tool_calls") or []:
        try:
            args = json.loads(c["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"__raw__": c["function"].get("arguments")}
        calls.append(ToolCallRequest(id=c["id"], name=c["function"]["name"], arguments=args))
    content = raw.get("content")
    # 推理模型（MiniMax / DeepSeek-R 系）偶尔把答案只放在 reasoning_content 里、content 为空
    if not calls and not (content or "").strip() and raw.get("reasoning_content"):
        content = raw["reasoning_content"]
    return Message(role="assistant", content=content, tool_calls=calls)


class ScriptedLLM:
    """按顺序返回预置回复，用于测试 Agent 循环。"""

    def __init__(self, replies: list[Message], model: str = "scripted") -> None:
        self.replies = deque(replies)
        self.requests: list[ChatRequest] = []
        self.model = model

    async def chat(self, req: ChatRequest) -> ChatResponse:
        self.requests.append(req)
        if not self.replies:
            raise LLMError("scripted llm exhausted")
        return ChatResponse(message=self.replies.popleft(), usage=Usage(100, 20), model=self.model, latency_ms=1)
