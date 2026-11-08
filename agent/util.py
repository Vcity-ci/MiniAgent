"""Shared helpers: ids, time, atomic JSON, redaction, truncation."""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._\-]{8,}"),
)
_SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "tavily_api_key",
    "deepseek_api_key",
    "key",
    "secret",
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().astimezone().isoformat(timespec="seconds")


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def truncate(text: str, limit: int, marker: str = "\n…[truncated]") -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(marker))
    return text[:keep] + marker


def redact_text(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    if not text:
        return text
    result = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            result = result.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            result = pattern.sub(lambda m: (m.group(1) or "") + "***", result)
        else:
            result = pattern.sub("sk-***", result)
    return result


def redact_obj(obj: Any, secrets: list[str] | tuple[str, ...] = ()) -> Any:
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if str(key).lower() in _SECRET_KEYS:
                out[key] = "***"
            else:
                out[key] = redact_obj(value, secrets)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(item, secrets) for item in obj]
    if isinstance(obj, str):
        return redact_text(obj, secrets)
    return obj


def mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "••••"
    return f"{secret[:3]}…{secret[-4:]}"


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def text_tail(text: str, limit: int = 4000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[-limit:]
