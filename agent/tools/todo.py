"""Session-scoped todo tools.

The runtime gives these tools a *draft* copy of the session todos.  Nothing is
persisted until the whole turn commits, so a failed turn cannot leave a
half-added todo behind.
"""
from __future__ import annotations

from ..errors import ToolError
from ..util import json_dumps, now_iso
from .base import (
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    optional_bool,
    require_int,
    require_str,
    todos_payload,
)


def _find(todos: list[dict], todo_id: int) -> dict:
    for item in todos:
        if item.get("id") == todo_id:
            return item
    raise ToolError(f"找不到待办 #{todo_id}")


def _result(todos: list[dict], summary: str) -> ToolResult:
    payload = {"ok": True, "todos": todos_payload(todos)}
    return ToolResult(
        ok=True,
        content=json_dumps(payload),
        summary=summary,
        data=payload,
    )


def todo_add(ctx: ToolContext, args: dict) -> ToolResult:
    title = require_str(args, "title", max_len=200)
    todo_id = max(1, int(ctx.next_todo_id or 1))
    now = now_iso()
    ctx.todos.append(
        {
            "id": todo_id,
            "title": title,
            "done": False,
            "created_at": now,
            "updated_at": now,
        }
    )
    ctx.next_todo_id = todo_id + 1
    return _result(ctx.todos, f"已添加待办 #{todo_id}：{title}")


def todo_list(ctx: ToolContext, args: dict) -> ToolResult:
    include_done = optional_bool(args, "include_done", True)
    rows = [item for item in ctx.todos if include_done or not item.get("done")]
    total = len(ctx.todos)
    open_count = sum(1 for item in ctx.todos if not item.get("done"))
    payload = {
        "ok": True,
        "todos": todos_payload(rows),
        "total": total,
        "open": open_count,
    }
    return ToolResult(
        ok=True,
        content=json_dumps(payload),
        summary=f"当前待办 {total} 条，未完成 {open_count} 条",
        data=payload,
    )


def todo_complete(ctx: ToolContext, args: dict) -> ToolResult:
    todo_id = require_int(args, "id")
    item = _find(ctx.todos, todo_id)
    if not item.get("done"):
        item["done"] = True
        item["updated_at"] = now_iso()
    return _result(ctx.todos, f"已完成待办 #{todo_id}：{item.get('title', '')}")


def todo_delete(ctx: ToolContext, args: dict) -> ToolResult:
    todo_id = require_int(args, "id")
    item = _find(ctx.todos, todo_id)
    ctx.todos.remove(item)
    return _result(ctx.todos, f"已删除待办 #{todo_id}：{item.get('title', '')}")


def register_todo_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="todo_add",
            description="给当前会话添加一条待办。用户说“记一下/提醒我/加个待办”时使用。",
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "待办内容，简洁明确",
                    }
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            handler=todo_add,
        )
    )
    registry.register(
        ToolSpec(
            name="todo_list",
            description="查看当前会话的待办列表。用户问“我有哪些待办/还有什么没做”时使用。",
            parameters={
                "type": "object",
                "properties": {
                    "include_done": {
                        "type": "boolean",
                        "description": "是否包含已完成项，默认 true",
                    }
                },
                "additionalProperties": False,
            },
            handler=todo_list,
        )
    )
    registry.register(
        ToolSpec(
            name="todo_complete",
            description="把当前会话的一条待办标记为完成。需要待办 id。",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "待办 id"},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=todo_complete,
        )
    )
    registry.register(
        ToolSpec(
            name="todo_delete",
            description="删除当前会话的一条待办。需要待办 id。",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "待办 id"},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=todo_delete,
        )
    )
