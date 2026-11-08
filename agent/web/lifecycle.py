"""Browser-client lifecycle: auto-open is in run.py; this closes the backend.

Each browser tab registers a random client id and sends a heartbeat.  Closing
the last page stops the server after a short grace period.  A heartbeat timeout
is the fallback when the browser dies without firing pagehide.
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class ClientRegistry:
    def __init__(
        self,
        *,
        timeout: float = 75.0,
        grace: float = 8.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._clients: dict[str, float] = {}
        self._has_seen = False
        self._empty_since: float | None = None
        self._timeout = float(timeout)
        self._grace = float(grace)
        self._clock = clock

    def heartbeat(self, client_id: str) -> int:
        client_id = str(client_id or "").strip()[:80]
        if not client_id:
            return len(self._clients)
        now = self._clock()
        with self._lock:
            self._clients[client_id] = now
            self._has_seen = True
            self._empty_since = None
            return len(self._clients)

    def close(self, client_id: str) -> int:
        client_id = str(client_id or "").strip()[:80]
        now = self._clock()
        with self._lock:
            if client_id:
                self._clients.pop(client_id, None)
            self._mark_empty(now)
            return len(self._clients)

    def _mark_empty(self, now: float) -> None:
        if self._has_seen and not self._clients and self._empty_since is None:
            self._empty_since = now

    def prune(self) -> int:
        now = self._clock()
        with self._lock:
            stale = [
                client_id
                for client_id, seen in self._clients.items()
                if now - seen > self._timeout
            ]
            for client_id in stale:
                self._clients.pop(client_id, None)
            self._mark_empty(now)
            return len(self._clients)

    def active_count(self) -> int:
        return self.prune()

    def has_seen(self) -> bool:
        with self._lock:
            return self._has_seen

    def empty_for(self) -> float:
        now = self._clock()
        with self._lock:
            self.prune()
            if not self._has_seen or self._clients or self._empty_since is None:
                return 0.0
            return max(0.0, now - self._empty_since)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "clients": len(self._clients),
                "has_seen": self._has_seen,
                "empty_for": self.empty_for(),
            }


class AutoShutdown(threading.Thread):
    def __init__(
        self,
        server,
        app,
        registry: ClientRegistry,
        *,
        grace: float = 8.0,
        interval: float = 1.5,
    ) -> None:
        super().__init__(name="miniagent-auto-shutdown", daemon=True)
        self.server = server
        self.app = app
        self.registry = registry
        self.grace = float(grace)
        self.interval = float(interval)
        self.triggered = False
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.registry.prune()
                if (
                    self.registry.has_seen()
                    and self.registry.active_count() == 0
                    and self.app.active_turns() == 0
                    and self.registry.empty_for() >= self.grace
                ):
                    self.triggered = True
                    self.server.shutdown()
                    return
            except Exception:
                pass
            self._stop_event.wait(self.interval)
