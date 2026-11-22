"""PathCopilot task-management and Agent lifecycle runtime.

This package is intentionally independent from the mature pathology pipeline.
The first integration stage provides lifecycle primitives only; importing it
does not start workers, alter Flask routes, or change the legacy Agent.
"""

from .artifact_store import ArtifactStore
from .attempts import (
    AttemptState,
    LeaseState,
    RetryPolicy,
    TaskAttemptRecord,
    TaskClaim,
    TaskLeaseRecord,
)
from .cancellation import CancellationInfo, CancellationToken
from .errors import (
    ArtifactSecurityError,
    AttemptConflict,
    CancellationRequested,
    ConcurrencyConflict,
    DuplicateActiveRun,
    IdempotencyConflict,
    IllegalTransition,
    LeaseLost,
    NotFoundError,
    RuntimeValidationError,
    SchedulerOverloaded,
    TaskCapacityExceeded,
)
from .events import (
    AttemptCancelledPayload,
    AttemptFailedPayload,
    AttemptLeaseAcquiredPayload,
    AttemptLeaseExpiredPayload,
    AttemptLeaseReleasedPayload,
    AttemptStartedPayload,
    AttemptSucceededPayload,
    EventType,
    RuntimeActivityPayload,
    RuntimeEvent,
    RuntimeEventDraft,
    SubjectKind,
    TaskRetryScheduledPayload,
    Visibility,
)
from .executor import (
    DurableTaskExecutor,
    ExecutionContext,
    ExecutorSnapshot,
    RetryableExecutionError,
    YieldExecution,
)
from .models import (
    RunRecord,
    RunPolicy,
    RunState,
    TaskRecord,
    TaskState,
    WaitReason,
    WorkflowCounts,
)
from .registry import RunRegistry
from .scheduler import BoundedTaskScheduler
from .sqlite_event_store import SQLiteEventStore

__all__ = [
    "ArtifactSecurityError",
    "ArtifactStore",
    "AttemptCancelledPayload",
    "AttemptConflict",
    "AttemptFailedPayload",
    "AttemptLeaseAcquiredPayload",
    "AttemptLeaseExpiredPayload",
    "AttemptLeaseReleasedPayload",
    "AttemptStartedPayload",
    "AttemptState",
    "AttemptSucceededPayload",
    "BoundedTaskScheduler",
    "CancellationInfo",
    "CancellationRequested",
    "CancellationToken",
    "ConcurrencyConflict",
    "DuplicateActiveRun",
    "DurableTaskExecutor",
    "ExecutionContext",
    "ExecutorSnapshot",
    "EventType",
    "IdempotencyConflict",
    "IllegalTransition",
    "LeaseLost",
    "LeaseState",
    "NotFoundError",
    "RunRecord",
    "RunPolicy",
    "RunRegistry",
    "RunState",
    "RetryPolicy",
    "RetryableExecutionError",
    "RuntimeActivityPayload",
    "RuntimeEvent",
    "RuntimeEventDraft",
    "RuntimeValidationError",
    "SchedulerOverloaded",
    "SQLiteEventStore",
    "SubjectKind",
    "TaskRecord",
    "TaskAttemptRecord",
    "TaskCapacityExceeded",
    "TaskClaim",
    "TaskLeaseRecord",
    "TaskRetryScheduledPayload",
    "TaskState",
    "Visibility",
    "WaitReason",
    "WorkflowCounts",
    "YieldExecution",
]
