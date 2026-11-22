"""Durable, public Agent records.

These records deliberately exclude private chain-of-thought.  ``content_text``
is either user-authored text, a user-visible answer, or an auditable clinical
summary/evidence statement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class _LowerValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class AgentMessageRole(_LowerValueEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AgentMessageKind(_LowerValueEnum):
    MESSAGE = "message"
    INSTRUCTION = "instruction"
    SUMMARY = "summary"
    EVIDENCE = "evidence"
    TOOL_RESULT = "tool_result"
    FOLLOWUP = "followup"


class AgentToolCallState(_LowerValueEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AgentContextSnapshot:
    snapshot_id: str
    run_id: str
    task_id: str
    parent_snapshot_id: str | None
    context: Mapping[str, Any]
    content_hash: str
    created_at: str
    schema_version: int = 1
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "schema_version": self.schema_version,
            "context": dict(self.context),
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class AgentMessage:
    message_id: str
    run_id: str
    task_id: str
    role: AgentMessageRole
    kind: AgentMessageKind
    content_text: str
    payload: Mapping[str, Any]
    content_hash: str
    created_at: str
    sender_task_id: str | None = None
    recipient_task_id: str | None = None
    delivered_at: str | None = None
    consumed_at: str | None = None
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "sender_task_id": self.sender_task_id,
            "recipient_task_id": self.recipient_task_id,
            "role": self.role.value,
            "kind": self.kind.value,
            "content_text": self.content_text,
            "payload": dict(self.payload),
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
            "consumed_at": self.consumed_at,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class AgentToolCall:
    tool_call_id: str
    run_id: str
    task_id: str
    name: str
    state: AgentToolCallState
    arguments: Mapping[str, Any]
    output: Any
    error: Mapping[str, Any] | None
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "name": self.name,
            "state": self.state.value,
            "arguments": dict(self.arguments),
            "output": self.output,
            "error": dict(self.error) if self.error is not None else None,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "idempotency_key": self.idempotency_key,
        }
