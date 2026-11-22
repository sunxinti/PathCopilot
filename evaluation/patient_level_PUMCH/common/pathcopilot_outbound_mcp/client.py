"""Official-SDK MCP client used by the PathCopilot Agent.

The client deliberately starts an allowlisted stdio server for one bounded
request and then closes it.  This costs a small process startup but avoids
leaking subprocesses across Flask workers and keeps cancellation/failure
semantics deterministic.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "mcp.outbound.json"
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})


class MCPConfigurationError(RuntimeError):
    """The outbound MCP configuration is absent, invalid, or unsafe."""


class MCPDisabledError(RuntimeError):
    """Outbound MCP was explicitly disabled for this process."""


class MCPToolError(RuntimeError):
    """An MCP server rejected or failed a tool call."""


@dataclass(frozen=True)
class MCPCallResult:
    server: str
    tool: str
    content: str
    structured_content: Any
    is_error: bool


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _FALSE_VALUES:
        return False
    if value in _TRUE_VALUES:
        return True
    raise MCPConfigurationError(
        f"{name} must be one of: 1/0, true/false, yes/no, on/off"
    )


def _config_path() -> Path:
    raw = os.environ.get("PATHCOPILOT_OUTBOUND_MCP_CONFIG", "").strip()
    return Path(raw).expanduser().resolve() if raw else DEFAULT_CONFIG_PATH


def _load_config() -> tuple[Path, dict[str, Any]]:
    path = _config_path()
    if not path.is_file():
        raise MCPConfigurationError(f"MCP config not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MCPConfigurationError(f"Invalid MCP config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MCPConfigurationError("MCP config root must be an object")
    return path, value


def is_outbound_mcp_enabled() -> bool:
    """Return whether the full product may make outbound MCP calls.

    The complete application defaults to the checked-in configuration.  A
    benchmark or offline evaluation must set ``PATHCOPILOT_OUTBOUND_MCP=0``.
    """

    explicit = os.environ.get("PATHCOPILOT_OUTBOUND_MCP")
    if explicit is not None:
        return _env_bool("PATHCOPILOT_OUTBOUND_MCP", True)
    try:
        _, config = _load_config()
    except MCPConfigurationError:
        return False
    return bool(config.get("enabled", True))


def _server_config(server_name: str) -> dict[str, Any]:
    if not is_outbound_mcp_enabled():
        raise MCPDisabledError(
            "Outbound MCP is disabled; set PATHCOPILOT_OUTBOUND_MCP=1 "
            "for the full application"
        )
    config_path, config = _load_config()
    servers = config.get("mcpServers")
    if not isinstance(servers, Mapping):
        raise MCPConfigurationError("mcpServers must be an object")
    item = servers.get(server_name)
    if not isinstance(item, Mapping):
        raise MCPConfigurationError(f"Unknown MCP server: {server_name}")
    if item.get("disabled") is True:
        raise MCPDisabledError(f"MCP server is disabled: {server_name}")

    command = str(item.get("command", "")).strip()
    if command == "${PYTHON}":
        command = sys.executable
    if not command:
        raise MCPConfigurationError(f"Missing command for MCP server: {server_name}")

    root = config_path.parent.resolve()
    args: list[str] = []
    for raw in item.get("args", []):
        value = str(raw).replace("${PROJECT_ROOT}", str(root))
        args.append(value)
    raw_cwd = str(item.get("cwd", "${PROJECT_ROOT}"))
    cwd = Path(raw_cwd.replace("${PROJECT_ROOT}", str(root))).expanduser()
    if not cwd.is_absolute():
        cwd = root / cwd
    cwd = cwd.resolve()

    env = dict(os.environ)
    configured_env = item.get("env", {})
    if configured_env is not None and not isinstance(configured_env, Mapping):
        raise MCPConfigurationError(f"env must be an object for {server_name}")
    for key, raw in (configured_env or {}).items():
        env[str(key)] = str(raw).replace("${PROJECT_ROOT}", str(root))

    allowed_tools = item.get("allowedTools", [])
    if not isinstance(allowed_tools, list) or not all(
        isinstance(name, str) and name.strip() for name in allowed_tools
    ):
        raise MCPConfigurationError(
            f"allowedTools must be a non-empty string list for {server_name}"
        )
    if not allowed_tools:
        raise MCPConfigurationError(
            f"No allowedTools configured for MCP server: {server_name}"
        )
    try:
        timeout_seconds = float(item.get("timeoutSeconds", 30))
    except (TypeError, ValueError) as exc:
        raise MCPConfigurationError(
            f"timeoutSeconds must be numeric for {server_name}"
        ) from exc
    if not 1 <= timeout_seconds <= 120:
        raise MCPConfigurationError(
            f"timeoutSeconds must be between 1 and 120 for {server_name}"
        )
    return {
        "command": command,
        "args": args,
        "cwd": str(cwd),
        "env": env,
        "allowed_tools": frozenset(allowed_tools),
        "timeout_seconds": timeout_seconds,
    }


def _text_content(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", ()) or ():
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


async def _list_tools_async(server_name: str) -> list[dict[str, Any]]:
    item = _server_config(server_name)
    parameters = StdioServerParameters(
        command=item["command"],
        args=item["args"],
        env=item["env"],
        cwd=item["cwd"],
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(
            *streams,
            read_timeout_seconds=timedelta(seconds=item["timeout_seconds"]),
        ) as session:
            await session.initialize()
            listed = await session.list_tools()
            return [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.inputSchema,
                }
                for tool in listed.tools
                if tool.name in item["allowed_tools"]
            ]


async def _call_tool_async(
    server_name: str,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> MCPCallResult:
    item = _server_config(server_name)
    if tool_name not in item["allowed_tools"]:
        raise MCPConfigurationError(
            f"Tool {tool_name!r} is not allowlisted for {server_name!r}"
        )
    parameters = StdioServerParameters(
        command=item["command"],
        args=item["args"],
        env=item["env"],
        cwd=item["cwd"],
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(
            *streams,
            read_timeout_seconds=timedelta(seconds=item["timeout_seconds"]),
        ) as session:
            await session.initialize()
            listed = await session.list_tools()
            discovered = {tool.name for tool in listed.tools}
            if tool_name not in discovered:
                raise MCPConfigurationError(
                    f"MCP server {server_name!r} did not expose {tool_name!r}"
                )
            result = await session.call_tool(tool_name, dict(arguments or {}))
            content = _text_content(result)
            structured = getattr(result, "structuredContent", None)
            is_error = bool(getattr(result, "isError", False))
            outcome = MCPCallResult(
                server=server_name,
                tool=tool_name,
                content=content,
                structured_content=structured,
                is_error=is_error,
            )
            if is_error:
                raise MCPToolError(content or f"{server_name}.{tool_name} failed")
            return outcome


def _run(coro: Any) -> Any:
    """Run an MCP coroutine from Flask threads or an already-running loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[Any] = []
    error: list[BaseException] = []

    def worker() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # propagate to the caller thread
            error.append(exc)

    thread = threading.Thread(target=worker, name="pathcopilot-mcp-call")
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def list_mcp_tools(server_name: str = "pubmed") -> list[dict[str, Any]]:
    """Initialize one server and return its allowlisted MCP tools."""

    return _run(_list_tools_async(server_name))


def call_mcp_tool(
    server_name: str,
    tool_name: str,
    arguments: Mapping[str, Any] | None = None,
) -> MCPCallResult:
    """Discover and invoke one allowlisted tool through the MCP protocol."""

    return _run(_call_tool_async(server_name, tool_name, arguments))

