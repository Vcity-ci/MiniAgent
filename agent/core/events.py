"""Tiny event emitter used by the runtime and SSE transport."""
from __future__ import annotations

from typing import Any


class EventEmitter:
    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        raise NotImplementedError


class NullEmitter(EventEmitter):
    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        return None


class RecordingEmitter(EventEmitter):
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        self.events.append((event, dict(data or {})))

    def names(self) -> list[str]:
        return [name for name, _data in self.events]

    def get(self, event: str) -> list[dict]:
        return [data for name, data in self.events if name == event]
