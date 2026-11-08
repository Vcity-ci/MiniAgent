"""Small, explicit error types used across the agent."""
from __future__ import annotations


class AgentError(Exception):
    code = "agent_error"
    http_status = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        hint: str | None = None,
        detail: object | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.hint = hint
        self.detail = detail

    def to_dict(self, include_detail: bool = False) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        if include_detail and self.detail is not None:
            payload["detail"] = self.detail
        return payload


class ConfigError(AgentError):
    code = "config_error"
    http_status = 400


class InvalidRequest(AgentError):
    code = "invalid_request"
    http_status = 400


class SessionNotFound(AgentError):
    code = "session_not_found"
    http_status = 404


class SessionBusy(AgentError):
    code = "session_busy"
    http_status = 409


class LLMError(AgentError):
    code = "llm_error"
    http_status = 502


class LLMNotConfigured(LLMError):
    code = "llm_not_configured"
    http_status = 400

    def __init__(self, message: str = "尚未配置 DeepSeek API Key") -> None:
        super().__init__(message, hint="打开设置，填写 API Key 后再发送消息")


class ToolError(AgentError):
    code = "tool_error"
    http_status = 400


class MCPError(AgentError):
    code = "mcp_error"
    http_status = 502


class MaxStepsError(AgentError):
    code = "max_steps"
    http_status = 422


class ClientDisconnected(Exception):
    """Raised when the browser closes an SSE connection mid-turn."""
