"""Portable-Python stdio entry point for the outbound Web Search MCP server."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pathcopilot_outbound_mcp.web_search_server import main

main()
