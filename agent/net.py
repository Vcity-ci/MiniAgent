"""Tiny HTTP layer with a Windows-friendly CA fallback.

Python on Windows often has no CA bundle of its own.  We load the OS root
store explicitly (and optional custom bundle) so real DeepSeek/Open-Meteo
requests can verify certificates without `certifi`.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any

_SSL_CONTEXT: ssl.SSLContext | None = None


def build_ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT

    ctx = ssl.create_default_context()
    candidates = [
        os.environ.get("AGENT_CA_BUNDLE"),
        os.environ.get("SSL_CERT_FILE"),
        ssl.get_default_verify_paths().cafile,
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            try:
                ctx.load_verify_locations(cafile=candidate)
            except (OSError, ssl.SSLError):
                pass

    try:  # optional, no hard dependency
        import certifi  # type: ignore

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass

    if os.name == "nt" and hasattr(ssl, "enum_certificates"):
        try:
            for cert, encoding, _trust in ssl.enum_certificates("ROOT"):
                if encoding == "x509_asn":
                    try:
                        ctx.load_verify_locations(cadata=cert)
                    except (OSError, ssl.SSLError):
                        pass
        except Exception:
            pass

    _SSL_CONTEXT = ctx
    return ctx


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str = "") -> None:
        self.status = status
        self.body = body or ""
        self.url = url
        super().__init__(self.friendly_message())

    def friendly_message(self) -> str:
        body = self.body.strip()
        if body:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    err = parsed.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        return str(err["message"])
                    if isinstance(err, str):
                        return err
                    if parsed.get("message"):
                        return str(parsed["message"])
            except (ValueError, TypeError):
                pass
        if self.status == 0:
            return body or "网络连接失败"
        return body[:500] if body else f"HTTP {self.status}"


def _request(url: str, method: str, headers: dict, payload: Any, timeout: float):
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=build_ssl_context())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        raise HttpError(exc.code, body, url) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError) as exc:
        reason = getattr(exc, "reason", None) or str(exc)
        raise HttpError(0, str(reason), url) from None


def request_json(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    payload: Any = None,
    timeout: float = 30.0,
) -> Any:
    hdrs = dict(headers or {})
    if payload is not None:
        hdrs.setdefault("Content-Type", "application/json")
        hdrs.setdefault("Accept", "application/json")
    response = _request(url, method, hdrs, payload, timeout)
    try:
        raw = response.read()
    finally:
        response.close()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def open_stream(
    url: str,
    method: str = "POST",
    headers: dict | None = None,
    payload: Any = None,
    timeout: float = 120.0,
):
    """Return the open urllib response; caller iterates and closes it."""
    hdrs = dict(headers or {})
    if payload is not None:
        hdrs.setdefault("Content-Type", "application/json")
        hdrs.setdefault("Accept", "text/event-stream")
    return _request(url, method, hdrs, payload, timeout)


def join_url(base: str, path: str) -> str:
    base = (base or "").strip().rstrip("/")
    path = "/" + path.lstrip("/")
    if base.endswith(path):
        return base
    return base + path
