import { api, ApiError, streamMessage } from "./api.js";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const state = {
  sessions: [],
  current: null,
  settings: null,
  health: null,
  profile: null,
  sending: false,
  pending: null,
  sendingSession: null,
  filter: "",
  theme: localStorage.getItem("miniagent-theme") || "dark",
  editingProfile: false,
  renderScheduled: false,
};

const clientId = globalThis.crypto && crypto.randomUUID
  ? crypto.randomUUID()
  : `client_${Date.now()}_${Math.random().toString(16).slice(2)}`;
let heartbeatTimer = null;

function sendHeartbeat() {
  fetch("/api/client/heartbeat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Agent-Client": "web" },
    body: JSON.stringify({ client_id: clientId }),
    keepalive: true,
  }).catch(() => {});
}

function closeClient() {
  const body = JSON.stringify({ client_id: clientId });
  try {
    if (navigator.sendBeacon) {
      const blob = new Blob([body], { type: "application/json" });
      if (navigator.sendBeacon("/api/client/close", blob)) return;
    }
  } catch (_err) {
    // fall through to keepalive fetch
  }
  try {
    fetch("/api/client/close", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Agent-Client": "web" },
      body,
      keepalive: true,
    }).catch(() => {});
  } catch (_err) {
    // page is going away
  }
}

function startLifecycle() {
  sendHeartbeat();
  heartbeatTimer = setInterval(sendHeartbeat, 5000);
  window.addEventListener("pagehide", closeClient);
  window.addEventListener("beforeunload", closeClient);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) sendHeartbeat();
  });
}

const els = {};

function cacheEls() {
  els.sidebar = $("#sidebar");
  els.sessionList = $("#session-list");
  els.sessionFilter = $("#session-filter");
  els.sessionTitle = $("#session-title");
  els.sessionChips = $("#session-chips");
  els.messages = $("#messages");
  els.emptyState = $("#empty-state");
  els.suggestions = $("#suggestions");
  els.composer = $("#composer");
  els.sendBtn = $("#send-btn");
  els.composerHint = $("#composer-hint");
  els.composerMeta = $("#composer-meta");
  els.thinkingToggle = $("#thinking-toggle");
  els.onlineToggle = $("#online-toggle");
  els.llmDot = $("#llm-dot");
  els.statusLine = $("#status-line");
  els.todosList = $("#todos-list");
  els.todoCount = $("#todo-count");
  els.profileSummary = $("#profile-summary");
  els.profileUpdated = $("#profile-updated");
  els.profileEditor = $("#profile-editor");
  els.profileInput = $("#profile-input");
  els.contextOnline = $("#context-online");
  els.contextModel = $("#context-model");
  els.contextThinking = $("#context-thinking");
  els.contextTurns = $("#context-turns");
  els.contextSummaryState = $("#context-summary-state");
  els.contextSummary = $("#context-summary");
  els.contextTools = $("#context-tools");
  els.contextToolsCount = $("#context-tools-count");
  els.modal = $("#settings-modal");
  els.settingsOnlineStatus = $("#settings-online-status");
  els.mcpStatus = $("#mcp-status");
  els.testResult = $("#test-result");
  els.keyStatus = $("#key-status");
  els.drawer = $("#drawer");
  els.toastContainer = $("#toast-container");
}

// ---------------------------------------------------------------------------
// Utilities

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function fmtTime(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString("zh-CN", { hour12: false });
  } catch (_err) {
    return iso;
  }
}

function fmtShort(iso) {
  if (!iso) return "";
  try {
    const date = new Date(iso);
    const now = new Date();
    const sameDay = date.toDateString() === now.toDateString();
    return sameDay
      ? date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
      : date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
  } catch (_err) {
    return "";
  }
}

function fmtDuration(ms) {
  if (ms === null || ms === undefined) return "";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function toast(message, type = "") {
  const node = el("div", `toast ${type}`.trim(), message);
  els.toastContainer.appendChild(node);
  setTimeout(() => {
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 180);
  }, 4200);
}

function toastError(err) {
  const message = err instanceof Error ? err.message : String(err);
  const hint = err && err.hint ? `\n${err.hint}` : "";
  toast(message + hint, "error");
}

function hasKey() {
  return Boolean(state.settings && state.settings.llm_ready);
}

function scheduleRender() {
  if (state.renderScheduled) return;
  state.renderScheduled = true;
  requestAnimationFrame(() => {
    state.renderScheduled = false;
    renderChat();
    scrollBottom();
  });
}

function scrollBottom() {
  if (!els.messages) return;
  els.messages.scrollTop = els.messages.scrollHeight;
}

function autoGrow() {
  const node = els.composer;
  node.style.height = "auto";
  node.style.height = `${Math.min(node.scrollHeight, 180)}px`;
}

function parseHash() {
  const match = location.hash.match(/^#\/s\/(s_[a-f0-9]{12})$/);
  return match ? match[1] : "";
}

function applyTheme(theme) {
  state.theme = theme;
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("miniagent-theme", theme);
  const button = $("#theme-toggle");
  if (button) button.textContent = theme === "dark" ? "☀" : "◐";
}

// ---------------------------------------------------------------------------
// Loading

async function loadHealth() {
  state.health = await api("/api/health");
  renderStatus();
  renderContext();
}

async function loadSettings() {
  state.settings = await api("/api/settings");
  renderHeader();
  renderComposer();
  renderContext();
}

async function loadProfile() {
  const data = await api("/api/profile");
  state.profile = data.profile;
  renderProfile();
}

async function loadSessions() {
  const data = await api("/api/sessions");
  state.sessions = data.sessions || [];
  renderSessions();
}

async function loadSession(id, { push = true } = {}) {
  const data = await api(`/api/sessions/${id}`);
  state.current = data.session;
  if (push) history.replaceState(null, "", `#/s/${id}`);
  renderHeader();
  renderChat();
  renderDrawer();
  renderSessions();
  renderComposer();
  scrollBottom();
}

async function refreshAll() {
  await Promise.all([loadHealth(), loadSettings(), loadProfile()]);
  await loadSessions();
}

// ---------------------------------------------------------------------------
// Sessions

async function createSession({ select = true } = {}) {
  if (state.sending) {
    toast("请等待当前回答完成", "error");
    return null;
  }
  try {
    const data = await api("/api/sessions", { method: "POST", body: {} });
    await loadSessions();
    if (select) await selectSession(data.session.id);
    return data.session;
  } catch (err) {
    toastError(err);
    return null;
  }
}

async function selectSession(id) {
  if (state.sending) {
    toast("请等待当前回答完成", "error");
    return;
  }
  try {
    await loadSession(id);
    els.sidebar.classList.remove("open");
    els.drawer.classList.remove("open");
  } catch (err) {
    toastError(err);
    await loadSessions();
    if (state.sessions.length) await loadSession(state.sessions[0].id);
  }
}

async function renameSession(session) {
  if (state.sending) return;
  const title = window.prompt("重命名会话", session.title || "新会话");
  if (title === null) return;
  try {
    const data = await api(`/api/sessions/${session.id}`, {
      method: "PATCH",
      body: { title },
    });
    if (state.current && state.current.id === session.id) state.current = data.session;
    await loadSessions();
    renderHeader();
  } catch (err) {
    toastError(err);
  }
}

async function deleteSession(session) {
  if (state.sending) return;
  if (!window.confirm(`删除会话「${session.title || "新会话"}」？此操作不可撤销。`)) return;
  try {
    await api(`/api/sessions/${session.id}`, { method: "DELETE" });
    const wasCurrent = state.current && state.current.id === session.id;
    await loadSessions();
    if (wasCurrent) {
      state.current = null;
      if (state.sessions.length) await loadSession(state.sessions[0].id);
      else await createSession({ select: true });
    }
    renderSessions();
  } catch (err) {
    toastError(err);
  }
}

function openSessionWindow(id) {
  window.open(`${location.origin}/#/s/${id}`, "_blank", "noopener,noreferrer");
}

function renderSessions() {
  clear(els.sessionList);
  const query = (state.filter || "").trim().toLowerCase();
  const rows = state.sessions.filter((session) => {
    if (!query) return true;
    return (session.title || "").toLowerCase().includes(query);
  });
  if (!rows.length) {
    els.sessionList.appendChild(el("div", "todo-empty", query ? "没有匹配的会话" : "暂无会话"));
    return;
  }
  for (const session of rows) {
    const item = el("div", "session-item");
    if (state.current && state.current.id === session.id) item.classList.add("active");
    item.dataset.id = session.id;

    const title = el("div", "session-title", session.title || "新会话");
    const meta = el("div", "session-meta");
    meta.appendChild(el("span", "", `${session.turn_count || 0} 轮`));
    if (session.todo_open) meta.appendChild(el("span", "", `${session.todo_open} 待办`));
    meta.appendChild(el("span", "", fmtShort(session.updated_at)));

    const actions = el("div", "session-actions");
    const rename = el("button", "", "✎");
    rename.title = "重命名";
    rename.addEventListener("click", (event) => {
      event.stopPropagation();
      renameSession(session);
    });
    const open = el("button", "", "↗");
    open.title = "在新窗口打开";
    open.addEventListener("click", (event) => {
      event.stopPropagation();
      openSessionWindow(session.id);
    });
    const del = el("button", "", "×");
    del.title = "删除";
    del.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteSession(session);
    });
    actions.append(rename, open, del);

    item.append(title, meta, actions);
    item.addEventListener("click", () => selectSession(session.id));
    els.sessionList.appendChild(item);
  }
}

// ---------------------------------------------------------------------------
// Header / composer / status

function renderHeader() {
  const session = state.current;
  els.sessionTitle.textContent = session ? session.title || "新会话" : "新会话";
  clear(els.sessionChips);
  if (state.settings) {
    els.sessionChips.appendChild(el("span", "chip", state.settings.model));
    els.sessionChips.appendChild(
      el("span", `chip ${state.settings.thinking ? "active" : ""}`, state.settings.thinking ? "思考开" : "思考关")
    );
    const online = state.settings.online_status || (state.health && state.health.online) || {};
    const onlineText = !state.settings.online
      ? "联网关"
      : online.status === "ready"
        ? "联网就绪"
        : online.status === "error"
          ? "联网异常"
          : "联网中";
    els.sessionChips.appendChild(
      el("span", `chip ${state.settings.online ? "active" : ""}`, onlineText)
    );
  }
  els.thinkingToggle.classList.toggle("active", Boolean(state.settings && state.settings.thinking));
  els.onlineToggle.classList.toggle("active", Boolean(state.settings && state.settings.online));
  els.thinkingToggle.disabled = state.sending;
  els.onlineToggle.disabled = state.sending;
  renderStatus();
}

function renderStatus() {
  const health = state.health;
  const settings = state.settings;
  let dotClass = "status-dot";
  let text = "未配置模型";
  if (settings && settings.llm_ready) {
    const online = settings.online_status || (health && health.online) || {};
    if (settings.online && online.status === "error") {
      dotClass += " error";
      text = `${settings.model} · 联网异常`;
    } else if (settings.online && online.status === "ready") {
      dotClass += " ready";
      text = `${settings.model} · 联网就绪`;
    } else if (settings.online) {
      dotClass += " warn";
      text = `${settings.model} · 联网连接中`;
    } else {
      dotClass += " warn";
      text = `${settings.model} · 联网关闭`;
    }
  } else if (settings) {
    dotClass += " warn";
  }
  els.llmDot.className = dotClass;
  els.statusLine.textContent = text;
}

function renderComposer() {
  const ready = hasKey();
  const disabled = !ready || state.sending || !state.current;
  els.composer.disabled = disabled;
  els.sendBtn.disabled = disabled;
  els.composer.placeholder = ready
    ? "问点什么…（Enter 发送，Shift+Enter 换行）"
    : "请先在设置中配置 DeepSeek API Key";
  els.composerHint.textContent = !ready
    ? "未配置模型"
    : state.sending
      ? "处理中…"
      : "Enter 发送 · Shift+Enter 换行";
  els.composerMeta.textContent = "";
  if (state.settings) {
    if (!ready) {
      const warn = el("span", "warn", "点击左下角「设置」配置模型");
      els.composerMeta.appendChild(warn);
    } else {
      els.composerMeta.appendChild(el("span", "", `model ${state.settings.model}`));
      els.composerMeta.appendChild(el("span", "", `max_steps ${state.settings.max_steps}`));
      if (state.settings.online) els.composerMeta.appendChild(el("span", "", "联网开启"));
      if (state.current) {
        els.composerMeta.appendChild(el("span", "", `历史 ${state.current.turn_count || 0} 轮`));
      }
    }
  }
  autoGrow();
}

// ---------------------------------------------------------------------------
// Chat rendering

function renderChat() {
  clear(els.messages);
  const hasPending = Boolean(state.pending);
  const turns = state.current ? state.current.turns || [] : [];
  const hasContent = hasPending || turns.length > 0;
  els.emptyState.classList.toggle("hidden", hasContent);
  if (!hasContent) {
    els.messages.classList.add("hidden");
    return;
  }
  els.messages.classList.remove("hidden");

  for (const turn of turns) {
    els.messages.appendChild(renderTurn(turn, false));
  }
  if (state.pending) {
    els.messages.appendChild(renderTurn(state.pending, true));
  }
}

function renderTurn(turn, pending) {
  const wrapper = el("div", "turn");
  const user = el("div", "msg-user");
  user.appendChild(el("div", "user-bubble", turn.user || ""));
  wrapper.appendChild(user);

  const assistant = el("div", "assistant");
  assistant.appendChild(el("div", "avatar", "M"));
  const body = el("div", "assistant-body");

  const name = el("div", "assistant-name");
  name.appendChild(el("span", "", "MiniAgent"));
  if (pending && !turn.final) {
    name.appendChild(el("span", "live-dot"));
    name.appendChild(el("span", "", "运行中"));
  } else if (pending) {
    name.appendChild(el("span", "", turn.committed ? "已提交" : "完成"));
  } else {
    name.appendChild(el("span", "", fmtTime(turn.created_at)));
  }
  body.appendChild(name);

  if (pending && !turn.final && turn.statusText) {
    const status = el("div", "status-line-inline");
    status.appendChild(el("span", "spinner"));
    status.appendChild(el("span", "", turn.statusText));
    body.appendChild(status);
  }

  if (turn.error) {
    const error = el("div", "error-box");
    error.textContent = [turn.error.message, turn.error.hint].filter(Boolean).join("\n");
    body.appendChild(error);
  }

  const answerText = pending ? (turn.final || turn.liveText || "") : turn.final || "";
  if (answerText || !turn.error) {
    const answer = el("div", `answer ${pending && !turn.final ? "pending" : ""}`.trim(), answerText || (pending ? "…" : ""));
    body.appendChild(answer);
  }

  const steps = Array.isArray(turn.steps) ? turn.steps : [];
  if (steps.length || (turn.sources && turn.sources.length)) {
    body.appendChild(renderDetails(steps, Boolean(pending), turn.sources || []));
  }

  if (turn.sources && turn.sources.length) {
    body.appendChild(renderSources(turn.sources));
  }

  assistant.appendChild(body);
  wrapper.appendChild(assistant);
  return wrapper;
}

function renderDetails(steps, open, sources) {
  const details = el("details", "details");
  if (open) details.open = true;
  const toolCount = steps.filter((step) => step.type === "tool").length;
  const summary = el("summary");
  summary.appendChild(el("span", "", "执行详情"));
  summary.appendChild(el("span", "muted", `${steps.length} 步${toolCount ? ` · ${toolCount} 个工具` : ""}`));
  details.appendChild(summary);
  const body = el("div", "details-body");
  if (!steps.length) {
    body.appendChild(el("div", "muted", "本轮直接回答，没有调用工具。"));
  } else {
    for (const step of steps) {
      if (step.type === "tool") body.appendChild(renderToolStep(step));
      else body.appendChild(renderModelStep(step));
    }
  }
  details.appendChild(body);
  return details;
}

function renderModelStep(step) {
  const wrapper = el("div", "step model");
  const rail = el("div", "step-rail");
  rail.appendChild(el("div", "step-dot"));
  const content = el("div", "step-content");
  content.appendChild(el("div", "step-label", step.text ? "说明" : "思考"));
  if (step.text) content.appendChild(el("div", "step-text", step.text));
  if (step.reasoning) {
    const reasoning = el("details", "details");
    const summary = el("summary");
    summary.appendChild(el("span", "", "思考过程"));
    reasoning.appendChild(summary);
    const body = el("div", "details-body");
    body.appendChild(el("div", "reasoning-box", step.reasoning));
    reasoning.appendChild(body);
    content.appendChild(reasoning);
  }
  wrapper.append(rail, content);
  return wrapper;
}

function renderToolStep(step) {
  const wrapper = el("div", `step tool ${step.ok === false ? "tool-error" : ""}`.trim());
  const rail = el("div", "step-rail");
  rail.appendChild(el("div", "step-dot"));
  const content = el("div", "step-content");
  const head = el("div", "tool-head");
  head.appendChild(el("span", "step-label", step.label || step.name || "工具"));
  head.appendChild(el("span", "tool-badge", step.name || ""));
  const okBadge = el("span", `tool-badge ${step.ok === false ? "fail" : step.ok === true ? "ok" : ""}`.trim());
  okBadge.textContent = step.ok === false ? "失败" : step.ok === true ? "成功" : "执行中";
  head.appendChild(okBadge);
  if (step.duration_ms !== undefined && step.duration_ms !== null) {
    head.appendChild(el("span", "tool-duration", fmtDuration(step.duration_ms)));
  }
  content.appendChild(head);
  if (step.summary) content.appendChild(el("div", "tool-summary", step.summary));
  if (step.sources && step.sources.length) content.appendChild(renderSources(step.sources));
  wrapper.append(rail, content);
  return wrapper;
}

function renderSources(sources) {
  const wrap = el("div", "sources");
  for (const source of sources) {
    const url = source && source.url ? String(source.url) : "";
    const title = source && source.title ? String(source.title) : url;
    let chip;
    if (/^https?:\/\//i.test(url)) {
      chip = el("a", "source");
      chip.href = url;
      chip.target = "_blank";
      chip.rel = "noopener noreferrer";
    } else {
      chip = el("span", "source");
    }
    chip.appendChild(el("span", "", title || url || "来源"));
    wrap.appendChild(chip);
  }
  return wrap;
}

// ---------------------------------------------------------------------------
// Sending

async function sendMessage(textOverride) {
  if (state.sending) return;
  const text = (textOverride !== undefined ? textOverride : els.composer.value).trim();
  if (!text) return;
  if (!hasKey()) {
    openSettings();
    toast("请先配置 DeepSeek API Key", "error");
    return;
  }
  if (text.length > 8000) {
    toast("消息过长（上限 8000 字）", "error");
    return;
  }
  if (!state.current) {
    const session = await createSession({ select: true });
    if (!session) return;
  }
  const sessionId = state.current.id;
  state.sending = true;
  state.sendingSession = sessionId;
  state.pending = {
    user: text,
    steps: [],
    final: "",
    liveText: "",
    statusText: "正在准备…",
    sources: [],
    error: null,
    turnId: null,
    committed: false,
    currentStep: 0,
  };
  const pending = state.pending;
  els.composer.value = "";
  autoGrow();
  renderComposer();
  renderHeader();
  scheduleRender();
  scrollBottom();

  let streamError = null;
  try {
    await streamMessage(
      sessionId,
      text,
      (event, data) => {
        if (event === "turn_start") {
          pending.turnId = data.turn_id;
          pending.statusText = "模型生成中";
        } else if (event === "status") {
          pending.statusText = data.text || pending.statusText;
        } else if (event === "step_start") {
          pending.currentStep = data.step;
          pending.steps.push({
            type: "model",
            step: data.step,
            text: "",
            reasoning: "",
          });
          pending.liveText = "";
          pending.statusText = `第 ${data.step}/${data.max_steps} 步`;
        } else if (event === "assistant_delta") {
          const step = [...pending.steps].reverse().find((item) => item.type === "model" && item.step === data.step);
          if (step) {
            step.text = (step.text || "") + (data.text || "");
            pending.liveText = step.text;
          }
        } else if (event === "reasoning") {
          const step = [...pending.steps].reverse().find((item) => item.type === "model" && item.step === data.step);
          if (step) step.reasoning = (step.reasoning || "") + (data.text || "");
        } else if (event === "tool_start") {
          pending.steps.push({
            type: "tool",
            step: data.step,
            name: data.name,
            label: data.label,
            ok: null,
            summary: "执行中…",
            sources: [],
            duration_ms: null,
          });
          pending.liveText = "";
          pending.statusText = `正在${data.label || data.name || "调用工具"}…`;
        } else if (event === "tool_end") {
          const step = [...pending.steps]
            .reverse()
            .find((item) => item.type === "tool" && item.name === data.name && item.step === data.step);
          if (step) {
            step.ok = data.ok;
            step.summary = data.summary || "";
            step.sources = data.sources || [];
            step.duration_ms = data.duration_ms;
          }
          pending.statusText = data.ok ? "工具完成，继续生成" : "工具失败，继续处理";
        } else if (event === "profile") {
          state.profile = data.profile;
          renderProfile();
        } else if (event === "final") {
          pending.final = data.turn ? data.turn.final || "" : "";
          pending.steps = data.turn && data.turn.steps ? data.turn.steps : pending.steps;
          pending.sources = data.sources || (data.turn && data.turn.sources) || [];
          pending.statusText = "完成";
          pending.liveText = pending.final;
          if (data.title && state.current) state.current.title = data.title;
          if (data.todos && state.current) state.current.todos = data.todos;
          renderHeader();
          renderTodos();
        } else if (event === "commit") {
          pending.committed = true;
        } else if (event === "error") {
          pending.error = data || { message: "未知错误" };
          pending.statusText = "失败";
          toast([data.message, data.hint].filter(Boolean).join("\n"), "error");
        }
        scheduleRender();
      },
      undefined
    );
  } catch (err) {
    streamError = err;
  }

  const completed = Boolean(pending.final) || Boolean(pending.committed);
  const failed = Boolean(streamError || pending.error || !completed);
  state.sending = false;
  state.sendingSession = null;
  state.pending = null;
  if (streamError) {
    toastError(streamError);
  }
  if (failed) {
    if (state.current && state.current.id === sessionId) {
      els.composer.value = text;
      autoGrow();
    }
    toast("本轮未保存；可以修改后重新发送", "error");
  }
  try {
    if (state.current && state.current.id === sessionId) {
      await loadSession(sessionId);
    }
    await loadSessions();
    await loadProfile();
    await loadHealth();
  } catch (err) {
    toastError(err);
  }
  renderComposer();
  renderHeader();
  renderSessions();
  renderChat();
  scrollBottom();
  if (!failed) els.composer.focus();
}

// ---------------------------------------------------------------------------
// Settings

function openSettings() {
  if (!state.settings) return;
  const settings = state.settings;
  $("#setting-base-url").value = settings.base_url || "";
  $("#setting-api-key").value = "";
  $("#setting-api-key").placeholder = settings.api_key_set
    ? "已保存，留空保持不变"
    : "粘贴 DeepSeek API Key";
  $("#setting-clear-key").checked = false;
  $("#setting-model").value = settings.model || "deepseek-flash";
  $("#setting-thinking").checked = Boolean(settings.thinking);
  $("#setting-online").checked = Boolean(settings.online);
  $("#setting-max-steps").value = settings.max_steps || 10;
  $("#setting-profile-auto").checked = Boolean(settings.profile_auto);
  els.keyStatus.textContent = settings.api_key_set
    ? `已配置 ${settings.api_key_masked || ""}`
    : "未配置";
  els.testResult.textContent = "";
  renderSettingsOnline(settings.online_status || (state.health && state.health.online));
  els.modal.classList.remove("hidden");
  setTimeout(() => {
    if (!hasKey()) $("#setting-api-key").focus();
    else $("#setting-base-url").focus();
  }, 20);
}

function closeSettings() {
  els.modal.classList.add("hidden");
}

function renderSettingsOnline(online) {
  online = online || {};
  if (!state.settings || !state.settings.online) {
    els.settingsOnlineStatus.textContent = "联网：未开启（天气/搜索/抓取不可用）";
    els.mcpStatus.textContent = "MCP：未启动";
    return;
  }
  const status = online.status || "starting";
  const names = (online.tool_names || []).join(", ");
  if (status === "ready") {
    els.settingsOnlineStatus.textContent = `联网：就绪${names ? `（${names}）` : ""}`;
    els.mcpStatus.textContent = `MCP：ready · ${names || "无工具"}`;
  } else if (status === "error") {
    els.settingsOnlineStatus.textContent = `联网：启动失败\n${online.error || "未知错误"}`;
    els.mcpStatus.textContent = `MCP：error\n${online.error || "未知错误"}`;
  } else {
    els.settingsOnlineStatus.textContent = "联网：启动中…";
    els.mcpStatus.textContent = "MCP：starting";
  }
}

async function saveSettings({ close = true, silent = false } = {}) {
  if (state.sending) {
    toast("请等待当前回答完成", "error");
    return false;
  }
  const keyInput = $("#setting-api-key");
  const clearKey = $("#setting-clear-key").checked;
  const payload = {
    base_url: $("#setting-base-url").value.trim(),
    model: $("#setting-model").value.trim(),
    thinking: $("#setting-thinking").checked,
    online: $("#setting-online").checked,
    max_steps: Number($("#setting-max-steps").value || 10),
    profile_auto: $("#setting-profile-auto").checked,
  };
  if (clearKey) {
    payload.clear_api_key = true;
  } else if (keyInput.value.trim()) {
    payload.api_key = keyInput.value.trim();
  }
  if (!payload.base_url) {
    toast("Base URL 不能为空", "error");
    return false;
  }
  if (!payload.model) {
    toast("模型名不能为空", "error");
    return false;
  }
  const saveBtn = $("#settings-save");
  saveBtn.disabled = true;
  try {
    state.settings = await api("/api/settings", { method: "PUT", body: payload });
    keyInput.value = "";
    if (close) closeSettings();
    if (!silent) toast("设置已保存（仅本次进程有效）", "success");
    await loadHealth();
    renderHeader();
    renderComposer();
    renderContext();
    renderSettingsOnline(state.settings.online_status || state.health.online);
    renderProfile();
    if (state.settings.online && (state.settings.online_status || {}).status === "error") {
      toast("联网 MCP 启动失败，工具暂不可用", "error");
    }
    return true;
  } catch (err) {
    toastError(err);
    return false;
  } finally {
    saveBtn.disabled = false;
  }
}

async function testConnection(target) {
  const keyInput = $("#setting-api-key").value.trim();
  if (target === "llm" && !keyInput && !hasKey()) {
    els.testResult.textContent = "先填写 API Key";
    return;
  }
  els.testResult.textContent = "保存当前设置并测试…";
  const saved = await saveSettings({ close: false, silent: true });
  if (!saved) {
    els.testResult.textContent = "设置保存失败";
    return;
  }
  try {
    if (target === "llm") {
      if (!hasKey()) {
        els.testResult.textContent = "先保存 API Key";
        return;
      }
      const result = await api("/api/settings/test", {
        method: "POST",
        body: { target: "llm" },
      });
      els.testResult.textContent = result.ok
        ? `模型 OK · ${result.latency_ms} ms · ${result.model}`
        : `失败：${result.error || "未知错误"}`;
    } else {
      const result = await api("/api/settings/test", {
        method: "POST",
        body: { target: "online" },
      });
      const online = result.online || {};
      els.testResult.textContent = result.ok
        ? `MCP OK · ${(online.tool_names || []).join(", ")}`
        : `失败：${online.error || result.error || "未知错误"}`;
      renderSettingsOnline(online);
    }
  } catch (err) {
    els.testResult.textContent = `失败：${err.message}`;
  }
}

async function updateToggle(field, value) {
  if (state.sending) {
    toast("请等待当前回答完成", "error");
    return;
  }
  const payload = {};
  payload[field] = value;
  els.thinkingToggle.disabled = true;
  els.onlineToggle.disabled = true;
  try {
    state.settings = await api("/api/settings", { method: "PUT", body: payload });
    await loadHealth();
    if (field === "online") {
      const online = state.settings.online_status || state.health.online;
      if (value && online.status === "error") toast(`联网启动失败：${online.error || "未知错误"}`, "error");
      else if (value && online.status === "ready") toast("联网工具已就绪", "success");
    }
    renderHeader();
    renderComposer();
    renderContext();
  } catch (err) {
    toastError(err);
  } finally {
    els.thinkingToggle.disabled = false;
    els.onlineToggle.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Drawer panels

function renderDrawer() {
  renderTodos();
  renderProfile();
  renderContext();
}

function renderTodos() {
  clear(els.todosList);
  const todos = state.current ? state.current.todos || [] : [];
  els.todoCount.textContent = `${todos.filter((item) => !item.done).length} 未完成 / ${todos.length}`;
  if (!todos.length) {
    els.todosList.appendChild(el("div", "todo-empty", "暂无待办\n可以直接说：记待办：周五交周报"));
    return;
  }
  const ordered = [...todos].sort((a, b) => Number(a.done) - Number(b.done) || Number(a.id) - Number(b.id));
  for (const todo of ordered) {
    const item = el("div", `todo-item ${todo.done ? "done" : ""}`.trim());
    const check = el("div", "todo-check", todo.done ? "✓" : "");
    const content = el("div");
    content.className = "todo-content";
    const title = el("div", "todo-title", todo.title || "");
    const id = el("div", "todo-id", `#${todo.id}`);
    content.append(title, id);
    item.append(check, content);
    els.todosList.appendChild(item);
  }
}

function renderProfile() {
  const profile = state.profile || {};
  els.profileSummary.textContent =
    profile.summary || "尚未生成。完成 5 轮对话后自动刷新，或点击「刷新」。";
  els.profileUpdated.textContent = profile.updated_at ? fmtShort(profile.updated_at) : "—";
  els.profileEditor.classList.toggle("hidden", !state.editingProfile);
  if (state.editingProfile) {
    els.profileInput.value = profile.summary || "";
  }
}

function renderContext() {
  const settings = state.settings || {};
  const health = state.health || {};
  const online = settings.online_status || health.online || {};
  els.contextOnline.textContent = !settings.online
    ? "联网关闭"
    : online.status === "ready"
      ? "MCP 就绪"
      : online.status === "error"
        ? "MCP 异常"
        : "MCP 启动中";
  els.contextModel.textContent = settings.model || "—";
  els.contextThinking.textContent = settings.thinking ? "开启" : "关闭";
  els.contextTurns.textContent = state.current ? `${state.current.turn_count || 0} 轮` : "0";
  const summary = (state.current && state.current.summary) || "";
  els.contextSummaryState.textContent = summary ? `${summary.length} 字` : "无";
  els.contextSummary.textContent = summary || "尚未触发摘要。历史超过 8 个已提交 turn 时自动合并。";
  clear(els.contextTools);
  const tools = health.tools || [];
  els.contextToolsCount.textContent = `${tools.length}`;
  if (!tools.length) {
    els.contextTools.appendChild(el("div", "todo-empty", "工具未加载"));
  }
  for (const tool of tools) {
    const row = el("div", "tool-row");
    row.appendChild(el("span", "tool-name", tool.name));
    row.appendChild(el("span", "tool-source", tool.source));
    els.contextTools.appendChild(row);
  }
}

// ---------------------------------------------------------------------------
// Profile actions

async function refreshProfile() {
  if (!hasKey()) {
    toast("需要先配置 API Key", "error");
    return;
  }
  const button = $("#profile-refresh");
  button.disabled = true;
  try {
    const result = await api("/api/profile/refresh", { method: "POST", body: {} });
    await loadProfile();
    toast(result.ok ? "画像已刷新" : `画像刷新失败：${result.error || "未知错误"}`, result.ok ? "success" : "error");
  } catch (err) {
    toastError(err);
  } finally {
    button.disabled = false;
  }
}

async function saveProfile() {
  try {
    const data = await api("/api/profile", {
      method: "PUT",
      body: { summary: els.profileInput.value },
    });
    state.profile = data.profile;
    state.editingProfile = false;
    renderProfile();
    toast("画像已保存", "success");
  } catch (err) {
    toastError(err);
  }
}

async function clearProfile() {
  if (!window.confirm("清空用户画像？")) return;
  try {
    const data = await api("/api/profile", { method: "DELETE" });
    state.profile = data.profile;
    state.editingProfile = false;
    renderProfile();
    toast("画像已清空", "success");
  } catch (err) {
    toastError(err);
  }
}

async function logoutUser() {
  if (state.sending) {
    toast("请等待当前回答完成", "error");
    return;
  }
  const confirmed = window.confirm(
    "一键注销会删除本机所有会话、对话记录、待办、用户画像、trace 日志、进程内设置和 API Key，并移除本地 .env 中的 AGENT_API_KEY。\n\n此操作不可撤销，确认继续？"
  );
  if (!confirmed) return;
  const button = $("#logout-button");
  if (button) button.disabled = true;
  try {
    const result = await api("/api/account/logout", { method: "POST", body: {} });
    state.sending = false;
    state.sendingSession = null;
    state.pending = null;
    state.sessions = [];
    state.current = null;
    state.editingProfile = false;
    state.filter = "";
    state.profile = {
      summary: "",
      updated_at: "",
      version: 0,
      source: "empty",
      auto: true,
    };
    state.settings = result.settings || (await api("/api/settings"));
    state.health = await api("/api/health");
    els.composer.value = "";
    els.profileInput.value = "";
    els.sessionFilter.value = "";
    autoGrow();
    history.replaceState(null, "", location.pathname);
    closeSettings();
    renderSessions();
    renderHeader();
    renderChat();
    renderDrawer();
    renderComposer();
    renderStatus();
    toast("已清除本机会话、画像、日志和 API Key", "success");
    openSettings();
  } catch (err) {
    toastError(err);
  } finally {
    if (button) button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Events

function bindEvents() {
  $("#new-session").addEventListener("click", () => createSession({ select: true }));
  els.sessionFilter.addEventListener("input", (event) => {
    state.filter = event.target.value;
    renderSessions();
  });
  $("#open-settings").addEventListener("click", openSettings);
  $("#settings-close").addEventListener("click", closeSettings);
  $("#settings-cancel").addEventListener("click", closeSettings);
  els.modal.addEventListener("click", (event) => {
    if (event.target === els.modal) closeSettings();
  });
  $("#settings-save").addEventListener("click", () => saveSettings({ close: true }));
  $("#test-llm").addEventListener("click", () => testConnection("llm"));
  $("#test-online").addEventListener("click", () => testConnection("online"));
  els.thinkingToggle.addEventListener("click", () =>
    updateToggle("thinking", !(state.settings && state.settings.thinking))
  );
  els.onlineToggle.addEventListener("click", () =>
    updateToggle("online", !(state.settings && state.settings.online))
  );
  $("#theme-toggle").addEventListener("click", () => applyTheme(state.theme === "dark" ? "light" : "dark"));

  $("#send-btn").addEventListener("click", () => sendMessage());
  els.composer.addEventListener("input", autoGrow);
  els.composer.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage();
    }
  });
  els.suggestions.addEventListener("click", (event) => {
    const button = event.target.closest("[data-prompt]");
    if (!button) return;
    if (!hasKey()) {
      openSettings();
      return;
    }
    els.composer.value = button.dataset.prompt || "";
    autoGrow();
    sendMessage();
  });

  $$(".drawer-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".drawer-tab").forEach((item) => item.classList.remove("active"));
      $$(".tab-panel").forEach((panel) => panel.classList.remove("active"));
      tab.classList.add("active");
      const name = tab.dataset.tab;
      const panel = $(`[data-panel="${name}"]`);
      if (panel) panel.classList.add("active");
    });
  });

  $("#toggle-drawer").addEventListener("click", () => {
    if (window.innerWidth <= 1180) {
      els.drawer.classList.toggle("open");
    } else {
      document.getElementById("app").classList.toggle("no-drawer");
    }
  });
  $("#toggle-sidebar").addEventListener("click", () => els.sidebar.classList.toggle("open"));

  $("#profile-refresh").addEventListener("click", refreshProfile);
  $("#profile-edit").addEventListener("click", () => {
    state.editingProfile = true;
    renderProfile();
  });
  $("#profile-cancel").addEventListener("click", () => {
    state.editingProfile = false;
    renderProfile();
  });
  $("#profile-save").addEventListener("click", saveProfile);
  $("#profile-clear").addEventListener("click", clearProfile);
  $("#logout-button").addEventListener("click", logoutUser);

  window.addEventListener("hashchange", () => {
    const id = parseHash();
    if (id && (!state.current || state.current.id !== id)) {
      selectSession(id);
    }
  });
  window.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === ",") {
      event.preventDefault();
      openSettings();
    }
    if (event.key === "Escape") closeSettings();
  });
}

// ---------------------------------------------------------------------------
// Boot

async function init() {
  cacheEls();
  applyTheme(state.theme);
  startLifecycle();
  bindEvents();
  try {
    await refreshAll();
    if (!state.sessions.length) {
      await createSession({ select: false });
    }
    if (!state.sessions.length) {
      renderChat();
      return;
    }
    const hashId = parseHash();
    const target = state.sessions.find((session) => session.id === hashId) || state.sessions[0];
    await loadSession(target.id, { push: false });
    if (hashId) history.replaceState(null, "", `#/s/${target.id}`);
    if (!hasKey()) openSettings();
  } catch (err) {
    toastError(err);
    els.statusLine.textContent = "服务不可用";
    els.llmDot.className = "status-dot error";
  }
}

init();
