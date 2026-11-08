"""In-memory, process-lifetime settings.

The settings panel intentionally does not persist the API key.  `.env` can
provide initial defaults, but changes made in the browser die with the process.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from urllib.parse import urlparse

from . import config
from .errors import InvalidRequest
from .util import mask_secret


@dataclass
class Settings:
    base_url: str = config.DEFAULT_BASE_URL
    api_key: str = ""
    model: str = config.DEFAULT_MODEL
    thinking: bool = False
    online: bool = False
    max_steps: int = config.MAX_STEPS_DEFAULT
    profile_auto: bool = True


@dataclass
class SettingsChange:
    settings: Settings
    online_changed: bool = False
    connection_changed: bool = False


class SettingsManager:
    def __init__(self, initial: Settings | None = None) -> None:
        self._lock = threading.RLock()
        if initial is None:
            initial = Settings(
                base_url=config.env("AGENT_BASE_URL", config.DEFAULT_BASE_URL),
                api_key=config.env("AGENT_API_KEY", ""),
                model=config.env("AGENT_MODEL", config.DEFAULT_MODEL),
                thinking=config.env_bool("AGENT_THINKING", False),
                online=config.env_bool("AGENT_ONLINE", False),
                max_steps=config.env_int("AGENT_MAX_STEPS", config.MAX_STEPS_DEFAULT),
                profile_auto=config.env_bool("AGENT_PROFILE_AUTO", True),
            )
        self._settings = self._validate_initial(initial)

    @staticmethod
    def _validate_initial(settings: Settings) -> Settings:
        settings.base_url = (settings.base_url or config.DEFAULT_BASE_URL).strip().rstrip("/")
        settings.model = (settings.model or config.DEFAULT_MODEL).strip()
        settings.max_steps = int(settings.max_steps)
        if not settings.base_url.startswith(("http://", "https://")):
            settings.base_url = config.DEFAULT_BASE_URL
        if settings.max_steps < config.MAX_STEPS_MIN or settings.max_steps > config.MAX_STEPS_MAX:
            settings.max_steps = config.MAX_STEPS_DEFAULT
        return settings

    def snapshot(self) -> Settings:
        with self._lock:
            return copy.deepcopy(self._settings)

    def public(self) -> dict:
        with self._lock:
            s = copy.deepcopy(self._settings)
        return {
            "base_url": s.base_url,
            "model": s.model,
            "model_options": list(config.MODEL_OPTIONS),
            "thinking": s.thinking,
            "online": s.online,
            "max_steps": s.max_steps,
            "profile_auto": s.profile_auto,
            "api_key_set": bool(s.api_key),
            "api_key_masked": mask_secret(s.api_key),
            "llm_ready": bool(s.api_key),
        }

    def update(self, payload: dict) -> SettingsChange:
        if not isinstance(payload, dict):
            raise InvalidRequest("设置必须是 JSON 对象")
        with self._lock:
            before = copy.deepcopy(self._settings)
            candidate = copy.deepcopy(self._settings)

            if "base_url" in payload:
                base = str(payload["base_url"] or "").strip().rstrip("/")
                parsed = urlparse(base)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise InvalidRequest("Base URL 必须是合法的 http/https 地址")
                candidate.base_url = base

            if payload.get("clear_api_key") is True:
                candidate.api_key = ""
            elif "api_key" in payload:
                key = payload["api_key"]
                if key is None:
                    key = ""
                if not isinstance(key, str):
                    raise InvalidRequest("API Key 必须是字符串")
                key = key.strip()
                if key:
                    candidate.api_key = key

            if "model" in payload:
                model = str(payload["model"] or "").strip()
                if not model or len(model) > 128:
                    raise InvalidRequest("模型名不能为空且不能超过 128 字符")
                candidate.model = model

            for field in ("thinking", "online", "profile_auto"):
                if field in payload:
                    value = payload[field]
                    if not isinstance(value, bool):
                        raise InvalidRequest(f"{field} 必须是布尔值")
                    setattr(candidate, field, value)

            if "max_steps" in payload:
                value = payload["max_steps"]
                if isinstance(value, bool):
                    raise InvalidRequest("max_steps 必须是整数")
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise InvalidRequest("max_steps 必须是整数") from None
                if value < config.MAX_STEPS_MIN or value > config.MAX_STEPS_MAX:
                    raise InvalidRequest(
                        f"max_steps 必须在 {config.MAX_STEPS_MIN}–{config.MAX_STEPS_MAX} 之间"
                    )
                candidate.max_steps = value

            self._settings = candidate
            online_changed = before.online != candidate.online
            connection_changed = (
                before.base_url != candidate.base_url
                or before.api_key != candidate.api_key
                or before.model != candidate.model
            )
            return SettingsChange(copy.deepcopy(candidate), online_changed, connection_changed)

    def reset(self) -> Settings:
        with self._lock:
            self._settings = Settings()
            return copy.deepcopy(self._settings)
