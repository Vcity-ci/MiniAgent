"""The hand-written agent loop.

Core invariant: a turn is a fully in-memory draft until the final answer is
ready.  Nothing about the draft is written to the session or used as context
by other turns/sessions.  A failure discards the entire turn.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .. import config
from ..errors import (
    AgentError,
    ClientDisconnected,
    InvalidRequest,
    LLMError,
    MaxStepsError,
)
from ..llm import LLMResult
from ..tools import ToolContext, ToolRegistry, ToolResult, build_default_registry
from ..util import json_dumps, new_id, now_iso, truncate
from .context import build_messages, context_stats, maybe_summarize
from .images import ImageStore
from .events import EventEmitter
from .profile import ProfileManager
from .session import Turn
from .store import GlobalState, SessionStore


TOOL_LABELS = {
    "todo_add": "添加待办",
    "todo_list": "查看待办",
    "todo_complete": "完成待办",
    "todo_delete": "删除待办",
    "calculate": "计算",
    "get_weather": "查询天气",
    "web_search": "联网搜索",
    "fetch_url": "抓取网页",
}


@dataclass
class TurnDraft:
    id: str
    user: str
    messages: list[dict] = field(default_factory=list)
    todos: list[dict] = field(default_factory=list)
    next_todo_id: int = 1
    steps: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    images: list[dict] = field(default_factory=list)
    final: str = ""
    trace_id: str = ""
    started_at: float = field(default_factory=time.monotonic)


class Runtime:
    def __init__(
        self,
        *,
        settings,
        store: SessionStore,
        state: GlobalState,
        profile: ProfileManager,
        trace,
        llm_factory: Callable[[Any], Any],
        mcp=None,
        base_registry: ToolRegistry | None = None,
        images: ImageStore | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.state = state
        self.profile = profile
        self.trace = trace
        self.llm_factory = llm_factory
        self.mcp = mcp
        self.base_registry = base_registry or build_default_registry()
        self.images = images

    # -- helpers -------------------------------------------------------------

    def _online_status(self, settings) -> dict:
        if not getattr(settings, "online", False):
            return {"enabled": False, "status": "off", "tool_names": []}
        if self.mcp is None:
            return {"enabled": True, "status": "error", "error": "MCP 未初始化", "tool_names": []}
        try:
            status = dict(self.mcp.status() or {})
        except Exception as exc:
            return {"enabled": True, "status": "error", "error": str(exc), "tool_names": []}
        status.setdefault("enabled", True)
        status.setdefault("tool_names", [])
        return status

    def _build_registry(self, settings) -> ToolRegistry:
        registry = ToolRegistry()
        for spec in self.base_registry.all():
            registry.register(spec)
        if getattr(settings, "online", False) and self.mcp is not None:
            try:
                for spec in self.mcp.tool_specs() or []:
                    if registry.get(spec.name) is None:
                        registry.register(spec)
            except Exception as exc:
                self.trace.log("mcp_tools_error", data={"error": str(exc)[:500]})
        return registry

    @staticmethod
    def _add_sources(target: list[dict], sources: list[dict] | None) -> None:
        if not sources:
            return
        seen = {str(item.get("url") or "") for item in target}
        for source in sources:
            url = str((source or {}).get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            target.append(
                {
                    "title": str((source or {}).get("title") or url)[:200],
                    "url": url,
                }
            )

    @staticmethod
    def _add_usage(target: dict, usage: dict | None) -> None:
        if not isinstance(usage, dict):
            return
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                target[key] = int(target.get(key, 0)) + int(value)

    @staticmethod
    def _tool_label(name: str) -> str:
        return TOOL_LABELS.get(name, name or "工具")

    def _error_emit(self, emitter: EventEmitter, exc: BaseException) -> None:
        if isinstance(exc, AgentError):
            payload = exc.to_dict()
        else:
            payload = {"code": "internal_error", "message": "服务内部错误"}
        try:
            emitter.emit("error", payload)
            emitter.emit("done", {})
        except ClientDisconnected:
            pass
        except Exception:
            pass

    # -- main entry ----------------------------------------------------------

    def handle(
        self,
        session_id: str,
        content: str,
        emitter: EventEmitter,
        image_ids: list[str] | None = None,
    ) -> Turn | None:
        content = (content or "").strip()
        raw_ids = list(image_ids or [])
        if not content and not raw_ids:
            raise InvalidRequest("消息不能为空")
        if len(content) > config.MAX_INPUT_CHARS:
            raise InvalidRequest(
                f"消息过长（上限 {config.MAX_INPUT_CHARS} 字）",
                hint="请分段发送或先总结",
            )
        if len(raw_ids) > config.MAX_IMAGES_PER_TURN:
            raise InvalidRequest(
                f"每条消息最多 {config.MAX_IMAGES_PER_TURN} 张图片",
                code="too_many_images",
            )
        ids: list[str] = []
        for raw in raw_ids:
            if not isinstance(raw, str) or not raw.strip():
                raise InvalidRequest("image_ids 必须是图片 ID 字符串")
            value = raw.strip()
            if value not in ids:
                ids.append(value)
        if len(ids) > config.MAX_IMAGES_PER_TURN:
            raise InvalidRequest(
                f"每条消息最多 {config.MAX_IMAGES_PER_TURN} 张图片",
                code="too_many_images",
            )

        with self.store.lock(session_id):
            session = self.store.get(session_id)
            settings = self.settings.snapshot()
            if ids and not config.supports_vision(settings.model):
                raise InvalidRequest(
                    "当前模型不支持图片，请切换到 deepseek-flash",
                    code="vision_unsupported",
                    hint="deepseek-v4-pro 仅支持文本",
                )
            image_meta: list[dict] = []
            image_bytes = 0
            user_content: str | list[dict]
            if ids:
                if self.images is None:
                    raise InvalidRequest("图片存储未初始化", code="image_store_unavailable")
                for image_id in ids:
                    raw, info = self.images.read(session.id, image_id)
                    image_bytes += len(raw)
                    image_meta.append(
                        {
                            "id": image_id,
                            "mime": info["mime"],
                            "bytes": len(raw),
                            "width": info["width"],
                            "height": info["height"],
                            "name": f"image.{info['ext']}",
                            "created_at": now_iso(),
                        }
                    )
                if image_bytes > config.MAX_REQUEST_IMAGE_BYTES:
                    raise InvalidRequest(
                        "本轮图片总大小超过限制",
                        code="image_too_large",
                        http_status=413,
                    )
                user_blocks: list[dict] = [
                    {"type": "text", "text": content or "请描述这张图片。"}
                ]
                user_blocks.extend(
                    {"type": "image_ref", "image_id": image_id} for image_id in ids
                )
                user_content = user_blocks
            else:
                user_content = content

            client = self.llm_factory(settings)
            turn_id = new_id("t")
            trace_id = new_id("tr")
            display_user = content or "（图片）"
            draft = TurnDraft(
                id=turn_id,
                user=display_user,
                messages=[{"role": "user", "content": user_content}],
                todos=copy.deepcopy(session.todos),
                next_todo_id=int(session.next_todo_id or 1),
                images=image_meta,
                trace_id=trace_id,
            )
            started = time.monotonic()
            emitter.emit(
                "turn_start",
                {
                    "turn_id": turn_id,
                    "session_id": session.id,
                    "user": display_user,
                    "images": copy.deepcopy(image_meta),
                },
            )
            self.trace.log(
                "turn_start",
                trace_id=trace_id,
                session_id=session.id,
                turn_id=turn_id,
                data={
                    "chars": len(content),
                    "images": len(image_meta),
                    "image_bytes": image_bytes,
                    "thinking": settings.thinking,
                    "online": settings.online,
                },
            )
            try:
                # Optional maintenance, based only on already-committed turns.
                if session.pending_turns() > config.HISTORY_WINDOW:
                    emitter.emit("status", {"text": "整理历史摘要"})
                    maybe_summarize(session, settings, client, self.store, self.trace)

                profile = self.profile.get()
                counter = self.state.current()
                if (
                    getattr(settings, "profile_auto", True)
                    and counter - int(profile.get("last_turn_counter") or 0) >= config.PROFILE_EVERY
                ):
                    emitter.emit("status", {"text": "更新用户画像"})
                    outcome = self.profile.maybe_update(
                        self.store, self.state, settings, client, self.trace
                    )
                    if outcome.get("updated"):
                        profile = self.profile.get()
                        emitter.emit("profile", {"profile": self.profile.public()})
                    else:
                        profile = self.profile.get()

                online_status = self._online_status(settings)
                registry = self._build_registry(settings)
                tool_specs = registry.openai_tools()
                tool_ctx = ToolContext(
                    todos=draft.todos,
                    next_todo_id=draft.next_todo_id,
                    now=datetime.now().astimezone(),
                    settings=settings,
                )

                final_reached = False
                for step in range(1, max(1, int(settings.max_steps)) + 1):
                    emitter.emit("step_start", {"step": step, "max_steps": settings.max_steps})
                    messages = build_messages(
                        session, draft, settings, profile, online_status
                    )
                    image_stats = {"images_sent": 0, "images_omitted": 0, "image_bytes": 0}
                    if self.images is not None:
                        messages, image_stats = self.images.hydrate_messages(
                            messages, session.id
                        )
                    stats = context_stats(session, draft, settings, profile, online_status)
                    stats.update(image_stats)
                    self.trace.log(
                        "context",
                        trace_id=trace_id,
                        session_id=session.id,
                        turn_id=turn_id,
                        step=step,
                        data=stats,
                    )
                    self.trace.log(
                        "llm_request",
                        trace_id=trace_id,
                        session_id=session.id,
                        turn_id=turn_id,
                        step=step,
                        data={
                            "model": settings.model,
                            "thinking": settings.thinking,
                            "messages": len(messages),
                            "tools": [spec.name for spec in registry.all()],
                            "online": online_status.get("status"),
                            **image_stats,
                        },
                    )
                    emitter.emit("status", {"text": "模型生成中"})

                    content_parts: list[str] = []
                    reasoning_parts: list[str] = []

                    def on_content(text: str) -> None:
                        content_parts.append(text)
                        emitter.emit("assistant_delta", {"step": step, "text": text})

                    def on_reasoning(text: str) -> None:
                        reasoning_parts.append(text)

                    result: LLMResult = client.stream_chat(
                        messages,
                        tools=tool_specs,
                        thinking=settings.thinking,
                        on_content=on_content,
                        on_reasoning=on_reasoning,
                    )
                    self._add_usage(draft.usage, result.usage)
                    self.trace.log(
                        "llm_response",
                        trace_id=trace_id,
                        session_id=session.id,
                        turn_id=turn_id,
                        step=step,
                        duration_ms=getattr(result, "latency_ms", None),
                        data={
                            "content_chars": len(result.content or ""),
                            "reasoning_chars": len(result.reasoning_content or ""),
                            "tool_calls": len(result.tool_calls or []),
                            "finish_reason": result.finish_reason,
                            "usage": result.usage,
                        },
                    )
                    if result.reasoning_content:
                        emitter.emit(
                            "reasoning",
                            {"step": step, "text": result.reasoning_content},
                        )

                    if result.tool_calls:
                        assistant_message = result.assistant_message(
                            include_reasoning=bool(settings.thinking)
                        )
                        draft.messages.append(assistant_message)
                        preamble = "".join(content_parts).strip()
                        if preamble or result.reasoning_content:
                            draft.steps.append(
                                {
                                    "type": "model",
                                    "step": step,
                                    "text": preamble,
                                    "reasoning": result.reasoning_content,
                                }
                            )
                        for call in result.tool_calls:
                            label = self._tool_label(call.name)
                            emitter.emit(
                                "tool_start",
                                {"step": step, "name": call.name, "label": label},
                            )
                            tool_started = time.monotonic()
                            if call.arguments is None:
                                tool_result = ToolResult(
                                    ok=False,
                                    content=json_dumps(
                                        {
                                            "ok": False,
                                            "error": "工具参数不是合法 JSON 对象",
                                        }
                                    ),
                                    summary="工具参数不是合法 JSON 对象",
                                    error="invalid tool arguments",
                                )
                            else:
                                tool_result = registry.execute(
                                    call.name, call.arguments, tool_ctx
                                )
                            duration_ms = int((time.monotonic() - tool_started) * 1000)
                            draft.next_todo_id = int(tool_ctx.next_todo_id or draft.next_todo_id)
                            draft.messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call.id,
                                    "content": tool_result.content
                                    or json_dumps(
                                        {
                                            "ok": tool_result.ok,
                                            "error": tool_result.error or "empty result",
                                        }
                                    ),
                                }
                            )
                            self._add_sources(draft.sources, tool_result.sources)
                            public_step = {
                                "type": "tool",
                                "step": step,
                                "name": call.name,
                                "label": label,
                                "ok": bool(tool_result.ok),
                                "summary": tool_result.summary
                                or tool_result.error
                                or ("完成" if tool_result.ok else "失败"),
                                "sources": copy.deepcopy(tool_result.sources),
                                "duration_ms": duration_ms,
                            }
                            draft.steps.append(public_step)
                            emitter.emit(
                                "tool_end",
                                {
                                    "step": step,
                                    "name": call.name,
                                    "label": label,
                                    "ok": bool(tool_result.ok),
                                    "summary": public_step["summary"],
                                    "sources": public_step["sources"],
                                    "duration_ms": duration_ms,
                                },
                            )
                            self.trace.log(
                                "tool_result",
                                trace_id=trace_id,
                                session_id=session.id,
                                turn_id=turn_id,
                                step=step,
                                duration_ms=duration_ms,
                                data={
                                    "name": call.name,
                                    "arguments": truncate(call.arguments_raw, 800),
                                    "ok": tool_result.ok,
                                    "summary": truncate(public_step["summary"], 500),
                                    "sources": public_step["sources"],
                                },
                            )
                        continue

                    final_text = (result.content or "").strip()
                    if not final_text:
                        raise LLMError("模型返回了空答案", code="llm_empty_response", http_status=502)
                    draft.messages.append(
                        result.assistant_message(include_reasoning=bool(settings.thinking))
                    )
                    if result.reasoning_content:
                        draft.steps.append(
                            {
                                "type": "model",
                                "step": step,
                                "text": "",
                                "reasoning": result.reasoning_content,
                            }
                        )
                    draft.final = final_text
                    final_reached = True
                    break

                if not final_reached:
                    raise MaxStepsError(
                        f"已达到本轮最大步骤数（{settings.max_steps}）",
                        hint="把任务拆小，或在设置中提高最大步骤数",
                    )

                turn = Turn(
                    id=draft.id,
                    user=draft.user,
                    messages=draft.messages,
                    steps=draft.steps,
                    final=draft.final,
                    sources=copy.deepcopy(draft.sources),
                    usage=dict(draft.usage),
                    images=copy.deepcopy(draft.images),
                    created_at=now_iso(),
                    trace_id=draft.trace_id,
                )
                # Commit the whole turn only now.
                seq = self.state.increment()
                turn.global_seq = seq
                session.turns.append(turn)
                session.todos = copy.deepcopy(draft.todos)
                session.next_todo_id = max(
                    int(session.next_todo_id or 1), int(draft.next_todo_id or 1)
                )
                if not session.title:
                    session.title = session.default_title(display_user)
                session.updated_at = now_iso()
                self.store.save(session)
                self.trace.log(
                    "turn_commit",
                    trace_id=trace_id,
                    session_id=session.id,
                    turn_id=turn.id,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    data={
                        "steps": len(turn.steps),
                        "final_chars": len(turn.final),
                        "sources": len(turn.sources),
                        "images": len(turn.images),
                        "todos": len(session.todos),
                        "usage": turn.usage,
                    },
                )
                emitter.emit(
                    "final",
                    {
                        "turn": turn.to_public(),
                        "todos": copy.deepcopy(session.todos),
                        "title": session.title,
                        "sources": copy.deepcopy(turn.sources),
                        "usage": turn.usage,
                    },
                )
                emitter.emit("commit", {"turn_id": turn.id})
                emitter.emit("done", {})
                return turn
            except ClientDisconnected:
                self.trace.log(
                    "client_disconnected",
                    trace_id=trace_id,
                    session_id=session.id,
                    turn_id=turn_id,
                )
                raise
            except AgentError as exc:
                self.trace.log(
                    "turn_error",
                    trace_id=trace_id,
                    session_id=session.id,
                    turn_id=turn_id,
                    data={"code": exc.code, "message": exc.message},
                )
                self._error_emit(emitter, exc)
                return None
            except Exception as exc:
                self.trace.log(
                    "turn_error",
                    trace_id=trace_id,
                    session_id=session.id,
                    turn_id=turn_id,
                    data={"type": type(exc).__name__, "message": str(exc)[:500]},
                )
                self._error_emit(
                    emitter,
                    LLMError("本轮执行失败，未保存", code="internal_error", http_status=500),
                )
                return None
