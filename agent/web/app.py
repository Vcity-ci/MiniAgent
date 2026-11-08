"""Application context: settings, storage, runtime, MCP, lifecycle."""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import config
from ..core.profile import ProfileManager
from ..core.runtime import Runtime
from ..core.store import GlobalState, SessionStore
from ..errors import InvalidRequest
from ..llm import make_client
from ..mcp.manager import MCPManager
from ..settings import SettingsManager
from ..tools import build_default_registry
from ..trace import TraceLogger
from .lifecycle import ClientRegistry


_ENV_KEY_RE = re.compile(
    r"^\s*(?:export\s+)?(?:AGENT_API_KEY|DEEPSEEK_API_KEY)\s*=",
    re.IGNORECASE,
)


def purge_env_api_keys(path: Path) -> bool:
    """Remove API-key lines from .env without printing or returning the value."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return False
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if not _ENV_KEY_RE.match(line)]
    if len(kept) == len(lines):
        return False
    tmp = path.with_name(path.name + ".logout.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(kept))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


class AppContext:
    def __init__(
        self,
        *,
        settings: SettingsManager,
        store: SessionStore,
        state: GlobalState,
        profile: ProfileManager,
        trace: TraceLogger,
        runtime: Runtime,
        mcp: Any,
        base_registry,
        data_dir: Path,
        log_dir: Path,
        env_path: Path,
        clients: ClientRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.state = state
        self.profile = profile
        self.trace = trace
        self.runtime = runtime
        self.mcp = mcp
        self.base_registry = base_registry
        self.data_dir = data_dir
        self.log_dir = log_dir
        self.env_path = env_path
        self.clients = clients or ClientRegistry(
            timeout=config.CLIENT_HEARTBEAT_TIMEOUT,
            grace=config.CLIENT_SHUTDOWN_GRACE,
        )
        self.started_at = time.monotonic()
        self._active_lock = threading.RLock()
        self._active_turns = 0

    # -- turn activity -------------------------------------------------------

    def begin_turn(self) -> None:
        with self._active_lock:
            self._active_turns += 1

    def end_turn(self) -> None:
        with self._active_lock:
            self._active_turns = max(0, self._active_turns - 1)

    def active_turns(self) -> int:
        with self._active_lock:
            return self._active_turns

    # -- views ---------------------------------------------------------------

    def online_status(self) -> dict:
        settings = self.settings.snapshot()
        if not settings.online:
            return {"enabled": False, "status": "off", "error": "", "tool_names": [], "tools": []}
        try:
            status = self.mcp.status()
            status.setdefault("enabled", True)
            return status
        except Exception as exc:
            return {
                "enabled": True,
                "status": "error",
                "error": str(exc)[:500],
                "tool_names": [],
                "tools": [],
            }

    def settings_view(self) -> dict:
        view = self.settings.public()
        view["online_status"] = self.online_status()
        return view

    def profile_view(self) -> dict:
        view = self.profile.public()
        view["auto"] = bool(self.settings.snapshot().profile_auto)
        return view

    def tools_view(self) -> list[dict]:
        rows = [
            {"name": spec.name, "description": spec.description, "source": spec.source}
            for spec in self.base_registry.all()
        ]
        try:
            for spec in self.mcp.tool_specs():
                rows.append(
                    {"name": spec.name, "description": spec.description, "source": "mcp"}
                )
        except Exception:
            pass
        return rows

    def health(self) -> dict:
        settings = self.settings.snapshot()
        try:
            session_count = len(self.store.list_summaries())
        except Exception:
            session_count = 0
        return {
            "ok": True,
            "version": config.VERSION,
            "uptime_s": int(time.monotonic() - self.started_at),
            "llm": {
                "ready": bool(settings.api_key),
                "model": settings.model,
                "thinking": settings.thinking,
            },
            "online": self.online_status(),
            "tools": self.tools_view(),
            "sessions": session_count,
            "profile_version": self.profile.get().get("version", 0),
            "clients": self.clients.active_count(),
        }

    # -- actions -------------------------------------------------------------

    def apply_settings(self, change) -> dict:
        settings = self.settings.snapshot()
        if change.online_changed or change.connection_changed or settings.online:
            try:
                self.mcp.sync(settings)
            except Exception as exc:
                self.trace.log("mcp_sync_error", data={"error": str(exc)[:500]})
        return self.settings_view()

    def test_llm(self) -> dict:
        settings = self.settings.snapshot()
        if not settings.api_key:
            return {"ok": False, "error": "尚未配置 API Key"}
        started = time.monotonic()
        try:
            client = self.runtime.llm_factory(settings)
            result = client.complete(
                [
                    {"role": "system", "content": "只回复 OK。"},
                    {"role": "user", "content": "连接测试"},
                ],
                max_tokens=16,
                thinking=False,
            )
            return {
                "ok": True,
                "target": "llm",
                "latency_ms": int((time.monotonic() - started) * 1000),
                "model": result.model,
                "content": result.content[:200],
            }
        except Exception as exc:
            return {
                "ok": False,
                "target": "llm",
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": str(exc)[:500],
            }

    def test_online(self) -> dict:
        settings = self.settings.snapshot()
        try:
            status = self.mcp.sync(settings)
            return {"ok": status.get("status") == "ready", "target": "online", "online": status}
        except Exception as exc:
            return {"ok": False, "target": "online", "error": str(exc)[:500]}

    def refresh_profile(self):
        settings = self.settings.snapshot()
        if not settings.api_key:
            return {"updated": False, "error": "尚未配置 API Key"}
        client = self.runtime.llm_factory(settings)
        return self.profile.refresh(self.store, self.state, settings, client, self.trace)

    def logout(self) -> dict:
        """One-click user logout: wipe all persisted/in-memory user state."""
        with self._active_lock:
            if self._active_turns:
                raise InvalidRequest(
                    "当前有回答正在运行，请等待完成后再注销",
                    code="session_busy",
                    http_status=409,
                )
        try:
            self.mcp.stop()
        except Exception:
            pass
        sessions = self.store.purge()
        profile_removed = self.profile.reset()
        self.state.reset()
        trace_removed = self.trace.purge()
        self.settings.reset()
        env_key_removed = purge_env_api_keys(self.env_path)
        os.environ.pop("AGENT_API_KEY", None)
        os.environ.pop("DEEPSEEK_API_KEY", None)
        return {
            "ok": True,
            "cleared": {
                "sessions_removed": sessions,
                "profile_removed": bool(profile_removed),
                "state_reset": True,
                "trace_removed": bool(trace_removed),
                "settings_reset": True,
                "env_key_removed": bool(env_key_removed),
            },
            "settings": self.settings_view(),
        }

    def shutdown(self) -> None:
        try:
            self.mcp.shutdown()
        except Exception:
            pass


def create_app(
    *,
    data_dir: Path | None = None,
    log_dir: Path | None = None,
    settings_manager: SettingsManager | None = None,
    llm_factory: Callable[[Any], Any] | None = None,
    mcp=None,
    trace: TraceLogger | None = None,
    env_path: Path | None = None,
    clients: ClientRegistry | None = None,
) -> AppContext:
    data_dir = Path(data_dir) if data_dir else config.data_dir()
    log_dir = Path(log_dir) if log_dir else config.log_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    settings = settings_manager or SettingsManager()
    if trace is None:
        trace = TraceLogger(
            log_dir / "trace.jsonl",
            lambda: [settings.snapshot().api_key],
        )
    store = SessionStore(data_dir)
    state = GlobalState(data_dir)
    profile = ProfileManager(data_dir)
    mcp_manager = mcp if mcp is not None else MCPManager(config.ROOT, trace)
    registry = build_default_registry()
    runtime = Runtime(
        settings=settings,
        store=store,
        state=state,
        profile=profile,
        trace=trace,
        llm_factory=llm_factory or make_client,
        mcp=mcp_manager,
        base_registry=registry,
    )
    app = AppContext(
        settings=settings,
        store=store,
        state=state,
        profile=profile,
        trace=trace,
        runtime=runtime,
        mcp=mcp_manager,
        base_registry=registry,
        data_dir=data_dir,
        log_dir=log_dir,
        env_path=Path(env_path) if env_path else (config.ROOT / ".env"),
        clients=clients,
    )
    if settings.snapshot().online:
        try:
            mcp_manager.sync(settings.snapshot())
        except Exception:
            pass
    return app
