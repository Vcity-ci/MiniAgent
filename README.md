# MiniAgent

一个从零实现的最小可用 Agent：Python 3.12 标准库 + 原生 Web，**不依赖任何 agent 框架**，直接调用 DeepSeek 的原生 function calling、SSE 流式输出、Search/Fetch 联网 API，并通过一个手写 MCP stdio server 提供联网工具。


## 快速开始

```powershell
# Python 3.12+，无第三方依赖
python run.py
# 自动打开默认浏览器：http://127.0.0.1:8000
```

- 启动时自动打开默认浏览器；关闭最后一个页面后，后端约 8 秒自动停止（页面心跳兜底 75 秒）。
- 如果自动打开失败，手动访问上面的 URL 即可；页面一旦注册过，关闭页面就会触发自动停止。
- `--no-browser`：不自动开浏览器；`--no-auto-shutdown`：关闭页面后保留后端（调试用）。
- 从未有页面连接过时（纯 API/headless 使用），不会因为“没有页面”而自动退出。

首次打开会进入设置面板：

| 项 | 默认 |
|---|---|
| Base URL | `https://api.deepseek.com` |
| Model | `deepseek-flash`（可改 `deepseek-v4-pro`） |
| API Key | 你的 DeepSeek Key |
| 思考模式 | 关闭（设置里可开） |
| 联网模式 | 关闭（设置里可开） |

也可以把初始默认写进 `.env`（见 `.env.example`）。**设置和 Key 只在当前进程内存中，重启恢复默认**；会话、待办、画像、trace 落盘。

## 能力

- **原生 function calling**：`tools` + `tool_calls` + `role:"tool"`，不是自定义 JSON 协议。
- **完整 SSE**：最终答案 token 级流式；每一步思考/工具/结果都推送到前端；原生 tool_call 的参数拼接和 ID 不暴露给 UI。
- **事务式 turn**：用户输入、工具结果、todo 修改和 assistant 消息先存在于内存 draft；整轮成功后才原子写入 session。失败/超步数/异常时整轮丢弃，不进入上下文。
- **多窗口会话隔离**：`#/s/<session_id>` 绑定独立会话；A 窗口的 turns/todos 与 B 不共享；重启可续。
- **简单记忆**：固定窗口 + 一次 LLM 滚动摘要；每 5 个已提交 turn 刷新全局用户画像。不做向量库/深层压缩。
- **联网 MCP**：手写 stdio JSON-RPC client + 内置 MCP server；`get_weather`（Open-Meteo）、`web_search`/`fetch_url`（DeepSeek Search/Fetch）。
- **设置面板**：模型/Key/思考/联网/最大步骤/画像开关；可测试模型和 MCP；Key 只显示掩码。
- **Trace**：UI 执行详情 + `logs/trace.jsonl`；Key 会 redact。

## 工具（目前支持三类）

| 类别 | 工具 | 说明 |
|---|---|---|
| 待办 | `todo_add` / `todo_list` / `todo_complete` / `todo_delete` | 当前会话隔离 |
| 计算 | `calculate` | 安全 AST：求值、符号求导、定积分（数值）、区间求根；单变量高数范围 |
| 联网 | `get_weather` / `web_search` / `fetch_url` | 来自 MCP；真实 Open-Meteo 和 DeepSeek Search/Fetch |

联网关闭时联网工具不出现在 `tools` 列表中；系统提示会明确告诉模型不要伪造实时信息。

## 当前 DeepSeek API 适配

实现依据 2026-11 的官方文档：

- [Tool Calls](https://api-docs.deepseek.com/zh-cn/guides/tool_calls)：`deepseek-flash` / `deepseek-v4-pro`，`tools`、`tool_calls`、`role:"tool"`。
- [思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode)：请求体 `thinking: {"type":"enabled"|"disabled"}`；**省略时默认开启**，本项目默认显式 `disabled`。思考模式 + tools 时，历史 assistant 的 `reasoning_content` 必须回传；会话存储会保留并在上下文中回放。
- [Search / Fetch](https://api-docs.deepseek.com/zh-cn/guides/search)：`POST /v1/tools/search`、`POST /v1/tools/fetch`，与 Chat 共用同一 Bearer Key。模型不会自动联网，本项目把它做成 MCP 工具供模型调用。
- [Chat Completions](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion)：`stream:true`、`stream_options.include_usage`、`reasoning_content`、usage。

> 注意：`deepseek-chat` / `deepseek-reasoner` 是默认命名；当前文档使用 `deepseek-flash` / `deepseek-v4-pro`。设置面板允许手填模型名。

Windows 自带的 Python 可能没有 CA bundle；`agent/net.py` 会额外加载 Windows ROOT 证书。跨平台可用 `AGENT_CA_BUNDLE` 指定 CA 文件。

## 运行与目录

```text
run.py                    # 启动 Web
agent/
  settings.py             # 进程内存设置
  llm.py                  # DeepSeek 流式 + 原生 tools
  net.py                  # TLS/HTTP
  tools/                  # todo + calculate + registry
  mcp/                    # 手写 MCP client / server / DeepSeek Search-Fetch / Open-Meteo
  core/                   # 事务式 runtime、session、context、summary、profile
  web/                    # HTTP API + SSE + 静态文件
web/                      # 原生 HTML/CSS/ES Module
docs/                     # 架构、验收
scripts/                  # live_smoke / eval_tool_use
data/ logs/               # 运行时生成（.gitignore）
```

API（主要）：

- `GET /api/health`
- `GET/PUT /api/settings`、`POST /api/settings/test`
- `GET/PUT/DELETE /api/profile`、`POST /api/profile/refresh`
- `GET /api/tools`
- `GET/POST /api/sessions`、`GET/PATCH/DELETE /api/sessions/{id}`
- `POST /api/sessions/{id}/messages`（SSE）
- `GET /api/sessions/{id}/trace`
- `POST /api/account/logout`（一键注销）
- `POST /api/client/heartbeat`、`POST /api/client/close`（页面生命周期）

HTTP 服务默认只绑 `127.0.0.1`；状态变更请求要求 `X-Agent-Client: web` 并校验 localhost Origin。页面心跳/关闭接口只接受 localhost Origin，用于驱动自动停止。

## 数据生命周期与一键注销

设置面板底部有 **「一键注销用户」**。点击后会：

- 删除 `data/sessions/` 下所有会话、对话记录和待办；
- 删除 `data/profile.json` 用户画像、`data/state.json` 全局计数；
- 删除 `logs/trace.jsonl` 执行日志；
- 停止 MCP 子进程；
- 重置进程内模型/Key/思考/联网/步骤/画像设置；
- 移除本地 `.env` 中的 `AGENT_API_KEY` / `DEEPSEEK_API_KEY` 行，并清除当前进程环境变量。

如果当前有回答正在运行，注销会被拒绝，避免半轮数据。浏览器同时会清空当前页面里的会话、待办、画像编辑框和输入框。**父 shell 里手工 export 的 Key 无法由应用清除，请自行 unset。**

| 时机 | 设置 / API Key | sessions / profile / state / trace |
|---|---|---|
| `git clone` | 无 | 无（只有代码和 `.env.example`） |
| 第一次 `python run.py` | 默认值，无 Key，进入 onboarding | 创建空目录，不产生用户内容 |
| 关闭浏览器页面 | 进程结束，内存清零 | 保留（下次启动可续聊） |
| 再次启动 | 恢复默认，需重新填 Key | 保留 |
| 一键注销 | 恢复默认，Key 清除 | 全部删除 |

持久化边界：**只有** `data/`、`logs/` 和可选的 `.env` 是本机运行数据；三者都被 `.gitignore` 忽略，clone 后不存在，所以第一次启动与本地第一次启动一致。

上传前建议跑：

```powershell
python scripts/security_check.py
```

它会检查 `.gitignore` 覆盖、根目录是否残留 `.env`/`data`/`logs`、以及源码里明显的 `sk-*`/`AGENT_API_KEY=` 字面量；匹配值不会打印。`_acl_recovery/`、`tests/.tmp/` 等被忽略的本地目录会给出 warning，打包 zip 前请删除。

## 测试

真实联调（需要你的 DeepSeek Key 和网络）：

```powershell
$env:AGENT_API_KEY="<your key>"
python scripts/live_smoke.py        # DeepSeek ping + Open-Meteo + MCP web_search
python scripts/eval_tool_use.py     # 10 条真实 prompt 的工具选择评测
```

`live_smoke.py` 不会把 Key 写盘。也可以把 Key 放进本地 `.env`（已被 `.gitignore` 忽略）。

## 设计要点

- **事务不变量**：`draft` 在内存中；成功才 `os.replace` 写 session；失败整轮丢弃。todo 也改 draft 副本。摘要/画像维护只读取已提交 turn。
- **上下文**：system 分层（行为规则 + 工具规则 + 联网状态 + 画像 + 会话摘要/待办/时间）；最近 8 个已提交 turn 原样回放；超过窗口时把最老 4 个 turn 合并进摘要。思考内容只进 trace/UI，但思考+tools 的 `reasoning_content` 按 API 要求保留回放。
- **最大步骤**：默认 10，可配置 1–20；超过直接报错并丢弃整轮。
- **联网判断**：由模型根据工具描述和系统提示自主决策；本项目不做额外的硬编码分类器/强制拦截。
- **SSE 隐藏内部协议**：前端只看到“查询天气/联网搜索/计算”等友好步骤和结果摘要；原始 `tool_calls` JSON/ID/参数拼接只留在服务端 trace。
- **失败不重试**：按需求保持简单；LLM/MCP 错误直接显示并丢弃本轮，不做退避/自动重连/断线恢复。
- **生命周期**：`run.py` 自动开浏览器；页面用随机 client id 每 5 秒心跳，关闭最后一次页面后 8 秒宽限自动 `server.shutdown()`；活动 turn 未结束不会强停。
- **安全注销**：注销只允许在没有活动 turn 时执行；先停 MCP，再删会话/画像/状态/日志，最后重置设置和 `.env` Key。

更多细节见 [docs/architecture.md](docs/architecture.md) 
## 限制

- 单用户本地应用，无登录；不要直接暴露到公网。
- 设置/Key/MCP 子进程生命周期跟随进程；重启需重新填写 Key。
- 关闭最后一个浏览器页面会停止后端；如需后台常驻，用 `--no-auto-shutdown`。
- 一键注销不会删除父 shell 中手工 export 的 `AGENT_API_KEY`，需要自行 unset。
- MCP 只实现 stdio；不做远程 HTTP MCP/第三方搜索 provider。
- 计算为单变量高数范围；不做矩阵/多变量/符号积分。
- 搜索/抓取的可用性和质量取决于 DeepSeek Search/Fetch 与目标站点。
- UI 以 Chrome/Edge 等现代浏览器为目标；无构建步骤。
