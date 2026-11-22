"""Deprecated compatibility wrapper for the former prompt-module name.

PathCopilot's loop is an Agent Loop. MCP names are reserved for actual
protocol servers, registries, transports, and tools.
"""

try:
    from .agent_loop_prompts import *  # noqa: F401,F403
except ImportError:  # pragma: no cover - direct script compatibility
    from agent_loop_prompts import *  # type: ignore  # noqa: F401,F403
