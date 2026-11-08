"""Tool registry and tool result contract."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .. import config
from ..errors import ToolError
from ..util import json_dumps, now_iso, truncate


@dataclass
class ToolResult:
    ok: bool
    content: str = ""
    summary: str = ""
    data: Any = None
    sources: list[dict] = field(default_factory=list)
    error: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    todos: list[dict] = field(default_factory=list)
    next_todo_id: int = 1
    now: datetime = field(default_factory=datetime.now)
    settings: Any = None
    extra: dict = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], ToolResult]
    source: str = "builtin"

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._lock = threading.RLock()

    def register(self, spec: ToolSpec) -> None:
        with self._lock:
            if spec.name in self._tools:
                raise ValueError(f"duplicate tool: {spec.name}")
            self._tools[spec.name] = spec

    def register_all(self, specs: list[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        with self._lock:
            return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        with self._lock:
            return [self._tools[name] for name in sorted(self._tools)]

    def openai_tools(self) -> list[dict]:
        return [spec.to_openai() for spec in self.all()]

    def public(self) -> list[dict]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "source": spec.source,
            }
            for spec in self.all()
        ]

    def execute(self, name: str, arguments: Any, ctx: ToolContext) -> ToolResult:
        spec = self.get(name)
        if spec is None:
            available = ", ".join(self.names())
            return ToolResult(
                ok=False,
                content=json_dumps(
                    {
                        "ok": False,
                        "error": f"未知工具 {name!r}。可用工具: {available}",
                    }
                ),
                summary=f"未知工具：{name}",
                error=f"unknown tool: {name}",
            )
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return ToolResult(
                ok=False,
                content=json_dumps({"ok": False, "error": "工具参数必须是 JSON 对象"}),
                summary="工具参数格式错误",
                error="arguments must be an object",
            )
        try:
            result = spec.handler(ctx, arguments)
        except ToolError as exc:
            return ToolResult(
                ok=False,
                content=json_dumps({"ok": False, "error": exc.message}),
                summary=exc.message,
                error=exc.message,
            )
        except Exception as exc:  # tool failures are data, not process crashes
            message = f"工具执行失败: {type(exc).__name__}: {exc}"
            return ToolResult(
                ok=False,
                content=json_dumps({"ok": False, "error": message}),
                summary=truncate(message, 300),
                error=message,
            )
        if not isinstance(result, ToolResult):
            return ToolResult(
                ok=False,
                content=json_dumps({"ok": False, "error": "工具返回了非法结果"}),
                summary="工具返回了非法结果",
                error="invalid tool result",
            )
        result.content = truncate(result.content or "", config.TOOL_RESULT_MAX_CHARS)
        result.summary = truncate(result.summary or "", 500)
        return result


def require_str(args: dict, key: str, *, max_len: int = 500) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"参数 {key} 必须是非空字符串")
    value = value.strip()
    if len(value) > max_len:
        raise ToolError(f"参数 {key} 过长（上限 {max_len} 字符）")
    return value


def optional_bool(args: dict, key: str, default: bool) -> bool:
    value = args.get(key, default)
    return bool(value) if isinstance(value, bool) else default


def require_int(args: dict, key: str) -> int:
    value = args.get(key)
    if isinstance(value, bool):
        raise ToolError(f"参数 {key} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"参数 {key} 必须是整数") from None


def todo_view(todo: dict) -> dict:
    return {
        "id": todo.get("id"),
        "title": todo.get("title", ""),
        "done": bool(todo.get("done", False)),
        "updated_at": todo.get("updated_at", ""),
    }


def todos_payload(todos: list[dict]) -> list[dict]:
    return [todo_view(item) for item in todos]


def todo_now() -> str:
    return now_iso()
