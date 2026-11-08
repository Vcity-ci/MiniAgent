"""Lifecycle and tool adaptation for the bundled online MCP server."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

from .. import config
from ..errors import MCPError
from ..tools.base import ToolContext, ToolResult, ToolSpec
from ..util import json_dumps, truncate
from .client import MCPClient, MCPTool


def _normalize_sources(raw: Any) -> list[dict]:
    sources: list[dict] = []
    seen = set()
    if not isinstance(raw, list):
        return sources
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append({"title": str(item.get("title") or url)[:200], "url": url})
    return sources


class MCPManager:
    def __init__(self, root: Path, trace=None) -> None:
        self.root = Path(root)
        self.trace = trace
        self._lock = threading.RLock()
        self.client: MCPClient | None = None
        self._signature: tuple | None = None
        self._status = "off"
        self._error = ""

    # -- lifecycle -----------------------------------------------------------

    def _command(self) -> list[str]:
        return [sys.executable, "-u", "-m", "agent.mcp.server_main"]

    def _env(self, settings) -> dict:
        existing = os.environ.get("PYTHONPATH", "")
        pythonpath = str(self.root) + (os.pathsep + existing if existing else "")
        return {
            "PYTHONPATH": pythonpath,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "DEEPSEEK_API_KEY": settings.api_key or "",
            "DEEPSEEK_BASE_URL": settings.base_url or config.DEFAULT_BASE_URL,
        }

    def sync(self, settings) -> dict:
        with self._lock:
            if not getattr(settings, "online", False):
                self.stop()
                self._status = "off"
                self._error = ""
                return self.status()
            signature = (settings.base_url, settings.api_key)
            if (
                self.client is not None
                and self._signature == signature
                and self._status == "ready"
            ):
                return self.status()
            self.stop()
            self._status = "starting"
            self._error = ""
            if self.trace:
                self.trace.log("mcp_start", data={"base_url": settings.base_url})
            try:
                client = MCPClient(
                    self._command(),
                    env=self._env(settings),
                    cwd=self.root,
                    name="online",
                )
                client.start()
                self.client = client
                self._signature = signature
                self._status = "ready"
                if self.trace:
                    self.trace.log(
                        "mcp_ready",
                        data={"tools": [tool.name for tool in client.tools]},
                    )
                return self.status()
            except Exception as exc:
                self._status = "error"
                self._error = str(exc)[:800]
                if self.trace:
                    self.trace.log("mcp_error", data={"error": self._error})
                return self.status()

    def stop(self) -> None:
        with self._lock:
            client = self.client
            self.client = None
            self._signature = None
            if client is not None:
                try:
                    client.stop()
                except Exception:
                    pass
            self._status = "off"
            if self.trace:
                self.trace.log("mcp_stop")

    def shutdown(self) -> None:
        self.stop()

    def status(self) -> dict:
        with self._lock:
            if self.client is not None:
                status = self.client.status()
                status["enabled"] = True
                return status
            return {
                "enabled": self._status not in {"off", "stopped"},
                "status": self._status,
                "error": self._error,
                "tool_names": [],
                "tools": [],
            }

    # -- tool adaptation -----------------------------------------------------

    def tool_specs(self) -> list[ToolSpec]:
        with self._lock:
            client = self.client
            status = self._status
            tools = list(client.tools) if client is not None else []
        if client is None or status != "ready":
            return []
        specs: list[ToolSpec] = []
        for tool in tools:
            specs.append(
                ToolSpec(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.input_schema,
                    handler=self._make_handler(tool.name),
                    source="mcp",
                )
            )
        return specs

    def _make_handler(self, name: str):
        def handler(ctx: ToolContext, args: dict) -> ToolResult:
            with self._lock:
                client = self.client
                ready = self._status == "ready"
            if client is None or not ready:
                return ToolResult(
                    ok=False,
                    content=json_dumps({"ok": False, "error": "联网 MCP 未就绪"}),
                    summary="联网 MCP 未就绪",
                    error="mcp not ready",
                )
            try:
                result = client.call_tool(name, args or {})
            except MCPError as exc:
                message = str(exc)
                return ToolResult(
                    ok=False,
                    content=json_dumps({"ok": False, "error": message}),
                    summary=truncate(message, 300),
                    error=message,
                )
            return self._convert_result(result)

        return handler

    @staticmethod
    def _convert_result(result: dict) -> ToolResult:
        content_items = result.get("content") or []
        texts = []
        if isinstance(content_items, list):
            for item in content_items:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(str(item.get("text") or ""))
        text = "\n".join(item for item in texts if item).strip()
        structured = result.get("structuredContent")
        data: Any = None
        summary = ""
        sources: list[dict] = []
        if isinstance(structured, dict):
            summary = str(structured.get("summary") or "")
            data = structured.get("data", structured)
            sources = _normalize_sources(structured.get("sources"))
        if not data and text:
            try:
                import json

                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    data = parsed
                    summary = summary or str(parsed.get("summary") or "")
                    sources = sources or _normalize_sources(parsed.get("sources"))
            except ValueError:
                pass
        if result.get("isError"):
            message = summary or text or "MCP 工具执行失败"
            return ToolResult(
                ok=False,
                content=json_dumps({"ok": False, "error": message}),
                summary=truncate(message, 300),
                error=message,
                sources=sources,
            )
        summary = summary or truncate(text, 300) or "完成"
        content = text or json_dumps({"ok": True, "summary": summary, "data": data})
        return ToolResult(
            ok=True,
            content=content,
            summary=summary,
            data=data,
            sources=sources,
        )
