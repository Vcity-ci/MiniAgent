"""JSONL trace logging and a tiny reader for the debug endpoint."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from .util import json_dumps, now_iso, redact_obj, truncate


class TraceLogger:
    def __init__(
        self,
        path: Path,
        secrets_provider: Callable[[], list[str]] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._secrets_provider = secrets_provider or (lambda: [])

    def _secrets(self) -> list[str]:
        try:
            return [s for s in self._secrets_provider() if s]
        except Exception:
            return []

    def log(
        self,
        event: str,
        *,
        trace_id: str = "",
        session_id: str = "",
        turn_id: str = "",
        step: int | None = None,
        data: object | None = None,
        duration_ms: int | None = None,
    ) -> None:
        record = {
            "ts": now_iso(),
            "event": event,
            "trace_id": trace_id,
            "session_id": session_id,
            "turn_id": turn_id,
        }
        if step is not None:
            record["step"] = step
        if duration_ms is not None:
            record["duration_ms"] = duration_ms
        if data is not None:
            record["data"] = redact_obj(data, self._secrets())
        try:
            line = json_dumps(record)
            with self._lock:
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
        except OSError:
            # Logging must never take down a live conversation.
            pass

    def read_session(self, session_id: str, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit or 100), 500))
        found: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("session_id") == session_id:
                        found.append(record)
        except FileNotFoundError:
            return []
        except OSError:
            return []
        if len(found) > limit:
            found = found[-limit:]
        return [redact_obj(item, self._secrets()) for item in found]

    def tail_text(self, limit: int = 500) -> str:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
            return truncate("".join(lines), 8000)
        except OSError:
            return ""

    def purge(self) -> bool:
        """Delete the trace log (used by one-click logout)."""
        with self._lock:
            try:
                self.path.unlink()
                return True
            except FileNotFoundError:
                return False
            except OSError:
                return False
