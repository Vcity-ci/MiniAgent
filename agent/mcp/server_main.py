"""Bundled MCP stdio server exposing weather + web search + fetch."""
from __future__ import annotations

import json
import sys

from .. import config
from ..util import json_dumps
from . import online


def _tool_list() -> list[dict]:
    return [online.TOOL_SCHEMAS[name] for name in ("get_weather", "web_search", "fetch_url")]


def _handle(request: dict) -> dict | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion") or "2025-06-18"
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mini-agent-online", "version": config.VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": _tool_list()},
        }
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            outcome = online.dispatch(str(name), arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": json_dumps(outcome)}],
                    "structuredContent": outcome,
                    "isError": False,
                },
            }
        except Exception as exc:
            message = str(exc)[:1000] or "联网工具执行失败"
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": message}],
                    "isError": True,
                },
            }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        try:
            response = _handle(request)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32000, "message": str(exc)[:1000]},
            }
        if response is not None:
            try:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
