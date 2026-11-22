"""Shared control kernel used by both the CLI and MCP server."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from runtime_integration import PathCopilotRuntimeService


class ControlKernel:
    """Owner-scoped, side-effect-free access to one Runtime database."""

    def __init__(self, db_path: str | Path, owner_id: str):
        self.db_path = Path(db_path).expanduser().resolve()
        self.owner_id = str(owner_id).strip()
        if not self.owner_id:
            raise ValueError("owner_id is required")
        self.service = PathCopilotRuntimeService(self.db_path, mode="shadow")
        self.service.start()

    def close(self) -> None:
        self.service.stop()

    def __enter__(self) -> "ControlKernel":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def status(self, run_id: str) -> dict[str, Any]:
        return self.service.status(
            self._text(run_id, "run_id"), owner_id=self.owner_id
        ).to_dict()

    def agents(self, run_id: str) -> dict[str, Any]:
        return self.service.agents.agent_tree(
            run_id=self._text(run_id, "run_id"), owner_id=self.owner_id
        )

    def messages(
        self, run_id: str, *, task_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return self.service.agents.messages(
            run_id=self._text(run_id, "run_id"),
            owner_id=self.owner_id,
            task_id=task_id,
            limit=self._limit(limit),
        )

    def events(
        self, run_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        return [
            item.to_dict()
            for item in self.service.events(
                self._text(run_id, "run_id"),
                owner_id=self.owner_id,
                after_seq=after_seq,
                limit=self._limit(limit),
            )
        ]

    def tool_calls(
        self, run_id: str, *, task_id: str | None = None
    ) -> list[dict[str, Any]]:
        run_id = self._text(run_id, "run_id")
        self.service.assert_owner(run_id, self.owner_id)
        return [
            item.to_dict()
            for item in self.service.agents.agent_store.list_tool_calls(
                run_id, task_id=task_id
            )
        ]

    def followup(
        self,
        run_id: str,
        *,
        task_id: str,
        message: str,
        request_id: str,
    ) -> dict[str, Any]:
        return self.service.agents.followup(
            run_id=self._text(run_id, "run_id"),
            owner_id=self.owner_id,
            task_id=self._text(task_id, "task_id"),
            message=self._bounded(message, "message", 4000),
            idempotency_key=self._bounded(request_id, "request_id", 200),
        )

    def cancel(
        self, run_id: str, *, task_id: str | None = None, reason: str
    ) -> dict[str, Any]:
        run_id = self._text(run_id, "run_id")
        reason = self._bounded(reason, "reason", 500)
        if task_id:
            task = self.service.agents.stop_agent(
                run_id=run_id,
                owner_id=self.owner_id,
                task_id=self._text(task_id, "task_id"),
                reason=reason,
            )
            return {"run_id": run_id, "task": self.service.agents.task_view(task)}
        return self.service.cancel(
            run_id,
            owner_id=self.owner_id,
            requested_by=self.owner_id,
            reason=reason,
        ).to_dict()

    def wait(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 0.0,
        poll_seconds: float = 0.2,
    ) -> dict[str, Any]:
        if timeout_seconds < 0 or timeout_seconds > 86400:
            raise ValueError("timeout_seconds must be from 0 to 86400")
        if poll_seconds < 0.05 or poll_seconds > 5:
            raise ValueError("poll_seconds must be from 0.05 to 5")
        deadline = (
            time.monotonic() + timeout_seconds if timeout_seconds else None
        )
        while True:
            current = self.status(run_id)
            if current["run_state"] in {
                "succeeded",
                "failed",
                "cancelled",
                "timed_out",
            }:
                return current
            if deadline is not None and time.monotonic() >= deadline:
                return current
            time.sleep(poll_seconds)

    @staticmethod
    def _text(value: Any, label: str) -> str:
        return ControlKernel._bounded(value, label, 240)

    @staticmethod
    def _bounded(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is required")
        value = value.strip()
        if len(value) > maximum:
            raise ValueError(f"{label} exceeds {maximum} characters")
        return value

    @staticmethod
    def _limit(value: Any) -> int:
        if type(value) is not int or not 1 <= value <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        return value
