"""Threaded HTTP API + chunked SSE transport + static web app."""
from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import config
from ..core.events import EventEmitter
from ..errors import AgentError, ClientDisconnected, InvalidRequest
from ..util import is_relative_to, json_dumps
from .app import AppContext


LOG = logging.getLogger("miniagent.http")
SESSION_PATH = re.compile(
    r"^/api/sessions/(s_[a-f0-9]{12})(/messages|/trace|/uploads)?$"
)
IMAGE_PATH = re.compile(
    r"^/api/sessions/(s_[a-f0-9]{12})/images/(img_[a-f0-9]{12})$"
)
UPLOAD_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "application/octet-stream"}


class SSEEmitter(EventEmitter):
    def __init__(self, handler: "Handler") -> None:
        self.handler = handler
        self.closed = False

    def emit(self, event: str, data: dict | None = None) -> None:
        if self.closed:
            return
        frame = f"event: {event}\ndata: {json_dumps(data or {})}\n\n"
        self.handler.write_chunk(frame)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.handler.end_chunked()
        except ClientDisconnected:
            pass


class Handler(BaseHTTPRequestHandler):
    app: AppContext
    protocol_version = "HTTP/1.1"
    server_version = f"MiniAgent/{config.VERSION}"

    # -- low-level helpers ---------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )

    def _send_json(self, status: int, payload: dict) -> None:
        body = json_dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _error(self, exc: BaseException) -> None:
        if isinstance(exc, AgentError):
            self._send_json(exc.http_status, {"error": exc.to_dict()})
        else:
            LOG.exception("unhandled request error")
            self._send_json(
                500,
                {"error": {"code": "internal_error", "message": "服务内部错误"}},
            )

    def _read_json(self) -> dict:
        length = self.headers.get("Content-Length")
        if length is None:
            return {}
        try:
            size = int(length)
        except ValueError:
            raise InvalidRequest("Content-Length 不合法") from None
        if size > config.MAX_BODY_BYTES:
            raise InvalidRequest("请求体过大", http_status=413)
        raw = self.rfile.read(size) if size else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise InvalidRequest("请求体必须是合法 JSON") from None
        if not isinstance(data, dict):
            raise InvalidRequest("请求体必须是 JSON 对象")
        return data

    def _read_image_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length is None:
            raise InvalidRequest("缺少 Content-Length", http_status=411)
        try:
            size = int(length)
        except ValueError:
            raise InvalidRequest("Content-Length 不合法") from None
        if size <= 0:
            raise InvalidRequest("图片内容为空")
        if size > config.MAX_IMAGE_BYTES:
            raise InvalidRequest(
                f"单张图片不能超过 {config.MAX_IMAGE_BYTES // (1024 * 1024)} MiB",
                code="image_too_large",
                http_status=413,
            )
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise InvalidRequest("图片请求体不完整", code="invalid_image")
        return raw

    def _csrf(self, relaxed: bool = False) -> None:
        if self.command not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        if not relaxed and self.headers.get("X-Agent-Client") != "web":
            raise InvalidRequest("缺少客户端标识", http_status=403)
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
                "127.0.0.1",
                "localhost",
            }:
                raise InvalidRequest("Origin 不被允许", http_status=403)

    def write_chunk(self, text: str) -> None:
        data = text.encode("utf-8")
        chunk = f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n"
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise ClientDisconnected() from None

    def end_chunked(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise ClientDisconnected() from None

    def _serve_session_image(self, session_id: str, image_id: str) -> None:
        self.app.store.get(session_id)
        raw, info = self.app.images.read(session_id, image_id)
        self.send_response(200)
        self.send_header("Content-Type", info["mime"])
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Disposition", "inline")
        self._security_headers()
        self.end_headers()
        try:
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # -- static files --------------------------------------------------------

    def _serve_static(self, route_path: str) -> None:
        if route_path == "/favicon.ico":
            self.send_response(204)
            self._security_headers()
            self.end_headers()
            return
        rel = "index.html" if route_path in {"", "/"} else route_path.lstrip("/")
        candidate = (config.WEB_DIR / rel).resolve()
        if not is_relative_to(candidate, config.WEB_DIR) or not candidate.is_file():
            self._send_json(404, {"error": {"code": "not_found", "message": "页面不存在"}})
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".json": "application/json; charset=utf-8",
        }.get(candidate.suffix.lower(), "application/octet-stream")
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # -- routes --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/health":
                self._send_json(200, self.app.health())
                return
            if path == "/api/settings":
                self._send_json(200, self.app.settings_view())
                return
            if path == "/api/profile":
                self._send_json(200, {"profile": self.app.profile_view()})
                return
            if path == "/api/tools":
                self._send_json(200, {"tools": self.app.tools_view()})
                return
            if path == "/api/sessions":
                self._send_json(200, {"sessions": self.app.store.list_summaries()})
                return
            image_match = IMAGE_PATH.match(path)
            if image_match:
                self._serve_session_image(image_match.group(1), image_match.group(2))
                return
            match = SESSION_PATH.match(path)
            if match and match.group(2) is None:
                session = self.app.store.get(match.group(1))
                self._send_json(200, {"session": session.to_public()})
                return
            if match and match.group(2) == "/trace":
                query = parse_qs(parsed.query)
                try:
                    limit = int((query.get("limit") or ["100"])[0])
                except ValueError:
                    limit = 100
                events = self.app.trace.read_session(match.group(1), limit)
                self._send_json(200, {"events": events})
                return
            if path.startswith("/api/"):
                self._send_json(404, {"error": {"code": "not_found", "message": "接口不存在"}})
                return
            self._serve_static(path)
        except BaseException as exc:
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            relaxed = path in {"/api/client/heartbeat", "/api/client/close"}
            self._csrf(relaxed=relaxed)
            if path == "/api/client/heartbeat":
                payload = self._read_json()
                client_id = payload.get("client_id")
                if not isinstance(client_id, str) or not client_id.strip():
                    raise InvalidRequest("client_id 不能为空")
                count = self.app.clients.heartbeat(client_id)
                self._send_json(200, {"ok": True, "clients": count})
                return
            if path == "/api/client/close":
                payload = self._read_json()
                client_id = payload.get("client_id")
                if isinstance(client_id, str) and client_id.strip():
                    self.app.clients.close(client_id)
                self._send_json(200, {"ok": True})
                return
            if path == "/api/account/logout":
                self._send_json(200, self.app.logout())
                return
            if path == "/api/settings/test":
                payload = self._read_json()
                target = payload.get("target") or "llm"
                if target == "llm":
                    self._send_json(200, self.app.test_llm())
                elif target == "online":
                    self._send_json(200, self.app.test_online())
                else:
                    raise InvalidRequest("target 必须是 llm 或 online")
                return
            if path == "/api/profile/refresh":
                outcome = self.app.refresh_profile()
                status = 200 if outcome.get("updated") else 502
                self._send_json(status, {"ok": bool(outcome.get("updated")), **outcome})
                return
            match = SESSION_PATH.match(path)
            if match and match.group(2) == "/uploads":
                self._handle_upload(match.group(1))
                return
            if path == "/api/sessions":
                payload = self._read_json()
                session = self.app.store.create(str(payload.get("title") or ""))
                self._send_json(200, {"session": session.to_public()})
                return
            if match and match.group(2) == "/messages":
                self._handle_message(match.group(1))
                return
            self._send_json(404, {"error": {"code": "not_found", "message": "接口不存在"}})
        except ClientDisconnected:
            LOG.info("client disconnected")
        except BaseException as exc:
            self._error(exc)

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            self._csrf()
            if path == "/api/settings":
                payload = self._read_json()
                change = self.app.settings.update(payload)
                self._send_json(200, self.app.apply_settings(change))
                return
            if path == "/api/profile":
                payload = self._read_json()
                summary = payload.get("summary")
                if not isinstance(summary, str):
                    raise InvalidRequest("summary 必须是字符串")
                profile = self.app.profile.set_manual(summary, self.app.state)
                self._send_json(200, {"ok": True, "profile": profile})
                return
            self._send_json(404, {"error": {"code": "not_found", "message": "接口不存在"}})
        except BaseException as exc:
            self._error(exc)

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            self._csrf()
            match = SESSION_PATH.match(path)
            if match and match.group(2) is None:
                payload = self._read_json()
                session = self.app.store.rename(match.group(1), str(payload.get("title") or ""))
                self._send_json(200, {"session": session.to_public()})
                return
            self._send_json(404, {"error": {"code": "not_found", "message": "接口不存在"}})
        except BaseException as exc:
            self._error(exc)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            self._csrf()
            if path == "/api/profile":
                profile = self.app.profile.clear(self.app.state)
                self._send_json(200, {"ok": True, "profile": profile})
                return
            match = SESSION_PATH.match(path)
            if match and match.group(2) is None:
                if self.app.store.is_busy(match.group(1)):
                    raise AgentError("会话正在处理中，暂时不能删除", code="session_busy", http_status=409)
                self.app.store.delete(match.group(1))
                self.app.images.delete_session(match.group(1))
                self._send_json(200, {"ok": True})
                return
            self._send_json(404, {"error": {"code": "not_found", "message": "接口不存在"}})
        except BaseException as exc:
            self._error(exc)

    # -- image upload --------------------------------------------------------

    def _handle_upload(self, session_id: str) -> None:
        # Uploads are raw bodies; close after the response so an early
        # rejection cannot leave unread bytes to be parsed as another request.
        self.close_connection = True
        self.app.store.get(session_id)
        if self.app.store.is_busy(session_id):
            raise AgentError("该会话正在处理上一条消息", code="session_busy", http_status=409)
        settings = self.app.settings.snapshot()
        if not config.supports_vision(settings.model):
            raise InvalidRequest(
                "当前模型不支持图片，请切换到 deepseek-flash",
                code="vision_unsupported",
                hint="deepseek-v4-pro 仅支持文本",
            )
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type not in UPLOAD_TYPES:
            raise InvalidRequest(
                "图片 Content-Type 不被支持",
                code="unsupported_image_type",
                http_status=415,
            )
        raw = self._read_image_body()
        raw_name = unquote(self.headers.get("X-Image-Name") or "")
        image = self.app.images.create(session_id, raw, raw_name)
        self._send_json(201, {"image": image})

    # -- SSE turn ------------------------------------------------------------

    def _handle_message(self, session_id: str) -> None:
        payload = self._read_json()
        content = payload.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise InvalidRequest("content 必须是字符串")
        content = content.strip()
        image_ids = payload.get("image_ids")
        if image_ids is None:
            image_ids = []
        if not isinstance(image_ids, list):
            raise InvalidRequest("image_ids 必须是数组")
        if len(image_ids) > config.MAX_IMAGES_PER_TURN:
            raise InvalidRequest(
                f"每条消息最多 {config.MAX_IMAGES_PER_TURN} 张图片",
                code="too_many_images",
            )
        if not content and not image_ids:
            raise InvalidRequest("content 不能为空")
        if len(content) > config.MAX_INPUT_CHARS:
            raise InvalidRequest(f"消息过长（上限 {config.MAX_INPUT_CHARS} 字）", http_status=413)
        self.app.store.get(session_id)
        settings = self.app.settings.snapshot()
        if image_ids and not config.supports_vision(settings.model):
            raise InvalidRequest(
                "当前模型不支持图片，请切换到 deepseek-flash",
                code="vision_unsupported",
                hint="deepseek-v4-pro 仅支持文本",
            )
        if not settings.api_key:
            raise AgentError(
                "尚未配置 DeepSeek API Key",
                code="llm_not_configured",
                http_status=400,
                hint="打开设置填写 Key 后再发送",
            )
        if self.app.store.is_busy(session_id):
            raise AgentError("该会话正在处理上一条消息", code="session_busy", http_status=409)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self._security_headers()
        self.end_headers()
        try:
            self.write_chunk(": connected\n\n")
        except ClientDisconnected:
            self.close_connection = True
            return

        emitter = SSEEmitter(self)
        self.app.begin_turn()
        try:
            self.app.runtime.handle(
                session_id, content, emitter, image_ids=image_ids
            )
        except AgentError as exc:
            try:
                emitter.emit("error", exc.to_dict())
                emitter.emit("done", {})
            except ClientDisconnected:
                pass
        except ClientDisconnected:
            LOG.info("client disconnected during turn")
        except BaseException:
            LOG.exception("turn failed")
            try:
                emitter.emit(
                    "error",
                    {"code": "internal_error", "message": "服务内部错误"},
                )
                emitter.emit("done", {})
            except ClientDisconnected:
                pass
        finally:
            self.app.end_turn()
            emitter.close()
            self.close_connection = True


def make_server(app: AppContext, host: str = config.DEFAULT_HOST, port: int = config.DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("MiniAgentHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    server.allow_reuse_address = True
    return server
