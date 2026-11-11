"""System prompt, committed-history replay, and the simple rolling summary."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import config
from ..util import json_dumps, truncate
from .profile import _parse_json_object
from .session import Session
from .store import SessionStore


def _summary_excerpt(turn) -> str:
    parts = [f"用户: {truncate(turn.user, 220)}"]
    for step in turn.steps or []:
        if step.get("type") == "tool":
            parts.append(
                f"工具 {step.get('name')}: {truncate(str(step.get('summary') or ''), 180)}"
            )
    parts.append(f"助手: {truncate(turn.final, 260)}")
    return "\n".join(parts)


def maybe_summarize(session: Session, settings, llm, store: SessionStore, trace=None) -> dict:
    pending = session.pending_turns()
    if pending <= config.HISTORY_WINDOW:
        return {"updated": False, "pending": pending}
    start = int(session.summarized_through or 0)
    batch = session.turns[start : start + config.SUMMARY_BATCH]
    if not batch:
        return {"updated": False, "pending": pending}
    prompt = [
        {
            "role": "system",
            "content": (
                "你在压缩一段较早的 Agent 会话。保留用户目标、关键结论、重要工具结果和待办变化；"
                "不要编造，不要保留寒暄。输出严格 JSON：{\"summary\": \"...\"}，"
                f"summary 不超过 {config.SUMMARY_MAX_CHARS} 字。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"已有摘要：\n{session.summary or '（暂无）'}\n\n"
                "需要合并的较早对话：\n" + "\n\n".join(_summary_excerpt(turn) for turn in batch)
            ),
        },
    ]
    try:
        result = llm.complete(prompt, max_tokens=500, thinking=False)
        data = _parse_json_object(result.content)
        if not data or not isinstance(data.get("summary"), str):
            raise ValueError("摘要模型没有返回合法 JSON")
        summary = data["summary"].strip()[: config.SUMMARY_MAX_CHARS]
        if not summary:
            raise ValueError("摘要为空")
        session.summary = summary
        session.summarized_through = start + len(batch)
        store.save(session)
        if trace:
            trace.log(
                "summary_update",
                data={
                    "ok": True,
                    "through": session.summarized_through,
                    "summary_length": len(summary),
                    "batch": len(batch),
                },
            )
        return {"updated": True, "through": session.summarized_through, "summary": summary}
    except Exception as exc:
        if trace:
            trace.log("summary_update", data={"ok": False, "error": str(exc)[:500]})
        return {"updated": False, "error": str(exc)[:500], "pending": pending}


def format_todos(todos: list[dict], limit: int = 20) -> str:
    if not todos:
        return "（无）"
    ordered = sorted(todos, key=lambda item: (bool(item.get("done")), int(item.get("id") or 0)))
    lines = []
    for item in ordered[:limit]:
        mark = "x" if item.get("done") else " "
        lines.append(f"#{item.get('id')} [{mark}] {truncate(str(item.get('title') or ''), 120)}")
    if len(ordered) > limit:
        lines.append(f"…还有 {len(ordered) - limit} 条")
    return "\n".join(lines)


def _online_text(online_status: dict | None) -> str:
    status = online_status or {"enabled": False, "status": "off"}
    state = status.get("status") or "off"
    if not status.get("enabled") or state == "off":
        return "关闭（天气、联网搜索、网页抓取工具不可用；不要声称查过实时信息）"
    if state == "ready":
        tools = ", ".join(status.get("tool_names") or [])
        return f"开启，MCP 就绪（{tools or '联网工具可用'}）"
    error = status.get("error") or "未知错误"
    return f"开启但 MCP 不可用：{truncate(str(error), 200)}（不要声称联网成功）"


def build_system_prompt(
    session: Session,
    draft,
    settings,
    profile: dict,
    online_status: dict | None,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now().astimezone()
    sections = [
        "你是 MiniAgent，一个从零实现的本地 Agent。用中文回答；用户使用其他语言时跟随用户。",
        f"当前时间：{now.isoformat(timespec='seconds')}",
        f"联网模式：{_online_text(online_status)}",
        (
            "工具使用规则：\n"
            "- 数学/高数计算使用 calculate（求值、求导、定积分、求根）。\n"
            "- 待办使用 todo_add / todo_list / todo_complete / todo_delete，只能操作当前会话。\n"
            "- 天气使用 get_weather；新闻、实时事实、外部资料使用 web_search；需要网页正文时使用 fetch_url。\n"
            "- 需要多个工具就分多轮调用；收到工具结果后继续，不要编造工具未返回的内容。\n"
            "- 工具失败时如实说明失败原因，或换一种方式重试；不要伪造结果。\n"
            "- 联网关闭时不要声称访问过网络；如果用户要实时信息，说明联网未开启。\n"
            "- 最终回答使用轻量 Markdown：##/### 标题、**粗体**、- 或 1. 列表、围栏代码块、> 引用、标准 GFM 管道表、[标题](https://...) 链接。\n"
            "- 表格使用“表头行 + |---| 分隔行”，保持简单，不要嵌套表格或 HTML；不要用代码块包住整个回答。\n"
            "- 数学公式用 $...$ 行内、$$...$$ 独立；兼容 \\(...\\) / \\[...\\]。\n"
            "- 公式使用常见 LaTeX 子集：\\frac、\\sqrt、^、_、\\times、\\cdot、\\approx、\\cos、\\sin、希腊字母等；复杂矩阵/宏包改用普通文本或代码块。\n"
            "- 不要把整条公式放进代码块；不要在公式里输出 HTML。\n"
            "- 不要输出原始 HTML；引用网页时优先使用 Markdown 链接，裸 URL 也允许。\n"
            "- 用户可能上传图片；只根据图片中可见内容回答，不要编造细节；看不清时直接说明。\n"
            "- 普通聊天直接回答，不必强行调用工具。"
        ),
    ]
    profile_text = (profile or {}).get("summary", "").strip()
    if profile_text:
        sections.append(
            "用户画像（全局，可能过时；冲突时以当前对话为准）：\n"
            + truncate(profile_text, config.PROFILE_MAX_CHARS)
        )
    if session.summary.strip():
        sections.append(
            "当前会话的早期摘要（已提交的历史，可能不完整）：\n"
            + truncate(session.summary, config.SUMMARY_MAX_CHARS)
        )
    sections.append(
        "当前会话待办（运行时状态，不是历史消息）：\n" + format_todos(draft.todos)
    )
    return "\n\n".join(sections)


def build_messages(
    session: Session,
    draft,
    settings,
    profile: dict,
    online_status: dict | None,
    now: datetime | None = None,
) -> list[dict]:
    messages: list[dict] = [
        {
            "role": "system",
            "content": build_system_prompt(session, draft, settings, profile, online_status, now),
        }
    ]
    start = int(session.summarized_through or 0)
    for turn in session.turns[start:]:
        if turn.messages:
            messages.extend(turn.messages)
        elif turn.user or turn.final:
            messages.append({"role": "user", "content": turn.user})
            if turn.final:
                messages.append({"role": "assistant", "content": turn.final})
    messages.extend(draft.messages)
    return messages


def context_stats(
    session: Session,
    draft,
    settings,
    profile: dict,
    online_status: dict | None,
) -> dict:
    prompt = build_system_prompt(session, draft, settings, profile, online_status)
    return {
        "system_chars": len(prompt),
        "summary_chars": len(session.summary or ""),
        "profile_chars": len((profile or {}).get("summary", "") or ""),
        "pending_turns": session.pending_turns(),
        "draft_messages": len(draft.messages),
        "online": (online_status or {}).get("status", "off"),
    }


def summary_debug(session: Session) -> str:
    return json_dumps(
        {
            "summary": session.summary,
            "summarized_through": session.summarized_through,
            "pending": session.pending_turns(),
        }
    )
