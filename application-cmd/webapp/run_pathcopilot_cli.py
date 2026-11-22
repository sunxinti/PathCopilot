"""Portable-Python entry point (embeddable Python omits the script directory)."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pathcopilot_cli.cli import main

raise SystemExit(main())
