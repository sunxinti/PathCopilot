"""SQLite/WAL lifecycle and event store.

State snapshots and their transition events are committed in the same short
transaction. Network, LLM, OpenSlide, and Socket.IO work must happen outside
these transactions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence

from .attempts import (
    AttemptState,
    LeaseState,
    RetryPolicy,
    TaskAttemptRecord,
    TaskClaim,
    TaskLeaseRecord,
)
from .errors import (
    AttemptConflict,
    ConcurrencyConflict,
    DuplicateActiveRun,
    IdempotencyConflict,
    LeaseLost,
    NotFoundError,
    RuntimeValidationError,
    TaskCapacityExceeded,
)
from .event_codec import EventCodec
from .events import (
    ArtifactPayload,
    AttemptCancelledPayload,
    AttemptFailedPayload,
    AttemptLeaseAcquiredPayload,
    AttemptLeaseExpiredPayload,
    AttemptLeaseReleasedPayload,
    AttemptStartedPayload,
    AttemptSucceededPayload,
    CancelRequestedPayload,
    EventType,
    RunCreatedPayload,
    RuntimeEvent,
    RuntimeEventDraft,
    StateChangedPayload,
    SubjectKind,
    TaskCreatedPayload,
    TaskRetryScheduledPayload,
    Visibility,
)
from .models import (
    RUN_TERMINAL_STATES,
    TASK_TERMINAL_STATES,
    RunPolicy,
    RunRecord,
    RunState,
    TaskRecord,
    TaskState,
    WaitReason,
    WorkflowCounts,
    utc_now_iso,
    workflow_counts,
)
from .state_machine import validate_run_transition, validate_task_transition


LOGGER = logging.getLogger(__name__)
EventSubscriber = Callable[[RuntimeEvent], None]
_SAFE_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_CANCEL_GRACE_SECONDS = 30.0
_PROJECTION_EVENT_TYPES = frozenset(
    event_type
    for event_type in EventType
    if event_type is not EventType.RUNTIME_ACTIVITY_RECORDED
)


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeValidationError(f"value is not canonical JSON: {exc}") from exc


def _json_loads(value: Optional[str], default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeValidationError("persisted JSON is invalid") from exc


def _run_creation_fingerprint(record: RunRecord) -> str:
    command = {
        "owner_id": record.owner_id,
        "session_id": record.session_id,
        "file_id": record.file_id,
        "parent_run_id": record.parent_run_id,
        "client_request_id": record.client_request_id,
        "terminal_intent": record.terminal_intent,
        "config": dict(record.config),
        "metadata": dict(record.metadata),
    }
    return hashlib.sha256(_json_dumps(command).encode("utf-8")).hexdigest()


def _task_creation_fingerprint(record: TaskRecord) -> str:
    command = {
        "run_id": record.run_id,
        "parent_task_id": record.parent_task_id,
        "continuation_of_task_id": record.continuation_of_task_id,
        "name": record.name,
        "role": record.role,
        "required": record.required,
        "metadata": dict(record.metadata),
    }
    return hashlib.sha256(_json_dumps(command).encode("utf-8")).hexdigest()


def _retry_policy_to_dict(policy: RetryPolicy) -> dict[str, Any]:
    return {
        "max_attempts": policy.max_attempts,
        "initial_backoff_seconds": float(policy.initial_backoff_seconds),
        "max_backoff_seconds": float(policy.max_backoff_seconds),
        "backoff_multiplier": float(policy.backoff_multiplier),
        "jitter_ratio": float(policy.jitter_ratio),
        "retryable_error_codes": list(policy.retryable_error_codes),
    }


def _retry_policy_from_value(value: Any) -> RetryPolicy:
    if value is None:
        return RetryPolicy()
    if isinstance(value, RetryPolicy):
        return value
    if not isinstance(value, Mapping):
        raise RuntimeValidationError("persisted RetryPolicy must be an object")
    data = dict(value)
    codes = data.get("retryable_error_codes", ())
    if isinstance(codes, list):
        data["retryable_error_codes"] = tuple(codes)
    try:
        return RetryPolicy(**data)
    except TypeError as exc:
        raise RuntimeValidationError(f"invalid RetryPolicy fields: {exc}") from exc


def _retry_creation_fingerprint(
    record: TaskRecord, policy: RetryPolicy
) -> str:
    """Keep the exact 2A fingerprint for the default retry contract."""

    legacy = _task_creation_fingerprint(record)
    if policy == RetryPolicy():
        return legacy
    command = {
        "task_creation_fingerprint_v1": legacy,
        "retry_policy": _retry_policy_to_dict(policy),
    }
    return hashlib.sha256(_json_dumps(command).encode("utf-8")).hexdigest()


def _utc_datetime(value: str | datetime | None, *, label: str = "now") -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeValidationError(
                f"{label} must be an RFC3339 timestamp"
            ) from exc
    else:
        raise RuntimeValidationError(
            f"{label} must be an RFC3339 string or datetime"
        )
    if parsed.tzinfo is None:
        raise RuntimeValidationError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: str | datetime | None, *, label: str = "now") -> str:
    return _utc_datetime(value, label=label).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _future_iso(now: datetime, seconds: float) -> str:
    return (now + timedelta(seconds=seconds)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _positive_seconds(value: Any, *, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise RuntimeValidationError(f"{label} must be a finite number > 0")
    return float(value)


def _nonnegative_seconds(value: Any, *, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise RuntimeValidationError(f"{label} must be a finite number >= 0")
    return float(value)


class SQLiteEventStore:
    """Thread/process-safe persistence for Runtime facts and projections."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        producer: str = "pathcopilot-runtime/0.1.0",
        busy_timeout_ms: int = 5000,
        codec: EventCodec | None = None,
        clock: Callable[[], str | datetime] | None = None,
        allow_test_time: bool = False,
    ):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.producer = producer
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.codec = codec or EventCodec()
        if clock is not None and not callable(clock):
            raise RuntimeValidationError("clock must be callable")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._allow_test_time = bool(allow_test_time)
        self._subscriber_lock = threading.RLock()
        self._subscribers: dict[int, EventSubscriber] = {}
        self._next_subscriber_id = 1
        self._journal_mode_initialized = False
        self._apply_migrations()

    def _command_now(
        self,
        supplied: str | datetime | None,
        *,
        label: str = "now",
    ) -> datetime:
        """Resolve command time only after the transaction owns its write lock.

        Production callers cannot forge the authority clock.  Deterministic
        tests must opt in explicitly (or inject a clock at construction).
        """

        if supplied is not None:
            if not self._allow_test_time:
                raise RuntimeValidationError(
                    f"{label} is test-only; production uses the store clock"
                )
            return _utc_datetime(supplied, label=label)
        return _utc_datetime(self._clock(), label="store clock")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=max(0.001, self.busy_timeout_ms / 1000),
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            connection.execute("PRAGMA synchronous=FULL")
            if not self._journal_mode_initialized:
                self._enable_wal_with_retry(connection)
                self._journal_mode_initialized = True
            yield connection
        finally:
            connection.close()

    def _enable_wal_with_retry(self, connection: sqlite3.Connection) -> None:
        deadline = time.monotonic() + max(
            0.001, self.busy_timeout_ms / 1000
        )
        delay = 0.005
        while True:
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                is_locked = "locked" in str(exc).lower()
                if not is_locked or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)

    def _apply_migrations(self) -> None:
        migration_dir = Path(__file__).with_name("migrations")
        migrations = sorted(migration_dir.glob("*.sql"))
        if not migrations:
            raise RuntimeError(f"no Runtime migrations found in {migration_dir}")

        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
        for migration in migrations:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    version = int(migration.name.split("_", 1)[0])
                except (ValueError, IndexError) as exc:
                    connection.rollback()
                    raise RuntimeError(
                        f"migration filename must start with an integer: {migration.name}"
                    ) from exc
                sql = migration.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                row = connection.execute(
                    "SELECT checksum FROM runtime_schema_migrations WHERE version=?",
                    (version,),
                ).fetchone()
                if row is not None:
                    if row["checksum"] != checksum:
                        connection.rollback()
                        raise RuntimeError(
                            f"migration checksum mismatch for {migration.name}"
                        )
                    connection.commit()
                    continue
                try:
                    for statement in self._iter_sql_statements(sql):
                        connection.execute(statement)
                    connection.execute(
                        """
                        INSERT INTO runtime_schema_migrations(
                            version, name, checksum, applied_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (version, migration.name, checksum, utc_now_iso()),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    @staticmethod
    def _iter_sql_statements(sql: str) -> Iterable[str]:
        """Split a migration with SQLite's parser, preserving trigger bodies."""

        buffer = ""
        for line in sql.splitlines(keepends=True):
            buffer += line
            if sqlite3.complete_statement(buffer):
                statement = buffer.strip()
                buffer = ""
                if statement:
                    yield statement
        if buffer.strip():
            raise RuntimeError("migration ends with an incomplete SQL statement")

    def subscribe(self, callback: EventSubscriber) -> Callable[[], None]:
        with self._subscriber_lock:
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            self._subscribers[subscriber_id] = callback

        def unsubscribe() -> None:
            with self._subscriber_lock:
                self._subscribers.pop(subscriber_id, None)

        return unsubscribe

    def _notify(self, events: Iterable[RuntimeEvent]) -> None:
        with self._subscriber_lock:
            subscribers = list(self._subscribers.values())
        for event in events:
            for subscriber in subscribers:
                try:
                    subscriber(event)
                except Exception:
                    # Persistence is authoritative. A failed Socket/CLI sink can
                    # replay the committed event later.
                    LOGGER.exception(
                        "Runtime event subscriber failed for %s", event.event_id
                    )

    def create_run(
        self,
        record: RunRecord,
        *,
        max_total_tasks: int = 256,
        max_task_depth: int = 8,
    ) -> tuple[RunRecord, bool]:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (record.owner_id, record.session_id, record.file_id)
        ):
            raise RuntimeValidationError("owner_id, session_id, and file_id are required")
        if record.idempotency_key is not None and (
            not isinstance(record.idempotency_key, str)
            or not record.idempotency_key.strip()
        ):
            raise RuntimeValidationError(
                "idempotency_key must be a non-empty string when supplied"
            )
        if type(max_total_tasks) is not int or max_total_tasks < 1:
            raise RuntimeValidationError("max_total_tasks must be an integer >= 1")
        if type(max_task_depth) is not int or max_task_depth < 0:
            raise RuntimeValidationError("max_task_depth must be an integer >= 0")
        if (
            record.state is not RunState.CREATED
            or record.state_version != 0
            or record.root_task_id is not None
            or record.started_at is not None
            or record.ended_at is not None
            or record.cancel_requested_at is not None
            or record.error is not None
            or record.last_seq != 0
        ):
            raise RuntimeValidationError(
                "Run must be created as a pristine CREATED record"
            )
        creation_fingerprint = _run_creation_fingerprint(record)
        now = utc_now_iso()
        record = replace(record, created_at=record.created_at or now, updated_at=now)
        event: RuntimeEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if record.idempotency_key:
                    existing_row = connection.execute(
                        """
                        SELECT * FROM runtime_runs
                        WHERE owner_id=? AND idempotency_key=?
                        """,
                        (record.owner_id, record.idempotency_key),
                    ).fetchone()
                    if existing_row is not None:
                        existing = self._row_to_run(existing_row)
                        stored_fingerprint = existing_row[
                            "creation_fingerprint"
                        ]
                        if stored_fingerprint is None:
                            # Compatibility for databases produced by the
                            # unpublished v1 prototype. Mutable projection
                            # fields cannot be reconstructed perfectly, so
                            # only stable command identity is accepted once.
                            same_legacy_command = (
                                existing.session_id == record.session_id
                                and existing.file_id == record.file_id
                                and dict(existing.config) == dict(record.config)
                                and existing.parent_run_id
                                == record.parent_run_id
                                and existing.client_request_id
                                == record.client_request_id
                            )
                            if same_legacy_command:
                                connection.execute(
                                    """
                                    UPDATE runtime_runs
                                    SET creation_fingerprint=?
                                    WHERE run_id=? AND creation_fingerprint IS NULL
                                    """,
                                    (creation_fingerprint, existing.run_id),
                                )
                                stored_fingerprint = creation_fingerprint
                        if stored_fingerprint != creation_fingerprint:
                            raise IdempotencyConflict(
                                "run idempotency key was reused for a different request"
                            )
                        self._get_run_policy_conn(connection, existing.run_id)
                        connection.commit()
                        return existing, False

                active_row = self._find_active_run_row(
                    connection,
                    owner_id=record.owner_id,
                    session_id=record.session_id,
                    file_id=record.file_id,
                )
                if active_row is not None:
                    raise DuplicateActiveRun(active_row["run_id"])

                connection.execute(
                    """
                    INSERT INTO runtime_runs(
                        run_id, owner_id, session_id, file_id, parent_run_id,
                        client_request_id, idempotency_key, state, state_version,
                        root_task_id, terminal_intent, config_json, metadata_json,
                        error_json, last_seq, created_at, started_at, updated_at,
                        ended_at, cancel_requested_at, creation_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.run_id,
                        record.owner_id,
                        record.session_id,
                        record.file_id,
                        record.parent_run_id,
                        record.client_request_id,
                        record.idempotency_key,
                        record.state.value,
                        record.state_version,
                        record.root_task_id,
                        record.terminal_intent,
                        _json_dumps(record.config),
                        _json_dumps(record.metadata),
                        _json_dumps(record.error) if record.error is not None else None,
                        0,
                        record.created_at,
                        record.started_at,
                        record.updated_at,
                        record.ended_at,
                        record.cancel_requested_at,
                        creation_fingerprint,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runtime_run_policies(
                        run_id, max_total_tasks, max_task_depth, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        record.run_id,
                        max_total_tasks,
                        max_task_depth,
                        record.created_at,
                    ),
                )
                draft = RuntimeEventDraft(
                    run_id=record.run_id,
                    event_type=EventType.RUN_CREATED,
                    payload=RunCreatedPayload(
                        owner_id=record.owner_id,
                        session_id=record.session_id,
                        file_id=record.file_id,
                        initial_state=record.state.value,
                    ),
                    subject_kind=SubjectKind.RUN,
                    subject_id=record.run_id,
                    visibility=Visibility.AUDIT,
                    idempotency_key=f"run-created:{record.run_id}",
                )
                event, _ = self._append_event_conn(connection, draft)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_run_conn(connection, record.run_id)
        if event is not None:
            self._notify([event])
        return saved, True

    def create_task(self, record: TaskRecord) -> TaskRecord:
        """Compatibility wrapper around the single atomic spawn primitive."""

        saved, _ = self.spawn_task(record)
        return saved

    def spawn_task(self, record: TaskRecord) -> tuple[TaskRecord, bool]:
        """Create a genuinely legacy 2A Task.

        The first worker claim atomically and permanently upgrades this Task
        to Attempt-managed execution.  Until then the 2A transition API
        remains available.
        """

        return self._spawn_task_with_retry_policy(
            record, RetryPolicy(), attempt_managed=False
        )

    def spawn_task_with_retry_policy(
        self,
        record: TaskRecord,
        retry_policy: RetryPolicy,
    ) -> tuple[TaskRecord, bool]:
        """Create an Attempt-managed Task with immutable retry policy."""

        return self._spawn_task_with_retry_policy(
            record, retry_policy, attempt_managed=True
        )

    def _spawn_task_with_retry_policy(
        self,
        record: TaskRecord,
        retry_policy: RetryPolicy,
        *,
        attempt_managed: bool,
    ) -> tuple[TaskRecord, bool]:
        """Atomically deduplicate, normalize, validate, and create one Task."""

        retry_policy = _retry_policy_from_value(retry_policy)
        if (
            not isinstance(record.name, str)
            or not isinstance(record.role, str)
            or not record.name.strip()
            or not record.role.strip()
        ):
            raise RuntimeValidationError("task name and role are required")
        if (
            record.state is not TaskState.CREATED
            or record.state_version != 0
            or record.wait_reason is not None
            or record.attempt != 0
            or record.started_at is not None
            or record.ended_at is not None
            or record.cancel_requested_at is not None
            or record.result is not None
            or record.error is not None
        ):
            raise RuntimeValidationError(
                "Task must be created as a pristine CREATED record"
            )
        if record.spawn_idempotency_key is not None and (
            not isinstance(record.spawn_idempotency_key, str)
            or not record.spawn_idempotency_key.strip()
        ):
            raise RuntimeValidationError(
                "spawn_idempotency_key must be a non-empty string when supplied"
            )
        record = replace(
            record,
            name=record.name.strip(),
            role=record.role.strip(),
            spawn_idempotency_key=(
                record.spawn_idempotency_key.strip()
                if record.spawn_idempotency_key is not None
                else None
            ),
        )
        creation_fingerprint = _retry_creation_fingerprint(
            record, retry_policy
        )
        if attempt_managed:
            creation_fingerprint = hashlib.sha256(
                _json_dumps(
                    {
                        "task_creation_fingerprint_v1": creation_fingerprint,
                        "execution_mode": "attempt_managed",
                    }
                ).encode("utf-8")
            ).hexdigest()
        event: RuntimeEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if record.spawn_idempotency_key:
                    existing_row = connection.execute(
                        """
                        SELECT * FROM runtime_tasks
                        WHERE run_id=? AND spawn_idempotency_key=?
                        """,
                        (record.run_id, record.spawn_idempotency_key),
                    ).fetchone()
                    if existing_row is not None:
                        if (
                            existing_row["creation_fingerprint"]
                            != creation_fingerprint
                        ):
                            raise IdempotencyConflict(
                                "Task spawn idempotency key was reused for a "
                                "different request"
                            )
                        connection.commit()
                        return self._row_to_task(existing_row), False

                run = self._get_run_conn(connection, record.run_id)
                if run.state in {
                    RunState.FINALIZING,
                    RunState.CANCELLING,
                    *RUN_TERMINAL_STATES,
                } or run.cancel_requested_at is not None:
                    raise RuntimeValidationError(
                        f"cannot create a Task while Run is {run.state.value} "
                        "or cancellation has been requested"
                    )

                if record.parent_task_id is None:
                    if run.root_task_id is not None:
                        raise RuntimeValidationError("Run already has a root Task")
                    depth = 0
                else:
                    parent = self._get_task_conn(connection, record.parent_task_id)
                    if parent.run_id != record.run_id:
                        raise RuntimeValidationError(
                            "parent Task must belong to the same Run"
                        )
                    if (
                        parent.is_terminal
                        or parent.state is TaskState.CANCELLING
                        or parent.cancel_requested_at is not None
                    ):
                        raise RuntimeValidationError(
                            "cannot attach a child to a terminal or cancelling Task"
                        )
                    depth = parent.depth + 1

                policy = self._get_run_policy_conn(connection, record.run_id)
                if depth > policy.max_task_depth:
                    raise RuntimeValidationError(
                        f"Task depth {depth} exceeds "
                        f"max_depth={policy.max_task_depth}"
                    )
                task_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM runtime_tasks WHERE run_id=?",
                        (record.run_id,),
                    ).fetchone()[0]
                )
                if task_count >= policy.max_total_tasks:
                    raise TaskCapacityExceeded(
                        record.run_id, policy.max_total_tasks
                    )

                if record.continuation_of_task_id:
                    previous = self._get_task_conn(
                        connection, record.continuation_of_task_id
                    )
                    if previous.run_id != record.run_id or not previous.is_terminal:
                        raise RuntimeValidationError(
                            "continuation target must be terminal and in the same Run"
                        )

                now = utc_now_iso()
                record = replace(
                    record,
                    depth=depth,
                    created_at=record.created_at or now,
                    updated_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO runtime_tasks(
                        task_id, run_id, parent_task_id, continuation_of_task_id,
                        name, role, state, state_version, depth, required,
                        wait_reason, attempt, metadata_json, result_json, error_json,
                        created_at, started_at, updated_at, ended_at,
                        cancel_requested_at, spawn_idempotency_key,
                        creation_fingerprint, attempt_managed
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?
                    )
                    """,
                    (
                        record.task_id,
                        record.run_id,
                        record.parent_task_id,
                        record.continuation_of_task_id,
                        record.name,
                        record.role,
                        record.state.value,
                        record.state_version,
                        record.depth,
                        int(record.required),
                        record.wait_reason.value if record.wait_reason else None,
                        record.attempt,
                        _json_dumps(record.metadata),
                        _json_dumps(record.result) if record.result is not None else None,
                        _json_dumps(record.error) if record.error is not None else None,
                        record.created_at,
                        record.started_at,
                        record.updated_at,
                        record.ended_at,
                        record.cancel_requested_at,
                        record.spawn_idempotency_key,
                        creation_fingerprint,
                        int(attempt_managed),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runtime_task_retry_policies(
                        run_id, task_id, max_attempts,
                        initial_backoff_seconds, max_backoff_seconds,
                        backoff_multiplier, jitter_ratio,
                        retryable_error_codes_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.run_id,
                        record.task_id,
                        retry_policy.max_attempts,
                        float(retry_policy.initial_backoff_seconds),
                        float(retry_policy.max_backoff_seconds),
                        float(retry_policy.backoff_multiplier),
                        float(retry_policy.jitter_ratio),
                        _json_dumps(
                            list(retry_policy.retryable_error_codes)
                        ),
                        record.created_at,
                    ),
                )
                if record.parent_task_id is None:
                    cursor = connection.execute(
                        """
                        UPDATE runtime_runs
                        SET root_task_id=?, state_version=state_version+1, updated_at=?
                        WHERE run_id=? AND root_task_id IS NULL
                        """,
                        (record.task_id, now, record.run_id),
                    )
                    if cursor.rowcount != 1:
                        raise ConcurrencyConflict(
                            "Run root Task changed during spawn"
                        )
                draft = RuntimeEventDraft(
                    run_id=record.run_id,
                    event_type=EventType.TASK_CREATED,
                    payload=TaskCreatedPayload(
                        name=record.name,
                        role=record.role,
                        initial_state=record.state.value,
                        depth=record.depth,
                        required=record.required,
                    ),
                    subject_kind=SubjectKind.TASK,
                    subject_id=record.task_id,
                    parent_subject_id=record.parent_task_id,
                    visibility=Visibility.AUDIT,
                    idempotency_key=f"task-created:{record.task_id}",
                )
                event, _ = self._append_event_conn(connection, draft)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_task_conn(connection, record.task_id)
        if event is not None:
            self._notify([event])
        return saved, True

    def append(self, draft: RuntimeEventDraft) -> RuntimeEvent:
        """Append a non-projecting fact.

        Lifecycle and Artifact events are protected because writing one without
        updating its projection in the same transaction would corrupt replay.
        """

        if draft.event_type in _PROJECTION_EVENT_TYPES:
            raise RuntimeValidationError(
                f"{draft.event_type.value} must be written through its "
                "lifecycle command"
            )
        return self._append_internal(draft)

    def _append_internal(self, draft: RuntimeEventDraft) -> RuntimeEvent:
        event: RuntimeEvent
        is_new: bool
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                event, is_new = self._append_event_conn(connection, draft)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if is_new:
            self._notify([event])
        return event

    def append_many(
        self, drafts: Sequence[RuntimeEventDraft]
    ) -> list[RuntimeEvent]:
        events: list[RuntimeEvent] = []
        new_events: list[RuntimeEvent] = []
        if not drafts:
            return events
        protected = [
            draft.event_type.value
            for draft in drafts
            if draft.event_type in _PROJECTION_EVENT_TYPES
        ]
        if protected:
            raise RuntimeValidationError(
                "projecting events must be written through lifecycle commands: "
                + ", ".join(protected)
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for draft in drafts:
                    event, is_new = self._append_event_conn(connection, draft)
                    events.append(event)
                    if is_new:
                        new_events.append(event)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._notify(new_events)
        return events

    def _append_event_conn(
        self, connection: sqlite3.Connection, draft: RuntimeEventDraft
    ) -> tuple[RuntimeEvent, bool]:
        payload_json, content_hash = self.codec.encode_draft(draft)
        run_row = connection.execute(
            "SELECT last_seq FROM runtime_runs WHERE run_id=?", (draft.run_id,)
        ).fetchone()
        if run_row is None:
            raise NotFoundError(f"Run not found: {draft.run_id}")

        if draft.idempotency_key:
            old_row = connection.execute(
                """
                SELECT * FROM runtime_events
                WHERE run_id=? AND idempotency_key=?
                """,
                (draft.run_id, draft.idempotency_key),
            ).fetchone()
            if old_row is not None:
                if old_row["content_hash"] != content_hash:
                    raise IdempotencyConflict(
                        f"event idempotency conflict: {draft.idempotency_key}"
                    )
                return self.codec.event_from_row(old_row), False

        seq = int(run_row["last_seq"]) + 1
        event_id = f"evt_{uuid.uuid4().hex}"
        recorded_at = utc_now_iso()
        correlation_id = draft.correlation_id or draft.run_id
        connection.execute(
            """
            INSERT INTO runtime_events(
                event_id, run_id, seq, schema_version, event_type, event_version,
                producer, subject_kind, subject_id, parent_subject_id, attempt_id,
                correlation_id, causation_id, idempotency_key, visibility,
                occurred_at, recorded_at, payload_json, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                draft.run_id,
                seq,
                draft.schema_version,
                draft.event_type.value,
                draft.event_version,
                self.producer,
                draft.subject_kind.value,
                draft.subject_id,
                draft.parent_subject_id,
                draft.attempt_id,
                correlation_id,
                draft.causation_id,
                draft.idempotency_key,
                draft.visibility.value,
                draft.occurred_at,
                recorded_at,
                payload_json,
                content_hash,
            ),
        )
        connection.execute(
            """
            UPDATE runtime_runs
            SET last_seq=?, updated_at=?
            WHERE run_id=?
            """,
            (seq, recorded_at, draft.run_id),
        )
        row = connection.execute(
            "SELECT * FROM runtime_events WHERE event_id=?", (event_id,)
        ).fetchone()
        assert row is not None
        return self.codec.event_from_row(row), True

    def transition_run(
        self,
        run_id: str,
        target: RunState,
        *,
        cause: str,
        expected_version: int | None = None,
        error: Mapping[str, Any] | None = None,
        metadata_patch: Mapping[str, Any] | None = None,
    ) -> RunRecord:
        if target in {RunState.CANCELLING, RunState.CANCELLED}:
            raise RuntimeValidationError(
                "cancellation states require request_cancellation()"
            )
        event: RuntimeEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._get_run_conn(connection, run_id)
                if expected_version is not None and current.state_version != expected_version:
                    raise ConcurrencyConflict(
                        f"Run version is {current.state_version}, expected "
                        f"{expected_version}"
                    )
                validate_run_transition(current.state, target)
                tasks = self._list_tasks_conn(connection, run_id)
                metadata = dict(current.metadata)
                if metadata_patch:
                    metadata.update(metadata_patch)
                self._validate_run_target(current, target, tasks, error, metadata)

                now = utc_now_iso()
                started_at = current.started_at
                ended_at = current.ended_at
                cancel_requested_at = current.cancel_requested_at
                if target is RunState.RUNNING and started_at is None:
                    started_at = now
                if target is RunState.CANCELLING and cancel_requested_at is None:
                    cancel_requested_at = now
                if target in RUN_TERMINAL_STATES:
                    ended_at = now

                cursor = connection.execute(
                    """
                    UPDATE runtime_runs
                    SET state=?, state_version=state_version+1, updated_at=?,
                        started_at=?, ended_at=?, cancel_requested_at=?,
                        metadata_json=?, error_json=?
                    WHERE run_id=? AND state_version=?
                    """,
                    (
                        target.value,
                        now,
                        started_at,
                        ended_at,
                        cancel_requested_at,
                        _json_dumps(metadata),
                        _json_dumps(error) if error is not None else None,
                        run_id,
                        current.state_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConcurrencyConflict("Run changed during transition")
                draft = RuntimeEventDraft(
                    run_id=run_id,
                    event_type=EventType.RUN_STATUS_CHANGED,
                    payload=StateChangedPayload(
                        from_state=current.state.value,
                        to_state=target.value,
                        cause=cause,
                    ),
                    subject_kind=SubjectKind.RUN,
                    subject_id=run_id,
                    visibility=Visibility.AUDIT,
                    idempotency_key=(
                        f"run-status:{run_id}:{current.state_version + 1}:{target.value}"
                    ),
                )
                event, _ = self._append_event_conn(connection, draft)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_run_conn(connection, run_id)
        if event is not None:
            self._notify([event])
        return saved

    def transition_task(
        self,
        task_id: str,
        target: TaskState,
        *,
        cause: str,
        expected_version: int | None = None,
        wait_reason: WaitReason | None = None,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        metadata_patch: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        if target in {TaskState.CANCELLING, TaskState.CANCELLED}:
            raise RuntimeValidationError(
                "cancellation states require request_cancellation()"
            )
        event: RuntimeEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._get_task_conn(connection, task_id)
                execution_row = self._task_execution_row_conn(
                    connection, task_id
                )
                attempt_exists = connection.execute(
                    """
                    SELECT 1
                    FROM runtime_task_attempts
                    WHERE task_id=?
                    LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
                is_attempt_managed = (
                    bool(execution_row["attempt_managed"])
                    or attempt_exists is not None
                    or execution_row["active_attempt_id"] is not None
                    or execution_row["active_lease_id"] is not None
                    or int(execution_row["fencing_token"]) != 0
                    or execution_row["next_retry_at"] is not None
                )
                if is_attempt_managed:
                    allowed_prepare = (
                        current.state
                        in {TaskState.CREATED, TaskState.BLOCKED}
                        and target in {TaskState.BLOCKED, TaskState.READY}
                        and execution_row["active_attempt_id"] is None
                        and execution_row["active_lease_id"] is None
                        and execution_row["next_retry_at"] is None
                    )
                    allowed_resume = (
                        current.state is TaskState.WAITING
                        and target is TaskState.READY
                        and current.wait_reason
                        is not WaitReason.RETRY_BACKOFF
                        and execution_row["active_attempt_id"] is not None
                        and execution_row["active_lease_id"] is None
                        and execution_row["next_retry_at"] is None
                    )
                    if allowed_resume and (
                        current.wait_reason is WaitReason.CHILDREN
                    ):
                        children = self._children_conn(
                            connection, current.run_id, task_id
                        )
                        allowed_resume = not any(
                            not child.is_terminal for child in children
                        )
                    if not (allowed_prepare or allowed_resume):
                        raise AttemptConflict(
                            "Attempt-managed execution may only be prepared "
                            "or resume a yielded, lease-free Attempt; worker "
                            "execution and terminal commits require fenced "
                            "Attempt commands"
                        )
                run = self._get_run_conn(connection, current.run_id)
                if expected_version is not None and current.state_version != expected_version:
                    raise ConcurrencyConflict(
                        f"Task version is {current.state_version}, expected "
                        f"{expected_version}"
                    )
                validate_task_transition(
                    current.state, target, wait_reason=wait_reason
                )
                self._validate_task_run_target(run, current, target)
                children = self._children_conn(connection, current.run_id, task_id)
                self._validate_task_target(current, target, children, result, error)

                now = utc_now_iso()
                started_at = current.started_at
                ended_at = current.ended_at
                cancel_requested_at = current.cancel_requested_at
                attempt = current.attempt
                metadata = dict(current.metadata)
                if metadata_patch:
                    metadata.update(metadata_patch)
                if target is TaskState.RUNNING and started_at is None:
                    started_at = started_at or now
                    attempt += 1
                if target is TaskState.CANCELLING and cancel_requested_at is None:
                    cancel_requested_at = now
                if target in TASK_TERMINAL_STATES:
                    ended_at = now

                cursor = connection.execute(
                    """
                    UPDATE runtime_tasks
                    SET state=?, state_version=state_version+1, wait_reason=?,
                        attempt=?, metadata_json=?, result_json=?, error_json=?,
                        started_at=?, updated_at=?, ended_at=?,
                        cancel_requested_at=?
                    WHERE task_id=? AND state_version=?
                    """,
                    (
                        target.value,
                        wait_reason.value if wait_reason else None,
                        attempt,
                        _json_dumps(metadata),
                        _json_dumps(result) if result is not None else None,
                        _json_dumps(error) if error is not None else None,
                        started_at,
                        now,
                        ended_at,
                        cancel_requested_at,
                        task_id,
                        current.state_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConcurrencyConflict("Task changed during transition")
                draft = RuntimeEventDraft(
                    run_id=current.run_id,
                    event_type=EventType.TASK_STATUS_CHANGED,
                    payload=StateChangedPayload(
                        from_state=current.state.value,
                        to_state=target.value,
                        cause=cause,
                        wait_reason=wait_reason.value if wait_reason else None,
                    ),
                    subject_kind=SubjectKind.TASK,
                    subject_id=task_id,
                    parent_subject_id=current.parent_task_id,
                    visibility=Visibility.AUDIT,
                    idempotency_key=(
                        f"task-status:{task_id}:{current.state_version + 1}:{target.value}"
                    ),
                )
                event, _ = self._append_event_conn(connection, draft)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_task_conn(connection, task_id)
        if event is not None:
            self._notify([event])
        return saved

    def request_task_cancellation(
        self,
        task_id: str,
        *,
        requested_by: str | None,
        reason: str,
        cancel_grace_seconds: float = _DEFAULT_CANCEL_GRACE_SECONDS,
        now: str | datetime | None = None,
    ) -> tuple[list[TaskRecord], bool]:
        """Persist cancellation for one non-root Task and its descendants."""

        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeValidationError("cancellation reason is required")
        if requested_by is not None and (
            not isinstance(requested_by, str) or not requested_by.strip()
        ):
            raise RuntimeValidationError(
                "requested_by must be a non-empty string when supplied"
            )
        grace = _nonnegative_seconds(
            cancel_grace_seconds, label="cancel_grace_seconds"
        )
        events: list[RuntimeEvent] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = self._command_now(now)
                now_iso = _utc_iso(now_dt)
                cancel_deadline = _future_iso(now_dt, grace)
                target = self._get_task_conn(connection, task_id)
                if target.parent_task_id is None:
                    raise RuntimeValidationError(
                        "the root Task represents the Run; cancel the Run instead"
                    )
                subtree = self._task_subtree_conn(
                    connection, target.run_id, target.task_id
                )
                if target.is_terminal or target.cancel_requested_at is not None:
                    connection.commit()
                    return subtree, False

                now = now_iso
                for task in reversed(subtree):
                    if task.is_terminal or task.cancel_requested_at is not None:
                        continue
                    event, _ = self._append_event_conn(
                        connection,
                        RuntimeEventDraft(
                            run_id=target.run_id,
                            event_type=EventType.TASK_CANCEL_REQUESTED,
                            payload=CancelRequestedPayload(
                                reason=reason, requested_by=requested_by
                            ),
                            subject_kind=SubjectKind.TASK,
                            subject_id=task.task_id,
                            parent_subject_id=task.parent_task_id,
                            visibility=Visibility.USER,
                            idempotency_key=(
                                f"task-cancel-request:{task.task_id}"
                            ),
                        ),
                    )
                    events.append(event)
                    (
                        cancel_target,
                        waiting_on_children,
                    ) = self._cancel_target_for_task_conn(connection, task)
                    events.extend(
                        self._prepare_attempt_cancellation_conn(
                            connection,
                            task=task,
                            target=cancel_target,
                            now=now,
                            cancel_deadline=cancel_deadline,
                            reason=reason,
                        )
                    )
                    events.append(
                        self._transition_task_for_cancel_conn(
                            connection,
                            task,
                            cancel_target,
                            now=now,
                            cause=(
                                "subtree cancellation waiting for active "
                                "descendants"
                                if waiting_on_children
                                else (
                                    "subtree cancelled before execution"
                                    if cancel_target is TaskState.CANCELLED
                                    else "subtree cancellation requested"
                                )
                            ),
                            waiting_on_children=waiting_on_children,
                        )
                    )

                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved_subtree = self._task_subtree_conn(
                connection, target.run_id, target.task_id
            )

        self._notify(events)
        return saved_subtree, True

    def request_cancellation(
        self,
        run_id: str,
        *,
        requested_by: str | None,
        reason: str,
        cancel_grace_seconds: float = _DEFAULT_CANCEL_GRACE_SECONDS,
        now: str | datetime | None = None,
    ) -> tuple[RunRecord, list[TaskRecord]]:
        """Atomically record a cancellation request and update all projections."""

        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeValidationError("cancellation reason is required")
        if requested_by is not None and (
            not isinstance(requested_by, str) or not requested_by.strip()
        ):
            raise RuntimeValidationError(
                "requested_by must be a non-empty string when supplied"
            )
        grace = _nonnegative_seconds(
            cancel_grace_seconds, label="cancel_grace_seconds"
        )
        events: list[RuntimeEvent] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = self._command_now(now)
                now_iso = _utc_iso(now_dt)
                cancel_deadline = _future_iso(now_dt, grace)
                run = self._get_run_conn(connection, run_id)
                if run.is_terminal or run.cancel_requested_at is not None:
                    connection.commit()
                    return run, self._list_tasks_conn(connection, run_id)

                now = now_iso
                event, _ = self._append_event_conn(
                    connection,
                    RuntimeEventDraft(
                        run_id=run_id,
                        event_type=EventType.RUN_CANCEL_REQUESTED,
                        payload=CancelRequestedPayload(
                            reason=reason, requested_by=requested_by
                        ),
                        subject_kind=SubjectKind.RUN,
                        subject_id=run_id,
                        visibility=Visibility.USER,
                        idempotency_key=f"run-cancel-request:{run_id}",
                    ),
                )
                events.append(event)

                tasks = self._list_tasks_conn(connection, run_id)
                for task in reversed(tasks):
                    if task.is_terminal or task.cancel_requested_at is not None:
                        continue
                    event, _ = self._append_event_conn(
                        connection,
                        RuntimeEventDraft(
                            run_id=run_id,
                            event_type=EventType.TASK_CANCEL_REQUESTED,
                            payload=CancelRequestedPayload(
                                reason=reason, requested_by=requested_by
                            ),
                            subject_kind=SubjectKind.TASK,
                            subject_id=task.task_id,
                            parent_subject_id=task.parent_task_id,
                            visibility=Visibility.USER,
                            idempotency_key=(
                                f"task-cancel-request:{task.task_id}"
                            ),
                        ),
                    )
                    events.append(event)
                    (
                        cancel_target,
                        waiting_on_children,
                    ) = self._cancel_target_for_task_conn(connection, task)
                    events.extend(
                        self._prepare_attempt_cancellation_conn(
                            connection,
                            task=task,
                            target=cancel_target,
                            now=now,
                            cancel_deadline=cancel_deadline,
                            reason=reason,
                        )
                    )
                    events.append(
                        self._transition_task_for_cancel_conn(
                            connection,
                            task,
                            cancel_target,
                            now=now,
                            cause=(
                                "cancellation waiting for active descendants"
                                if waiting_on_children
                                else (
                                    "cancelled before execution"
                                    if cancel_target is TaskState.CANCELLED
                                    else "cancel requested"
                                )
                            ),
                            waiting_on_children=waiting_on_children,
                        )
                    )

                refreshed_tasks = self._list_tasks_conn(connection, run_id)
                if run.state in {RunState.CREATED, RunState.QUEUED}:
                    run_target = RunState.CANCELLED
                    run_cause = "cancelled before execution"
                elif run.state in {
                    RunState.RUNNING,
                    RunState.WAITING,
                    RunState.FINALIZING,
                }:
                    run_target = RunState.CANCELLING
                    run_cause = "cancel requested"
                else:
                    run_target = None
                    run_cause = ""

                if run_target is not None:
                    events.append(
                        self._transition_run_for_cancel_conn(
                            connection,
                            run,
                            run_target,
                            refreshed_tasks,
                            now=now,
                            cause=run_cause,
                        )
                    )
                else:
                    connection.execute(
                        """
                        UPDATE runtime_runs
                        SET cancel_requested_at=?, updated_at=?
                        WHERE run_id=?
                        """,
                        (now, now, run_id),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved_run = self._get_run_conn(connection, run_id)
            saved_tasks = self._list_tasks_conn(connection, run_id)

        self._notify(events)
        return saved_run, saved_tasks

    def _prepare_attempt_cancellation_conn(
        self,
        connection: sqlite3.Connection,
        *,
        task: TaskRecord,
        target: TaskState,
        now: str,
        cancel_deadline: str,
        reason: str,
    ) -> list[RuntimeEvent]:
        execution = self._task_execution_row_conn(
            connection, task.task_id
        )
        attempt_id = execution["active_attempt_id"]
        if attempt_id is None:
            return []
        attempt = self._get_attempt_conn(connection, attempt_id)
        if attempt.state is not AttemptState.ACTIVE:
            raise AttemptConflict(
                "Task references a non-active Attempt during cancellation"
            )
        lease_id = execution["active_lease_id"]
        if lease_id is not None:
            lease = self._get_lease_conn(connection, lease_id)
            if (
                lease.attempt_id != attempt_id
                or lease.fencing_token != int(execution["fencing_token"])
                or lease.state
                not in {LeaseState.ACTIVE, LeaseState.REVOKING}
            ):
                raise LeaseLost(
                    "Task cancellation found a non-authoritative lease"
                )
            if lease.state is LeaseState.ACTIVE:
                cursor = connection.execute(
                    """
                    UPDATE runtime_task_leases
                    SET state='revoking',
                        state_version=state_version+1
                    WHERE lease_id=? AND state='active'
                      AND state_version=?
                    """,
                    (lease_id, lease.state_version),
                )
                if cursor.rowcount != 1:
                    raise LeaseLost(
                        "lease changed while cancellation was requested"
                    )
            connection.execute(
                """
                UPDATE runtime_tasks
                SET cancel_deadline_at=?
                WHERE task_id=? AND active_attempt_id=?
                  AND active_lease_id=? AND fencing_token=?
                """,
                (
                    cancel_deadline,
                    task.task_id,
                    attempt_id,
                    lease_id,
                    lease.fencing_token,
                ),
            )
            return []

        cursor = connection.execute(
            """
            UPDATE runtime_task_attempts
            SET state='cancelled',
                state_version=state_version+1,
                retryable=0,
                failure_code='CANCELLED',
                updated_at=?,
                ended_at=?
            WHERE attempt_id=? AND state='active'
              AND state_version=?
            """,
            (now, now, attempt_id, attempt.state_version),
        )
        if cursor.rowcount != 1:
            raise AttemptConflict(
                "Attempt changed while cancellation was requested"
            )
        connection.execute(
            """
            UPDATE runtime_tasks
            SET active_attempt_id=NULL,
                active_lease_id=NULL,
                next_retry_at=NULL,
                cancel_deadline_at=?
            WHERE task_id=? AND active_attempt_id=?
            """,
            (
                None if target is TaskState.CANCELLED else now,
                task.task_id,
                attempt_id,
            ),
        )
        event, is_new = self._append_event_conn(
            connection,
            RuntimeEventDraft(
                run_id=task.run_id,
                event_type=EventType.ATTEMPT_CANCELLED,
                payload=AttemptCancelledPayload(
                    task_id=task.task_id,
                    attempt_no=attempt.attempt_no,
                    reason=reason,
                ),
                subject_kind=SubjectKind.ATTEMPT,
                subject_id=attempt_id,
                parent_subject_id=task.task_id,
                attempt_id=attempt_id,
                visibility=Visibility.AUDIT,
                occurred_at=now,
                idempotency_key=f"attempt-cancelled:{attempt_id}",
            ),
        )
        return [event] if is_new else []

    def _cancel_attempt_conn(
        self,
        connection: sqlite3.Connection,
        *,
        task: TaskRecord,
        attempt: TaskAttemptRecord,
        lease: TaskLeaseRecord,
        now: str,
        reason: str,
        lease_state: LeaseState,
    ) -> list[RuntimeEvent]:
        if lease_state not in {LeaseState.RELEASED, LeaseState.REVOKED}:
            raise RuntimeValidationError(
                "cancelled Attempt lease must be released or revoked"
            )
        if task.state is not TaskState.CANCELLING:
            raise AttemptConflict(
                "Attempt cancellation requires a CANCELLING Task"
            )
        cancellation_reason = self._task_cancel_reason_conn(
            connection, task.run_id, task.task_id
        ) or reason
        execution = self._task_execution_row_conn(
            connection, task.task_id
        )
        if (
            execution["active_attempt_id"] != attempt.attempt_id
            or execution["active_lease_id"] != lease.lease_id
            or int(execution["fencing_token"]) != lease.fencing_token
            or lease.attempt_id != attempt.attempt_id
            or attempt.state is not AttemptState.ACTIVE
            or lease.state
            not in {LeaseState.ACTIVE, LeaseState.REVOKING}
        ):
            raise LeaseLost(
                "cancellation no longer owns the authoritative fence"
            )
        cursor = connection.execute(
            """
            UPDATE runtime_task_leases
            SET state=?,
                state_version=state_version+1,
                released_at=?,
                release_reason=?
            WHERE lease_id=?
              AND state IN ('active', 'revoking')
              AND state_version=?
            """,
            (
                lease_state.value,
                now,
                reason,
                lease.lease_id,
                lease.state_version,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseLost("lease changed during Attempt cancellation")
        cursor = connection.execute(
            """
            UPDATE runtime_task_attempts
            SET state='cancelled',
                state_version=state_version+1,
                retryable=0,
                failure_code='CANCELLED',
                result_json=NULL,
                updated_at=?,
                ended_at=?
            WHERE attempt_id=? AND state='active'
              AND state_version=?
            """,
            (
                now,
                now,
                attempt.attempt_id,
                attempt.state_version,
            ),
        )
        if cursor.rowcount != 1:
            raise AttemptConflict(
                "Attempt changed during cancellation acknowledgement"
            )
        cursor = connection.execute(
            """
            UPDATE runtime_tasks
            SET active_attempt_id=NULL,
                active_lease_id=NULL,
                next_retry_at=NULL,
                cancel_deadline_at=NULL,
                updated_at=?
            WHERE task_id=? AND state='cancelling'
              AND active_attempt_id=?
              AND active_lease_id=?
              AND fencing_token=?
            """,
            (
                now,
                task.task_id,
                attempt.attempt_id,
                lease.lease_id,
                lease.fencing_token,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseLost(
                "Task fence changed during Attempt cancellation"
            )
        events: list[RuntimeEvent] = []
        for draft in (
            RuntimeEventDraft(
                run_id=task.run_id,
                event_type=EventType.ATTEMPT_LEASE_RELEASED,
                payload=AttemptLeaseReleasedPayload(
                    task_id=task.task_id,
                    lease_id=lease.lease_id,
                    lease_epoch=lease.lease_epoch,
                    worker_id=lease.worker_id,
                    reason=reason,
                ),
                subject_kind=SubjectKind.ATTEMPT,
                subject_id=attempt.attempt_id,
                parent_subject_id=task.task_id,
                attempt_id=attempt.attempt_id,
                visibility=Visibility.AUDIT,
                occurred_at=now,
                idempotency_key=f"lease-released:{lease.lease_id}",
            ),
            RuntimeEventDraft(
                run_id=task.run_id,
                event_type=EventType.ATTEMPT_CANCELLED,
                payload=AttemptCancelledPayload(
                    task_id=task.task_id,
                    attempt_no=attempt.attempt_no,
                    reason=cancellation_reason,
                ),
                subject_kind=SubjectKind.ATTEMPT,
                subject_id=attempt.attempt_id,
                parent_subject_id=task.task_id,
                attempt_id=attempt.attempt_id,
                visibility=Visibility.AUDIT,
                occurred_at=now,
                idempotency_key=(
                    f"attempt-cancelled:{attempt.attempt_id}"
                ),
            ),
        ):
            event, is_new = self._append_event_conn(connection, draft)
            if is_new:
                events.append(event)
        children = self._children_conn(
            connection, task.run_id, task.task_id
        )
        if not any(not child.is_terminal for child in children):
            events.append(
                self._transition_task_for_cancel_conn(
                    connection,
                    task,
                    TaskState.CANCELLED,
                    now=now,
                    cause=reason,
                )
            )
        return events

    def _task_cancel_reason_conn(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        task_id: str,
    ) -> str | None:
        row = connection.execute(
            """
            SELECT payload_json
            FROM runtime_events
            WHERE run_id=?
              AND event_type=?
              AND subject_kind=?
              AND subject_id=?
            ORDER BY seq ASC
            LIMIT 1
            """,
            (
                run_id,
                EventType.TASK_CANCEL_REQUESTED.value,
                SubjectKind.TASK.value,
                task_id,
            ),
        ).fetchone()
        if row is None:
            return None
        payload = _json_loads(row["payload_json"], {})
        reason = payload.get("reason") if isinstance(payload, Mapping) else None
        return (
            reason.strip()
            if isinstance(reason, str) and reason.strip()
            else None
        )

    def _finalize_cancelled_ancestors_conn(
        self,
        connection: sqlite3.Connection,
        *,
        task: TaskRecord,
        now: str,
    ) -> list[RuntimeEvent]:
        events: list[RuntimeEvent] = []
        parent_task_id = task.parent_task_id
        while parent_task_id is not None:
            parent = self._get_task_conn(connection, parent_task_id)
            row = connection.execute(
                """
                SELECT cancel_waiting_on_children, active_attempt_id,
                       active_lease_id
                FROM runtime_tasks WHERE task_id=?
                """,
                (parent_task_id,),
            ).fetchone()
            assert row is not None
            if (
                parent.state is not TaskState.CANCELLING
                or row["active_attempt_id"] is not None
                or row["active_lease_id"] is not None
            ):
                break
            children = self._children_conn(
                connection, parent.run_id, parent.task_id
            )
            if any(not child.is_terminal for child in children):
                break
            events.append(
                self._transition_task_for_cancel_conn(
                    connection,
                    parent,
                    TaskState.CANCELLED,
                    now=now,
                    cause="all cancelled descendants stopped",
                )
            )
            parent_task_id = parent.parent_task_id
        return events

    def _finalize_cancelled_run_conn(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        now: str,
    ) -> list[RuntimeEvent]:
        run = self._get_run_conn(connection, run_id)
        if run.state is not RunState.CANCELLING:
            return []
        tasks = self._list_tasks_conn(connection, run_id)
        if any(not task.is_terminal for task in tasks):
            return []
        failed = [task for task in tasks if task.state is TaskState.FAILED]
        if failed:
            target = RunState.FAILED
            error: Mapping[str, Any] | None = {
                "code": "CANCEL_FAILED",
                "message": "One or more Tasks failed while cancelling",
                "category": "cancellation",
                "retryable": False,
                "failed_task_ids": [task.task_id for task in failed],
            }
            cause = "cancellation did not cleanly stop all Tasks"
        else:
            target = RunState.CANCELLED
            error = None
            cause = "all Tasks stopped"
        return [
            self._transition_run_for_cancel_conn(
                connection,
                run,
                target,
                tasks,
                now=now,
                cause=cause,
                error=error,
            )
        ]

    def _transition_task_for_cancel_conn(
        self,
        connection: sqlite3.Connection,
        current: TaskRecord,
        target: TaskState,
        *,
        now: str,
        cause: str,
        waiting_on_children: bool = False,
    ) -> RuntimeEvent:
        validate_task_transition(current.state, target)
        children = self._children_conn(
            connection, current.run_id, current.task_id
        )
        self._validate_task_target(current, target, children, None, None)
        ended_at = now if target is TaskState.CANCELLED else current.ended_at
        cursor = connection.execute(
            """
            UPDATE runtime_tasks
            SET state=?, state_version=state_version+1, wait_reason=NULL,
                updated_at=?, ended_at=?, cancel_requested_at=?,
                cancel_waiting_on_children=?,
                cancel_deadline_at=?
            WHERE task_id=? AND state_version=?
            """,
            (
                target.value,
                now,
                ended_at,
                now,
                int(waiting_on_children),
                None if target is TaskState.CANCELLED else (
                    self._task_execution_row_conn(
                        connection, current.task_id
                    )["cancel_deadline_at"]
                ),
                current.task_id,
                current.state_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyConflict("Task changed during cancellation")
        event, _ = self._append_event_conn(
            connection,
            RuntimeEventDraft(
                run_id=current.run_id,
                event_type=EventType.TASK_STATUS_CHANGED,
                payload=StateChangedPayload(
                    from_state=current.state.value,
                    to_state=target.value,
                    cause=cause,
                ),
                subject_kind=SubjectKind.TASK,
                subject_id=current.task_id,
                parent_subject_id=current.parent_task_id,
                visibility=Visibility.AUDIT,
                idempotency_key=(
                    f"task-status:{current.task_id}:"
                    f"{current.state_version + 1}:{target.value}"
                ),
            ),
        )
        return event

    def _cancel_target_for_task_conn(
        self,
        connection: sqlite3.Connection,
        task: TaskRecord,
    ) -> tuple[TaskState, bool]:
        """Choose a truthful cancellation state after descendants are updated."""

        if task.state in {
            TaskState.CREATED,
            TaskState.BLOCKED,
            TaskState.READY,
        }:
            children = self._children_conn(
                connection, task.run_id, task.task_id
            )
            waiting_on_children = any(
                not child.is_terminal for child in children
            )
            return (
                TaskState.CANCELLING
                if waiting_on_children
                else TaskState.CANCELLED,
                waiting_on_children,
            )
        if task.state in {TaskState.RUNNING, TaskState.WAITING}:
            return TaskState.CANCELLING, False
        raise RuntimeValidationError(
            f"Task cannot accept cancellation from {task.state.value}"
        )

    def _transition_run_for_cancel_conn(
        self,
        connection: sqlite3.Connection,
        current: RunRecord,
        target: RunState,
        tasks: list[TaskRecord],
        *,
        now: str,
        cause: str,
        error: Mapping[str, Any] | None = None,
    ) -> RuntimeEvent:
        validate_run_transition(current.state, target)
        self._validate_run_target(
            current, target, tasks, error, current.metadata
        )
        ended_at = now if target is RunState.CANCELLED else current.ended_at
        cursor = connection.execute(
            """
            UPDATE runtime_runs
            SET state=?, state_version=state_version+1, updated_at=?,
                ended_at=?, cancel_requested_at=?, error_json=?
            WHERE run_id=? AND state_version=?
            """,
            (
                target.value,
                now,
                ended_at,
                now,
                _json_dumps(error) if error is not None else None,
                current.run_id,
                current.state_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyConflict("Run changed during cancellation")
        event, _ = self._append_event_conn(
            connection,
            RuntimeEventDraft(
                run_id=current.run_id,
                event_type=EventType.RUN_STATUS_CHANGED,
                payload=StateChangedPayload(
                    from_state=current.state.value,
                    to_state=target.value,
                    cause=cause,
                ),
                subject_kind=SubjectKind.RUN,
                subject_id=current.run_id,
                visibility=Visibility.AUDIT,
                idempotency_key=(
                    f"run-status:{current.run_id}:"
                    f"{current.state_version + 1}:{target.value}"
                ),
            ),
        )
        return event

    def complete_task_cancellation(self, task_id: str) -> TaskRecord:
        events: list[RuntimeEvent] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._get_task_conn(connection, task_id)
                if current.state is TaskState.CANCELLED:
                    connection.commit()
                    return current
                if current.state is not TaskState.CANCELLING:
                    raise RuntimeValidationError(
                        f"Task is not cancelling: {current.state.value}"
                    )
                execution = self._task_execution_row_conn(
                    connection, task_id
                )
                if (
                    execution["active_attempt_id"] is not None
                    or execution["active_lease_id"] is not None
                ):
                    raise AttemptConflict(
                        "active Attempt cancellation requires worker "
                        "acknowledgement or the cancellation watchdog"
                    )
                now = utc_now_iso()
                events.append(
                    self._transition_task_for_cancel_conn(
                        connection,
                        current,
                        TaskState.CANCELLED,
                        now=now,
                        cause="worker exited after cancellation",
                    )
                )
                parent_task_id = current.parent_task_id
                while parent_task_id is not None:
                    parent = self._get_task_conn(connection, parent_task_id)
                    waiting_row = connection.execute(
                        """
                        SELECT cancel_waiting_on_children
                        FROM runtime_tasks
                        WHERE task_id=?
                        """,
                        (parent.task_id,),
                    ).fetchone()
                    if (
                        parent.state is not TaskState.CANCELLING
                        or waiting_row is None
                        or not bool(waiting_row["cancel_waiting_on_children"])
                    ):
                        break
                    children = self._children_conn(
                        connection, parent.run_id, parent.task_id
                    )
                    if any(not child.is_terminal for child in children):
                        break
                    events.append(
                        self._transition_task_for_cancel_conn(
                            connection,
                            parent,
                            TaskState.CANCELLED,
                            now=now,
                            cause="all cancelled descendants stopped",
                        )
                    )
                    parent_task_id = parent.parent_task_id
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_task_conn(connection, task_id)
        self._notify(events)
        return saved

    def complete_run_cancellation(self, run_id: str) -> RunRecord:
        event: RuntimeEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._get_run_conn(connection, run_id)
                if current.is_terminal:
                    connection.commit()
                    return current
                if current.state is not RunState.CANCELLING:
                    raise RuntimeValidationError(
                        f"Run is not cancelling: {current.state.value}"
                    )
                tasks = self._list_tasks_conn(connection, run_id)
                if any(not task.is_terminal for task in tasks):
                    connection.commit()
                    return current
                failed = [
                    task for task in tasks if task.state is TaskState.FAILED
                ]
                if failed:
                    target = RunState.FAILED
                    error: Mapping[str, Any] | None = {
                        "code": "CANCEL_FAILED",
                        "message": "One or more Tasks failed while cancelling",
                        "category": "cancellation",
                        "retryable": False,
                        "failed_task_ids": [
                            task.task_id for task in failed
                        ],
                    }
                    cause = "cancellation did not cleanly stop all Tasks"
                else:
                    target = RunState.CANCELLED
                    error = None
                    cause = "all Tasks stopped"
                event = self._transition_run_for_cancel_conn(
                    connection,
                    current,
                    target,
                    tasks,
                    now=utc_now_iso(),
                    cause=cause,
                    error=error,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_run_conn(connection, run_id)
        if event is not None:
            self._notify([event])
        return saved

    def renew_attempt_lease(
        self,
        claim: TaskClaim,
        lease_ttl_seconds: float,
        *,
        now: str | datetime | None = None,
    ) -> TaskLeaseRecord:
        """Heartbeat the current lease without producing an event."""

        ttl = _positive_seconds(
            lease_ttl_seconds, label="lease_ttl_seconds"
        )
        (
            task_id,
            attempt_id,
            lease_id,
            worker_id,
            fencing_token,
        ) = self._claim_identity(claim)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = self._command_now(now)
                now_iso = _utc_iso(now_dt)
                expires_at = _future_iso(now_dt, ttl)
                _, _, lease = self._validate_authority_conn(
                    connection,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    lease_id=lease_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    now=now_iso,
                )
                cursor = connection.execute(
                    """
                    UPDATE runtime_task_leases
                    SET heartbeat_at=?, expires_at=?,
                        state_version=state_version+1
                    WHERE lease_id=? AND state='active'
                      AND state_version=? AND fencing_token=?
                    """,
                    (
                        now_iso,
                        expires_at,
                        lease_id,
                        lease.state_version,
                        fencing_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseLost("lease changed during heartbeat")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return self._get_lease_conn(connection, lease_id)

    def yield_attempt(
        self,
        claim: TaskClaim,
        wait_reason: WaitReason,
        *,
        cause: str = "worker yielded Attempt",
        now: str | datetime | None = None,
    ) -> TaskRecord:
        """Release authority but keep the logical Attempt active for resume."""

        if not isinstance(wait_reason, WaitReason):
            try:
                wait_reason = WaitReason(wait_reason)
            except (TypeError, ValueError) as exc:
                raise RuntimeValidationError(
                    "yield requires a valid wait_reason"
                ) from exc
        if wait_reason is WaitReason.RETRY_BACKOFF:
            raise RuntimeValidationError(
                "retry_backoff is reserved for failed Attempt scheduling"
            )
        if not isinstance(cause, str) or not cause.strip():
            raise RuntimeValidationError("yield cause is required")
        (
            task_id,
            attempt_id,
            lease_id,
            worker_id,
            fencing_token,
        ) = self._claim_identity(claim)
        release_reason = f"yield:{wait_reason.value}"
        events: list[RuntimeEvent] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_iso = _utc_iso(self._command_now(now))
                try:
                    task, attempt, lease = self._validate_authority_conn(
                        connection,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        lease_id=lease_id,
                        worker_id=worker_id,
                        fencing_token=fencing_token,
                        now=now_iso,
                    )
                except LeaseLost:
                    task = self._get_task_conn(connection, task_id)
                    attempt = self._get_attempt_conn(
                        connection, attempt_id
                    )
                    lease = self._get_lease_conn(connection, lease_id)
                    execution = self._task_execution_row_conn(
                        connection, task_id
                    )
                    if (
                        lease.state is LeaseState.RELEASED
                        and lease.release_reason == release_reason
                        and attempt.state is AttemptState.ACTIVE
                        and execution["active_attempt_id"] == attempt_id
                        and execution["active_lease_id"] is None
                        and task.state is TaskState.WAITING
                        and task.wait_reason is wait_reason
                    ):
                        connection.commit()
                        return task
                    raise
                if task.state is not TaskState.RUNNING:
                    raise AttemptConflict(
                        f"Task is not running: {task.state.value}"
                    )
                run = self._get_run_conn(connection, task.run_id)
                self._validate_task_run_target(
                    run, task, TaskState.WAITING
                )
                cursor = connection.execute(
                    """
                    UPDATE runtime_task_leases
                    SET state='released',
                        state_version=state_version+1,
                        released_at=?, release_reason=?
                    WHERE lease_id=? AND state='active'
                      AND state_version=? AND fencing_token=?
                    """,
                    (
                        now_iso,
                        release_reason,
                        lease_id,
                        lease.state_version,
                        fencing_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseLost("lease changed during yield")
                cursor = connection.execute(
                    """
                    UPDATE runtime_tasks
                    SET state='waiting',
                        state_version=state_version+1,
                        wait_reason=?,
                        updated_at=?,
                        active_lease_id=NULL
                    WHERE task_id=? AND state='running'
                      AND state_version=?
                      AND active_attempt_id=?
                      AND active_lease_id=?
                      AND fencing_token=?
                    """,
                    (
                        wait_reason.value,
                        now_iso,
                        task_id,
                        task.state_version,
                        attempt_id,
                        lease_id,
                        fencing_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseLost("Task fence changed during yield")
                connection.execute(
                    """
                    UPDATE runtime_task_attempts
                    SET updated_at=?
                    WHERE attempt_id=? AND state='active'
                    """,
                    (now_iso, attempt_id),
                )
                event, is_new = self._append_event_conn(
                    connection,
                    RuntimeEventDraft(
                        run_id=task.run_id,
                        event_type=EventType.ATTEMPT_LEASE_RELEASED,
                        payload=AttemptLeaseReleasedPayload(
                            task_id=task_id,
                            lease_id=lease_id,
                            lease_epoch=lease.lease_epoch,
                            worker_id=worker_id,
                            reason=release_reason,
                        ),
                        subject_kind=SubjectKind.ATTEMPT,
                        subject_id=attempt_id,
                        parent_subject_id=task_id,
                        attempt_id=attempt_id,
                        visibility=Visibility.AUDIT,
                        occurred_at=now_iso,
                        idempotency_key=f"lease-released:{lease_id}",
                    ),
                )
                if is_new:
                    events.append(event)
                event, is_new = self._append_event_conn(
                    connection,
                    RuntimeEventDraft(
                        run_id=task.run_id,
                        event_type=EventType.TASK_STATUS_CHANGED,
                        payload=StateChangedPayload(
                            from_state=TaskState.RUNNING.value,
                            to_state=TaskState.WAITING.value,
                            cause=cause,
                            wait_reason=wait_reason.value,
                        ),
                        subject_kind=SubjectKind.TASK,
                        subject_id=task_id,
                        parent_subject_id=task.parent_task_id,
                        attempt_id=attempt_id,
                        visibility=Visibility.AUDIT,
                        occurred_at=now_iso,
                        idempotency_key=(
                            f"task-status:{task_id}:"
                            f"{task.state_version + 1}:waiting"
                        ),
                    ),
                )
                if is_new:
                    events.append(event)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_task_conn(connection, task_id)
        self._notify(events)
        return saved

    def complete_attempt(
        self,
        claim: TaskClaim,
        result: Mapping[str, Any],
        *,
        cause: str = "Attempt completed successfully",
        now: str | datetime | None = None,
    ) -> TaskRecord:
        if not isinstance(result, Mapping):
            raise RuntimeValidationError(
                "successful Attempt requires a result object"
            )
        if not isinstance(cause, str) or not cause.strip():
            raise RuntimeValidationError("completion cause is required")
        result_json = _json_dumps(result)
        (
            task_id,
            attempt_id,
            lease_id,
            worker_id,
            fencing_token,
        ) = self._claim_identity(claim)
        events: list[RuntimeEvent] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_iso = _utc_iso(self._command_now(now))
                persisted_attempt = self._get_attempt_conn(
                    connection, attempt_id
                )
                if persisted_attempt.state is AttemptState.SUCCEEDED:
                    persisted_lease = self._get_lease_conn(
                        connection, lease_id
                    )
                    if (
                        persisted_lease.attempt_id != attempt_id
                        or persisted_lease.worker_id != worker_id
                        or persisted_lease.fencing_token
                        != fencing_token
                        or persisted_lease.state
                        is not LeaseState.RELEASED
                        or persisted_lease.release_reason != "completed"
                    ):
                        raise LeaseLost(
                            "completion does not match the committed "
                            "lease/fence"
                        )
                    if (
                        _json_dumps(persisted_attempt.result)
                        != result_json
                    ):
                        raise IdempotencyConflict(
                            "Attempt completion result differs from the "
                            "committed result"
                        )
                    task = self._get_task_conn(connection, task_id)
                    connection.commit()
                    return task
                task, attempt, lease = self._validate_authority_conn(
                    connection,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    lease_id=lease_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    now=now_iso,
                )
                if task.state is not TaskState.RUNNING:
                    raise AttemptConflict(
                        f"Task is not running: {task.state.value}"
                    )
                run = self._get_run_conn(connection, task.run_id)
                self._validate_task_run_target(
                    run, task, TaskState.SUCCEEDED
                )
                children = self._children_conn(
                    connection, task.run_id, task_id
                )
                self._validate_task_target(
                    task, TaskState.SUCCEEDED, children, result, None
                )
                cursor = connection.execute(
                    """
                    UPDATE runtime_task_leases
                    SET state='released',
                        state_version=state_version+1,
                        released_at=?, release_reason='completed'
                    WHERE lease_id=? AND state='active'
                      AND state_version=? AND fencing_token=?
                    """,
                    (
                        now_iso,
                        lease_id,
                        lease.state_version,
                        fencing_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseLost("lease changed during completion")
                cursor = connection.execute(
                    """
                    UPDATE runtime_task_attempts
                    SET state='succeeded',
                        state_version=state_version+1,
                        result_json=?,
                        error_json=NULL,
                        retryable=NULL,
                        failure_code=NULL,
                        updated_at=?,
                        ended_at=?
                    WHERE attempt_id=? AND state='active'
                      AND state_version=?
                    """,
                    (
                        result_json,
                        now_iso,
                        now_iso,
                        attempt_id,
                        attempt.state_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AttemptConflict(
                        "Attempt changed during completion"
                    )
                cursor = connection.execute(
                    """
                    UPDATE runtime_tasks
                    SET state='succeeded',
                        state_version=state_version+1,
                        wait_reason=NULL,
                        result_json=?,
                        error_json=NULL,
                        updated_at=?,
                        ended_at=?,
                        active_attempt_id=NULL,
                        active_lease_id=NULL,
                        next_retry_at=NULL,
                        cancel_deadline_at=NULL
                    WHERE task_id=? AND state='running'
                      AND state_version=?
                      AND active_attempt_id=?
                      AND active_lease_id=?
                      AND fencing_token=?
                    """,
                    (
                        result_json,
                        now_iso,
                        now_iso,
                        task_id,
                        task.state_version,
                        attempt_id,
                        lease_id,
                        fencing_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseLost(
                        "Task fence changed during completion"
                    )
                for draft in (
                    RuntimeEventDraft(
                        run_id=task.run_id,
                        event_type=EventType.ATTEMPT_LEASE_RELEASED,
                        payload=AttemptLeaseReleasedPayload(
                            task_id=task_id,
                            lease_id=lease_id,
                            lease_epoch=lease.lease_epoch,
                            worker_id=worker_id,
                            reason="completed",
                        ),
                        subject_kind=SubjectKind.ATTEMPT,
                        subject_id=attempt_id,
                        parent_subject_id=task_id,
                        attempt_id=attempt_id,
                        visibility=Visibility.AUDIT,
                        occurred_at=now_iso,
                        idempotency_key=f"lease-released:{lease_id}",
                    ),
                    RuntimeEventDraft(
                        run_id=task.run_id,
                        event_type=EventType.ATTEMPT_SUCCEEDED,
                        payload=AttemptSucceededPayload(
                            task_id=task_id,
                            attempt_no=attempt.attempt_no,
                        ),
                        subject_kind=SubjectKind.ATTEMPT,
                        subject_id=attempt_id,
                        parent_subject_id=task_id,
                        attempt_id=attempt_id,
                        visibility=Visibility.AUDIT,
                        occurred_at=now_iso,
                        idempotency_key=(
                            f"attempt-succeeded:{attempt_id}"
                        ),
                    ),
                    RuntimeEventDraft(
                        run_id=task.run_id,
                        event_type=EventType.TASK_STATUS_CHANGED,
                        payload=StateChangedPayload(
                            from_state=TaskState.RUNNING.value,
                            to_state=TaskState.SUCCEEDED.value,
                            cause=cause,
                        ),
                        subject_kind=SubjectKind.TASK,
                        subject_id=task_id,
                        parent_subject_id=task.parent_task_id,
                        attempt_id=attempt_id,
                        visibility=Visibility.AUDIT,
                        occurred_at=now_iso,
                        idempotency_key=(
                            f"task-status:{task_id}:"
                            f"{task.state_version + 1}:succeeded"
                        ),
                    ),
                ):
                    event, is_new = self._append_event_conn(
                        connection, draft
                    )
                    if is_new:
                        events.append(event)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_task_conn(connection, task_id)
        self._notify(events)
        return saved

    def fail_attempt(
        self,
        claim: TaskClaim,
        error: Mapping[str, Any],
        *,
        retryable: bool | None = None,
        cause: str = "Attempt failed",
        now: str | datetime | None = None,
    ) -> TaskRecord:
        if not isinstance(error, Mapping) or not error:
            raise RuntimeValidationError(
                "failed Attempt requires a non-empty error object"
            )
        if retryable is not None and not isinstance(retryable, bool):
            raise RuntimeValidationError("retryable must be bool or None")
        if not isinstance(cause, str) or not cause.strip():
            raise RuntimeValidationError("failure cause is required")
        error_json = _json_dumps(error)
        (
            task_id,
            attempt_id,
            lease_id,
            worker_id,
            fencing_token,
        ) = self._claim_identity(claim)
        events: list[RuntimeEvent] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = self._command_now(now)
                now_iso = _utc_iso(now_dt)
                persisted_attempt = self._get_attempt_conn(
                    connection, attempt_id
                )
                if persisted_attempt.state is AttemptState.FAILED:
                    persisted_lease = self._get_lease_conn(
                        connection, lease_id
                    )
                    if (
                        persisted_lease.attempt_id != attempt_id
                        or persisted_lease.worker_id != worker_id
                        or persisted_lease.fencing_token
                        != fencing_token
                        or persisted_lease.state
                        is not LeaseState.RELEASED
                        or persisted_lease.release_reason != "failed"
                    ):
                        raise LeaseLost(
                            "failure does not match the committed "
                            "lease/fence"
                        )
                    if (
                        _json_dumps(persisted_attempt.error)
                        != error_json
                    ):
                        raise IdempotencyConflict(
                            "Attempt failure differs from the committed error"
                        )
                    persisted_code_value = error.get("code")
                    persisted_code = (
                        persisted_code_value.strip()
                        if isinstance(persisted_code_value, str)
                        and persisted_code_value.strip()
                        else None
                    )
                    requested_again = (
                        bool(error.get("retryable", False))
                        if retryable is None
                        else retryable
                    )
                    effective_again = requested_again
                    if (
                        persisted_attempt.retry_policy
                        .retryable_error_codes
                    ):
                        effective_again = (
                            requested_again
                            and persisted_code
                            in persisted_attempt.retry_policy
                            .retryable_error_codes
                        )
                    if persisted_attempt.retryable != effective_again:
                        raise IdempotencyConflict(
                            "Attempt failure retryability differs from "
                            "the committed command"
                        )
                    task = self._get_task_conn(connection, task_id)
                    connection.commit()
                    return task
                task, attempt, lease = self._validate_authority_conn(
                    connection,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    lease_id=lease_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    now=now_iso,
                )
                if task.state is not TaskState.RUNNING:
                    raise AttemptConflict(
                        f"Task is not running: {task.state.value}"
                    )
                error_code_value = error.get("code")
                failure_code = (
                    error_code_value.strip()
                    if isinstance(error_code_value, str)
                    and error_code_value.strip()
                    else None
                )
                requested_retryable = (
                    bool(error.get("retryable", False))
                    if retryable is None
                    else retryable
                )
                effective_retryable = requested_retryable
                if attempt.retry_policy.retryable_error_codes:
                    effective_retryable = (
                        requested_retryable
                        and failure_code
                        in attempt.retry_policy.retryable_error_codes
                    )
                can_retry = (
                    effective_retryable
                    and attempt.attempt_no
                    < attempt.retry_policy.max_attempts
                    and task.cancel_requested_at is None
                )
                run = self._get_run_conn(connection, task.run_id)
                if run.cancel_requested_at is not None:
                    can_retry = False
                children = self._children_conn(
                    connection, task.run_id, task_id
                )
                children_active = any(
                    not child.is_terminal for child in children
                )
                cursor = connection.execute(
                    """
                    UPDATE runtime_task_leases
                    SET state='released',
                        state_version=state_version+1,
                        released_at=?, release_reason='failed'
                    WHERE lease_id=? AND state='active'
                      AND state_version=? AND fencing_token=?
                    """,
                    (
                        now_iso,
                        lease_id,
                        lease.state_version,
                        fencing_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseLost("lease changed during failure")
                cursor = connection.execute(
                    """
                    UPDATE runtime_task_attempts
                    SET state='failed',
                        state_version=state_version+1,
                        retryable=?,
                        failure_code=?,
                        result_json=NULL,
                        error_json=?,
                        updated_at=?,
                        ended_at=?
                    WHERE attempt_id=? AND state='active'
                      AND state_version=?
                    """,
                    (
                        int(effective_retryable),
                        failure_code,
                        error_json,
                        now_iso,
                        now_iso,
                        attempt_id,
                        attempt.state_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AttemptConflict("Attempt changed during failure")
                for draft in (
                    RuntimeEventDraft(
                        run_id=task.run_id,
                        event_type=EventType.ATTEMPT_LEASE_RELEASED,
                        payload=AttemptLeaseReleasedPayload(
                            task_id=task_id,
                            lease_id=lease_id,
                            lease_epoch=lease.lease_epoch,
                            worker_id=worker_id,
                            reason="failed",
                        ),
                        subject_kind=SubjectKind.ATTEMPT,
                        subject_id=attempt_id,
                        parent_subject_id=task_id,
                        attempt_id=attempt_id,
                        visibility=Visibility.AUDIT,
                        occurred_at=now_iso,
                        idempotency_key=f"lease-released:{lease_id}",
                    ),
                    RuntimeEventDraft(
                        run_id=task.run_id,
                        event_type=EventType.ATTEMPT_FAILED,
                        payload=AttemptFailedPayload(
                            task_id=task_id,
                            attempt_no=attempt.attempt_no,
                            error=dict(error),
                            retryable=effective_retryable,
                        ),
                        subject_kind=SubjectKind.ATTEMPT,
                        subject_id=attempt_id,
                        parent_subject_id=task_id,
                        attempt_id=attempt_id,
                        visibility=Visibility.AUDIT,
                        occurred_at=now_iso,
                        idempotency_key=f"attempt-failed:{attempt_id}",
                    ),
                ):
                    event, is_new = self._append_event_conn(
                        connection, draft
                    )
                    if is_new:
                        events.append(event)
                if can_retry and not children_active:
                    events.extend(
                        self._schedule_retry_conn(
                            connection,
                            task=task,
                            attempt=attempt,
                            lease_id=lease_id,
                            fencing_token=fencing_token,
                            error=error,
                            now_dt=now_dt,
                            cause=cause,
                            reason=(
                                failure_code or "retryable_attempt_failure"
                            ),
                        )
                    )
                else:
                    target_state = (
                        TaskState.WAITING
                        if children_active
                        else TaskState.FAILED
                    )
                    wait_reason = (
                        WaitReason.CHILDREN.value
                        if children_active
                        else None
                    )
                    ended_at = None if children_active else now_iso
                    if not children_active:
                        self._validate_task_run_target(
                            run, task, TaskState.FAILED
                        )
                        self._validate_task_target(
                            task,
                            TaskState.FAILED,
                            children,
                            None,
                            error,
                        )
                    cursor = connection.execute(
                        """
                        UPDATE runtime_tasks
                        SET state=?,
                            state_version=state_version+1,
                            wait_reason=?,
                            result_json=NULL,
                            error_json=?,
                            updated_at=?,
                            ended_at=?,
                            active_attempt_id=NULL,
                            active_lease_id=NULL,
                            next_retry_at=NULL,
                            cancel_deadline_at=NULL
                        WHERE task_id=? AND state='running'
                          AND state_version=?
                          AND active_attempt_id=?
                          AND active_lease_id=?
                          AND fencing_token=?
                        """,
                        (
                            target_state.value,
                            wait_reason,
                            error_json,
                            now_iso,
                            ended_at,
                            task_id,
                            task.state_version,
                            attempt_id,
                            lease_id,
                            fencing_token,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise LeaseLost(
                            "Task fence changed during failure"
                        )
                    event, is_new = self._append_event_conn(
                        connection,
                        RuntimeEventDraft(
                            run_id=task.run_id,
                            event_type=EventType.TASK_STATUS_CHANGED,
                            payload=StateChangedPayload(
                                from_state=TaskState.RUNNING.value,
                                to_state=target_state.value,
                                cause=cause,
                                wait_reason=wait_reason,
                            ),
                            subject_kind=SubjectKind.TASK,
                            subject_id=task_id,
                            parent_subject_id=task.parent_task_id,
                            attempt_id=attempt_id,
                            visibility=Visibility.AUDIT,
                            occurred_at=now_iso,
                            idempotency_key=(
                                f"task-status:{task_id}:"
                                f"{task.state_version + 1}:"
                                f"{target_state.value}"
                            ),
                        ),
                    )
                    if is_new:
                        events.append(event)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_task_conn(connection, task_id)
        self._notify(events)
        return saved

    def acknowledge_attempt_cancellation(
        self,
        claim: TaskClaim,
        *,
        reason: str = "worker acknowledged cancellation",
        now: str | datetime | None = None,
    ) -> TaskRecord:
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeValidationError(
                "cancellation acknowledgement reason is required"
            )
        (
            task_id,
            attempt_id,
            lease_id,
            worker_id,
            fencing_token,
        ) = self._claim_identity(claim)
        events: list[RuntimeEvent] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_iso = _utc_iso(self._command_now(now))
                task = self._get_task_conn(connection, task_id)
                attempt = self._get_attempt_conn(
                    connection, attempt_id
                )
                lease = self._get_lease_conn(connection, lease_id)
                if attempt.state is AttemptState.CANCELLED:
                    if (
                        lease.state is LeaseState.RELEASED
                        and lease.release_reason == reason
                        and lease.worker_id == worker_id
                        and lease.fencing_token == fencing_token
                    ):
                        connection.commit()
                        return task
                    raise LeaseLost(
                        "cancellation was already completed by another "
                        "authority"
                    )
                execution = self._task_execution_row_conn(
                    connection, task_id
                )
                if (
                    task.state is not TaskState.CANCELLING
                    or task.cancel_requested_at is None
                    or attempt.state is not AttemptState.ACTIVE
                    or lease.state is not LeaseState.REVOKING
                    or lease.worker_id != worker_id
                    or lease.fencing_token != fencing_token
                    or lease.attempt_id != attempt_id
                    or execution["active_attempt_id"] != attempt_id
                    or execution["active_lease_id"] != lease_id
                    or int(execution["fencing_token"]) != fencing_token
                ):
                    raise LeaseLost(
                        "worker no longer owns the revoking Task lease/fence"
                    )
                if now_iso < lease.acquired_at or now_iso < lease.heartbeat_at:
                    raise RuntimeValidationError(
                        "acknowledgement time cannot precede the lease clock"
                    )
                events.extend(
                    self._cancel_attempt_conn(
                        connection,
                        task=task,
                        attempt=attempt,
                        lease=lease,
                        now=now_iso,
                        reason=reason,
                        lease_state=LeaseState.RELEASED,
                    )
                )
                saved = self._get_task_conn(connection, task_id)
                if saved.state is TaskState.CANCELLED:
                    events.extend(
                        self._finalize_cancelled_ancestors_conn(
                            connection, task=saved, now=now_iso
                        )
                    )
                events.extend(
                    self._finalize_cancelled_run_conn(
                        connection, run_id=task.run_id, now=now_iso
                    )
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_task_conn(connection, task_id)
        self._notify(events)
        return saved

    def reap_cancel_timeouts(
        self,
        *,
        now: str | datetime | None = None,
        run_id: str | None = None,
        limit: int = 1000,
    ) -> list[TaskRecord]:
        """Revoke cancellation-grace leases and fence late worker results."""

        if type(limit) is not int or limit < 1:
            raise RuntimeValidationError("limit must be an integer >= 1")
        events: list[RuntimeEvent] = []
        task_ids: list[str] = []
        affected_runs: set[str] = set()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_iso = _utc_iso(self._command_now(now))
                if run_id is not None:
                    self._get_run_conn(connection, run_id)
                clauses = [
                    "state='cancelling'",
                    "cancel_deadline_at IS NOT NULL",
                    "cancel_deadline_at<=?",
                ]
                params: list[Any] = [now_iso]
                if run_id is not None:
                    clauses.append("run_id=?")
                    params.append(run_id)
                params.append(limit)
                rows = connection.execute(
                    """
                    SELECT * FROM runtime_tasks
                    WHERE """
                    + " AND ".join(clauses)
                    + """
                    ORDER BY depth DESC, cancel_deadline_at ASC, task_id ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
                for row in rows:
                    task = self._get_task_conn(
                        connection, row["task_id"]
                    )
                    execution = self._task_execution_row_conn(
                        connection, task.task_id
                    )
                    if (
                        task.state is not TaskState.CANCELLING
                        or execution["cancel_deadline_at"] is None
                        or execution["cancel_deadline_at"] > now_iso
                    ):
                        continue
                    attempt_id = execution["active_attempt_id"]
                    lease_id = execution["active_lease_id"]
                    if attempt_id is not None:
                        attempt = self._get_attempt_conn(
                            connection, attempt_id
                        )
                        if lease_id is None:
                            if attempt.state is AttemptState.ACTIVE:
                                cursor = connection.execute(
                                    """
                                    UPDATE runtime_task_attempts
                                    SET state='cancelled',
                                        state_version=state_version+1,
                                        retryable=0,
                                        failure_code='CANCELLED',
                                        updated_at=?,
                                        ended_at=?
                                    WHERE attempt_id=? AND state='active'
                                      AND state_version=?
                                    """,
                                    (
                                        now_iso,
                                        now_iso,
                                        attempt_id,
                                        attempt.state_version,
                                    ),
                                )
                                if cursor.rowcount != 1:
                                    continue
                                event, is_new = self._append_event_conn(
                                    connection,
                                    RuntimeEventDraft(
                                        run_id=task.run_id,
                                        event_type=(
                                            EventType.ATTEMPT_CANCELLED
                                        ),
                                        payload=AttemptCancelledPayload(
                                            task_id=task.task_id,
                                            attempt_no=attempt.attempt_no,
                                            reason=(
                                                self._task_cancel_reason_conn(
                                                    connection,
                                                    task.run_id,
                                                    task.task_id,
                                                )
                                                or "cancellation grace "
                                                "period expired"
                                            ),
                                        ),
                                        subject_kind=SubjectKind.ATTEMPT,
                                        subject_id=attempt_id,
                                        parent_subject_id=task.task_id,
                                        attempt_id=attempt_id,
                                        visibility=Visibility.AUDIT,
                                        occurred_at=now_iso,
                                        idempotency_key=(
                                            f"attempt-cancelled:"
                                            f"{attempt_id}"
                                        ),
                                    ),
                                )
                                if is_new:
                                    events.append(event)
                            connection.execute(
                                """
                                UPDATE runtime_tasks
                                SET active_attempt_id=NULL,
                                    active_lease_id=NULL,
                                    next_retry_at=NULL,
                                    cancel_deadline_at=NULL,
                                    updated_at=?
                                WHERE task_id=?
                                """,
                                (now_iso, task.task_id),
                            )
                            children = self._children_conn(
                                connection, task.run_id, task.task_id
                            )
                            if not any(
                                not child.is_terminal
                                for child in children
                            ):
                                events.append(
                                    self._transition_task_for_cancel_conn(
                                        connection,
                                        task,
                                        TaskState.CANCELLED,
                                        now=now_iso,
                                        cause=(
                                            "cancellation grace period "
                                            "expired"
                                        ),
                                    )
                                )
                        else:
                            lease = self._get_lease_conn(
                                connection, lease_id
                            )
                            events.extend(
                                self._cancel_attempt_conn(
                                    connection,
                                    task=task,
                                    attempt=attempt,
                                    lease=lease,
                                    now=now_iso,
                                    reason=(
                                        "cancellation grace period expired"
                                    ),
                                    lease_state=LeaseState.REVOKED,
                                )
                            )
                    else:
                        children = self._children_conn(
                            connection, task.run_id, task.task_id
                        )
                        if any(
                            not child.is_terminal for child in children
                        ):
                            continue
                        connection.execute(
                            """
                            UPDATE runtime_tasks
                            SET cancel_deadline_at=NULL
                            WHERE task_id=?
                            """,
                            (task.task_id,),
                        )
                        events.append(
                            self._transition_task_for_cancel_conn(
                                connection,
                                task,
                                TaskState.CANCELLED,
                                now=now_iso,
                                cause=(
                                    "cancellation grace period expired"
                                ),
                            )
                        )
                    saved = self._get_task_conn(
                        connection, task.task_id
                    )
                    if saved.state is TaskState.CANCELLED:
                        events.extend(
                            self._finalize_cancelled_ancestors_conn(
                                connection, task=saved, now=now_iso
                            )
                        )
                    task_ids.append(task.task_id)
                    affected_runs.add(task.run_id)
                for affected_run_id in sorted(affected_runs):
                    events.extend(
                        self._finalize_cancelled_run_conn(
                            connection,
                            run_id=affected_run_id,
                            now=now_iso,
                        )
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            tasks = [
                self._get_task_conn(connection, task_id)
                for task_id in dict.fromkeys(task_ids)
            ]
        self._notify(events)
        return tasks

    def promote_due_retries(
        self,
        *,
        now: str | datetime | None = None,
        run_id: str | None = None,
        limit: int = 1000,
    ) -> list[TaskRecord]:
        if type(limit) is not int or limit < 1:
            raise RuntimeValidationError("limit must be an integer >= 1")
        events: list[RuntimeEvent] = []
        task_ids: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_iso = _utc_iso(self._command_now(now))
                if run_id is not None:
                    self._get_run_conn(connection, run_id)
                events = self._promote_due_retries_conn(
                    connection,
                    now=now_iso,
                    run_id=run_id,
                    limit=limit,
                    promoted_task_ids=task_ids,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            tasks = [
                self._get_task_conn(connection, task_id)
                for task_id in task_ids
            ]
        self._notify(events)
        return tasks

    def recover_runtime(
        self,
        *,
        now: str | datetime | None = None,
        run_id: str | None = None,
        limit: int = 1000,
    ) -> Mapping[str, int]:
        """Run idempotent watchdog passes after startup or worker loss."""

        cancelled = self.reap_cancel_timeouts(
            now=now, run_id=run_id, limit=limit
        )
        expired = self.reap_expired_leases(
            now=now, run_id=run_id, limit=limit
        )
        reconciled = self.reconcile_orphaned_tasks(
            now=now, run_id=run_id, limit=limit
        )
        promoted = self.promote_due_retries(
            now=now, run_id=run_id, limit=limit
        )
        return {
            "cancelled_tasks": len(cancelled),
            "expired_leases": len(expired),
            "reconciled_tasks": len(reconciled),
            "promoted_retries": len(promoted),
        }

    def reconcile_orphaned_tasks(
        self,
        *,
        now: str | datetime | None = None,
        run_id: str | None = None,
        limit: int = 1000,
    ) -> list[TaskRecord]:
        """Converge restart orphans without inventing execution authority."""

        if type(limit) is not int or limit < 1:
            raise RuntimeValidationError("limit must be an integer >= 1")
        events: list[RuntimeEvent] = []
        affected_task_ids: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = self._command_now(now)
                now_iso = _utc_iso(now_dt)
                if run_id is not None:
                    self._get_run_conn(connection, run_id)

                params: list[Any] = []
                run_clause = ""
                if run_id is not None:
                    run_clause = " AND task.run_id=?"
                    params.append(run_id)
                params.append(limit)
                legacy_rows = connection.execute(
                    """
                    SELECT task.*
                    FROM runtime_tasks task
                    JOIN runtime_runs run ON run.run_id=task.run_id
                    WHERE task.attempt_managed=0
                      AND task.state='running'
                      AND task.active_attempt_id IS NULL
                      AND task.active_lease_id IS NULL
                      AND task.cancel_requested_at IS NULL
                      AND run.state IN ('running', 'waiting')
                      AND run.cancel_requested_at IS NULL
                    """
                    + run_clause
                    + """
                    ORDER BY task.updated_at ASC, task.task_id ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
                for row in legacy_rows:
                    task = self._row_to_task(row)
                    cursor = connection.execute(
                        """
                        UPDATE runtime_tasks
                        SET state='ready',
                            state_version=state_version+1,
                            wait_reason=NULL,
                            attempt=0,
                            result_json=NULL,
                            error_json=NULL,
                            updated_at=?,
                            ended_at=NULL
                        WHERE task_id=? AND state='running'
                          AND state_version=?
                          AND attempt_managed=0
                          AND active_attempt_id IS NULL
                          AND active_lease_id IS NULL
                        """,
                        (now_iso, task.task_id, task.state_version),
                    )
                    if cursor.rowcount != 1:
                        continue
                    event, is_new = self._append_event_conn(
                        connection,
                        RuntimeEventDraft(
                            run_id=task.run_id,
                            event_type=EventType.TASK_STATUS_CHANGED,
                            payload=StateChangedPayload(
                                from_state=TaskState.RUNNING.value,
                                to_state=TaskState.READY.value,
                                cause=(
                                    "legacy execution recovered after "
                                    "Runtime restart"
                                ),
                            ),
                            subject_kind=SubjectKind.TASK,
                            subject_id=task.task_id,
                            parent_subject_id=task.parent_task_id,
                            visibility=Visibility.AUDIT,
                            occurred_at=now_iso,
                            idempotency_key=(
                                f"task-status:{task.task_id}:"
                                f"{task.state_version + 1}:ready"
                            ),
                        ),
                    )
                    if is_new:
                        events.append(event)
                    affected_task_ids.append(task.task_id)

                params = []
                run_clause = ""
                if run_id is not None:
                    run_clause = " AND task.run_id=?"
                    params.append(run_id)
                params.append(limit)
                waiting_rows = connection.execute(
                    """
                    SELECT task.*
                    FROM runtime_tasks task
                    JOIN runtime_runs run ON run.run_id=task.run_id
                    WHERE task.attempt_managed=1
                      AND task.state='waiting'
                      AND task.wait_reason='children'
                      AND task.active_lease_id IS NULL
                      AND task.cancel_requested_at IS NULL
                      AND run.state IN ('running', 'waiting')
                      AND run.cancel_requested_at IS NULL
                    """
                    + run_clause
                    + """
                    ORDER BY task.depth DESC, task.updated_at ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
                for row in waiting_rows:
                    task = self._row_to_task(row)
                    children = self._children_conn(
                        connection, task.run_id, task.task_id
                    )
                    if any(not child.is_terminal for child in children):
                        continue
                    execution = self._task_execution_row_conn(
                        connection, task.task_id
                    )
                    active_attempt_id = execution["active_attempt_id"]
                    target = TaskState.READY
                    wait_reason: str | None = None
                    next_retry_at: str | None = None
                    ended_at: str | None = None
                    error = task.error
                    retry_event: RuntimeEventDraft | None = None
                    event_attempt_id: str | None = active_attempt_id
                    if active_attempt_id is not None:
                        active_attempt = self._get_attempt_conn(
                            connection, active_attempt_id
                        )
                        if active_attempt.state is not AttemptState.ACTIVE:
                            raise AttemptConflict(
                                "yielded parent references a non-active "
                                "Attempt"
                            )
                    else:
                        policy = self._get_task_retry_policy_conn(
                            connection, task.task_id
                        )
                        attempt_row = connection.execute(
                            """
                            SELECT *
                            FROM runtime_task_attempts
                            WHERE task_id=?
                            ORDER BY attempt_no DESC
                            LIMIT 1
                            """,
                            (task.task_id,),
                        ).fetchone()
                        last_attempt = (
                            self._row_to_attempt(attempt_row)
                            if attempt_row is not None
                            else None
                        )
                        if (
                            last_attempt is not None
                            and last_attempt.retryable is True
                            and task.attempt < policy.max_attempts
                        ):
                            delay = self._retry_delay_seconds(
                                policy,
                                run_id=task.run_id,
                                task_id=task.task_id,
                                failed_attempt_no=last_attempt.attempt_no,
                            )
                            ended = _utc_datetime(
                                last_attempt.ended_at or now_iso,
                                label="Attempt ended_at",
                            )
                            retry_at = _future_iso(ended, delay)
                            event_attempt_id = last_attempt.attempt_id
                            if retry_at > now_iso:
                                target = TaskState.WAITING
                                wait_reason = (
                                    WaitReason.RETRY_BACKOFF.value
                                )
                                next_retry_at = retry_at
                            retry_event = RuntimeEventDraft(
                                run_id=task.run_id,
                                event_type=EventType.TASK_RETRY_SCHEDULED,
                                payload=TaskRetryScheduledPayload(
                                    task_id=task.task_id,
                                    failed_attempt_id=(
                                        last_attempt.attempt_id
                                    ),
                                    failed_attempt_no=(
                                        last_attempt.attempt_no
                                    ),
                                    next_attempt_no=(
                                        last_attempt.attempt_no + 1
                                    ),
                                    retry_at=retry_at,
                                    delay_seconds=delay,
                                    reason=(
                                        last_attempt.failure_code
                                        or "orphaned_parent_recovered"
                                    ),
                                ),
                                subject_kind=SubjectKind.TASK,
                                subject_id=task.task_id,
                                parent_subject_id=task.parent_task_id,
                                attempt_id=last_attempt.attempt_id,
                                visibility=Visibility.AUDIT,
                                occurred_at=now_iso,
                                idempotency_key=(
                                    "task-retry-scheduled:"
                                    f"{last_attempt.attempt_id}"
                                ),
                            )
                        else:
                            target = TaskState.FAILED
                            ended_at = now_iso
                            error = error or {
                                "code": "ATTEMPT_BUDGET_EXHAUSTED",
                                "message": (
                                    "Task execution stopped after its "
                                    "Attempt budget was exhausted"
                                ),
                                "category": "runtime",
                                "retryable": False,
                            }
                    cursor = connection.execute(
                        """
                        UPDATE runtime_tasks
                        SET state=?,
                            state_version=state_version+1,
                            wait_reason=?,
                            error_json=?,
                            updated_at=?,
                            ended_at=?,
                            next_retry_at=?
                        WHERE task_id=? AND state='waiting'
                          AND state_version=?
                          AND wait_reason='children'
                          AND active_lease_id IS NULL
                        """,
                        (
                            target.value,
                            wait_reason,
                            (
                                _json_dumps(error)
                                if error is not None
                                else None
                            ),
                            now_iso,
                            ended_at,
                            next_retry_at,
                            task.task_id,
                            task.state_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        continue
                    if retry_event is not None:
                        event, is_new = self._append_event_conn(
                            connection, retry_event
                        )
                        if is_new:
                            events.append(event)
                    event, is_new = self._append_event_conn(
                        connection,
                        RuntimeEventDraft(
                            run_id=task.run_id,
                            event_type=EventType.TASK_STATUS_CHANGED,
                            payload=StateChangedPayload(
                                from_state=TaskState.WAITING.value,
                                to_state=target.value,
                                cause=(
                                    "all child Tasks became terminal"
                                    if target is TaskState.READY
                                    else (
                                        "waiting for retry backoff"
                                        if target is TaskState.WAITING
                                        else "Attempt budget exhausted"
                                    )
                                ),
                                wait_reason=wait_reason,
                            ),
                            subject_kind=SubjectKind.TASK,
                            subject_id=task.task_id,
                            parent_subject_id=task.parent_task_id,
                            attempt_id=event_attempt_id,
                            visibility=Visibility.AUDIT,
                            occurred_at=now_iso,
                            idempotency_key=(
                                f"task-status:{task.task_id}:"
                                f"{task.state_version + 1}:"
                                f"{target.value}"
                            ),
                        ),
                    )
                    if is_new:
                        events.append(event)
                    affected_task_ids.append(task.task_id)

                params = []
                run_clause = ""
                if run_id is not None:
                    run_clause = " AND task.run_id=?"
                    params.append(run_id)
                params.append(limit)
                exhausted_rows = connection.execute(
                    """
                    SELECT task.*
                    FROM runtime_tasks task
                    JOIN runtime_task_retry_policies policy
                      ON policy.run_id=task.run_id
                     AND policy.task_id=task.task_id
                    WHERE task.attempt_managed=1
                      AND task.state='ready'
                      AND task.active_attempt_id IS NULL
                      AND task.active_lease_id IS NULL
                      AND task.attempt>=policy.max_attempts
                      AND NOT EXISTS (
                          SELECT 1 FROM runtime_tasks child
                          WHERE child.run_id=task.run_id
                            AND child.parent_task_id=task.task_id
                            AND child.state NOT IN (
                                'succeeded', 'failed', 'cancelled', 'skipped'
                            )
                      )
                    """
                    + run_clause
                    + """
                    ORDER BY task.updated_at ASC, task.task_id ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
                for row in exhausted_rows:
                    task = self._row_to_task(row)
                    error = task.error or {
                        "code": "ATTEMPT_BUDGET_EXHAUSTED",
                        "message": (
                            "Task has no remaining execution Attempts"
                        ),
                        "category": "runtime",
                        "retryable": False,
                    }
                    cursor = connection.execute(
                        """
                        UPDATE runtime_tasks
                        SET state='failed',
                            state_version=state_version+1,
                            error_json=?,
                            updated_at=?,
                            ended_at=?
                        WHERE task_id=? AND state='ready'
                          AND state_version=?
                          AND active_attempt_id IS NULL
                          AND active_lease_id IS NULL
                        """,
                        (
                            _json_dumps(error),
                            now_iso,
                            now_iso,
                            task.task_id,
                            task.state_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        continue
                    event, is_new = self._append_event_conn(
                        connection,
                        RuntimeEventDraft(
                            run_id=task.run_id,
                            event_type=EventType.TASK_STATUS_CHANGED,
                            payload=StateChangedPayload(
                                from_state=TaskState.READY.value,
                                to_state=TaskState.FAILED.value,
                                cause="Attempt budget exhausted",
                            ),
                            subject_kind=SubjectKind.TASK,
                            subject_id=task.task_id,
                            parent_subject_id=task.parent_task_id,
                            visibility=Visibility.AUDIT,
                            occurred_at=now_iso,
                            idempotency_key=(
                                f"task-status:{task.task_id}:"
                                f"{task.state_version + 1}:failed"
                            ),
                        ),
                    )
                    if is_new:
                        events.append(event)
                    affected_task_ids.append(task.task_id)

                events.extend(
                    self._settle_exhausted_runs_conn(
                        connection, now=now_iso, run_id=run_id
                    )
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            tasks = [
                self._get_task_conn(connection, task_id)
                for task_id in dict.fromkeys(affected_task_ids)
            ]
        self._notify(events)
        return tasks

    def _settle_exhausted_runs_conn(
        self,
        connection: sqlite3.Connection,
        *,
        now: str,
        run_id: str | None,
    ) -> list[RuntimeEvent]:
        clauses = [
            "run.state IN ('queued', 'running', 'waiting', 'finalizing')",
            """
            NOT EXISTS (
                SELECT 1 FROM runtime_tasks active
                WHERE active.run_id=run.run_id
                  AND active.state NOT IN (
                      'succeeded', 'failed', 'cancelled', 'skipped'
                  )
            )
            """,
            """
            EXISTS (
                SELECT 1 FROM runtime_tasks failed
                WHERE failed.run_id=run.run_id
                  AND failed.state='failed'
                  AND (
                      failed.required=1
                      OR failed.task_id=run.root_task_id
                  )
            )
            """,
        ]
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run.run_id=?")
            params.append(run_id)
        rows = connection.execute(
            """
            SELECT run.*
            FROM runtime_runs run
            WHERE """
            + " AND ".join(clauses),
            params,
        ).fetchall()
        events: list[RuntimeEvent] = []
        for row in rows:
            run = self._row_to_run(row)
            validate_run_transition(run.state, RunState.FAILED)
            error = {
                "code": "REQUIRED_TASK_FAILED",
                "message": (
                    "Runtime recovery found no executable work after a "
                    "required Task failed"
                ),
                "category": "runtime",
                "retryable": False,
            }
            cursor = connection.execute(
                """
                UPDATE runtime_runs
                SET state='failed',
                    state_version=state_version+1,
                    error_json=?,
                    updated_at=?,
                    ended_at=?
                WHERE run_id=? AND state_version=?
                """,
                (
                    _json_dumps(error),
                    now,
                    now,
                    run.run_id,
                    run.state_version,
                ),
            )
            if cursor.rowcount != 1:
                continue
            event, is_new = self._append_event_conn(
                connection,
                RuntimeEventDraft(
                    run_id=run.run_id,
                    event_type=EventType.RUN_STATUS_CHANGED,
                    payload=StateChangedPayload(
                        from_state=run.state.value,
                        to_state=RunState.FAILED.value,
                        cause="required Task exhausted its Attempt budget",
                    ),
                    subject_kind=SubjectKind.RUN,
                    subject_id=run.run_id,
                    visibility=Visibility.AUDIT,
                    occurred_at=now,
                    idempotency_key=(
                        f"run-status:{run.run_id}:"
                        f"{run.state_version + 1}:failed"
                    ),
                ),
            )
            if is_new:
                events.append(event)
        return events

    def reap_expired_leases(
        self,
        *,
        now: str | datetime | None = None,
        run_id: str | None = None,
        limit: int = 1000,
    ) -> list[TaskRecord]:
        """Fence dead workers and atomically retry or fail their Attempts."""

        if type(limit) is not int or limit < 1:
            raise RuntimeValidationError("limit must be an integer >= 1")
        events: list[RuntimeEvent] = []
        task_ids: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = self._command_now(now)
                now_iso = _utc_iso(now_dt)
                if run_id is not None:
                    self._get_run_conn(connection, run_id)
                clauses = [
                    "lease.state='active'",
                    "lease.expires_at<=?",
                ]
                params: list[Any] = [now_iso]
                if run_id is not None:
                    clauses.append("lease.run_id=?")
                    params.append(run_id)
                params.append(limit)
                rows = connection.execute(
                    """
                    SELECT lease.*
                    FROM runtime_task_leases lease
                    WHERE """
                    + " AND ".join(clauses)
                    + """
                    ORDER BY lease.expires_at ASC, lease.lease_id ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
                for row in rows:
                    lease = self._row_to_lease(row)
                    task = self._get_task_conn(
                        connection, lease.task_id
                    )
                    execution = self._task_execution_row_conn(
                        connection, lease.task_id
                    )
                    if (
                        execution["active_attempt_id"]
                        != lease.attempt_id
                        or execution["active_lease_id"] != lease.lease_id
                        or int(execution["fencing_token"])
                        != lease.fencing_token
                    ):
                        # A non-authoritative row must never be allowed to
                        # mutate the Task; merely retire it for audit hygiene.
                        connection.execute(
                            """
                            UPDATE runtime_task_leases
                            SET state='expired',
                                state_version=state_version+1,
                                released_at=?,
                                release_reason='superseded_expired'
                            WHERE lease_id=? AND state='active'
                            """,
                            (now_iso, lease.lease_id),
                        )
                        continue
                    attempt = self._get_attempt_conn(
                        connection, lease.attempt_id
                    )
                    if attempt.state is not AttemptState.ACTIVE:
                        raise AttemptConflict(
                            "authoritative lease references a non-active "
                            "Attempt"
                        )
                    if (
                        task.cancel_requested_at is not None
                        or task.state is TaskState.CANCELLING
                    ):
                        events.extend(
                            self._cancel_attempt_conn(
                                connection,
                                task=task,
                                attempt=attempt,
                                lease=lease,
                                now=now_iso,
                                reason=(
                                    "lease expired during cancellation"
                                ),
                                lease_state=LeaseState.REVOKED,
                            )
                        )
                        task_ids.append(task.task_id)
                        continue
                    if task.state is not TaskState.RUNNING:
                        raise AttemptConflict(
                            "authoritative active lease requires RUNNING Task"
                        )
                    runtime_error: Mapping[str, Any] = {
                        "code": "LEASE_EXPIRED",
                        "message": (
                            "Worker lease expired before the Attempt "
                            "completed"
                        ),
                        "category": "runtime",
                        "retryable": True,
                        "lease_id": lease.lease_id,
                        "worker_id": lease.worker_id,
                    }
                    cursor = connection.execute(
                        """
                        UPDATE runtime_task_leases
                        SET state='expired',
                            state_version=state_version+1,
                            released_at=?,
                            release_reason='lease_expired'
                        WHERE lease_id=? AND state='active'
                          AND state_version=?
                        """,
                        (
                            now_iso,
                            lease.lease_id,
                            lease.state_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        continue
                    cursor = connection.execute(
                        """
                        UPDATE runtime_task_attempts
                        SET state='expired',
                            state_version=state_version+1,
                            retryable=1,
                            failure_code='LEASE_EXPIRED',
                            result_json=NULL,
                            error_json=?,
                            updated_at=?,
                            ended_at=?
                        WHERE attempt_id=? AND state='active'
                          AND state_version=?
                        """,
                        (
                            _json_dumps(runtime_error),
                            now_iso,
                            now_iso,
                            attempt.attempt_id,
                            attempt.state_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise AttemptConflict(
                            "Attempt changed during lease expiry"
                        )
                    for draft in (
                        RuntimeEventDraft(
                            run_id=task.run_id,
                            event_type=EventType.ATTEMPT_LEASE_EXPIRED,
                            payload=AttemptLeaseExpiredPayload(
                                task_id=task.task_id,
                                lease_id=lease.lease_id,
                                lease_epoch=lease.lease_epoch,
                                worker_id=lease.worker_id,
                                expired_at=now_iso,
                            ),
                            subject_kind=SubjectKind.ATTEMPT,
                            subject_id=attempt.attempt_id,
                            parent_subject_id=task.task_id,
                            attempt_id=attempt.attempt_id,
                            visibility=Visibility.AUDIT,
                            occurred_at=now_iso,
                            idempotency_key=(
                                f"lease-expired:{lease.lease_id}"
                            ),
                        ),
                        RuntimeEventDraft(
                            run_id=task.run_id,
                            event_type=EventType.ATTEMPT_FAILED,
                            payload=AttemptFailedPayload(
                                task_id=task.task_id,
                                attempt_no=attempt.attempt_no,
                                error=runtime_error,
                                retryable=True,
                            ),
                            subject_kind=SubjectKind.ATTEMPT,
                            subject_id=attempt.attempt_id,
                            parent_subject_id=task.task_id,
                            attempt_id=attempt.attempt_id,
                            visibility=Visibility.AUDIT,
                            occurred_at=now_iso,
                            idempotency_key=(
                                f"attempt-failed:{attempt.attempt_id}"
                            ),
                        ),
                    ):
                        event, is_new = self._append_event_conn(
                            connection, draft
                        )
                        if is_new:
                            events.append(event)
                    run = self._get_run_conn(connection, task.run_id)
                    can_retry = (
                        attempt.attempt_no
                        < attempt.retry_policy.max_attempts
                        and run.cancel_requested_at is None
                    )
                    children = self._children_conn(
                        connection, task.run_id, task.task_id
                    )
                    children_active = any(
                        not child.is_terminal for child in children
                    )
                    if can_retry and not children_active:
                        events.extend(
                            self._schedule_retry_conn(
                                connection,
                                task=task,
                                attempt=attempt,
                                lease_id=lease.lease_id,
                                fencing_token=lease.fencing_token,
                                error=runtime_error,
                                now_dt=now_dt,
                                cause="worker lease expired",
                                reason="LEASE_EXPIRED",
                            )
                        )
                    else:
                        if children_active:
                            target_state = TaskState.WAITING
                            wait_reason = WaitReason.CHILDREN.value
                            ended_at = None
                        else:
                            self._validate_task_run_target(
                                run, task, TaskState.FAILED
                            )
                            target_state = TaskState.FAILED
                            wait_reason = None
                            ended_at = now_iso
                        cursor = connection.execute(
                            """
                            UPDATE runtime_tasks
                            SET state=?,
                                state_version=state_version+1,
                                wait_reason=?,
                                result_json=NULL,
                                error_json=?,
                                updated_at=?,
                                ended_at=?,
                                active_attempt_id=NULL,
                                active_lease_id=NULL,
                                next_retry_at=NULL,
                                cancel_deadline_at=NULL
                            WHERE task_id=? AND state='running'
                              AND state_version=?
                              AND active_attempt_id=?
                              AND active_lease_id=?
                              AND fencing_token=?
                            """,
                            (
                                target_state.value,
                                wait_reason,
                                _json_dumps(runtime_error),
                                now_iso,
                                ended_at,
                                task.task_id,
                                task.state_version,
                                attempt.attempt_id,
                                lease.lease_id,
                                lease.fencing_token,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise LeaseLost(
                                "Task fence changed during lease expiry"
                            )
                        event, is_new = self._append_event_conn(
                            connection,
                            RuntimeEventDraft(
                                run_id=task.run_id,
                                event_type=(
                                    EventType.TASK_STATUS_CHANGED
                                ),
                                payload=StateChangedPayload(
                                    from_state=TaskState.RUNNING.value,
                                    to_state=target_state.value,
                                    cause="worker lease expired",
                                    wait_reason=wait_reason,
                                ),
                                subject_kind=SubjectKind.TASK,
                                subject_id=task.task_id,
                                parent_subject_id=task.parent_task_id,
                                attempt_id=attempt.attempt_id,
                                visibility=Visibility.AUDIT,
                                occurred_at=now_iso,
                                idempotency_key=(
                                    f"task-status:{task.task_id}:"
                                    f"{task.state_version + 1}:"
                                    f"{target_state.value}"
                                ),
                            ),
                        )
                        if is_new:
                            events.append(event)
                    task_ids.append(task.task_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            tasks = [
                self._get_task_conn(connection, task_id)
                for task_id in dict.fromkeys(task_ids)
            ]
        self._notify(events)
        return tasks

    def claim_next_task(
        self,
        worker_id: str,
        lease_ttl_seconds: float,
        run_id: str | None = None,
        roles: Iterable[str] | None = None,
        now: str | datetime | None = None,
        *,
        worker_slot_id: str | None = None,
        claim_request_id: str | None = None,
    ) -> TaskClaim | None:
        """Atomically select one READY Task and grant a fenced worker lease."""

        if not isinstance(worker_id, str) or not worker_id.strip():
            raise RuntimeValidationError("worker_id is required")
        worker_id = worker_id.strip()
        ttl = _positive_seconds(
            lease_ttl_seconds, label="lease_ttl_seconds"
        )
        if worker_slot_id is None:
            worker_slot_id = "__legacy_single_slot__"
        elif (
            not isinstance(worker_slot_id, str)
            or not worker_slot_id.strip()
        ):
            raise RuntimeValidationError(
                "worker_slot_id must be a non-empty string"
            )
        else:
            worker_slot_id = worker_slot_id.strip()
        if claim_request_id is not None:
            if (
                not isinstance(claim_request_id, str)
                or not claim_request_id.strip()
            ):
                raise RuntimeValidationError(
                    "claim_request_id must be a non-empty string"
                )
            claim_request_id = claim_request_id.strip()
        role_values: tuple[str, ...] | None = None
        if roles is not None:
            if isinstance(roles, str):
                roles = (roles,)
            normalized: list[str] = []
            for role in roles:
                if not isinstance(role, str) or not role.strip():
                    raise RuntimeValidationError(
                        "roles must contain non-empty strings"
                    )
                normalized.append(role.strip())
            if not normalized:
                return None
            role_values = tuple(dict.fromkeys(normalized))

        events: list[RuntimeEvent] = []
        claim: TaskClaim | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = self._command_now(now)
                now_iso = _utc_iso(now_dt)
                expires_at = _future_iso(now_dt, ttl)
                existing_lease_row = connection.execute(
                    """
                    SELECT lease.*
                    FROM runtime_task_leases lease
                    WHERE lease.worker_id=?
                      AND lease.worker_slot_id=?
                      AND lease.state IN ('active', 'revoking')
                    ORDER BY lease.acquired_at DESC
                    LIMIT 1
                    """,
                    (worker_id, worker_slot_id),
                ).fetchone()
                if existing_lease_row is not None:
                    existing_lease = self._row_to_lease(
                        existing_lease_row
                    )
                    if existing_lease.expires_at <= now_iso:
                        connection.commit()
                        return None
                    existing_attempt = self._get_attempt_conn(
                        connection, existing_lease.attempt_id
                    )
                    existing_task = self._get_task_conn(
                        connection, existing_lease.task_id
                    )
                    if (
                        (run_id is not None and existing_task.run_id != run_id)
                        or (
                            role_values is not None
                            and existing_task.role not in role_values
                        )
                        or (
                            claim_request_id is not None
                            and existing_lease.claim_request_id
                            != claim_request_id
                        )
                    ):
                        connection.commit()
                        return None
                    claim = TaskClaim(
                        run_id=existing_task.run_id,
                        task_id=existing_task.task_id,
                        attempt_no=existing_attempt.attempt_no,
                        attempt_id=existing_attempt.attempt_id,
                        lease_id=existing_lease.lease_id,
                        worker_id=existing_lease.worker_id,
                        fencing_token=existing_lease.fencing_token,
                        acquired_at=existing_lease.acquired_at,
                        expires_at=existing_lease.expires_at,
                        worker_slot_id=existing_lease.worker_slot_id,
                        claim_request_id=(
                            existing_lease.claim_request_id
                        ),
                    )
                    connection.commit()
                    return claim
                if claim_request_id is not None:
                    consumed = connection.execute(
                        """
                        SELECT 1
                        FROM runtime_task_leases
                        WHERE worker_id=? AND worker_slot_id=?
                          AND claim_request_id=?
                        LIMIT 1
                        """,
                        (
                            worker_id,
                            worker_slot_id,
                            claim_request_id,
                        ),
                    ).fetchone()
                    if consumed is not None:
                        connection.commit()
                        return None
                if run_id is not None:
                    self._get_run_conn(connection, run_id)
                events.extend(
                    self._promote_due_retries_conn(
                        connection, now=now_iso, run_id=run_id
                    )
                )
                clauses = [
                    "task.state='ready'",
                    "run.state IN ('running', 'waiting')",
                    "task.cancel_requested_at IS NULL",
                    "run.cancel_requested_at IS NULL",
                    "(task.next_retry_at IS NULL OR task.next_retry_at<=?)",
                    "task.active_lease_id IS NULL",
                    """
                    (
                        task.active_attempt_id IS NOT NULL
                        OR task.attempt < retry_policy.max_attempts
                    )
                    """,
                    """
                    NOT EXISTS (
                        SELECT 1
                        FROM runtime_tasks child
                        WHERE child.run_id=task.run_id
                          AND child.parent_task_id=task.task_id
                          AND child.state NOT IN (
                              'succeeded', 'failed', 'cancelled', 'skipped'
                          )
                    )
                    """,
                    """
                    NOT EXISTS (
                        SELECT 1
                        FROM runtime_task_leases lease
                        WHERE lease.run_id=task.run_id
                          AND lease.task_id=task.task_id
                          AND lease.state IN ('active', 'revoking')
                    )
                    """,
                ]
                params: list[Any] = [now_iso]
                if run_id is not None:
                    clauses.append("task.run_id=?")
                    params.append(run_id)
                if role_values is not None:
                    placeholders = ",".join("?" for _ in role_values)
                    clauses.append(f"task.role IN ({placeholders})")
                    params.extend(role_values)
                row = connection.execute(
                    """
                    SELECT task.*
                    FROM runtime_tasks task
                    JOIN runtime_runs run ON run.run_id=task.run_id
                    JOIN runtime_task_retry_policies retry_policy
                      ON retry_policy.run_id=task.run_id
                     AND retry_policy.task_id=task.task_id
                    WHERE """
                    + " AND ".join(clauses)
                    + """
                    ORDER BY task.created_at ASC, task.task_id ASC
                    LIMIT 1
                    """,
                    params,
                ).fetchone()
                if row is None:
                    connection.commit()
                else:
                    task = self._row_to_task(row)
                    active_attempt_id = row["active_attempt_id"]
                    attempt_started = False
                    if active_attempt_id is None:
                        retry_policy = self._get_task_retry_policy_conn(
                            connection, task.task_id
                        )
                        attempt_no = int(row["attempt"]) + 1
                        if attempt_no > retry_policy.max_attempts:
                            raise AttemptConflict(
                                "Task retry budget is exhausted"
                            )
                        attempt_id = f"att_{uuid.uuid4().hex}"
                        connection.execute(
                            """
                            INSERT INTO runtime_task_attempts(
                                attempt_id, run_id, task_id, attempt_no, state,
                                state_version, retry_policy_json, retryable,
                                failure_code, result_json, error_json,
                                created_at, started_at, updated_at, ended_at
                            ) VALUES (
                                ?, ?, ?, ?, 'active', 0, ?, NULL, NULL,
                                NULL, NULL, ?, ?, ?, NULL
                            )
                            """,
                            (
                                attempt_id,
                                task.run_id,
                                task.task_id,
                                attempt_no,
                                _json_dumps(
                                    _retry_policy_to_dict(retry_policy)
                                ),
                                now_iso,
                                now_iso,
                                now_iso,
                            ),
                        )
                        event, is_new = self._append_event_conn(
                            connection,
                            RuntimeEventDraft(
                                run_id=task.run_id,
                                event_type=EventType.ATTEMPT_STARTED,
                                payload=AttemptStartedPayload(
                                    task_id=task.task_id,
                                    attempt_no=attempt_no,
                                    retry_policy=_retry_policy_to_dict(
                                        retry_policy
                                    ),
                                ),
                                subject_kind=SubjectKind.ATTEMPT,
                                subject_id=attempt_id,
                                parent_subject_id=task.task_id,
                                attempt_id=attempt_id,
                                visibility=Visibility.AUDIT,
                                occurred_at=now_iso,
                                idempotency_key=(
                                    f"attempt-started:{attempt_id}"
                                ),
                            ),
                        )
                        if is_new:
                            events.append(event)
                        attempt_started = True
                    else:
                        attempt = self._get_attempt_conn(
                            connection, active_attempt_id
                        )
                        if attempt.state is not AttemptState.ACTIVE:
                            raise AttemptConflict(
                                "Task references a non-active Attempt"
                            )
                        attempt_id = attempt.attempt_id
                        attempt_no = attempt.attempt_no
                        retry_policy = attempt.retry_policy

                    epoch_row = connection.execute(
                        """
                        SELECT COALESCE(MAX(lease_epoch), 0) AS last_epoch
                        FROM runtime_task_leases
                        WHERE attempt_id=?
                        """,
                        (attempt_id,),
                    ).fetchone()
                    lease_epoch = int(epoch_row["last_epoch"]) + 1
                    fencing_token = int(row["fencing_token"]) + 1
                    lease_id = f"lease_{uuid.uuid4().hex}"
                    connection.execute(
                        """
                        INSERT INTO runtime_task_leases(
                            lease_id, run_id, task_id, attempt_id, lease_epoch,
                            worker_id, state, state_version, fencing_token,
                            acquired_at, heartbeat_at, expires_at,
                            released_at, release_reason, worker_slot_id,
                            claim_request_id
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?, ?,
                            NULL, NULL, ?, ?
                        )
                        """,
                        (
                            lease_id,
                            task.run_id,
                            task.task_id,
                            attempt_id,
                            lease_epoch,
                            worker_id,
                            fencing_token,
                            now_iso,
                            now_iso,
                            expires_at,
                            worker_slot_id,
                            claim_request_id,
                        ),
                    )
                    cursor = connection.execute(
                        """
                        UPDATE runtime_tasks
                        SET state='running',
                            state_version=state_version+1,
                            wait_reason=NULL,
                            attempt=?,
                            result_json=NULL,
                            error_json=NULL,
                            started_at=COALESCE(started_at, ?),
                            updated_at=?,
                            ended_at=NULL,
                            active_attempt_id=?,
                            active_lease_id=?,
                            fencing_token=?,
                            next_retry_at=NULL,
                            cancel_deadline_at=NULL
                            ,attempt_managed=1
                        WHERE task_id=?
                          AND state='ready'
                          AND state_version=?
                          AND active_lease_id IS NULL
                          AND cancel_requested_at IS NULL
                        """,
                        (
                            attempt_no,
                            now_iso,
                            now_iso,
                            attempt_id,
                            lease_id,
                            fencing_token,
                            task.task_id,
                            task.state_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConcurrencyConflict(
                            "Task changed during atomic claim"
                        )
                    event, is_new = self._append_event_conn(
                        connection,
                        RuntimeEventDraft(
                            run_id=task.run_id,
                            event_type=EventType.ATTEMPT_LEASE_ACQUIRED,
                            payload=AttemptLeaseAcquiredPayload(
                                task_id=task.task_id,
                                lease_id=lease_id,
                                lease_epoch=lease_epoch,
                                worker_id=worker_id,
                                fencing_token=fencing_token,
                                expires_at=expires_at,
                            ),
                            subject_kind=SubjectKind.ATTEMPT,
                            subject_id=attempt_id,
                            parent_subject_id=task.task_id,
                            attempt_id=attempt_id,
                            visibility=Visibility.AUDIT,
                            occurred_at=now_iso,
                            idempotency_key=f"lease-acquired:{lease_id}",
                        ),
                    )
                    if is_new:
                        events.append(event)
                    event, is_new = self._append_event_conn(
                        connection,
                        RuntimeEventDraft(
                            run_id=task.run_id,
                            event_type=EventType.TASK_STATUS_CHANGED,
                            payload=StateChangedPayload(
                                from_state=task.state.value,
                                to_state=TaskState.RUNNING.value,
                                cause=(
                                    "worker claimed new Attempt"
                                    if attempt_started
                                    else "worker resumed active Attempt"
                                ),
                            ),
                            subject_kind=SubjectKind.TASK,
                            subject_id=task.task_id,
                            parent_subject_id=task.parent_task_id,
                            attempt_id=attempt_id,
                            visibility=Visibility.AUDIT,
                            occurred_at=now_iso,
                            idempotency_key=(
                                f"task-status:{task.task_id}:"
                                f"{task.state_version + 1}:running"
                            ),
                        ),
                    )
                    if is_new:
                        events.append(event)
                    claim = TaskClaim(
                        run_id=task.run_id,
                        task_id=task.task_id,
                        attempt_no=attempt_no,
                        attempt_id=attempt_id,
                        lease_id=lease_id,
                        worker_id=worker_id,
                        fencing_token=fencing_token,
                        acquired_at=now_iso,
                        expires_at=expires_at,
                        worker_slot_id=worker_slot_id,
                        claim_request_id=claim_request_id,
                    )
                    connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._notify(events)
        return claim

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            return self._get_run_conn(connection, run_id)

    def get_run_policy(self, run_id: str) -> RunPolicy:
        with self._connect() as connection:
            self._get_run_conn(connection, run_id)
            return self._get_run_policy_conn(connection, run_id)

    def get_task(self, task_id: str) -> TaskRecord:
        with self._connect() as connection:
            return self._get_task_conn(connection, task_id)

    def get_task_retry_policy(self, task_id: str) -> RetryPolicy:
        with self._connect() as connection:
            self._get_task_conn(connection, task_id)
            return self._get_task_retry_policy_conn(connection, task_id)

    def list_tasks(self, run_id: str) -> list[TaskRecord]:
        with self._connect() as connection:
            self._get_run_conn(connection, run_id)
            return self._list_tasks_conn(connection, run_id)

    def get_attempt(self, attempt_id: str) -> TaskAttemptRecord:
        with self._connect() as connection:
            return self._get_attempt_conn(connection, attempt_id)

    def list_attempts(
        self,
        task_id: str | None = None,
        *,
        run_id: str | None = None,
    ) -> list[TaskAttemptRecord]:
        if task_id is None and run_id is None:
            raise RuntimeValidationError(
                "list_attempts requires task_id or run_id"
            )
        clauses: list[str] = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id=?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_task_attempts
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY task_id ASC, attempt_no ASC",
                params,
            ).fetchall()
            return [self._row_to_attempt(row) for row in rows]

    def get_active_attempt(
        self, task_id: str
    ) -> TaskAttemptRecord | None:
        with self._connect() as connection:
            execution = self._task_execution_row_conn(connection, task_id)
            if execution["active_attempt_id"] is None:
                return None
            return self._get_attempt_conn(
                connection, execution["active_attempt_id"]
            )

    def get_lease(self, lease_id: str) -> TaskLeaseRecord:
        with self._connect() as connection:
            return self._get_lease_conn(connection, lease_id)

    def list_leases(
        self,
        attempt_id: str | None = None,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[TaskLeaseRecord]:
        if attempt_id is None and task_id is None and run_id is None:
            raise RuntimeValidationError(
                "list_leases requires attempt_id, task_id, or run_id"
            )
        clauses: list[str] = []
        params: list[Any] = []
        if attempt_id is not None:
            clauses.append("attempt_id=?")
            params.append(attempt_id)
        if task_id is not None:
            clauses.append("task_id=?")
            params.append(task_id)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_task_leases
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY task_id ASC, fencing_token ASC",
                params,
            ).fetchall()
            return [self._row_to_lease(row) for row in rows]

    def get_active_lease(self, task_id: str) -> TaskLeaseRecord | None:
        with self._connect() as connection:
            execution = self._task_execution_row_conn(connection, task_id)
            if execution["active_lease_id"] is None:
                return None
            return self._get_lease_conn(
                connection, execution["active_lease_id"]
            )

    def validate_task_claim(
        self,
        claim: TaskClaim,
        *,
        now: str | datetime | None = None,
    ) -> TaskLeaseRecord:
        (
            task_id,
            attempt_id,
            lease_id,
            worker_id,
            fencing_token,
        ) = self._claim_identity(claim)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_iso = _utc_iso(self._command_now(now))
                task, attempt, lease = self._validate_authority_conn(
                    connection,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    lease_id=lease_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    now=now_iso,
                )
                if (
                    task.run_id != claim.run_id
                    or attempt.attempt_no != claim.attempt_no
                    or lease.acquired_at != claim.acquired_at
                    or (
                        claim.worker_slot_id is not None
                        and lease.worker_slot_id
                        != claim.worker_slot_id
                    )
                    or (
                        claim.claim_request_id is not None
                        and lease.claim_request_id
                        != claim.claim_request_id
                    )
                ):
                    raise LeaseLost(
                        "TaskClaim metadata does not match the authoritative "
                        "lease"
                    )
                connection.commit()
                return lease
            except Exception:
                connection.rollback()
                raise

    def validate_task_fence(
        self,
        task_id: str,
        attempt_id: str,
        fencing_token: int,
        *,
        lease_id: str | None = None,
        worker_id: str | None = None,
        now: str | datetime | None = None,
    ) -> TaskLeaseRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_iso = _utc_iso(self._command_now(now))
                execution = self._task_execution_row_conn(
                    connection, task_id
                )
                authoritative_lease_id = execution["active_lease_id"]
                if lease_id is None:
                    lease_id = authoritative_lease_id
                if lease_id is None:
                    raise LeaseLost("Task has no authoritative lease")
                lease = self._get_lease_conn(connection, lease_id)
                if worker_id is None:
                    worker_id = lease.worker_id
                _, _, validated = self._validate_authority_conn(
                    connection,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    lease_id=lease_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    now=now_iso,
                )
                connection.commit()
                return validated
            except Exception:
                connection.rollback()
                raise

    def validate_fence(
        self,
        task_id: str,
        attempt_id: str,
        fencing_token: int,
        *,
        lease_id: str | None = None,
        worker_id: str | None = None,
        now: str | datetime | None = None,
    ) -> TaskLeaseRecord:
        """Alias used by fenced Artifact integrations."""

        return self.validate_task_fence(
            task_id=task_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            lease_id=lease_id,
            worker_id=worker_id,
            now=now,
        )

    def counts(self, run_id: str) -> WorkflowCounts:
        return workflow_counts(self.list_tasks(run_id))

    def find_active_run(
        self, *, owner_id: str, session_id: str, file_id: str
    ) -> RunRecord | None:
        with self._connect() as connection:
            row = self._find_active_run_row(
                connection,
                owner_id=owner_id,
                session_id=session_id,
                file_id=file_id,
            )
            return self._row_to_run(row) if row is not None else None

    def find_active_run_by_session(
        self, *, session_id: str
    ) -> RunRecord | None:
        """Return the newest non-terminal Run bound to a Web session.

        This server-side lookup is intentionally not owner-scoped: callers use
        the returned owner only to *verify* a Socket room claim after a process
        restart.  It must never be exposed directly to an untrusted client.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise RuntimeValidationError("session_id is required")
        active = tuple(
            state.value for state in RunState
            if state not in RUN_TERMINAL_STATES
        )
        placeholders = ",".join("?" for _ in active)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM runtime_runs
                WHERE session_id=? AND state IN ({placeholders})
                ORDER BY created_at DESC, run_id DESC
                LIMIT 1
                """,
                (session_id.strip(), *active),
            ).fetchone()
            return self._row_to_run(row) if row is not None else None

    def find_run_by_idempotency(
        self, *, owner_id: str, idempotency_key: str
    ) -> RunRecord | None:
        """Look up a previously accepted owner-scoped command."""

        if not isinstance(owner_id, str) or not owner_id.strip():
            raise RuntimeValidationError("owner_id is required")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
        ):
            raise RuntimeValidationError("idempotency_key is required")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runtime_runs
                WHERE owner_id=? AND idempotency_key=?
                LIMIT 1
                """,
                (owner_id.strip(), idempotency_key.strip()),
            ).fetchone()
            return self._row_to_run(row) if row is not None else None

    def get_event(self, event_id: str) -> RuntimeEvent:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Event not found: {event_id}")
            return self.codec.event_from_row(row)

    def read_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        until_seq: int | None = None,
        limit: int = 500,
    ) -> list[RuntimeEvent]:
        if after_seq < 0:
            raise RuntimeValidationError("after_seq must be >= 0")
        if limit < 1 or limit > 5000:
            raise RuntimeValidationError("limit must be between 1 and 5000")
        query = "SELECT * FROM runtime_events WHERE run_id=? AND seq>?"
        params: list[Any] = [run_id, after_seq]
        if until_seq is not None:
            query += " AND seq<=?"
            params.append(until_seq)
        query += " ORDER BY seq ASC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            self._get_run_conn(connection, run_id)
            rows = connection.execute(query, params).fetchall()
            return [self.codec.event_from_row(row) for row in rows]

    def high_watermark(self, run_id: str) -> int:
        return self.get_run(run_id).last_seq

    def cancellation_request_for(
        self,
        run_id: str,
        *,
        subject_kind: SubjectKind,
        subject_id: str,
    ) -> CancelRequestedPayload | None:
        if subject_kind is SubjectKind.RUN:
            event_type = EventType.RUN_CANCEL_REQUESTED
        elif subject_kind is SubjectKind.TASK:
            event_type = EventType.TASK_CANCEL_REQUESTED
        else:
            raise RuntimeValidationError(
                "cancellation requests apply only to Run or Task subjects"
            )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runtime_events
                WHERE run_id=? AND event_type=? AND subject_kind=?
                  AND subject_id=?
                ORDER BY seq DESC
                LIMIT 1
                """,
                (
                    run_id,
                    event_type.value,
                    subject_kind.value,
                    subject_id,
                ),
            ).fetchone()
        if row is None:
            return None
        event = self.codec.event_from_row(row)
        if not isinstance(event.payload, CancelRequestedPayload):
            raise RuntimeValidationError(
                "persisted cancellation event has the wrong payload type"
            )
        return event.payload

    def create_artifact_metadata(
        self,
        *,
        run_id: str,
        artifact_id: str,
        task_id: str | None,
        kind: str,
        relative_path: str,
        display_name: str,
        mime_type: str,
    ) -> Mapping[str, Any]:
        self._validate_artifact_metadata(
            run_id=run_id,
            artifact_id=artifact_id,
            kind=kind,
            relative_path=relative_path,
            display_name=display_name,
            mime_type=mime_type,
        )
        event: RuntimeEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._get_run_conn(connection, run_id)
                if run.state is RunState.CANCELLING or run.is_terminal:
                    raise RuntimeValidationError(
                        f"cannot create an Artifact while Run is {run.state.value}"
                    )
                if task_id is not None:
                    task = self._get_task_conn(connection, task_id)
                    if task.run_id != run_id:
                        raise RuntimeValidationError(
                            "Artifact Task does not belong to Run"
                        )
                    execution = self._task_execution_row_conn(
                        connection, task_id
                    )
                    attempt_exists = connection.execute(
                        """
                        SELECT 1
                        FROM runtime_task_attempts
                        WHERE task_id=?
                        LIMIT 1
                        """,
                        (task_id,),
                    ).fetchone()
                    if (
                        bool(execution["attempt_managed"])
                        or execution["active_attempt_id"] is not None
                        or attempt_exists is not None
                    ):
                        raise AttemptConflict(
                            "Task is Attempt-managed; use "
                            "create_artifact_metadata_for_claim()"
                        )
                now = utc_now_iso()
                connection.execute(
                    """
                    INSERT INTO runtime_artifacts(
                        run_id, artifact_id, task_id, state, kind, relative_path,
                        display_name, mime_type, created_at
                    ) VALUES (?, ?, ?, 'staging', ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        artifact_id,
                        task_id,
                        kind,
                        relative_path,
                        display_name,
                        mime_type,
                        now,
                    ),
                )
                draft = RuntimeEventDraft(
                    run_id=run_id,
                    event_type=EventType.ARTIFACT_STAGED,
                    payload=ArtifactPayload(
                        artifact_id=artifact_id,
                        kind=kind,
                        display_name=display_name,
                        state="staging",
                    ),
                    subject_kind=SubjectKind.ARTIFACT,
                    subject_id=artifact_id,
                    parent_subject_id=task_id,
                    visibility=Visibility.INTERNAL,
                    idempotency_key=f"artifact-staged:{artifact_id}",
                )
                event, _ = self._append_event_conn(connection, draft)
                connection.execute(
                    """
                    UPDATE runtime_artifacts SET source_event_id=?
                    WHERE run_id=? AND artifact_id=?
                    """,
                    (event.event_id, run_id, artifact_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            row = self._get_artifact_conn(connection, run_id, artifact_id)
        if event is not None:
            self._notify([event])
        return dict(row)

    def create_artifact_metadata_for_claim(
        self,
        claim: TaskClaim,
        *,
        artifact_id: str,
        kind: str,
        relative_path: str,
        display_name: str,
        mime_type: str,
        run_id: str | None = None,
        task_id: str | None = None,
        now: str | datetime | None = None,
    ) -> Mapping[str, Any]:
        """Create claim-bound staging metadata under the current Task fence."""

        self._claim_identity(claim)
        resolved_run_id = claim.run_id if run_id is None else run_id
        resolved_task_id = claim.task_id if task_id is None else task_id
        if (
            resolved_run_id != claim.run_id
            or resolved_task_id != claim.task_id
        ):
            raise LeaseLost(
                "Artifact identity does not match the supplied TaskClaim"
            )
        self._validate_artifact_metadata(
            run_id=resolved_run_id,
            artifact_id=artifact_id,
            kind=kind,
            relative_path=relative_path,
            display_name=display_name,
            mime_type=mime_type,
        )
        event: RuntimeEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_iso = _utc_iso(self._command_now(now))
                task, _, _ = self._validate_artifact_claim_conn(
                    connection,
                    claim,
                    now=now_iso,
                    run_id=resolved_run_id,
                    task_id=resolved_task_id,
                )
                run = self._get_run_conn(connection, resolved_run_id)
                if run.state is RunState.CANCELLING or run.is_terminal:
                    raise RuntimeValidationError(
                        f"cannot create an Artifact while Run is {run.state.value}"
                    )
                existing = connection.execute(
                    """
                    SELECT * FROM runtime_artifacts
                    WHERE run_id=? AND artifact_id=?
                    """,
                    (resolved_run_id, artifact_id),
                ).fetchone()
                if existing is not None:
                    expected = {
                        "task_id": resolved_task_id,
                        "attempt_id": claim.attempt_id,
                        "lease_id": claim.lease_id,
                        "fencing_token": claim.fencing_token,
                        "kind": kind,
                        "relative_path": relative_path,
                        "display_name": display_name,
                        "mime_type": mime_type,
                    }
                    if any(
                        existing[column] != value
                        for column, value in expected.items()
                    ):
                        raise IdempotencyConflict(
                            "Artifact staging metadata differs from the "
                            "committed claim-bound value"
                        )
                    connection.commit()
                    return dict(existing)

                connection.execute(
                    """
                    INSERT INTO runtime_artifacts(
                        run_id, artifact_id, task_id, state, kind,
                        relative_path, display_name, mime_type, created_at,
                        attempt_id, lease_id, fencing_token
                    ) VALUES (
                        ?, ?, ?, 'staging', ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        resolved_run_id,
                        artifact_id,
                        resolved_task_id,
                        kind,
                        relative_path,
                        display_name,
                        mime_type,
                        now_iso,
                        claim.attempt_id,
                        claim.lease_id,
                        claim.fencing_token,
                    ),
                )
                draft = RuntimeEventDraft(
                    run_id=resolved_run_id,
                    event_type=EventType.ARTIFACT_STAGED,
                    payload=ArtifactPayload(
                        artifact_id=artifact_id,
                        kind=kind,
                        display_name=display_name,
                        state="staging",
                    ),
                    subject_kind=SubjectKind.ARTIFACT,
                    subject_id=artifact_id,
                    parent_subject_id=task.task_id,
                    attempt_id=claim.attempt_id,
                    visibility=Visibility.INTERNAL,
                    occurred_at=now_iso,
                    idempotency_key=f"artifact-staged:{artifact_id}",
                )
                event, _ = self._append_event_conn(connection, draft)
                connection.execute(
                    """
                    UPDATE runtime_artifacts SET source_event_id=?
                    WHERE run_id=? AND artifact_id=?
                    """,
                    (event.event_id, resolved_run_id, artifact_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            row = self._get_artifact_conn(
                connection, resolved_run_id, artifact_id
            )
        if event is not None:
            self._notify([event])
        return dict(row)

    def mark_artifact_ready(
        self,
        *,
        run_id: str,
        artifact_id: str,
        byte_size: int,
        sha256: str,
    ) -> Mapping[str, Any]:
        if (
            not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or byte_size < 0
        ):
            raise RuntimeValidationError("Artifact byte_size must be >= 0")
        if not isinstance(sha256, str) or _SHA256_HEX.fullmatch(sha256) is None:
            raise RuntimeValidationError(
                "Artifact sha256 must be 64 lowercase hexadecimal characters"
            )
        event: RuntimeEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_artifact_conn(connection, run_id, artifact_id)
                self._assert_legacy_artifact_write_allowed_conn(
                    connection, row
                )
                if row["state"] == "ready":
                    if int(row["byte_size"]) != byte_size or row["sha256"] != sha256:
                        raise IdempotencyConflict(
                            "Artifact ready metadata differs from committed value"
                        )
                    connection.commit()
                    return dict(row)
                if row["state"] != "staging":
                    raise RuntimeValidationError(
                        f"Artifact cannot become ready from {row['state']}"
                    )
                now = utc_now_iso()
                connection.execute(
                    """
                    UPDATE runtime_artifacts
                    SET state='ready', byte_size=?, sha256=?, ready_at=?, error_json=NULL
                    WHERE run_id=? AND artifact_id=? AND state='staging'
                    """,
                    (byte_size, sha256, now, run_id, artifact_id),
                )
                draft = RuntimeEventDraft(
                    run_id=run_id,
                    event_type=EventType.ARTIFACT_READY,
                    payload=ArtifactPayload(
                        artifact_id=artifact_id,
                        kind=row["kind"],
                        display_name=row["display_name"],
                        state="ready",
                        byte_size=byte_size,
                        sha256=sha256,
                    ),
                    subject_kind=SubjectKind.ARTIFACT,
                    subject_id=artifact_id,
                    parent_subject_id=row["task_id"],
                    visibility=Visibility.USER,
                    idempotency_key=f"artifact-ready:{artifact_id}",
                )
                event, _ = self._append_event_conn(connection, draft)
                connection.execute(
                    """
                    UPDATE runtime_artifacts SET source_event_id=?
                    WHERE run_id=? AND artifact_id=?
                    """,
                    (event.event_id, run_id, artifact_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_artifact_conn(connection, run_id, artifact_id)
        if event is not None:
            self._notify([event])
        return dict(saved)

    def mark_artifact_ready_for_claim(
        self,
        claim: TaskClaim,
        *,
        artifact_id: str,
        byte_size: int,
        sha256: str,
        run_id: str | None = None,
        now: str | datetime | None = None,
    ) -> Mapping[str, Any]:
        """Publish a claim-bound Artifact only under its current fence."""

        self._claim_identity(claim)
        resolved_run_id = claim.run_id if run_id is None else run_id
        if resolved_run_id != claim.run_id:
            raise LeaseLost(
                "Artifact Run does not match the supplied TaskClaim"
            )
        if (
            not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or byte_size < 0
        ):
            raise RuntimeValidationError("Artifact byte_size must be >= 0")
        if not isinstance(sha256, str) or _SHA256_HEX.fullmatch(sha256) is None:
            raise RuntimeValidationError(
                "Artifact sha256 must be 64 lowercase hexadecimal characters"
            )
        event: RuntimeEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_iso = _utc_iso(self._command_now(now))
                row = self._get_artifact_conn(
                    connection, resolved_run_id, artifact_id
                )
                self._validate_artifact_claim_conn(
                    connection,
                    claim,
                    now=now_iso,
                    run_id=resolved_run_id,
                    task_id=row["task_id"],
                    artifact=row,
                )
                if row["state"] == "ready":
                    if (
                        int(row["byte_size"]) != byte_size
                        or row["sha256"] != sha256
                    ):
                        raise IdempotencyConflict(
                            "Artifact ready metadata differs from committed value"
                        )
                    connection.commit()
                    return dict(row)
                if row["state"] != "staging":
                    raise RuntimeValidationError(
                        f"Artifact cannot become ready from {row['state']}"
                    )
                cursor = connection.execute(
                    """
                    UPDATE runtime_artifacts
                    SET state='ready', byte_size=?, sha256=?, ready_at=?,
                        error_json=NULL
                    WHERE run_id=? AND artifact_id=? AND state='staging'
                      AND task_id=? AND attempt_id=? AND lease_id=?
                      AND fencing_token=?
                    """,
                    (
                        byte_size,
                        sha256,
                        now_iso,
                        resolved_run_id,
                        artifact_id,
                        claim.task_id,
                        claim.attempt_id,
                        claim.lease_id,
                        claim.fencing_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseLost(
                        "Artifact fence changed during ready commit"
                    )
                draft = RuntimeEventDraft(
                    run_id=resolved_run_id,
                    event_type=EventType.ARTIFACT_READY,
                    payload=ArtifactPayload(
                        artifact_id=artifact_id,
                        kind=row["kind"],
                        display_name=row["display_name"],
                        state="ready",
                        byte_size=byte_size,
                        sha256=sha256,
                    ),
                    subject_kind=SubjectKind.ARTIFACT,
                    subject_id=artifact_id,
                    parent_subject_id=claim.task_id,
                    attempt_id=claim.attempt_id,
                    visibility=Visibility.USER,
                    occurred_at=now_iso,
                    idempotency_key=f"artifact-ready:{artifact_id}",
                )
                event, _ = self._append_event_conn(connection, draft)
                connection.execute(
                    """
                    UPDATE runtime_artifacts SET source_event_id=?
                    WHERE run_id=? AND artifact_id=?
                    """,
                    (event.event_id, resolved_run_id, artifact_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_artifact_conn(
                connection, resolved_run_id, artifact_id
            )
        if event is not None:
            self._notify([event])
        return dict(saved)

    def mark_artifact_failed(
        self,
        *,
        run_id: str,
        artifact_id: str,
        error: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._mark_unbound_artifact_failed(
            run_id=run_id,
            artifact_id=artifact_id,
            error=error,
            management_recovery=False,
        )

    def _mark_unbound_artifact_failed_for_recovery(
        self,
        *,
        run_id: str,
        artifact_id: str,
        error: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Internal compatibility path used only under Artifact root recovery."""

        return self._mark_unbound_artifact_failed(
            run_id=run_id,
            artifact_id=artifact_id,
            error=error,
            management_recovery=True,
        )

    def _mark_unbound_artifact_failed(
        self,
        *,
        run_id: str,
        artifact_id: str,
        error: Mapping[str, Any],
        management_recovery: bool,
    ) -> Mapping[str, Any]:
        if not isinstance(error, Mapping) or not error:
            raise RuntimeValidationError(
                "Failed Artifact requires a non-empty error object"
            )
        error_json = _json_dumps(error)
        event: RuntimeEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_artifact_conn(connection, run_id, artifact_id)
                if management_recovery:
                    self._assert_unbound_artifact_conn(row)
                else:
                    self._assert_legacy_artifact_write_allowed_conn(
                        connection, row
                    )
                if row["state"] == "ready":
                    raise RuntimeValidationError("Ready Artifact is immutable")
                if row["state"] == "failed":
                    if row["error_json"] != error_json:
                        raise IdempotencyConflict(
                            "Artifact failure metadata differs from committed value"
                        )
                    connection.commit()
                    return dict(row)
                connection.execute(
                    """
                    UPDATE runtime_artifacts
                    SET state='failed', error_json=?
                    WHERE run_id=? AND artifact_id=?
                    """,
                    (error_json, run_id, artifact_id),
                )
                draft = RuntimeEventDraft(
                    run_id=run_id,
                    event_type=EventType.ARTIFACT_FAILED,
                    payload=ArtifactPayload(
                        artifact_id=artifact_id,
                        kind=row["kind"],
                        display_name=row["display_name"],
                        state="failed",
                        error=error,
                    ),
                    subject_kind=SubjectKind.ARTIFACT,
                    subject_id=artifact_id,
                    parent_subject_id=row["task_id"],
                    visibility=Visibility.AUDIT,
                    idempotency_key=f"artifact-failed:{artifact_id}",
                )
                event, _ = self._append_event_conn(connection, draft)
                connection.execute(
                    """
                    UPDATE runtime_artifacts SET source_event_id=?
                    WHERE run_id=? AND artifact_id=?
                    """,
                    (event.event_id, run_id, artifact_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_artifact_conn(connection, run_id, artifact_id)
        if event is not None:
            self._notify([event])
        return dict(saved)

    def mark_artifact_failed_for_claim(
        self,
        claim: TaskClaim,
        *,
        artifact_id: str,
        error: Mapping[str, Any],
        run_id: str | None = None,
        now: str | datetime | None = None,
    ) -> Mapping[str, Any]:
        """Fail a claim-bound staging Artifact under its current fence."""

        self._claim_identity(claim)
        resolved_run_id = claim.run_id if run_id is None else run_id
        if resolved_run_id != claim.run_id:
            raise LeaseLost(
                "Artifact Run does not match the supplied TaskClaim"
            )
        if not isinstance(error, Mapping) or not error:
            raise RuntimeValidationError(
                "Failed Artifact requires a non-empty error object"
            )
        error_json = _json_dumps(error)
        event: RuntimeEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_iso = _utc_iso(self._command_now(now))
                row = self._get_artifact_conn(
                    connection, resolved_run_id, artifact_id
                )
                self._validate_artifact_claim_conn(
                    connection,
                    claim,
                    now=now_iso,
                    run_id=resolved_run_id,
                    task_id=row["task_id"],
                    artifact=row,
                )
                if row["state"] == "ready":
                    raise RuntimeValidationError("Ready Artifact is immutable")
                if row["state"] == "failed":
                    if row["error_json"] != error_json:
                        raise IdempotencyConflict(
                            "Artifact failure metadata differs from committed value"
                        )
                    connection.commit()
                    return dict(row)
                cursor = connection.execute(
                    """
                    UPDATE runtime_artifacts
                    SET state='failed', error_json=?
                    WHERE run_id=? AND artifact_id=? AND state='staging'
                      AND task_id=? AND attempt_id=? AND lease_id=?
                      AND fencing_token=?
                    """,
                    (
                        error_json,
                        resolved_run_id,
                        artifact_id,
                        claim.task_id,
                        claim.attempt_id,
                        claim.lease_id,
                        claim.fencing_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseLost(
                        "Artifact fence changed during failure commit"
                    )
                draft = RuntimeEventDraft(
                    run_id=resolved_run_id,
                    event_type=EventType.ARTIFACT_FAILED,
                    payload=ArtifactPayload(
                        artifact_id=artifact_id,
                        kind=row["kind"],
                        display_name=row["display_name"],
                        state="failed",
                        error=error,
                    ),
                    subject_kind=SubjectKind.ARTIFACT,
                    subject_id=artifact_id,
                    parent_subject_id=claim.task_id,
                    attempt_id=claim.attempt_id,
                    visibility=Visibility.AUDIT,
                    occurred_at=now_iso,
                    idempotency_key=f"artifact-failed:{artifact_id}",
                )
                event, _ = self._append_event_conn(connection, draft)
                connection.execute(
                    """
                    UPDATE runtime_artifacts SET source_event_id=?
                    WHERE run_id=? AND artifact_id=?
                    """,
                    (event.event_id, resolved_run_id, artifact_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_artifact_conn(
                connection, resolved_run_id, artifact_id
            )
        if event is not None:
            self._notify([event])
        return dict(saved)

    def mark_stale_artifact_failed(
        self,
        *,
        run_id: str,
        artifact_id: str,
        error: Mapping[str, Any],
        now: str | datetime | None = None,
    ) -> Mapping[str, Any]:
        """Administrative recovery path for a non-authoritative Artifact."""

        if not isinstance(error, Mapping) or not error:
            raise RuntimeValidationError(
                "Failed Artifact requires a non-empty error object"
            )
        error_json = _json_dumps(error)
        event: RuntimeEvent | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_iso = _utc_iso(self._command_now(now))
                row = self._get_artifact_conn(
                    connection, run_id, artifact_id
                )
                bound_values = (
                    row["task_id"],
                    row["attempt_id"],
                    row["lease_id"],
                    row["fencing_token"],
                )
                if any(value is None for value in bound_values):
                    raise RuntimeValidationError(
                        "stale recovery requires a fully claim-bound Artifact"
                    )
                lease = self._get_lease_conn(connection, row["lease_id"])
                try:
                    self._validate_authority_conn(
                        connection,
                        task_id=row["task_id"],
                        attempt_id=row["attempt_id"],
                        lease_id=row["lease_id"],
                        worker_id=lease.worker_id,
                        fencing_token=int(row["fencing_token"]),
                        now=now_iso,
                    )
                except LeaseLost:
                    pass
                else:
                    raise AttemptConflict(
                        "authoritative Artifact must use the claim-bound "
                        "failure API"
                    )
                if row["state"] == "ready":
                    raise RuntimeValidationError("Ready Artifact is immutable")
                if row["state"] == "failed":
                    if row["error_json"] != error_json:
                        raise IdempotencyConflict(
                            "Artifact failure metadata differs from committed value"
                        )
                    connection.commit()
                    return dict(row)
                cursor = connection.execute(
                    """
                    UPDATE runtime_artifacts
                    SET state='failed', error_json=?
                    WHERE run_id=? AND artifact_id=? AND state='staging'
                      AND task_id=? AND attempt_id=? AND lease_id=?
                      AND fencing_token=?
                    """,
                    (
                        error_json,
                        run_id,
                        artifact_id,
                        row["task_id"],
                        row["attempt_id"],
                        row["lease_id"],
                        row["fencing_token"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConcurrencyConflict(
                        "Artifact changed during stale recovery"
                    )
                draft = RuntimeEventDraft(
                    run_id=run_id,
                    event_type=EventType.ARTIFACT_FAILED,
                    payload=ArtifactPayload(
                        artifact_id=artifact_id,
                        kind=row["kind"],
                        display_name=row["display_name"],
                        state="failed",
                        error=error,
                    ),
                    subject_kind=SubjectKind.ARTIFACT,
                    subject_id=artifact_id,
                    parent_subject_id=row["task_id"],
                    attempt_id=row["attempt_id"],
                    visibility=Visibility.AUDIT,
                    occurred_at=now_iso,
                    idempotency_key=f"artifact-failed:{artifact_id}",
                )
                event, _ = self._append_event_conn(connection, draft)
                connection.execute(
                    """
                    UPDATE runtime_artifacts SET source_event_id=?
                    WHERE run_id=? AND artifact_id=?
                    """,
                    (event.event_id, run_id, artifact_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            saved = self._get_artifact_conn(
                connection, run_id, artifact_id
            )
        if event is not None:
            self._notify([event])
        return dict(saved)

    def get_artifact(self, run_id: str, artifact_id: str) -> Mapping[str, Any]:
        with self._connect() as connection:
            return dict(self._get_artifact_conn(connection, run_id, artifact_id))

    def list_staging_artifacts(self) -> list[Mapping[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_artifacts WHERE state='staging'"
            ).fetchall()
            return [dict(row) for row in rows]

    def _validate_artifact_claim_conn(
        self,
        connection: sqlite3.Connection,
        claim: TaskClaim,
        *,
        now: str,
        run_id: str,
        task_id: str,
        artifact: sqlite3.Row | None = None,
    ) -> tuple[TaskRecord, TaskAttemptRecord, TaskLeaseRecord]:
        (
            claim_task_id,
            attempt_id,
            lease_id,
            worker_id,
            fencing_token,
        ) = self._claim_identity(claim)
        if run_id != claim.run_id or task_id != claim_task_id:
            raise LeaseLost(
                "Artifact identity does not match the supplied TaskClaim"
            )
        task, attempt, lease = self._validate_authority_conn(
            connection,
            task_id=claim_task_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            now=now,
        )
        if (
            task.run_id != claim.run_id
            or attempt.attempt_no != claim.attempt_no
            or lease.acquired_at != claim.acquired_at
        ):
            raise LeaseLost(
                "TaskClaim metadata does not match the authoritative lease"
            )
        if artifact is not None and (
            artifact["run_id"] != claim.run_id
            or artifact["task_id"] != claim_task_id
            or artifact["attempt_id"] != attempt_id
            or artifact["lease_id"] != lease_id
            or artifact["fencing_token"] != fencing_token
        ):
            raise LeaseLost(
                "Artifact is not bound to the supplied Task claim/fence"
            )
        return task, attempt, lease

    def _assert_legacy_artifact_write_allowed_conn(
        self,
        connection: sqlite3.Connection,
        artifact: sqlite3.Row,
    ) -> None:
        self._assert_unbound_artifact_conn(artifact)
        task_id = artifact["task_id"]
        if task_id is None:
            return
        execution = self._task_execution_row_conn(connection, task_id)
        attempt_exists = connection.execute(
            """
            SELECT 1
            FROM runtime_task_attempts
            WHERE task_id=?
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if (
            bool(execution["attempt_managed"])
            or
            execution["active_attempt_id"] is not None
            or attempt_exists is not None
        ):
            raise AttemptConflict(
                "Task is Attempt-managed; use a fenced Artifact API"
            )

    @staticmethod
    def _assert_unbound_artifact_conn(artifact: sqlite3.Row) -> None:
        if any(
            artifact[column] is not None
            for column in ("attempt_id", "lease_id", "fencing_token")
        ):
            raise AttemptConflict(
                "Claim-bound Artifact requires a fenced Artifact API"
            )

    @staticmethod
    def _validate_artifact_metadata(
        *,
        run_id: str,
        artifact_id: str,
        kind: str,
        relative_path: str,
        display_name: str,
        mime_type: str,
    ) -> None:
        if (
            not isinstance(run_id, str)
            or not run_id.strip()
            or not isinstance(artifact_id, str)
            or artifact_id in {".", ".."}
            or _SAFE_ARTIFACT_ID.fullmatch(artifact_id) is None
        ):
            raise RuntimeValidationError("unsafe Artifact identity")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (kind, relative_path, display_name, mime_type)
        ):
            raise RuntimeValidationError(
                "Artifact kind, path, display name, and MIME type are required"
            )
        if (
            "\\" in relative_path
            or ":" in relative_path
            or "\x00" in relative_path
        ):
            raise RuntimeValidationError("unsafe Artifact relative_path")
        path = PurePosixPath(relative_path)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise RuntimeValidationError("unsafe Artifact relative_path")
        expected_prefix = ("runs", run_id, "artifacts", artifact_id)
        if path.parts[:4] != expected_prefix or len(path.parts) != 5:
            raise RuntimeValidationError(
                "Artifact path must remain in its Run/Artifact namespace"
            )
        if (
            path.name != display_name
            or "/" in display_name
            or "\\" in display_name
            or ":" in display_name
            or "\x00" in display_name
            or display_name in {".", ".."}
        ):
            raise RuntimeValidationError("unsafe Artifact display_name")

    def _validate_run_target(
        self,
        current: RunRecord,
        target: RunState,
        tasks: list[TaskRecord],
        error: Mapping[str, Any] | None,
        metadata: Mapping[str, Any],
    ) -> None:
        if target is RunState.FINALIZING:
            if any(not task.is_terminal for task in tasks):
                raise RuntimeValidationError(
                    "Run cannot finalize while Tasks are active"
                )

        if target in RUN_TERMINAL_STATES:
            if any(not task.is_terminal for task in tasks):
                raise RuntimeValidationError(
                    "Run cannot enter a terminal state while Tasks are active"
                )
            root = next(
                (task for task in tasks if task.task_id == current.root_task_id), None
            )
            if target in {RunState.SUCCEEDED, RunState.PARTIAL}:
                if root is None or root.state is not TaskState.SUCCEEDED:
                    raise RuntimeValidationError(
                        "Successful Run requires a succeeded root Task"
                    )
            if target is RunState.SUCCEEDED:
                if any(task.state is not TaskState.SUCCEEDED for task in tasks):
                    raise RuntimeValidationError(
                        "SUCCEEDED requires every Task to succeed"
                    )
            if target is RunState.PARTIAL:
                if any(
                    task.required and task.state is not TaskState.SUCCEEDED
                    for task in tasks
                ):
                    raise RuntimeValidationError(
                        "PARTIAL cannot hide a required Task failure"
                    )
                limitations = metadata.get("limitations")
                if not isinstance(limitations, list) or not limitations:
                    raise RuntimeValidationError(
                        "PARTIAL requires explicit limitations metadata"
                    )
            if target is RunState.FAILED and error is None:
                raise RuntimeValidationError("FAILED Run requires structured error")

    def _validate_task_target(
        self,
        current: TaskRecord,
        target: TaskState,
        children: list[TaskRecord],
        result: Mapping[str, Any] | None,
        error: Mapping[str, Any] | None,
    ) -> None:
        if target in TASK_TERMINAL_STATES and any(
            not child.is_terminal for child in children
        ):
            raise RuntimeValidationError(
                "Task cannot become terminal while children are active"
            )
        if target is TaskState.SUCCEEDED:
            if result is None:
                raise RuntimeValidationError(
                    "SUCCEEDED Task requires a non-empty result object"
                )
            if any(
                child.required and child.state is not TaskState.SUCCEEDED
                for child in children
            ):
                raise RuntimeValidationError(
                    "Task cannot succeed while a required child did not succeed"
                )
        if target is TaskState.FAILED and error is None:
            raise RuntimeValidationError("FAILED Task requires structured error")
        if target is TaskState.SKIPPED and current.started_at is not None:
            raise RuntimeValidationError("A started Task cannot be SKIPPED")

    @staticmethod
    def _validate_task_run_target(
        run: RunRecord, current: TaskRecord, target: TaskState
    ) -> None:
        if run.is_terminal:
            raise RuntimeValidationError(
                f"Task cannot transition while Run is {run.state.value}"
            )
        if target in {
            TaskState.BLOCKED,
            TaskState.READY,
            TaskState.SKIPPED,
        }:
            allowed = {
                RunState.CREATED,
                RunState.QUEUED,
                RunState.RUNNING,
                RunState.WAITING,
            }
        elif target in {
            TaskState.RUNNING,
            TaskState.WAITING,
            TaskState.SUCCEEDED,
        }:
            allowed = {RunState.RUNNING, RunState.WAITING}
        elif target is TaskState.FAILED:
            allowed = {
                RunState.QUEUED,
                RunState.RUNNING,
                RunState.WAITING,
            }
            if current.state is TaskState.CANCELLING:
                allowed = {RunState.CANCELLING}
        else:
            # Cancellation states are handled only by the atomic cancellation
            # commands and never enter this public transition path.
            allowed = set()
        if run.state not in allowed:
            raise RuntimeValidationError(
                f"Task cannot transition to {target.value} while Run is "
                f"{run.state.value}"
            )

    @staticmethod
    def _retry_delay_seconds(
        policy: RetryPolicy,
        *,
        run_id: str,
        task_id: str,
        failed_attempt_no: int,
    ) -> float:
        delay = policy.base_backoff_seconds(failed_attempt_no)
        if delay <= 0 or policy.jitter_ratio <= 0:
            return float(delay)
        digest = hashlib.sha256(
            (
                f"{run_id}\0{task_id}\0{failed_attempt_no}\0"
                "pathcopilot-retry-jitter-v1"
            ).encode("utf-8")
        ).digest()
        unit = int.from_bytes(digest[:8], "big") / float((1 << 64) - 1)
        factor = 1.0 + ((2.0 * unit) - 1.0) * policy.jitter_ratio
        delay = max(0.0, delay * factor)
        if policy.max_backoff_seconds > 0:
            delay = min(delay, policy.max_backoff_seconds)
        return float(delay)

    def _schedule_retry_conn(
        self,
        connection: sqlite3.Connection,
        *,
        task: TaskRecord,
        attempt: TaskAttemptRecord,
        lease_id: str,
        fencing_token: int,
        error: Mapping[str, Any],
        now_dt: datetime,
        cause: str,
        reason: str,
    ) -> list[RuntimeEvent]:
        now_iso = _utc_iso(now_dt)
        delay = self._retry_delay_seconds(
            attempt.retry_policy,
            run_id=task.run_id,
            task_id=task.task_id,
            failed_attempt_no=attempt.attempt_no,
        )
        retry_at = _future_iso(now_dt, delay)
        cursor = connection.execute(
            """
            UPDATE runtime_tasks
            SET state='waiting',
                state_version=state_version+1,
                wait_reason='retry_backoff',
                result_json=NULL,
                error_json=?,
                updated_at=?,
                ended_at=NULL,
                active_attempt_id=NULL,
                active_lease_id=NULL,
                next_retry_at=?,
                cancel_deadline_at=NULL
            WHERE task_id=? AND state='running'
              AND state_version=?
              AND active_attempt_id=?
              AND active_lease_id=?
              AND fencing_token=?
            """,
            (
                _json_dumps(error),
                now_iso,
                retry_at,
                task.task_id,
                task.state_version,
                attempt.attempt_id,
                lease_id,
                fencing_token,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseLost("Task fence changed during retry scheduling")
        events: list[RuntimeEvent] = []
        for draft in (
            RuntimeEventDraft(
                run_id=task.run_id,
                event_type=EventType.TASK_STATUS_CHANGED,
                payload=StateChangedPayload(
                    from_state=TaskState.RUNNING.value,
                    to_state=TaskState.WAITING.value,
                    cause=cause,
                    wait_reason=WaitReason.RETRY_BACKOFF.value,
                ),
                subject_kind=SubjectKind.TASK,
                subject_id=task.task_id,
                parent_subject_id=task.parent_task_id,
                attempt_id=attempt.attempt_id,
                visibility=Visibility.AUDIT,
                occurred_at=now_iso,
                idempotency_key=(
                    f"task-status:{task.task_id}:"
                    f"{task.state_version + 1}:waiting"
                ),
            ),
            RuntimeEventDraft(
                run_id=task.run_id,
                event_type=EventType.TASK_RETRY_SCHEDULED,
                payload=TaskRetryScheduledPayload(
                    task_id=task.task_id,
                    failed_attempt_id=attempt.attempt_id,
                    failed_attempt_no=attempt.attempt_no,
                    next_attempt_no=attempt.attempt_no + 1,
                    retry_at=retry_at,
                    delay_seconds=delay,
                    reason=reason,
                ),
                subject_kind=SubjectKind.TASK,
                subject_id=task.task_id,
                parent_subject_id=task.parent_task_id,
                attempt_id=attempt.attempt_id,
                visibility=Visibility.AUDIT,
                occurred_at=now_iso,
                idempotency_key=(
                    f"task-retry-scheduled:{attempt.attempt_id}"
                ),
            ),
        ):
            event, is_new = self._append_event_conn(connection, draft)
            if is_new:
                events.append(event)
        return events

    def _promote_due_retries_conn(
        self,
        connection: sqlite3.Connection,
        *,
        now: str,
        run_id: str | None = None,
        limit: int = 1000,
        promoted_task_ids: list[str] | None = None,
    ) -> list[RuntimeEvent]:
        clauses = [
            "task.state='waiting'",
            "task.wait_reason='retry_backoff'",
            "task.next_retry_at IS NOT NULL",
            "task.next_retry_at<=?",
            "task.active_attempt_id IS NULL",
            "task.active_lease_id IS NULL",
            "task.cancel_requested_at IS NULL",
            "run.state IN ('running', 'waiting')",
            "run.cancel_requested_at IS NULL",
        ]
        params: list[Any] = [now]
        if run_id is not None:
            clauses.append("task.run_id=?")
            params.append(run_id)
        params.append(limit)
        rows = connection.execute(
            """
            SELECT task.*
            FROM runtime_tasks task
            JOIN runtime_runs run ON run.run_id=task.run_id
            WHERE """
            + " AND ".join(clauses)
            + """
            ORDER BY task.next_retry_at ASC,
                     task.created_at ASC,
                     task.task_id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        events: list[RuntimeEvent] = []
        for row in rows:
            task = self._row_to_task(row)
            cursor = connection.execute(
                """
                UPDATE runtime_tasks
                SET state='ready',
                    state_version=state_version+1,
                    wait_reason=NULL,
                    updated_at=?,
                    next_retry_at=NULL
                WHERE task_id=? AND state='waiting'
                  AND state_version=?
                  AND wait_reason='retry_backoff'
                  AND next_retry_at IS NOT NULL
                  AND next_retry_at<=?
                  AND active_attempt_id IS NULL
                  AND active_lease_id IS NULL
                  AND cancel_requested_at IS NULL
                """,
                (now, task.task_id, task.state_version, now),
            )
            if cursor.rowcount != 1:
                continue
            if promoted_task_ids is not None:
                promoted_task_ids.append(task.task_id)
            event, is_new = self._append_event_conn(
                connection,
                RuntimeEventDraft(
                    run_id=task.run_id,
                    event_type=EventType.TASK_STATUS_CHANGED,
                    payload=StateChangedPayload(
                        from_state=TaskState.WAITING.value,
                        to_state=TaskState.READY.value,
                        cause="retry backoff elapsed",
                    ),
                    subject_kind=SubjectKind.TASK,
                    subject_id=task.task_id,
                    parent_subject_id=task.parent_task_id,
                    visibility=Visibility.AUDIT,
                    occurred_at=now,
                    idempotency_key=(
                        f"task-status:{task.task_id}:"
                        f"{task.state_version + 1}:ready"
                    ),
                ),
            )
            if is_new:
                events.append(event)
        return events

    @staticmethod
    def _row_to_attempt(row: Mapping[str, Any]) -> TaskAttemptRecord:
        return TaskAttemptRecord(
            attempt_id=row["attempt_id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            attempt_no=int(row["attempt_no"]),
            state=AttemptState(row["state"]),
            state_version=int(row["state_version"]),
            retry_policy=_retry_policy_from_value(
                _json_loads(row["retry_policy_json"], {})
            ),
            created_at=row["created_at"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            ended_at=row["ended_at"],
            retryable=(
                None
                if row["retryable"] is None
                else bool(row["retryable"])
            ),
            failure_code=row["failure_code"],
            result=_json_loads(row["result_json"], None),
            error=_json_loads(row["error_json"], None),
        )

    @staticmethod
    def _row_to_lease(row: Mapping[str, Any]) -> TaskLeaseRecord:
        return TaskLeaseRecord(
            lease_id=row["lease_id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            attempt_id=row["attempt_id"],
            lease_epoch=int(row["lease_epoch"]),
            worker_id=row["worker_id"],
            fencing_token=int(row["fencing_token"]),
            acquired_at=row["acquired_at"],
            heartbeat_at=row["heartbeat_at"],
            expires_at=row["expires_at"],
            state=LeaseState(row["state"]),
            state_version=int(row["state_version"]),
            released_at=row["released_at"],
            release_reason=row["release_reason"],
            worker_slot_id=row["worker_slot_id"],
            claim_request_id=row["claim_request_id"],
        )

    def _get_attempt_conn(
        self, connection: sqlite3.Connection, attempt_id: str
    ) -> TaskAttemptRecord:
        row = connection.execute(
            """
            SELECT * FROM runtime_task_attempts
            WHERE attempt_id=?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Attempt not found: {attempt_id}")
        return self._row_to_attempt(row)

    def _get_lease_conn(
        self, connection: sqlite3.Connection, lease_id: str
    ) -> TaskLeaseRecord:
        row = connection.execute(
            """
            SELECT * FROM runtime_task_leases
            WHERE lease_id=?
            """,
            (lease_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Lease not found: {lease_id}")
        return self._row_to_lease(row)

    @staticmethod
    def _get_task_retry_policy_conn(
        connection: sqlite3.Connection, task_id: str
    ) -> RetryPolicy:
        row = connection.execute(
            """
            SELECT * FROM runtime_task_retry_policies
            WHERE task_id=?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise RuntimeValidationError(
                f"Task is missing its immutable RetryPolicy: {task_id}"
            )
        codes = _json_loads(row["retryable_error_codes_json"], [])
        if not isinstance(codes, list):
            raise RuntimeValidationError(
                "persisted retryable_error_codes must be an array"
            )
        return RetryPolicy(
            max_attempts=int(row["max_attempts"]),
            initial_backoff_seconds=float(
                row["initial_backoff_seconds"]
            ),
            max_backoff_seconds=float(row["max_backoff_seconds"]),
            backoff_multiplier=float(row["backoff_multiplier"]),
            jitter_ratio=float(row["jitter_ratio"]),
            retryable_error_codes=tuple(codes),
        )

    @staticmethod
    def _task_execution_row_conn(
        connection: sqlite3.Connection, task_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT active_attempt_id, active_lease_id, fencing_token,
                   next_retry_at, cancel_deadline_at, metadata_json,
                   attempt_managed
            FROM runtime_tasks
            WHERE task_id=?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return row

    def _validate_authority_conn(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        attempt_id: str,
        lease_id: str,
        worker_id: str,
        fencing_token: int,
        now: str,
        allow_revoking: bool = False,
    ) -> tuple[TaskRecord, TaskAttemptRecord, TaskLeaseRecord]:
        task = self._get_task_conn(connection, task_id)
        attempt = self._get_attempt_conn(connection, attempt_id)
        lease = self._get_lease_conn(connection, lease_id)
        execution = self._task_execution_row_conn(connection, task_id)
        allowed_states = (
            {LeaseState.ACTIVE, LeaseState.REVOKING}
            if allow_revoking
            else {LeaseState.ACTIVE}
        )
        if (
            attempt.task_id != task_id
            or lease.task_id != task_id
            or lease.attempt_id != attempt_id
            or lease.worker_id != worker_id
            or lease.fencing_token != fencing_token
            or int(execution["fencing_token"]) != fencing_token
            or execution["active_attempt_id"] != attempt_id
            or execution["active_lease_id"] != lease_id
            or attempt.state is not AttemptState.ACTIVE
            or lease.state not in allowed_states
        ):
            raise LeaseLost(
                "worker no longer owns the authoritative Task lease/fence"
            )
        if now < lease.acquired_at or now < lease.heartbeat_at:
            raise RuntimeValidationError(
                "command time cannot precede the authoritative lease clock"
            )
        if lease.expires_at <= now:
            raise LeaseLost("worker lease has expired")
        return task, attempt, lease

    @staticmethod
    def _claim_identity(
        claim: TaskClaim,
    ) -> tuple[str, str, str, str, int]:
        if not isinstance(claim, TaskClaim):
            raise RuntimeValidationError("a TaskClaim is required")
        return (
            claim.task_id,
            claim.attempt_id,
            claim.lease_id,
            claim.worker_id,
            claim.fencing_token,
        )

    def _get_run_conn(
        self, connection: sqlite3.Connection, run_id: str
    ) -> RunRecord:
        row = connection.execute(
            "SELECT * FROM runtime_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Run not found: {run_id}")
        return self._row_to_run(row)

    def _get_task_conn(
        self, connection: sqlite3.Connection, task_id: str
    ) -> TaskRecord:
        row = connection.execute(
            "SELECT * FROM runtime_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return self._row_to_task(row)

    @staticmethod
    def _get_run_policy_conn(
        connection: sqlite3.Connection, run_id: str
    ) -> RunPolicy:
        row = connection.execute(
            "SELECT * FROM runtime_run_policies WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise RuntimeValidationError(
                f"Run is missing its immutable policy: {run_id}"
            )
        return RunPolicy(
            run_id=row["run_id"],
            max_total_tasks=int(row["max_total_tasks"]),
            max_task_depth=int(row["max_task_depth"]),
            created_at=row["created_at"],
        )

    def _get_artifact_conn(
        self, connection: sqlite3.Connection, run_id: str, artifact_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM runtime_artifacts
            WHERE run_id=? AND artifact_id=?
            """,
            (run_id, artifact_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Artifact not found: {run_id}/{artifact_id}")
        return row

    def _list_tasks_conn(
        self, connection: sqlite3.Connection, run_id: str
    ) -> list[TaskRecord]:
        rows = connection.execute(
            """
            SELECT * FROM runtime_tasks
            WHERE run_id=?
            ORDER BY depth ASC, created_at ASC, task_id ASC
            """,
            (run_id,),
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def _children_conn(
        self, connection: sqlite3.Connection, run_id: str, parent_task_id: str
    ) -> list[TaskRecord]:
        rows = connection.execute(
            """
            SELECT * FROM runtime_tasks
            WHERE run_id=? AND parent_task_id=?
            ORDER BY created_at ASC, task_id ASC
            """,
            (run_id, parent_task_id),
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def _task_subtree_conn(
        self, connection: sqlite3.Connection, run_id: str, task_id: str
    ) -> list[TaskRecord]:
        rows = connection.execute(
            """
            WITH RECURSIVE subtree(task_id) AS (
                SELECT task_id
                FROM runtime_tasks
                WHERE run_id=? AND task_id=?
                UNION ALL
                SELECT child.task_id
                FROM runtime_tasks child
                JOIN subtree parent
                  ON child.parent_task_id=parent.task_id
                WHERE child.run_id=?
            )
            SELECT task.*
            FROM runtime_tasks task
            JOIN subtree ON subtree.task_id=task.task_id
            WHERE task.run_id=?
            ORDER BY task.depth ASC, task.created_at ASC, task.task_id ASC
            """,
            (run_id, task_id, run_id, run_id),
        ).fetchall()
        if not rows:
            raise NotFoundError(f"Task not found: {task_id}")
        return [self._row_to_task(row) for row in rows]

    def _find_active_run_row(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        session_id: str,
        file_id: str,
    ) -> sqlite3.Row | None:
        active = tuple(state.value for state in RunState if state not in RUN_TERMINAL_STATES)
        placeholders = ",".join("?" for _ in active)
        return connection.execute(
            f"""
            SELECT * FROM runtime_runs
            WHERE owner_id=? AND session_id=? AND file_id=?
              AND state IN ({placeholders})
            ORDER BY created_at DESC LIMIT 1
            """,
            (owner_id, session_id, file_id, *active),
        ).fetchone()

    @staticmethod
    def _row_to_run(row: Mapping[str, Any]) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            owner_id=row["owner_id"],
            session_id=row["session_id"],
            file_id=row["file_id"],
            state=RunState(row["state"]),
            state_version=int(row["state_version"]),
            root_task_id=row["root_task_id"],
            idempotency_key=row["idempotency_key"],
            parent_run_id=row["parent_run_id"],
            client_request_id=row["client_request_id"],
            terminal_intent=row["terminal_intent"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            ended_at=row["ended_at"],
            cancel_requested_at=row["cancel_requested_at"],
            config=_json_loads(row["config_json"], {}),
            metadata=_json_loads(row["metadata_json"], {}),
            error=_json_loads(row["error_json"], None),
            last_seq=int(row["last_seq"]),
        )

    @staticmethod
    def _row_to_task(row: Mapping[str, Any]) -> TaskRecord:
        wait_reason = WaitReason(row["wait_reason"]) if row["wait_reason"] else None
        return TaskRecord(
            task_id=row["task_id"],
            run_id=row["run_id"],
            name=row["name"],
            role=row["role"],
            state=TaskState(row["state"]),
            state_version=int(row["state_version"]),
            parent_task_id=row["parent_task_id"],
            continuation_of_task_id=row["continuation_of_task_id"],
            depth=int(row["depth"]),
            required=bool(row["required"]),
            wait_reason=wait_reason,
            attempt=int(row["attempt"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            ended_at=row["ended_at"],
            cancel_requested_at=row["cancel_requested_at"],
            result=_json_loads(row["result_json"], None),
            error=_json_loads(row["error_json"], None),
            metadata=_json_loads(row["metadata_json"], {}),
            spawn_idempotency_key=row["spawn_idempotency_key"],
        )
