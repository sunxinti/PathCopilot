"""Runtime-specific exceptions with explicit failure semantics."""

from __future__ import annotations

from typing import Optional


class PathCopilotRuntimeError(Exception):
    """Base class for lifecycle runtime errors."""


class RuntimeValidationError(PathCopilotRuntimeError, ValueError):
    """A command or persisted value violates a runtime invariant."""


class IllegalTransition(RuntimeValidationError):
    """A Run or Task attempted an unlisted state transition."""


class NotFoundError(PathCopilotRuntimeError, LookupError):
    """A requested Run, Task, Event, or Artifact does not exist."""


class IdempotencyConflict(RuntimeValidationError):
    """An idempotency key was reused for a different command or event."""


class ConcurrencyConflict(PathCopilotRuntimeError):
    """Optimistic state/version checks failed because another writer won."""


class AttemptConflict(ConcurrencyConflict):
    """A logical Attempt command conflicts with the authoritative snapshot."""


class LeaseLost(ConcurrencyConflict):
    """A worker no longer owns the lease/fencing authority for a Task."""


class DuplicateActiveRun(RuntimeValidationError):
    """The same owner/session/file already has a non-terminal Run."""

    def __init__(self, existing_run_id: str):
        self.existing_run_id = existing_run_id
        super().__init__(f"active run already exists: {existing_run_id}")

    def __str__(self) -> str:
        return f"active run already exists: {self.existing_run_id}"


class TaskCapacityExceeded(RuntimeValidationError):
    """A Run's immutable total-task limit rejected a new spawn."""

    def __init__(self, run_id: str, max_total_tasks: int):
        self.run_id = run_id
        self.max_total_tasks = max_total_tasks
        super().__init__(
            f"Run {run_id} reached max_total_tasks={max_total_tasks}"
        )


class SchedulerOverloaded(PathCopilotRuntimeError):
    """The bounded scheduler cannot accept another work item."""


class CancellationRequested(PathCopilotRuntimeError):
    """Cooperative work noticed a cancellation request."""

    def __init__(self, reason: str = "cancelled", *, requested_by: Optional[str] = None):
        self.reason = reason
        self.requested_by = requested_by
        suffix = f" by {requested_by}" if requested_by else ""
        super().__init__(f"{reason}{suffix}")


class ArtifactSecurityError(RuntimeValidationError):
    """An Artifact path would escape its Run-specific storage boundary."""
