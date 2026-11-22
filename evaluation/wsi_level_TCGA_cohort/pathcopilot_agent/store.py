"""Agent conversation/context persistence on the Runtime SQLite database."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from pathcopilot_runtime.errors import (
    IdempotencyConflict,
    RuntimeValidationError,
)
from pathcopilot_runtime.models import utc_now_iso
from pathcopilot_runtime.sqlite_event_store import SQLiteEventStore

from .models import (
    AgentContextSnapshot,
    AgentMessage,
    AgentMessageKind,
    AgentMessageRole,
    AgentToolCall,
    AgentToolCallState,
)


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeValidationError("Agent value must be JSON serializable") from exc


def _hash(*values: Any) -> str:
    canonical = _json(values)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _required_text(value: Any, label: str, maximum: int = 65536) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeValidationError(f"{label} is required")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise RuntimeValidationError(f"{label} exceeds {maximum} characters")
    return normalized


class AgentStore:
    """Append-first Agent facts sharing Runtime transactions and foreign keys."""

    def __init__(self, runtime_store: SQLiteEventStore):
        self.runtime_store = runtime_store

    def snapshot_context(
        self,
        *,
        run_id: str,
        task_id: str,
        context: Mapping[str, Any],
        parent_snapshot_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> AgentContextSnapshot:
        if not isinstance(context, Mapping):
            raise RuntimeValidationError("context must be a mapping")
        context_json = _json(dict(context))
        content_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
        snapshot_id = f"ctx_{uuid.uuid4().hex}"
        now = utc_now_iso()
        with self.runtime_store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                row = connection.execute(
                    """
                    SELECT * FROM runtime_agent_context_snapshots
                    WHERE run_id=? AND idempotency_key=?
                    """,
                    (run_id, idempotency_key),
                ).fetchone()
                if row is not None:
                    existing = self._row_to_context(row)
                    if (
                        existing.task_id != task_id
                        or existing.parent_snapshot_id != parent_snapshot_id
                        or existing.content_hash != content_hash
                    ):
                        connection.rollback()
                        raise IdempotencyConflict(
                            "Agent context idempotency key has different content"
                        )
                    connection.commit()
                    return existing
            connection.execute(
                """
                INSERT INTO runtime_agent_context_snapshots(
                    snapshot_id, run_id, task_id, parent_snapshot_id,
                    schema_version, context_json, content_hash,
                    idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    run_id,
                    task_id,
                    parent_snapshot_id,
                    context_json,
                    content_hash,
                    idempotency_key,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM runtime_agent_context_snapshots
                WHERE snapshot_id=?
                """,
                (snapshot_id,),
            ).fetchone()
            connection.commit()
        return self._row_to_context(row)

    def latest_context(
        self, run_id: str, task_id: str
    ) -> AgentContextSnapshot | None:
        with self.runtime_store._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runtime_agent_context_snapshots
                WHERE run_id=? AND task_id=?
                ORDER BY created_at DESC, snapshot_id DESC
                LIMIT 1
                """,
                (run_id, task_id),
            ).fetchone()
        return self._row_to_context(row) if row is not None else None

    def append_message(
        self,
        *,
        run_id: str,
        task_id: str,
        role: AgentMessageRole | str,
        kind: AgentMessageKind | str = AgentMessageKind.MESSAGE,
        content_text: str,
        payload: Mapping[str, Any] | None = None,
        sender_task_id: str | None = None,
        recipient_task_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> AgentMessage:
        role = AgentMessageRole(role)
        kind = AgentMessageKind(kind)
        content_text = _required_text(content_text, "content_text")
        payload_value = dict(payload or {})
        payload_json = _json(payload_value)
        content_hash = _hash(
            task_id,
            sender_task_id,
            recipient_task_id,
            role.value,
            kind.value,
            content_text,
            payload_value,
        )
        message_id = f"msg_{uuid.uuid4().hex}"
        now = utc_now_iso()
        delivered_at = now if recipient_task_id is not None else None
        with self.runtime_store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                row = connection.execute(
                    """
                    SELECT * FROM runtime_agent_messages
                    WHERE run_id=? AND idempotency_key=?
                    """,
                    (run_id, idempotency_key),
                ).fetchone()
                if row is not None:
                    existing = self._row_to_message(row)
                    if existing.content_hash != content_hash:
                        connection.rollback()
                        raise IdempotencyConflict(
                            "Agent message idempotency key has different content"
                        )
                    connection.commit()
                    return existing
            connection.execute(
                """
                INSERT INTO runtime_agent_messages(
                    message_id, run_id, task_id, sender_task_id,
                    recipient_task_id, role, kind, content_text,
                    payload_json, content_hash, idempotency_key,
                    created_at, delivered_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    message_id,
                    run_id,
                    task_id,
                    sender_task_id,
                    recipient_task_id,
                    role.value,
                    kind.value,
                    content_text,
                    payload_json,
                    content_hash,
                    idempotency_key,
                    now,
                    delivered_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_agent_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            connection.commit()
        return self._row_to_message(row)

    def list_messages(
        self,
        run_id: str,
        *,
        task_id: str | None = None,
        recipient_task_id: str | None = None,
        unconsumed_only: bool = False,
        limit: int = 500,
    ) -> list[AgentMessage]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise RuntimeValidationError("limit must be an integer from 1 to 1000")
        clauses = ["run_id=?"]
        values: list[Any] = [run_id]
        if task_id is not None:
            clauses.append("task_id=?")
            values.append(task_id)
        if recipient_task_id is not None:
            clauses.append("recipient_task_id=?")
            values.append(recipient_task_id)
        if unconsumed_only:
            clauses.append("consumed_at IS NULL")
        values.append(limit)
        with self.runtime_store._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM runtime_agent_messages
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at ASC, message_id ASC
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def consume_message(self, run_id: str, message_id: str) -> AgentMessage:
        now = utc_now_iso()
        with self.runtime_store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE runtime_agent_messages
                SET consumed_at=COALESCE(consumed_at, ?)
                WHERE run_id=? AND message_id=?
                """,
                (now, run_id, message_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeValidationError("Agent message was not found")
            row = connection.execute(
                "SELECT * FROM runtime_agent_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            connection.commit()
        return self._row_to_message(row)

    def begin_tool_call(
        self,
        *,
        run_id: str,
        task_id: str,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> AgentToolCall:
        name = _required_text(name, "tool name", maximum=160)
        arguments_value = dict(arguments or {})
        arguments_json = _json(arguments_value)
        tool_call_id = f"tool_{uuid.uuid4().hex}"
        now = utc_now_iso()
        with self.runtime_store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                row = connection.execute(
                    """
                    SELECT * FROM runtime_agent_tool_calls
                    WHERE run_id=? AND idempotency_key=?
                    """,
                    (run_id, idempotency_key),
                ).fetchone()
                if row is not None:
                    existing = self._row_to_tool_call(row)
                    if (
                        existing.task_id != task_id
                        or existing.name != name
                        or dict(existing.arguments) != arguments_value
                    ):
                        connection.rollback()
                        raise IdempotencyConflict(
                            "Agent tool-call idempotency key has different content"
                        )
                    connection.commit()
                    return existing
            connection.execute(
                """
                INSERT INTO runtime_agent_tool_calls(
                    tool_call_id, run_id, task_id, name, state,
                    arguments_json, output_json, error_json,
                    idempotency_key, created_at, started_at, ended_at
                ) VALUES (?, ?, ?, ?, 'running', ?, NULL, NULL, ?, ?, ?, NULL)
                """,
                (
                    tool_call_id,
                    run_id,
                    task_id,
                    name,
                    arguments_json,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runtime_agent_tool_calls WHERE tool_call_id=?",
                (tool_call_id,),
            ).fetchone()
            connection.commit()
        return self._row_to_tool_call(row)

    def finish_tool_call(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        output: Any = None,
        error: Mapping[str, Any] | None = None,
        cancelled: bool = False,
    ) -> AgentToolCall:
        if cancelled and error is not None:
            raise RuntimeValidationError("cancelled tool call cannot also fail")
        state = (
            AgentToolCallState.CANCELLED
            if cancelled
            else (
                AgentToolCallState.FAILED
                if error is not None
                else AgentToolCallState.SUCCEEDED
            )
        )
        output_json = _json(output) if output is not None else None
        error_json = _json(dict(error)) if error is not None else None
        now = utc_now_iso()
        with self.runtime_store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE runtime_agent_tool_calls
                SET state=?, output_json=?, error_json=?, ended_at=?
                WHERE run_id=? AND tool_call_id=?
                  AND state IN ('pending', 'running')
                """,
                (
                    state.value,
                    output_json,
                    error_json,
                    now,
                    run_id,
                    tool_call_id,
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    """
                    SELECT * FROM runtime_agent_tool_calls
                    WHERE run_id=? AND tool_call_id=?
                    """,
                    (run_id, tool_call_id),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise RuntimeValidationError("Agent tool call was not found")
                existing = self._row_to_tool_call(row)
                if existing.state is not state:
                    connection.rollback()
                    raise RuntimeValidationError(
                        "Agent tool call is already terminal"
                    )
                connection.commit()
                return existing
            row = connection.execute(
                "SELECT * FROM runtime_agent_tool_calls WHERE tool_call_id=?",
                (tool_call_id,),
            ).fetchone()
            connection.commit()
        return self._row_to_tool_call(row)

    def list_tool_calls(
        self, run_id: str, *, task_id: str | None = None
    ) -> list[AgentToolCall]:
        query = "SELECT * FROM runtime_agent_tool_calls WHERE run_id=?"
        values: list[Any] = [run_id]
        if task_id is not None:
            query += " AND task_id=?"
            values.append(task_id)
        query += " ORDER BY created_at ASC, tool_call_id ASC"
        with self.runtime_store._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._row_to_tool_call(row) for row in rows]

    @staticmethod
    def _row_to_context(row: Mapping[str, Any]) -> AgentContextSnapshot:
        return AgentContextSnapshot(
            snapshot_id=row["snapshot_id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            parent_snapshot_id=row["parent_snapshot_id"],
            schema_version=int(row["schema_version"]),
            context=json.loads(row["context_json"]),
            content_hash=row["content_hash"],
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_message(row: Mapping[str, Any]) -> AgentMessage:
        return AgentMessage(
            message_id=row["message_id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            sender_task_id=row["sender_task_id"],
            recipient_task_id=row["recipient_task_id"],
            role=AgentMessageRole(row["role"]),
            kind=AgentMessageKind(row["kind"]),
            content_text=row["content_text"],
            payload=json.loads(row["payload_json"]),
            content_hash=row["content_hash"],
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
            delivered_at=row["delivered_at"],
            consumed_at=row["consumed_at"],
        )

    @staticmethod
    def _row_to_tool_call(row: Mapping[str, Any]) -> AgentToolCall:
        return AgentToolCall(
            tool_call_id=row["tool_call_id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            name=row["name"],
            state=AgentToolCallState(row["state"]),
            arguments=json.loads(row["arguments_json"]),
            output=(
                json.loads(row["output_json"])
                if row["output_json"] is not None
                else None
            ),
            error=(
                json.loads(row["error_json"])
                if row["error_json"] is not None
                else None
            ),
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
        )
