const CLIENT_HEADER = "web";

export class ApiError extends Error {
  constructor(message, { code = "request_failed", status = 0, hint = "" } = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.hint = hint;
  }
}

async function errorFromResponse(response) {
  let data = null;
  try {
    data = await response.json();
  } catch (_err) {
    // ignore non-JSON errors
  }
  const err = data && data.error ? data.error : {};
  const message = err.message || `请求失败（HTTP ${response.status}）`;
  return new ApiError(message, {
    code: err.code || "request_failed",
    status: response.status,
    hint: err.hint || "",
  });
}

export async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-Agent-Client"] = CLIENT_HEADER;
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    throw new ApiError(`网络请求失败：${err.message}`, { code: "network_error" });
  }
  if (!response.ok) throw await errorFromResponse(response);
  if (response.status === 204) return {};
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch (_err) {
    throw new ApiError("服务返回了非法 JSON", { code: "bad_response" });
  }
}

export async function uploadImage(sessionId, file) {
  let response;
  try {
    response = await fetch(`/api/sessions/${sessionId}/uploads`, {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-Agent-Client": CLIENT_HEADER,
        "X-Image-Name": encodeURIComponent(file.name || ""),
      },
      body: file,
    });
  } catch (err) {
    throw new ApiError(`图片上传失败：${err.message}`, { code: "network_error" });
  }
  if (!response.ok) throw await errorFromResponse(response);
  const data = await response.json();
  if (!data || !data.image) {
    throw new ApiError("图片上传返回格式不正确", { code: "bad_response" });
  }
  return data.image;
}

/**
 * POST a message and consume the agent's SSE stream.
 * onEvent(eventName, data) is called for every named event.
 */
export async function streamMessage(sessionId, content, imageIds, onEvent, signal) {
  let response;
  try {
    response = await fetch(`/api/sessions/${sessionId}/messages`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "X-Agent-Client": CLIENT_HEADER,
      },
      body: JSON.stringify({
        content,
        image_ids: Array.isArray(imageIds) ? imageIds : [],
      }),
      signal,
    });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError(`网络请求失败：${err.message}`, { code: "network_error" });
  }
  if (!response.ok) throw await errorFromResponse(response);
  if (!response.body) throw new ApiError("浏览器不支持流式响应", { code: "no_stream" });

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const dispatch = (frame) => {
    let eventName = "message";
    const dataLines = [];
    for (const line of frame.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (!dataLines.length) return;
    const raw = dataLines.join("\n");
    let data = {};
    try {
      data = JSON.parse(raw);
    } catch (_err) {
      data = { raw };
    }
    onEvent(eventName, data);
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      dispatch(frame);
      boundary = buffer.indexOf("\n\n");
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) dispatch(buffer);
}
