"""Portable-Python stdio MCP entry point."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pathcopilot_mcp.server import main

main()
