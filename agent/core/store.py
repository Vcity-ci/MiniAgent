"""File-backed session storage with per-session locks."""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from pathlib import Path

from .. import config
from ..errors import InvalidRequest, SessionBusy, SessionNotFound
from ..util import atomic_write_json, load_json, new_id, now_iso
from .session import Session


class SessionStore:
    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _path(self, session_id: str) -> Path:
        if not re.match(config.SESSION_ID_RE, session_id or ""):
            raise SessionNotFound("会话不存在")
        return self.dir / f"{session_id}.json"

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[session_id] = lock
            return lock

    def is_busy(self, session_id: str) -> bool:
        try:
            lock = self._lock_for(session_id)
        except Exception:
            return False
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
            return False
        return True

    @contextmanager
    def lock(self, session_id: str):
        lock = self._lock_for(session_id)
        if not lock.acquire(blocking=False):
            raise SessionBusy("该会话正在处理上一条消息，请稍候")
        try:
            yield
        finally:
            lock.release()

    def create(self, title: str = "") -> Session:
        session = Session(id=new_id("s"), title=(title or "").strip())
        self.save(session)
        return session

    def get(self, session_id: str) -> Session:
        path = self._path(session_id)
        try:
            data = load_json(path)
        except (OSError, ValueError):
            raise SessionNotFound("会话文件损坏或不可读") from None
        if not isinstance(data, dict):
            raise SessionNotFound("会话不存在")
        try:
            return Session.from_dict(data)
        except (TypeError, ValueError):
            raise SessionNotFound("会话数据格式不正确") from None

    def save(self, session: Session) -> None:
        session.updated_at = now_iso()
        atomic_write_json(self._path(session.id), session.to_dict())

    def list_summaries(self) -> list[dict]:
        rows: list[dict] = []
        for path in sorted(self.dir.glob("s_*.json")):
            try:
                data = load_json(path)
                if not isinstance(data, dict):
                    continue
                session = Session.from_dict(data)
                rows.append(session.summary_view())
            except Exception:
                continue
        rows.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return rows

    def rename(self, session_id: str, title: str) -> Session:
        title = (title or "").strip()
        if len(title) > 80:
            raise InvalidRequest("会话标题不能超过 80 字符")
        session = self.get(session_id)
        session.title = title or session.title or "新会话"
        self.save(session)
        return session

    def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        try:
            path.unlink()
        except FileNotFoundError:
            raise SessionNotFound("会话不存在") from None

    def purge(self) -> int:
        """Delete every persisted session (used by one-click logout)."""
        removed = 0
        for path in list(self.dir.glob("*.json")):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        with self._locks_guard:
            self._locks.clear()
        return removed

    def all_turns(self) -> list:
        turns = []
        for path in sorted(self.dir.glob("s_*.json")):
            try:
                data = load_json(path)
                if isinstance(data, dict):
                    turns.extend(Session.from_dict(data).turns)
            except Exception:
                continue
        turns.sort(key=lambda turn: turn.created_at)
        return turns


class GlobalState:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        data = load_json(self.path, default={}) or {}
        self._turn_counter = int(data.get("turn_counter") or 0)

    def current(self) -> int:
        with self._lock:
            return self._turn_counter

    def increment(self) -> int:
        with self._lock:
            self._turn_counter += 1
            atomic_write_json(self.path, {"turn_counter": self._turn_counter})
            return self._turn_counter

    def reset(self) -> None:
        with self._lock:
            self._turn_counter = 0
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                atomic_write_json(self.path, {"turn_counter": 0})
