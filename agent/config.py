"""Static configuration and environment helpers."""
from __future__ import annotations

import os
from pathlib import Path

VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
MODEL_OPTIONS = ("deepseek-flash", "deepseek-v4-pro")

MAX_STEPS_DEFAULT = 10
MAX_STEPS_MIN = 1
MAX_STEPS_MAX = 20
HISTORY_WINDOW = 8
SUMMARY_BATCH = 4
PROFILE_EVERY = 5

MAX_INPUT_CHARS = 8000
MAX_BODY_BYTES = 1_000_000
TOOL_RESULT_MAX_CHARS = 4000
SUMMARY_MAX_CHARS = 500
PROFILE_MAX_CHARS = 500

LLM_TIMEOUT = 120.0
MAINTENANCE_TIMEOUT = 90.0
MCP_START_TIMEOUT = 15.0
MCP_LIST_TIMEOUT = 15.0
MCP_CALL_TIMEOUT = 60.0
HTTP_TIMEOUT = 30.0
WEATHER_TIMEOUT = 15.0

CLIENT_HEARTBEAT_TIMEOUT = 75.0
CLIENT_SHUTDOWN_GRACE = 8.0
AUTO_SHUTDOWN_INTERVAL = 1.5

SESSION_ID_RE = r"^s_[a-f0-9]{12}$"
TRACE_LIMIT = 200

WEB_DIR = ROOT / "web"


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_dotenv(path: Path | None = None) -> None:
    """Load a tiny .env file without overriding real environment variables."""
    path = path or (ROOT / ".env")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def data_dir() -> Path:
    raw = env("AGENT_DATA_DIR", str(ROOT / "data"))
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def log_dir() -> Path:
    raw = env("AGENT_LOG_DIR", str(ROOT / "logs"))
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path
