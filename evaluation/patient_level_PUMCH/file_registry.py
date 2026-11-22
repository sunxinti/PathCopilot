# -*- coding: utf-8 -*-
"""In-process WSI registry for the headless PUMCH evaluator.

The evaluation runner registers every slide before invoking the Agent.  This
small module deliberately has no web framework, authentication, database, or disk
side effects, so importing the pathology core remains safe in a CLI process.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict


_LOCK = threading.RLock()
uploaded_files: Dict[str, Dict[str, Any]] = {}


def register_file(file_id: str, path: str | Path, *, filename: str | None = None) -> Dict[str, Any]:
    """Register one existing WSI and return a copy of its metadata."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"WSI file does not exist: {resolved}")

    record = {
        "file_id": str(file_id),
        "file_path": str(resolved),
        "filename": filename or resolved.name,
    }
    with _LOCK:
        uploaded_files[str(file_id)] = record
    return dict(record)


def get_file(file_id: str) -> Dict[str, Any]:
    """Return a copy of registered metadata, or an empty dict when absent."""
    with _LOCK:
        return dict(uploaded_files.get(str(file_id), {}))


def clear_registry() -> None:
    """Clear process-local registry state (primarily for tests)."""
    with _LOCK:
        uploaded_files.clear()
