"""Real network smoke test: DeepSeek chat + Open-Meteo + MCP web search.

Requires AGENT_API_KEY (or .env) and outbound network. Never prints the key.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import config  # noqa: E402
from agent.llm import make_client  # noqa: E402
from agent.mcp.manager import MCPManager  # noqa: E402
from agent.settings import Settings  # noqa: E402
from agent.tools.base import ToolContext  # noqa: E402


def main() -> int:
    config.load_dotenv()
    api_key = config.env("AGENT_API_KEY")
    if not api_key:
        print("AGENT_API_KEY is not set. Put it in .env or the environment first.")
        return 2
    settings = Settings(
        base_url=config.env("AGENT_BASE_URL", config.DEFAULT_BASE_URL),
        api_key=api_key,
        model=config.env("AGENT_MODEL", config.DEFAULT_MODEL),
        thinking=False,
        online=True,
    )
    failures = 0

    print(f"[1/3] DeepSeek model ping: {settings.model} @ {settings.base_url}")
    started = time.monotonic()
    try:
        client = make_client(settings, timeout=60)
        result = client.complete(
            [
                {"role": "system", "content": "只回复 OK。"},
                {"role": "user", "content": "连接测试"},
            ],
            max_tokens=16,
            thinking=False,
        )
        print(f"      OK {int((time.monotonic() - started) * 1000)} ms -> {result.content[:80]!r}")
    except Exception as exc:
        failures += 1
        print(f"      FAIL: {exc}")

    print("[2/3] MCP start")
    mcp = MCPManager(config.ROOT)
    try:
        status = mcp.sync(settings)
        if status.get("status") != "ready":
            failures += 1
            print(f"      FAIL: {status}")
            return 1
        print(f"      OK tools={status.get('tool_names')}")
        specs = {spec.name: spec for spec in mcp.tool_specs()}
        ctx = ToolContext()

        print("[3/3] Open-Meteo weather + DeepSeek Search")
        weather = specs["get_weather"].handler(ctx, {"city": "北京", "days": 1})
        if weather.ok:
            print(f"      weather OK: {weather.summary}")
            print(f"      sources: {[s.get('url') for s in weather.sources]}")
        else:
            failures += 1
            print(f"      weather FAIL: {weather.error}")

        search = specs["web_search"].handler(
            ctx, {"query": "DeepSeek API function calling", "count": 3}
        )
        if search.ok:
            print(f"      search OK: {search.summary}")
            print(f"      sources: {[s.get('url') for s in search.sources[:3]]}")
        else:
            failures += 1
            print(f"      search FAIL: {search.error}")
    finally:
        mcp.stop()

    print("RESULT:", "OK" if failures == 0 else f"{failures} failure(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
