"""Outbound Model Context Protocol integration for the pathology Agent.

This package is intentionally separate from :mod:`pathcopilot_mcp`, which is
the inbound control server used by external MCP clients.  Outbound MCP lets
the PathCopilot Agent discover and call evidence tools such as PubMed.
"""

from .client import (
    MCPCallResult,
    MCPConfigurationError,
    MCPDisabledError,
    MCPToolError,
    call_mcp_tool,
    is_outbound_mcp_enabled,
    list_mcp_tools,
)

__all__ = [
    "MCPCallResult",
    "MCPConfigurationError",
    "MCPDisabledError",
    "MCPToolError",
    "call_mcp_tool",
    "is_outbound_mcp_enabled",
    "list_mcp_tools",
]
