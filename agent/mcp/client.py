"""Minimal hand-written MCP stdio client (JSON-RPC 2.0, line framed)."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config
from ..errors import MCPError
from ..util import json_dumps, text_tail


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict


class MCPClient:
    def __init__(
        self,
        command: list[str],
        *,
        env: dict | None = None,
        cwd: str | Path | None = None,
        name: str = "online",
    ) -> None:
        self.command = list(command)
        self.env = dict(env or {})
        self.cwd = str(cwd) if cwd else None
        self.name = name
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._pending: dict[int, Future] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._status = "stopped"
        self._error = ""
        self._stderr_tail = ""
        self.tools: list[MCPTool] = []
        self._stopping = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._proc is not None:
            return
        self._status = "starting"
        self._error = ""
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ)
        env.update(self.env)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self.cwd,
                env=env,
                creationflags=flags,
            )
        except OSError as exc:
            self._status = "error"
            self._error = f"无法启动 MCP server: {exc}"
            raise MCPError(self._error) from None
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()
        try:
            self.initialize()
            self.list_tools()
            self._status = "ready"
        except Exception:
            self._status = "error"
            self._terminate()
            raise

    def initialize(self) -> dict:
        result = self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mini-agent", "version": config.VERSION},
            },
            timeout=config.MCP_START_TIMEOUT,
        )
        try:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
            )
        except MCPError:
            pass
        return result if isinstance(result, dict) else {}

    def list_tools(self) -> list[MCPTool]:
        result = self._request("tools/list", {}, timeout=config.MCP_LIST_TIMEOUT)
        tools = []
        for item in (result or {}).get("tools") or []:
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            schema = item.get("inputSchema") or {"type": "object", "properties": {}}
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            tools.append(
                MCPTool(
                    name=name,
                    description=str(item.get("description") or name),
                    input_schema=schema,
                )
            )
        self.tools = tools
        return tools

    def call_tool(self, name: str, arguments: dict, timeout: float = config.MCP_CALL_TIMEOUT) -> dict:
        if self._status != "ready":
            raise MCPError(self._error or "MCP server 未就绪")
        result = self._request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )
        return result if isinstance(result, dict) else {}

    def stop(self) -> None:
        self._stopping = True
        self._status = "stopped"
        self._terminate()
        self.tools = []

    def _terminate(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        self._proc = None
        self._fail_pending(MCPError("MCP server 已停止"))

    # -- JSON-RPC plumbing ---------------------------------------------------

    def _alloc_id(self) -> int:
        with self._pending_lock:
            value = self._next_id
            self._next_id += 1
            return value

    def _write(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError("MCP server 未启动")
        line = json_dumps(payload) + "\n"
        with self._write_lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise MCPError(f"写入 MCP server 失败: {exc}") from None

    def _request(self, method: str, params: dict, timeout: float = 30.0) -> Any:
        request_id = self._alloc_id()
        future: Future = Future()
        with self._pending_lock:
            self._pending[request_id] = future
        try:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            try:
                return future.result(timeout=timeout)
            except FutureTimeout:
                raise MCPError(f"MCP 请求超时: {method}") from None
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    self._stderr_tail = text_tail((self._stderr_tail + "\n" + line).strip(), 3000)
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if request_id is not None:
                    with self._pending_lock:
                        future = self._pending.get(request_id)
                    if future is not None and not future.done():
                        if message.get("error"):
                            error = message["error"]
                            text = error.get("message") if isinstance(error, dict) else str(error)
                            future.set_exception(MCPError(str(text)))
                        else:
                            future.set_result(message.get("result"))
        finally:
            if not self._stopping:
                self._status = "error"
                self._error = self._error or "MCP server 连接已关闭"
            self._fail_pending(MCPError("MCP server 连接已关闭"))

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.rstrip()
                if line:
                    self._stderr_tail = text_tail((self._stderr_tail + "\n" + line).strip(), 3000)
        except Exception:
            pass

    def _fail_pending(self, exc: Exception) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
        for future in pending:
            if not future.done():
                future.set_exception(exc)

    # -- status --------------------------------------------------------------

    def status(self) -> dict:
        return {
            "enabled": True,
            "status": self._status,
            "error": self._error,
            "stderr": self._stderr_tail[-1200:],
            "tool_names": [tool.name for tool in self.tools],
            "tools": [
                {"name": tool.name, "description": tool.description}
                for tool in self.tools
            ],
        }
