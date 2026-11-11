"""Session and turn data structures.

Provider messages are stored verbatim for native tool-call replay.  The public
view deliberately strips them so the browser never sees raw function-call JSON.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from ..util import new_id, now_iso


@dataclass
class Turn:
    id: str
    user: str
    messages: list[dict]
    steps: list[dict]
    final: str
    sources: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    images: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    trace_id: str = ""
    global_seq: int = 0

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "user": self.user,
            "steps": copy.deepcopy(self.steps),
            "final": self.final,
            "sources": copy.deepcopy(self.sources),
            "usage": copy.deepcopy(self.usage),
            "images": copy.deepcopy(self.images),
            "created_at": self.created_at,
            "trace_id": self.trace_id,
        }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user": self.user,
            "messages": copy.deepcopy(self.messages),
            "steps": copy.deepcopy(self.steps),
            "final": self.final,
            "sources": copy.deepcopy(self.sources),
            "usage": copy.deepcopy(self.usage),
            "images": copy.deepcopy(self.images),
            "created_at": self.created_at,
            "trace_id": self.trace_id,
            "global_seq": self.global_seq,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Turn":
        return cls(
            id=str(data.get("id") or new_id("t")),
            user=str(data.get("user") or ""),
            messages=list(data.get("messages") or []),
            steps=list(data.get("steps") or []),
            final=str(data.get("final") or ""),
            sources=list(data.get("sources") or []),
            usage=dict(data.get("usage") or {}),
            images=list(data.get("images") or []),
            created_at=str(data.get("created_at") or now_iso()),
            trace_id=str(data.get("trace_id") or ""),
            global_seq=int(data.get("global_seq") or 0),
        )


@dataclass
class Session:
    id: str
    title: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    turns: list[Turn] = field(default_factory=list)
    todos: list[dict] = field(default_factory=list)
    next_todo_id: int = 1
    summary: str = ""
    summarized_through: int = 0
    version: int = 1

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def pending_turns(self) -> int:
        return max(0, len(self.turns) - int(self.summarized_through or 0))

    def todo_open(self) -> int:
        return sum(1 for item in self.todos if not item.get("done"))

    def default_title(self, text: str) -> str:
        compact = " ".join((text or "").split())
        return (compact[:30] + "…") if len(compact) > 30 else compact

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turns": [turn.to_dict() for turn in self.turns],
            "todos": copy.deepcopy(self.todos),
            "next_todo_id": self.next_todo_id,
            "summary": self.summary,
            "summarized_through": self.summarized_through,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            id=str(data.get("id")),
            title=str(data.get("title") or ""),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            turns=[Turn.from_dict(item) for item in (data.get("turns") or [])],
            todos=list(data.get("todos") or []),
            next_todo_id=int(data.get("next_todo_id") or 1),
            summary=str(data.get("summary") or ""),
            summarized_through=int(data.get("summarized_through") or 0),
            version=int(data.get("version") or 1),
        )

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turns": [turn.to_public() for turn in self.turns],
            "todos": copy.deepcopy(self.todos),
            "summary": self.summary,
            "summarized_through": self.summarized_through,
            "turn_count": self.turn_count,
            "todo_open": self.todo_open(),
        }

    def summary_view(self) -> dict:
        return {
            "id": self.id,
            "title": self.title or "新会话",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turn_count": self.turn_count,
            "todo_open": self.todo_open(),
        }
