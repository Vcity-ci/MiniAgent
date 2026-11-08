"""Evaluate real-model tool selection against a small prompt set.

This is a live evaluation, not part of the offline unittest suite.
Requires AGENT_API_KEY and network. It prints a table and a score.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import config  # noqa: E402
from agent.core.events import RecordingEmitter  # noqa: E402
from agent.settings import Settings, SettingsManager  # noqa: E402
from agent.web.app import create_app  # noqa: E402


CASES = [
    ("计算 (18*7+4)/2", {"calculate"}),
    ("求 x^2+sin(x) 在 x=3 处的导数", {"calculate"}),
    ("北京今天天气怎么样？", {"get_weather"}),
    ("帮我记一条待办：周五交周报", {"todo_add"}),
    ("我有哪些待办？", {"todo_list"}),
    ("把第 1 条待办标记为完成", {"todo_complete"}),
    ("搜索一下 DeepSeek 最新的 API 更新", {"web_search"}),
    ("用一句话介绍你自己", set()),
    ("那明天北京天气呢？", {"get_weather"}),
    ("把刚才的待办再列一遍", {"todo_list"}),
]


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
        max_steps=8,
        profile_auto=False,
    )
    data_dir = ROOT / ".run" / "eval" / "data"
    log_dir = ROOT / ".run" / "eval" / "logs"
    app = create_app(
        data_dir=data_dir,
        log_dir=log_dir,
        settings_manager=SettingsManager(settings),
    )
    status = app.online_status()
    print(f"online: {status.get('status')} tools={status.get('tool_names')}")
    session = app.store.create()
    passed = 0
    failures = 0
    started = time.monotonic()
    try:
        for index, (prompt, expected) in enumerate(CASES, start=1):
            emitter = RecordingEmitter()
            turn = app.runtime.handle(session.id, prompt, emitter)
            if turn is None:
                failures += 1
                print(f"{index:02d} | FAIL | {prompt} | no final")
                continue
            called = {step.get("name") for step in turn.steps if step.get("type") == "tool"}
            ok = expected.issubset(called) if expected else not called
            if ok:
                passed += 1
            else:
                failures += 1
            print(
                f"{index:02d} | {'PASS' if ok else 'FAIL'} | {prompt} | "
                f"expected={sorted(expected) or 'none'} called={sorted(called)}"
            )
    finally:
        app.shutdown()
    elapsed = int(time.monotonic() - started)
    total = len(CASES)
    print(f"\nSCORE {passed}/{total} ({elapsed}s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
