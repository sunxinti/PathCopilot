"""Load API credentials from environment variables or a local text file.

The repository never ships usable credentials.  A local file is convenient for
parallel WSI evaluation because one key per line can be reviewed without putting
secrets in shell history.  Blank lines and lines beginning with ``#`` are ignored.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping


DEFAULT_KEY_FILE = Path(__file__).resolve().parent.parent / "config" / "api_keys.txt"


def _deduplicate(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def parse_key_text(text: str) -> list[str]:
    """Parse newline/comma-separated keys without ever logging their values."""
    values: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split("#", 1)[0].strip()
        values.extend(part.strip() for part in re.split(r"[,;]", line))
    return _deduplicate(values)


def load_api_keys(
    environment: Mapping[str, str] | None = None,
    key_file: str | Path | None = None,
) -> tuple[list[str], str]:
    """Return ``(keys, source)`` with environment variables taking precedence."""
    env = os.environ if environment is None else environment
    for name in (
        "PATHCOPILOT_API_KEYS",
        "OPENROUTER_API_KEYS",
        "PATHCOPILOT_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        raw = str(env.get(name, "") or "").strip()
        if raw:
            return parse_key_text(raw), f"environment variable {name}"

    configured_path = key_file or env.get("PATHCOPILOT_API_KEY_FILE") or DEFAULT_KEY_FILE
    path = Path(configured_path).expanduser()
    if path.is_file():
        return parse_key_text(path.read_text(encoding="utf-8-sig")), str(path)
    return [], str(path)
