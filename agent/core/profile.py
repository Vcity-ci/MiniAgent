"""Global, periodically refreshed user profile.

The profile is the only intentionally cross-session piece of conversation
memory.  Session turns/todos remain isolated.  It is refreshed from *committed*
turns only, before a new turn starts.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from .. import config
from ..util import atomic_write_json, load_json, now_iso, truncate
from .store import GlobalState, SessionStore


DEFAULT_PROFILE = {
    "summary": "",
    "updated_at": "",
    "version": 0,
    "last_turn_counter": 0,
    "source": "empty",
}


def _strip_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json_object(text: str) -> dict | None:
    text = _strip_fence(text)
    for candidate in (text,):
        try:
            value = json.loads(candidate)
            return value if isinstance(value, dict) else None
        except ValueError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else None
        except ValueError:
            return None
    return None


class ProfileManager:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "profile.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        data = load_json(self.path, default=None)
        self._profile = dict(DEFAULT_PROFILE)
        if isinstance(data, dict):
            for key in DEFAULT_PROFILE:
                if key in data:
                    self._profile[key] = data[key]
        self._profile["summary"] = str(self._profile.get("summary") or "")[: config.PROFILE_MAX_CHARS]

    def get(self) -> dict:
        with self._lock:
            return dict(self._profile)

    def public(self) -> dict:
        profile = self.get()
        return {
            "summary": profile.get("summary", ""),
            "updated_at": profile.get("updated_at", ""),
            "version": int(profile.get("version") or 0),
            "source": profile.get("source", "empty"),
            "auto": True,
        }

    def _save(
        self,
        *,
        summary: str,
        source: str,
        counter: int,
        version: int | None = None,
    ) -> dict:
        summary = (summary or "").strip()[: config.PROFILE_MAX_CHARS]
        profile = {
            "summary": summary,
            "updated_at": now_iso(),
            "version": int(version if version is not None else self._profile.get("version", 0)) + 1,
            "last_turn_counter": int(counter),
            "source": source,
        }
        atomic_write_json(self.path, profile)
        with self._lock:
            self._profile = profile
        return dict(profile)

    def _recent_turns(self, store: SessionStore, limit: int = 20) -> list:
        turns = store.all_turns()
        turns.sort(key=lambda turn: turn.created_at)
        return turns[-limit:]

    def _prompt(self, current: dict, turns: list) -> list[dict]:
        excerpts = []
        for turn in turns:
            user = truncate(turn.user, 260)
            final = truncate(turn.final, 320)
            excerpts.append(f"用户: {user}\n助手: {final}")
        current_text = current.get("summary") or "（暂无）"
        return [
            {
                "role": "system",
                "content": (
                    "你在维护一个跨会话的用户画像。只保留稳定的、用户级的信息："
                    "称呼、语言偏好、回答风格、长期目标、常用技术栈、明确禁忌。"
                    "不要记录一次性任务细节、敏感信息或工具原始结果。"
                    "如果新信息与旧画像冲突，以新信息为准；没有新信息就保留旧内容。"
                    f"输出严格 JSON：{{\"profile\": \"...\"}}，profile 不超过 {config.PROFILE_MAX_CHARS} 字。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"当前画像：\n{current_text}\n\n最近已完成的对话：\n"
                    + "\n\n".join(excerpts)
                ),
            },
        ]

    def maybe_update(self, store: SessionStore, state: GlobalState, settings, llm, trace=None) -> dict:
        if not getattr(settings, "profile_auto", True):
            return {"updated": False, "reason": "disabled"}
        counter = state.current()
        with self._lock:
            current = dict(self._profile)
        if counter - int(current.get("last_turn_counter") or 0) < config.PROFILE_EVERY:
            return {"updated": False, "reason": "not_due", "counter": counter}
        if not self._lock.acquire(blocking=False):
            return {"updated": False, "reason": "busy"}
        try:
            turns = self._recent_turns(store)
            if not turns:
                return {"updated": False, "reason": "no_turns"}
            result = llm.complete(
                self._prompt(current, turns),
                max_tokens=500,
                thinking=False,
            )
            data = _parse_json_object(result.content)
            if not data or not isinstance(data.get("profile"), str):
                raise ValueError("画像模型没有返回合法的 profile JSON")
            summary = data["profile"].strip()[: config.PROFILE_MAX_CHARS]
            if not summary:
                raise ValueError("画像为空")
            profile = self._save(summary=summary, source="auto", counter=counter)
            if trace:
                trace.log(
                    "profile_update",
                    data={
                        "ok": True,
                        "version": profile["version"],
                        "length": len(summary),
                        "turns": len(turns),
                    },
                )
            return {"updated": True, "profile": profile}
        except Exception as exc:
            # Do not block the user's turn. Wait for the next window instead.
            try:
                with self._lock:
                    self._profile["last_turn_counter"] = counter
                    atomic_write_json(self.path, self._profile)
            except Exception:
                pass
            if trace:
                trace.log("profile_update", data={"ok": False, "error": str(exc)[:500]})
            return {"updated": False, "reason": "error", "error": str(exc)[:500]}
        finally:
            self._lock.release()

    def refresh(self, store: SessionStore, state: GlobalState, settings, llm, trace=None) -> dict:
        counter = state.current()
        with self._lock:
            current = dict(self._profile)
        if not self._lock.acquire(blocking=False):
            return {"updated": False, "reason": "busy"}
        try:
            turns = self._recent_turns(store)
            if not turns:
                return {"updated": False, "reason": "no_turns"}
            result = llm.complete(self._prompt(current, turns), max_tokens=500, thinking=False)
            data = _parse_json_object(result.content)
            if not data or not isinstance(data.get("profile"), str):
                raise ValueError("画像模型没有返回合法的 profile JSON")
            summary = data["profile"].strip()[: config.PROFILE_MAX_CHARS]
            if not summary:
                raise ValueError("画像为空")
            profile = self._save(summary=summary, source="auto", counter=counter)
            if trace:
                trace.log("profile_refresh", data={"ok": True, "version": profile["version"]})
            return {"updated": True, "profile": profile}
        except Exception as exc:
            if trace:
                trace.log("profile_refresh", data={"ok": False, "error": str(exc)[:500]})
            return {"updated": False, "reason": "error", "error": str(exc)[:500]}
        finally:
            self._lock.release()

    def set_manual(self, summary: str, state: GlobalState) -> dict:
        with self._lock:
            return self._save(
                summary=summary,
                source="manual",
                counter=state.current(),
            )

    def clear(self, state: GlobalState) -> dict:
        with self._lock:
            return self._save(summary="", source="manual", counter=state.current())

    def reset(self) -> bool:
        """Remove the persisted profile and restore the empty in-memory state."""
        with self._lock:
            had = bool(self._profile.get("summary")) or int(self._profile.get("version") or 0) > 0
            self._profile = dict(DEFAULT_PROFILE)
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            return had
