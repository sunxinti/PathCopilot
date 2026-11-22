"""Versioned, typed runtime event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Union

from .models import utc_now_iso


class _LowerValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class Visibility(_LowerValueEnum):
    USER = "user"
    INTERNAL = "internal"
    AUDIT = "audit"


class SubjectKind(_LowerValueEnum):
    RUN = "run"
    TASK = "task"
    ATTEMPT = "attempt"
    ARTIFACT = "artifact"


class EventType(_LowerValueEnum):
    RUNTIME_ACTIVITY_RECORDED = "runtime.activity_recorded"
    RUN_CREATED = "run.created"
    RUN_STATUS_CHANGED = "run.status_changed"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"
    TASK_CREATED = "task.created"
    TASK_STATUS_CHANGED = "task.status_changed"
    TASK_CANCEL_REQUESTED = "task.cancel_requested"
    ATTEMPT_STARTED = "attempt.started"
    ATTEMPT_SUCCEEDED = "attempt.succeeded"
    ATTEMPT_FAILED = "attempt.failed"
    ATTEMPT_CANCELLED = "attempt.cancelled"
    ATTEMPT_LEASE_ACQUIRED = "attempt.lease_acquired"
    ATTEMPT_LEASE_RELEASED = "attempt.lease_released"
    ATTEMPT_LEASE_EXPIRED = "attempt.lease_expired"
    TASK_RETRY_SCHEDULED = "task.retry_scheduled"
    ARTIFACT_STAGED = "artifact.staged"
    ARTIFACT_READY = "artifact.ready"
    ARTIFACT_FAILED = "artifact.failed"


@dataclass(frozen=True)
class RunCreatedPayload:
    owner_id: str
    session_id: str
    file_id: str
    initial_state: str


@dataclass(frozen=True)
class TaskCreatedPayload:
    name: str
    role: str
    initial_state: str
    depth: int
    required: bool


@dataclass(frozen=True)
class StateChangedPayload:
    from_state: str
    to_state: str
    cause: str
    wait_reason: Optional[str] = None


@dataclass(frozen=True)
class CancelRequestedPayload:
    reason: str
    requested_by: Optional[str]


@dataclass(frozen=True)
class ArtifactPayload:
    artifact_id: str
    kind: str
    display_name: str
    state: str
    byte_size: Optional[int] = None
    sha256: Optional[str] = None
    error: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class RuntimeActivityPayload:
    stage: str
    summary: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttemptStartedPayload:
    task_id: str
    attempt_no: int
    retry_policy: Mapping[str, Any]


@dataclass(frozen=True)
class AttemptSucceededPayload:
    task_id: str
    attempt_no: int


@dataclass(frozen=True)
class AttemptFailedPayload:
    task_id: str
    attempt_no: int
    error: Mapping[str, Any]
    retryable: bool


@dataclass(frozen=True)
class AttemptCancelledPayload:
    task_id: str
    attempt_no: int
    reason: str


@dataclass(frozen=True)
class AttemptLeaseAcquiredPayload:
    task_id: str
    lease_id: str
    lease_epoch: int
    worker_id: str
    fencing_token: int
    expires_at: str


@dataclass(frozen=True)
class AttemptLeaseReleasedPayload:
    task_id: str
    lease_id: str
    lease_epoch: int
    worker_id: str
    reason: str


@dataclass(frozen=True)
class AttemptLeaseExpiredPayload:
    task_id: str
    lease_id: str
    lease_epoch: int
    worker_id: str
    expired_at: str


@dataclass(frozen=True)
class TaskRetryScheduledPayload:
    task_id: str
    failed_attempt_id: str
    failed_attempt_no: int
    next_attempt_no: int
    retry_at: str
    delay_seconds: float
    reason: str


EventPayload = Union[
    RuntimeActivityPayload,
    RunCreatedPayload,
    TaskCreatedPayload,
    StateChangedPayload,
    CancelRequestedPayload,
    ArtifactPayload,
    AttemptStartedPayload,
    AttemptSucceededPayload,
    AttemptFailedPayload,
    AttemptCancelledPayload,
    AttemptLeaseAcquiredPayload,
    AttemptLeaseReleasedPayload,
    AttemptLeaseExpiredPayload,
    TaskRetryScheduledPayload,
]


@dataclass(frozen=True)
class RuntimeEventDraft:
    run_id: str
    event_type: EventType
    payload: EventPayload
    subject_kind: SubjectKind
    subject_id: Optional[str] = None
    parent_subject_id: Optional[str] = None
    attempt_id: Optional[str] = None
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    visibility: Visibility = Visibility.INTERNAL
    occurred_at: str = field(default_factory=utc_now_iso)
    idempotency_key: Optional[str] = None
    schema_version: int = 1
    event_version: int = 1


@dataclass(frozen=True)
class RuntimeEvent:
    event_id: str
    run_id: str
    seq: int
    event_type: EventType
    payload: EventPayload
    subject_kind: SubjectKind
    subject_id: Optional[str]
    parent_subject_id: Optional[str]
    attempt_id: Optional[str]
    correlation_id: str
    causation_id: Optional[str]
    visibility: Visibility
    occurred_at: str
    recorded_at: str
    idempotency_key: Optional[str]
    content_hash: str
    producer: str
    schema_version: int = 1
    event_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type.value,
            "event_version": self.event_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "producer": self.producer,
            "subject": {
                "kind": self.subject_kind.value,
                "id": self.subject_id,
                "parent_id": self.parent_subject_id,
            },
            "attempt_id": self.attempt_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "visibility": self.visibility.value,
            "payload": asdict(self.payload),
            "idempotency_key": self.idempotency_key,
            "content_hash": self.content_hash,
        }
