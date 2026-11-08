"""DeepSeek/OpenAI-compatible streaming chat client with native tool calls.

No SDK, no agent framework.  The client keeps the exact assistant messages
needed for native tool-call replay, including `reasoning_content` when thinking
mode is enabled (DeepSeek requires it for tool-call continuations).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config
from .errors import LLMError, LLMNotConfigured
from .net import HttpError, open_stream, request_json
from .util import redact_text, truncate


@dataclass
class ToolCall:
    id: str
    name: str
    arguments_raw: str = "{}"
    arguments: dict | None = None


@dataclass
class LLMResult:
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)
    model: str = ""
    latency_ms: int = 0

    def assistant_message(self, *, include_reasoning: bool) -> dict:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.content if self.content else None,
        }
        if include_reasoning:
            message["reasoning_content"] = self.reasoning_content or ""
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_raw,
                    },
                }
                for call in self.tool_calls
            ]
        return message


ContentCallback = Callable[[str], None]
ReasoningCallback = Callable[[str], None]


class OpenAICompatClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = config.LLM_TIMEOUT,
    ) -> None:
        self.base_url = (base_url or config.DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or ""
        self.model = model or config.DEFAULT_MODEL
        self.timeout = timeout

    # -- request construction -------------------------------------------------

    def _chat_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _headers(self, accept: str = "application/json") -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": accept,
            "User-Agent": f"MiniAgent/{config.VERSION}",
        }

    def _body(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None,
        thinking: bool,
        stream: bool,
        max_tokens: int | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "thinking": {"type": "enabled" if thinking else "disabled"},
            "stream": stream,
        }
        if thinking:
            body["reasoning_effort"] = "high"
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if stream:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = tools
        return body

    def _raise_http(self, exc: HttpError) -> None:
        message = redact_text(exc.friendly_message(), [self.api_key])
        status = exc.status
        if status == 401:
            raise LLMError(
                "DeepSeek 鉴权失败：API Key 无效或已过期",
                code="llm_auth_failed",
                http_status=401,
                hint="打开设置检查 API Key",
                detail=message,
            ) from None
        if status == 402:
            raise LLMError(
                "DeepSeek 账户余额不足",
                code="llm_insufficient_balance",
                http_status=402,
                hint="请先在 DeepSeek 平台充值",
                detail=message,
            ) from None
        if status == 429:
            raise LLMError(
                "DeepSeek 请求过于频繁，请稍后再试",
                code="llm_rate_limited",
                http_status=429,
                detail=message,
            ) from None
        if status == 0:
            raise LLMError(
                "无法连接 DeepSeek 服务",
                code="llm_network_error",
                http_status=502,
                hint="检查网络、Base URL 或代理设置",
                detail=message,
            ) from None
        raise LLMError(
            f"DeepSeek 请求失败（HTTP {status}）",
            code="llm_http_error",
            http_status=502,
            detail=message,
        ) from None

    # -- streaming -----------------------------------------------------------

    def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        thinking: bool = False,
        on_content: ContentCallback | None = None,
        on_reasoning: ReasoningCallback | None = None,
    ) -> LLMResult:
        if not self.api_key:
            raise LLMNotConfigured()
        started = time.monotonic()
        body = self._body(messages, tools=tools, thinking=thinking, stream=True)
        try:
            response = open_stream(
                self._chat_url(),
                headers=self._headers("text/event-stream"),
                payload=body,
                timeout=self.timeout,
            )
        except HttpError as exc:
            self._raise_http(exc)
            raise AssertionError("unreachable")

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        finish_reason = "stop"
        usage: dict = {}
        model = self.model
        try:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace") if isinstance(raw_line, bytes) else raw_line
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                model = event.get("model") or model
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] or {}
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if isinstance(text, str) and text:
                    content_parts.append(text)
                    if on_content:
                        on_content(text)
                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    reasoning_parts.append(reasoning)
                    if on_reasoning:
                        on_reasoning(reasoning)
                for position, item in enumerate(delta.get("tool_calls") or []):
                    if not isinstance(item, dict):
                        continue
                    try:
                        index = int(item.get("index", position))
                    except (TypeError, ValueError):
                        index = position
                    slot = tool_acc.setdefault(
                        index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    if item.get("id"):
                        slot["id"] = item["id"]
                    fn = item.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments") is not None:
                        slot["arguments"] += str(fn.get("arguments") or "")
        except (HttpError, OSError, ValueError) as exc:
            message = redact_text(str(exc), [self.api_key])
            raise LLMError(
                "读取 DeepSeek 流式响应失败",
                code="llm_stream_error",
                http_status=502,
                detail=message,
            ) from None
        finally:
            try:
                response.close()
            except Exception:
                pass

        calls: list[ToolCall] = []
        for index in sorted(tool_acc):
            slot = tool_acc[index]
            raw = slot.get("arguments") or "{}"
            parsed: dict | None
            try:
                candidate = json.loads(raw) if raw.strip() else {}
                parsed = candidate if isinstance(candidate, dict) else None
            except ValueError:
                parsed = None
            calls.append(
                ToolCall(
                    id=slot.get("id") or f"call_{index}",
                    name=slot.get("name") or "",
                    arguments_raw=raw,
                    arguments=parsed,
                )
            )

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        if not content and not calls:
            raise LLMError("模型没有返回内容或工具调用", code="llm_empty_response", http_status=502)
        if finish_reason == "length":
            raise LLMError(
                "模型输出达到长度上限，回答被截断",
                code="llm_truncated",
                http_status=502,
                hint="可以缩短问题或重试",
            )
        return LLMResult(
            content=content,
            reasoning_content=reasoning,
            tool_calls=calls,
            finish_reason=finish_reason,
            usage=usage,
            model=model,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    # -- non-streaming maintenance call --------------------------------------

    def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 600,
        thinking: bool = False,
    ) -> LLMResult:
        if not self.api_key:
            raise LLMNotConfigured()
        started = time.monotonic()
        body = self._body(
            messages,
            tools=None,
            thinking=thinking,
            stream=False,
            max_tokens=max_tokens,
        )
        try:
            data = request_json(
                self._chat_url(),
                method="POST",
                headers=self._headers(),
                payload=body,
                timeout=self.timeout,
            )
        except HttpError as exc:
            self._raise_http(exc)
            raise AssertionError("unreachable")
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("模型没有返回内容", code="llm_empty_response", http_status=502)
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        if not isinstance(content, str):
            content = str(content)
        if not isinstance(reasoning, str):
            reasoning = ""
        return LLMResult(
            content=content,
            reasoning_content=reasoning,
            usage=data.get("usage") or {},
            model=data.get("model") or self.model,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def make_client(settings, *, timeout: float = config.LLM_TIMEOUT) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
        timeout=timeout,
    )


def assistant_message_with_reasoning(result: LLMResult, thinking: bool) -> dict:
    return result.assistant_message(include_reasoning=thinking)


def tool_call_summary(call: ToolCall) -> str:
    name = call.name or "unknown"
    return f"{name}({truncate(call.arguments_raw, 120)})"
