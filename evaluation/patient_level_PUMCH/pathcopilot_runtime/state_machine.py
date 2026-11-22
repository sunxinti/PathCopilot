"""Explicit and closed lifecycle state machines."""

from __future__ import annotations

from typing import Dict, FrozenSet

from .errors import IllegalTransition, RuntimeValidationError
from .models import (
    RUN_TERMINAL_STATES,
    TASK_TERMINAL_STATES,
    RunState,
    TaskState,
    WaitReason,
)


RUN_TRANSITIONS: Dict[RunState, FrozenSet[RunState]] = {
    RunState.CREATED: frozenset({RunState.QUEUED, RunState.CANCELLED}),
    RunState.QUEUED: frozenset(
        {RunState.RUNNING, RunState.CANCELLED, RunState.FAILED}
    ),
    RunState.RUNNING: frozenset(
        {
            RunState.WAITING,
            RunState.FINALIZING,
            RunState.CANCELLING,
            RunState.FAILED,
        }
    ),
    RunState.WAITING: frozenset(
        {RunState.RUNNING, RunState.CANCELLING, RunState.FAILED}
    ),
    RunState.FINALIZING: frozenset(
        {
            RunState.SUCCEEDED,
            RunState.PARTIAL,
            RunState.FAILED,
            RunState.CANCELLING,
        }
    ),
    RunState.CANCELLING: frozenset({RunState.CANCELLED, RunState.FAILED}),
    RunState.SUCCEEDED: frozenset(),
    RunState.PARTIAL: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


TASK_TRANSITIONS: Dict[TaskState, FrozenSet[TaskState]] = {
    TaskState.CREATED: frozenset(
        {
            TaskState.BLOCKED,
            TaskState.READY,
            TaskState.CANCELLING,
            TaskState.CANCELLED,
        }
    ),
    TaskState.BLOCKED: frozenset(
        {
            TaskState.READY,
            TaskState.CANCELLING,
            TaskState.SKIPPED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.READY: frozenset(
        {
            TaskState.RUNNING,
            TaskState.CANCELLING,
            TaskState.SKIPPED,
            TaskState.CANCELLED,
            TaskState.FAILED,
        }
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.WAITING,
            TaskState.CANCELLING,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
        }
    ),
    TaskState.WAITING: frozenset(
        {TaskState.READY, TaskState.CANCELLING, TaskState.FAILED}
    ),
    TaskState.CANCELLING: frozenset({TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.SKIPPED: frozenset(),
}


def validate_run_transition(current: RunState, target: RunState) -> None:
    if target not in RUN_TRANSITIONS[current]:
        raise IllegalTransition(f"Run cannot transition {current.value} -> {target.value}")


def validate_task_transition(
    current: TaskState,
    target: TaskState,
    *,
    wait_reason: WaitReason | None = None,
) -> None:
    if target not in TASK_TRANSITIONS[current]:
        raise IllegalTransition(
            f"Task cannot transition {current.value} -> {target.value}"
        )
    if target is TaskState.WAITING and wait_reason is None:
        raise RuntimeValidationError("WAITING requires an explicit wait_reason")
    if target is not TaskState.WAITING and wait_reason is not None:
        raise RuntimeValidationError("wait_reason is only valid for WAITING")


def is_run_terminal(state: RunState) -> bool:
    return state in RUN_TERMINAL_STATES


def is_task_terminal(state: TaskState) -> bool:
    return state in TASK_TERMINAL_STATES
