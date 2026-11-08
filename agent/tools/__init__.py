from .base import ToolContext, ToolRegistry, ToolResult, ToolSpec
from .todo import register_todo_tools
from .calculate import register_calculate_tool


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_todo_tools(registry)
    register_calculate_tool(registry)
    return registry


__all__ = [
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "build_default_registry",
]
