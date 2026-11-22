"""Execution-attempt and worker-lease contracts.

An :class:`AttemptState` represents one logical execution attempt.  A logical
attempt may acquire more than one lease epoch while it remains active (for
example after yielding for child tasks and later resuming).  A lease is the
short-lived authority granted to one worker; only the current fencing token
may commit task results or publish artifacts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

from .errors import RuntimeValidationError
from .models import utc_now_iso


class _LowerValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class AttemptState(_LowerValueEnum):
    """Durable state of one logical task execution attempt."""

    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class LeaseState(_LowerValueEnum):
    """Durable state of a worker's time-bounded execution authority."""

    ACTIVE = "active"
    REVOKING = "revoking"
    RELEASED = "released"
    EXPIRED = "expired"
    REVOKED = "revoked"


ATTEMPT_TERMINAL_STATES = frozenset(
    {
        AttemptState.SUCCEEDED,
        AttemptState.FAILED,
        AttemptState.CANCELLED,
        AttemptState.EXPIRED,
    }
)
LEASE_TERMINAL_STATES = frozenset(
    {
        LeaseState.RELEASED,
        LeaseState.EXPIRED,
        LeaseState.REVOKED,
    }
)


@dataclass(frozen=True)
class RetryPolicy:
    """Immutable retry limits captured when a logical Attempt is created.

    ``max_attempts=1`` preserves the 2A execution semantics: failures are
    terminal unless a caller explicitly opts into retry.  Jitter is described
    by the policy but applied by the claiming service so tests can inject a
    deterministic clock/random source.
    """

    max_attempts: int = 1
    initial_backoff_seconds: float = 0.0
    max_backoff_seconds: float = 0.0
    backoff_multiplier: float = 2.0
    jitter_ratio: float = 0.0
    retryable_error_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts < 1
        ):
            raise RuntimeValidationError("max_attempts must be an integer >= 1")
        for label, value in (
            ("initial_backoff_seconds", self.initial_backoff_seconds),
            ("max_backoff_seconds", self.max_backoff_seconds),
            ("backoff_multiplier", self.backoff_multiplier),
            ("jitter_ratio", self.jitter_ratio),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise RuntimeValidationError(f"{label} must be finite")
        if self.initial_backoff_seconds < 0:
            raise RuntimeValidationError(
                "initial_backoff_seconds must be >= 0"
            )
        if self.max_backoff_seconds < 0:
            raise RuntimeValidationError("max_backoff_seconds must be >= 0")
        if (
            self.max_backoff_seconds > 0
            and self.max_backoff_seconds < self.initial_backoff_seconds
        ):
            raise RuntimeValidationError(
                "max_backoff_seconds must be 0 or >= initial_backoff_seconds"
            )
        if self.backoff_multiplier < 1:
            raise RuntimeValidationError("backoff_multiplier must be >= 1")
        if not 0 <= self.jitter_ratio <= 1:
            raise RuntimeValidationError("jitter_ratio must be between 0 and 1")
        if not isinstance(self.retryable_error_codes, tuple):
            raise RuntimeValidationError(
                "retryable_error_codes must be a tuple"
            )
        normalized_codes: list[str] = []
        for code in self.retryable_error_codes:
            if not isinstance(code, str) or not code.strip():
                raise RuntimeValidationError(
                    "retryable_error_codes must contain non-empty strings"
                )
            normalized_codes.append(code.strip())
        if len(set(normalized_codes)) != len(normalized_codes):
            raise RuntimeValidationError(
                "retryable_error_codes must not contain duplicates"
            )
        if tuple(normalized_codes) != self.retryable_error_codes:
            object.__setattr__(
                self, "retryable_error_codes", tuple(normalized_codes)
            )

    def base_backoff_seconds(self, failed_attempt_no: int) -> float:
        """Return deterministic backoff before the next logical Attempt."""

        if (
            not isinstance(failed_attempt_no, int)
            or isinstance(failed_attempt_no, bool)
            or failed_attempt_no < 1
        ):
            raise RuntimeValidationError(
                "failed_attempt_no must be an integer >= 1"
            )
        if self.initial_backoff_seconds == 0:
            return 0.0
        delay = self.initial_backoff_seconds * (
            self.backoff_multiplier ** (failed_attempt_no - 1)
        )
        if self.max_backoff_seconds > 0:
            delay = min(delay, self.max_backoff_seconds)
        return float(delay)


@dataclass(frozen=True)
class TaskAttemptRecord:
    """Durable snapshot of one logical execution Attempt."""

    attempt_id: str
    run_id: str
    task_id: str
    attempt_no: int
    state: AttemptState = AttemptState.ACTIVE
    state_version: int = 0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    ended_at: Optional[str] = None
    retryable: Optional[bool] = None
    failure_code: Optional[str] = None
    result: Optional[Mapping[str, Any]] = None
    error: Optional[Mapping[str, Any]] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in ATTEMPT_TERMINAL_STATES


@dataclass(frozen=True)
class TaskLeaseRecord:
    """Durable snapshot of a worker lease epoch."""

    lease_id: str
    run_id: str
    task_id: str
    attempt_id: str
    lease_epoch: int
    worker_id: str
    fencing_token: int
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    state: LeaseState = LeaseState.ACTIVE
    state_version: int = 0
    released_at: Optional[str] = None
    release_reason: Optional[str] = None
    worker_slot_id: Optional[str] = None
    claim_request_id: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in LEASE_TERMINAL_STATES


@dataclass(frozen=True)
class TaskClaim:
    """Authority returned to an executor by an atomic task claim."""

    run_id: str
    task_id: str
    attempt_no: int
    attempt_id: str
    lease_id: str
    worker_id: str
    fencing_token: int
    acquired_at: str
    expires_at: str
    worker_slot_id: Optional[str] = None
    claim_request_id: Optional[str] = None
