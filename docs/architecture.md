# MiniAgent 架构

## 1. 目标与边界

从零实现一个最小可用 Agent：

- 接收用户消息；
- LLM 自主决定直接回答或调用工具；
- 执行工具；
- 根据工具结果继续或给出最终答案；
- 多 session 隔离、可续聊、上下文记忆、最大轮次、基础摘要；
- 工具注册 + 原生 function calling + 完整 trace；
- 真实 DeepSeek 模型、真实联网 MCP、真实天气；
- 不做 agent 框架（LangGraph/OpenAI SDK Agents/OpenHands 等）。

产品路径没有 mock/离线模式。`tests/` 中的 fake LLM/HTTP/MCP 只用于确定性测试。

## 2. 组件

```text
Browser (fetch + ReadableStream / SSE)
   │
   ▼
agent/web/http.py  ThreadingHTTPServer + chunked SSE
   │
   ├── SettingsManager        进程内存，重启恢复默认
   ├── SessionStore           每 session JSON，原子写，per-session lock
   ├── GlobalState            turn_counter
   ├── ProfileManager         全局画像，每 5 个已提交 turn 刷新
   ├── TraceLogger            JSONL
   └── AgentRuntime
          ├── ContextBuilder / Summary
          ├── OpenAICompatClient (DeepSeek stream + native tools)
          ├── ToolRegistry (todo_* + calculate)
          └── MCPManager → stdio MCP server
                              ├── get_weather (Open-Meteo)
                              ├── web_search (DeepSeek Search)
                              └── fetch_url (DeepSeek Fetch)
```

## 3. 单轮事务状态机

```text
POST message
  └─ validate content/session/key/busy
  └─ acquire per-session lock
  └─ load committed session
  └─ draft = {user, messages, todos copy, next_todo_id, steps, sources, usage}
  └─ emit turn_start
  └─ maybe summarize / maybe update profile      (only committed turns)
  └─ for step in 1..max_steps:
        build messages (system + summary + committed window + draft)
        stream LLM with native tools
        emit content deltas and reasoning
        if tool_calls:
            append assistant message (with reasoning_content if thinking)
            execute tools; append role:"tool"; emit friendly tool events
            continue
        else:
            final = content; break
  └─ if no final: raise max_steps; discard draft
  └─ atomically write session (turn + todos + title)
  └─ increment GlobalState
  └─ emit final / commit / done
```

### 事务不变量

> 当前对话未完整落盘提交前，整轮对话不写入任何 session 上下文。

- `draft` 只在当前请求/线程内存在。
- 用户消息、assistant 消息、tool 结果、todo 修改都在 draft/副本中。
- 失败、异常、超步数、客户端断开 → 整轮丢弃，session 文件保持提交前状态。
- 摘要/画像维护只读取已提交 turn，绝不读取当前 draft。
- 没有 running/interrupted 半状态；成功才 `os.replace` 原子提交。
- 同 session 串行；不同 session 可并行。

## 4. 原生 function calling

请求（`POST https://api.deepseek.com/chat/completions`）：

```json
{
  "model": "deepseek-flash",
  "messages": [...],
  "tools": [ ... ],
  "thinking": {"type": "disabled"},
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

- 模型返回 `delta.content`、`delta.reasoning_content`、`delta.tool_calls`。
- 工具调用的参数可能分片，按 `index` 累积；解析后交给 `ToolRegistry`。
- 下一轮把 assistant 消息原样追加（含 `tool_calls`；思考模式下含 `reasoning_content`），再追加 `role:"tool"` + `tool_call_id`。
- 多个 tool_calls 在同一条 assistant 消息中按顺序执行。
- 工具参数不是合法 JSON / 未知工具 / 工具异常 → 返回一条 `role:"tool"` 错误文本，模型可自行修正。
- 无重试/退避；流/HTTP 错误直接 SSE error + 丢弃整轮。
- 思考模式 + tools 时，DeepSeek 要求历史 `reasoning_content` 回传；session 的 provider messages 保留它，上下文回放时一并发送。摘要替换掉的旧 turn 不再回放。

## 5. 工具

| 工具 | 来源 | 实现 |
|---|---|---|
| `todo_add/list/complete/delete` | builtin | 当前 session 的 draft todos |
| `calculate` | builtin | 安全 AST；evaluate/derivative/integral/solve |
| `get_weather` | MCP | Open-Meteo geocode + forecast |
| `web_search` | MCP | DeepSeek Search `/v1/tools/search` |
| `fetch_url` | MCP | DeepSeek Fetch `/v1/tools/fetch` |

- 工具注册包含 name/description/JSON Schema；原生 `tools` 直接交给模型。
- `calculate` 求值支持常见初等函数；符号求导支持四则/幂/链式/常见函数；定积分为 Simpson 数值；求根为区间二分。禁止属性/下标/任意代码。
- 联网关闭时 MCP 工具不注册。

## 6. 上下文与摘要

system 分层：

1. 基础行为与语言；
2. 当前时间；
3. 联网状态；
4. 工具调用规则；
5. 全局用户画像（可能过时）；
6. 当前会话早期摘要；
7. 当前会话待办。

历史：

- 最近 `HISTORY_WINDOW = 8` 个已提交 turn 原样回放（provider messages）。
- 未摘要 turn 超过 8 时，在下一轮开始前把最老 `SUMMARY_BATCH = 4` 个 turn 合并进 `summary`（≤500 字，LLM 一次）；失败保留旧摘要，继续。
- 思考/推理内容不在最终答题里重复；但 provider messages 中的 `reasoning_content` 按 API 约束保留。
- 工具结果截断：进 context / 存储 ≤4000 字。
- 不做 token 预算、向量检索、复杂压缩。

用户画像：

- 全局一份，≤500 字；每 5 个已提交 turn 在下一轮开始前刷新。
- 只读取已提交 turn 的 user/assistant 文本；不读取当前 draft、不搬运工具原始载荷。
- 失败不阻塞本轮；可手动刷新/编辑/清空。
- 会话消息/todos 仍隔离；画像是唯一有意跨会话的上下文。

## 7. MCP 联网层

- 手写 `MCPClient`：stdio、按行 JSON-RPC 2.0、id/future、reader thread；`initialize` → `notifications/initialized` → `tools/list` → `tools/call`。
- 内置 `agent/mcp/server_main.py`，最多三个工具；由子进程隔离。
- `web_search` / `fetch_url` 直接调用 DeepSeek 的 Search/Fetch，不需要第三方搜索 Key。
- `get_weather` 调用 Open-Meteo（免费、无 Key）。
- online 开关启停子进程；status 为 off/starting/ready/error；失败只显示错误，不自动重连。
- 原生 tool schema 从 MCP `inputSchema` 转换。
- 不做远程 HTTP MCP、第三方 provider、mock provider。

## 8. SSE 协议

| event | 内容 |
|---|---|
| `turn_start` | turn_id、user |
| `status` | 思考/工具/摘要/画像/生成中 |
| `step_start` | step/max_steps |
| `assistant_delta` | 最终答案或工具前说明的 token |
| `reasoning` | 该步 reasoning 文本（折叠） |
| `tool_start` | 友好标签；不含原始 args/ID |
| `tool_end` | ok/summary/sources/duration |
| `final` | 完整 final、sources、todos、title |
| `commit` | 落盘 turn_id |
| `error` | 错误；本轮丢弃 |
| `done` | 流结束 |

- `text/event-stream`、HTTP/1.1 chunked、逐帧 flush。
- 原始 `tool_calls` JSON、ID、参数拼接不发给前端；只在 `logs/trace.jsonl` 中保留。

## 9. 生命周期、一键注销与数据边界

### 页面生命周期

- `run.py` 启动 HTTP server 后自动 `webbrowser.open(url)`（可 `--no-browser` 关闭）。
- 前端每个页面生成随机 `client_id`，每 5s `POST /api/client/heartbeat`；`pagehide`/`beforeunload` 通过 `navigator.sendBeacon` 发 `POST /api/client/close`。
- `ClientRegistry` 记录活跃页面；最后一次页面关闭后进入 8s 宽限；`AutoShutdown` 线程确认没有活动 turn 后调用 `server.shutdown()`，`run.py` 退出。
- 页面心跳超时 75s 是浏览器崩溃/未触发 pagehide 的兜底；从未有页面连接过时不会自动退出（纯 API/headless 场景）。
- `AppContext.active_turns` 由 SSE 消息处理期间维护；活动 turn 未结束不会强停。`--no-auto-shutdown` 可关闭该行为。

### 一键注销

设置面板「一键注销用户」→ `POST /api/account/logout`：

1. 若有活动 turn，返回 409，不做任何删除；
2. 停止 MCP 子进程；
3. 删除所有 session JSON、`profile.json`、`state.json`、`trace.jsonl`；
4. 重置进程内设置（Key/模型/思考/联网/步骤/画像开关）；
5. 删除 `.env` 中的 `AGENT_API_KEY` / `DEEPSEEK_API_KEY` 行，并 `os.environ.pop` 当前进程变量；
6. 前端清空会话列表、当前会话、待办、画像编辑框、输入框和 URL hash，回到 onboarding。

父 shell 手工 export 的 Key 不在应用控制范围，需要用户自行 unset。

### 数据边界

| 时机 | 设置/Key | sessions/profile/state/trace |
|---|---|---|
| clone | 无 | 无（只有代码、`.env.example`） |
| 第一次启动 | 默认/无 Key，onboarding | 创建空目录，无用户内容 |
| 浏览器关闭 | 进程结束，内存清零 | 保留，下次可续聊 |
| 再次启动 | 默认，需重填 Key | 保留 |
| 一键注销 | 重置，Key 清除 | 全部删除 |

- 持久化只涉及 `data/`、`logs/` 和可选 `.env`；三者都在 `.gitignore` 中，clone 后不存在。
- `scripts/security_check.py` 检查 `.gitignore`、根目录残留 `.env`/`data`/`logs`、源码中明显的 `sk-*`/`AGENT_API_KEY=` 字面量；不打印匹配值。
- `_acl_recovery/`、`tests/.tmp/` 等本地被忽略目录会在安全脚本中 warning；打包 zip 前应删除。

## 10. 错误与安全

- 无 Key：onboarding/设置提示，不生成假回复。
- 401/402/429/5xx/超时：显示明确错误；无自动重试。
- 工具异常：结构化错误回传模型。
- MCP 不可用：联网工具暂不可用，模型会被告知。
- `max_steps`：报错并丢弃整轮。
- Key 只存内存；GET 只返回掩码；日志/异常统一 redact（DeepSeek 401 会回显 Key）。
- 只绑 localhost；状态变更请求要求 `X-Agent-Client: web` + localhost Origin；页面心跳/关闭接口仅校验 localhost Origin；CSP/nosniff/no-referrer；静态防穿越。
- 一键注销只允许在没有活动 turn 时执行，避免删除过程中产生新数据。

## 11. 需求映射

| 题目要求 | 实现 |
|---|---|
| 从零/无框架 | stdlib + 手写 loop/MCP；无第三方运行时依赖 |
| 基本循环 | `Runtime.handle` 事务式状态机 |
| ≥3 工具 + todo | todo 系列、calculate、联网三件套 |
| 工具注册 Schema | `ToolSpec` + 原生 `tools` |
| 解析思考/工具/最终答案 | 流式 `reasoning_content`/`tool_calls`/`content` |
| 多 session 隔离/续聊 | 每 session JSON + 锁 + provider messages |
| 追问 | 已提交 turns + 摘要 + 工具结果 |
| 最大轮次 | `max_steps` 1–20 |
| 上下文压缩 | 固定窗口 + LLM 滚动摘要 |
| 异常/trace | 结构化错误 + JSONL + UI 执行详情 |
| 生命周期/注销 | 自动开浏览器、页面关闭自动停后端、一键清空用户状态 |
| 测试 | unittest 50 项 + live smoke/eval/security 脚本 |
