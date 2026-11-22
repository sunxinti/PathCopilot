"""Authoritative lifecycle records and enums.

ROI identifiers (for example ``R2-B3-G4``) remain pathology-domain data. They
are never parsed to infer Agent parentage; the task graph uses explicit IDs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional


def utc_now_iso() -> str:
    """Return an RFC3339 UTC timestamp with microsecond precision."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class _LowerValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class RunState(_LowerValueEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    FINALIZING = "finalizing"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskState(_LowerValueEnum):
    CREATED = "created"
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class WaitReason(_LowerValueEnum):
    CHILDREN = "children"
    USER_INPUT = "user_input"
    EXTERNAL_TOOL = "external_tool"
    RESOURCE = "resource"
    RATE_LIMIT = "rate_limit"
    RETRY_BACKOFF = "retry_backoff"


RUN_TERMINAL_STATES = frozenset(
    {
        RunState.SUCCEEDED,
        RunState.PARTIAL,
        RunState.FAILED,
        RunState.CANCELLED,
    }
)
TASK_TERMINAL_STATES = frozenset(
    {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.SKIPPED,
    }
)

RUN_ACTIVE_STATES = frozenset(set(RunState) - set(RUN_TERMINAL_STATES))
TASK_ACTIVE_STATES = frozenset(set(TaskState) - set(TASK_TERMINAL_STATES))


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    owner_id: str
    session_id: str
    file_id: str
    state: RunState = RunState.CREATED
    state_version: int = 0
    root_task_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    parent_run_id: Optional[str] = None
    client_request_id: Optional[str] = None
    terminal_intent: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    updated_at: str = field(default_factory=utc_now_iso)
    ended_at: Optional[str] = None
    cancel_requested_at: Optional[str] = None
    config: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    error: Optional[Mapping[str, Any]] = None
    last_seq: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.state in RUN_TERMINAL_STATES


@dataclass(frozen=True)
class RunPolicy:
    """Immutable task-graph limits captured when a Run is created."""

    run_id: str
    max_total_tasks: int
    max_task_depth: int
    created_at: str


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    run_id: str
    name: str
    role: str
    state: TaskState = TaskState.CREATED
    state_version: int = 0
    parent_task_id: Optional[str] = None
    continuation_of_task_id: Optional[str] = None
    depth: int = 0
    required: bool = True
    wait_reason: Optional[WaitReason] = None
    attempt: int = 0
    created_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    updated_at: str = field(default_factory=utc_now_iso)
    ended_at: Optional[str] = None
    cancel_requested_at: Optional[str] = None
    result: Optional[Mapping[str, Any]] = None
    error: Optional[Mapping[str, Any]] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    spawn_idempotency_key: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TASK_TERMINAL_STATES


@dataclass(frozen=True)
class WorkflowCounts:
    """Counts derived from Task snapshots; never maintained independently."""

    total: int
    active: int
    executing: int
    waiting: int
    queued: int
    completed: int
    failed: int
    cancelled: int
    skipped: int
    terminal: int

    def to_dict(self) -> Dict[str, int]:
        return {
            "total": self.total,
            "active": self.active,
            "executing": self.executing,
            "waiting": self.waiting,
            "queued": self.queued,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "skipped": self.skipped,
            "terminal": self.terminal,
        }


def workflow_counts(tasks: list[TaskRecord]) -> WorkflowCounts:
    counts = {state: 0 for state in TaskState}
    for task in tasks:
        counts[task.state] += 1

    completed = counts[TaskState.SUCCEEDED]
    failed = counts[TaskState.FAILED]
    cancelled = counts[TaskState.CANCELLED]
    skipped = counts[TaskState.SKIPPED]
    terminal = completed + failed + cancelled + skipped
    return WorkflowCounts(
        total=len(tasks),
        active=(
            counts[TaskState.RUNNING]
            + counts[TaskState.WAITING]
            + counts[TaskState.CANCELLING]
        ),
        executing=counts[TaskState.RUNNING],
        waiting=counts[TaskState.WAITING],
        queued=(
            counts[TaskState.CREATED]
            + counts[TaskState.BLOCKED]
            + counts[TaskState.READY]
        ),
        completed=completed,
        failed=failed,
        cancelled=cancelled,
        skipped=skipped,
        terminal=terminal,
    )
