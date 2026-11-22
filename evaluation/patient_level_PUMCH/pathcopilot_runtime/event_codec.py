"""Canonical JSON codec and payload validation for runtime events."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from typing import Any, Mapping, Type

from .errors import RuntimeValidationError
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
    EventPayload,
    EventType,
    RunCreatedPayload,
    RuntimeActivityPayload,
    RuntimeEvent,
    RuntimeEventDraft,
    StateChangedPayload,
    SubjectKind,
    TaskCreatedPayload,
    TaskRetryScheduledPayload,
    Visibility,
)


EVENT_PAYLOAD_TYPES: dict[EventType, Type[EventPayload]] = {
    EventType.RUNTIME_ACTIVITY_RECORDED: RuntimeActivityPayload,
    EventType.RUN_CREATED: RunCreatedPayload,
    EventType.RUN_STATUS_CHANGED: StateChangedPayload,
    EventType.RUN_CANCEL_REQUESTED: CancelRequestedPayload,
    EventType.TASK_CREATED: TaskCreatedPayload,
    EventType.TASK_STATUS_CHANGED: StateChangedPayload,
    EventType.TASK_CANCEL_REQUESTED: CancelRequestedPayload,
    EventType.ATTEMPT_STARTED: AttemptStartedPayload,
    EventType.ATTEMPT_SUCCEEDED: AttemptSucceededPayload,
    EventType.ATTEMPT_FAILED: AttemptFailedPayload,
    EventType.ATTEMPT_CANCELLED: AttemptCancelledPayload,
    EventType.ATTEMPT_LEASE_ACQUIRED: AttemptLeaseAcquiredPayload,
    EventType.ATTEMPT_LEASE_RELEASED: AttemptLeaseReleasedPayload,
    EventType.ATTEMPT_LEASE_EXPIRED: AttemptLeaseExpiredPayload,
    EventType.TASK_RETRY_SCHEDULED: TaskRetryScheduledPayload,
    EventType.ARTIFACT_STAGED: ArtifactPayload,
    EventType.ARTIFACT_READY: ArtifactPayload,
    EventType.ARTIFACT_FAILED: ArtifactPayload,
}

_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:api_?key|access_?token|authorization|password|secret|"
    r"system_?prompt|chain_?of_?thought|hidden_?reasoning)(?:$|_)",
    re.IGNORECASE,
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EventCodec:
    SCHEMA_VERSION = 1

    def __init__(self, *, max_payload_bytes: int = 256 * 1024):
        if max_payload_bytes < 1024:
            raise ValueError("max_payload_bytes must be >= 1024")
        self.max_payload_bytes = max_payload_bytes

    def encode_draft(self, draft: RuntimeEventDraft) -> tuple[str, str]:
        self._validate_draft(draft)
        payload_dict = asdict(draft.payload)
        self._validate_json_value(payload_dict)
        self._reject_sensitive_fields(payload_dict)
        payload_json = self._canonical_json(payload_dict)
        if len(payload_json.encode("utf-8")) > self.max_payload_bytes:
            raise RuntimeValidationError("event payload exceeds configured size limit")

        content_hash = self._content_hash(
            {
                "schema_version": draft.schema_version,
                "event_type": draft.event_type.value,
                "event_version": draft.event_version,
                "run_id": draft.run_id,
                "subject_kind": draft.subject_kind.value,
                "subject_id": draft.subject_id,
                "parent_subject_id": draft.parent_subject_id,
                "attempt_id": draft.attempt_id,
                "correlation_id": draft.correlation_id or draft.run_id,
                "causation_id": draft.causation_id,
                "visibility": draft.visibility.value,
                "payload": payload_dict,
            }
        )
        return payload_json, content_hash

    def payload_from_json(
        self, event_type: EventType, event_version: int, payload_json: str
    ) -> EventPayload:
        if event_version != 1:
            raise RuntimeValidationError(
                f"unsupported {event_type.value} event version: {event_version}"
            )
        try:
            value = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise RuntimeValidationError("invalid event payload JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeValidationError("event payload must be a JSON object")
        payload_type = EVENT_PAYLOAD_TYPES[event_type]
        allowed = {field.name for field in fields(payload_type)}
        unknown = set(value) - allowed
        if unknown:
            raise RuntimeValidationError(
                f"unknown payload fields for {event_type.value}: {sorted(unknown)}"
            )
        try:
            payload = payload_type(**value)
        except TypeError as exc:
            raise RuntimeValidationError(
                f"invalid payload for {event_type.value}: {exc}"
            ) from exc
        self._reject_sensitive_fields(value)
        self._validate_payload_semantics(event_type, payload)
        return payload

    def event_from_row(self, row: Mapping[str, Any]) -> RuntimeEvent:
        schema_version = int(row["schema_version"])
        if schema_version != self.SCHEMA_VERSION:
            raise RuntimeValidationError(
                f"unsupported event schema version: {schema_version}"
            )
        try:
            event_type = EventType(row["event_type"])
            subject_kind = SubjectKind(row["subject_kind"])
            visibility = Visibility(row["visibility"])
        except ValueError as exc:
            raise RuntimeValidationError(f"unknown persisted event enum: {exc}") from exc
        event_version = int(row["event_version"])
        payload = self.payload_from_json(
            event_type, event_version, row["payload_json"]
        )
        stored_hash = row["content_hash"]
        expected_hash = self._content_hash(
            {
                "schema_version": schema_version,
                "event_type": event_type.value,
                "event_version": event_version,
                "run_id": row["run_id"],
                "subject_kind": subject_kind.value,
                "subject_id": row["subject_id"],
                "parent_subject_id": row["parent_subject_id"],
                "attempt_id": row["attempt_id"],
                "correlation_id": row["correlation_id"],
                "causation_id": row["causation_id"],
                "visibility": visibility.value,
                "payload": asdict(payload),
            }
        )
        if (
            not isinstance(stored_hash, str)
            or not _SHA256.fullmatch(stored_hash)
            or not hmac.compare_digest(stored_hash, expected_hash)
        ):
            raise RuntimeValidationError(
                "persisted event content hash verification failed"
            )
        return RuntimeEvent(
            event_id=row["event_id"],
            run_id=row["run_id"],
            seq=int(row["seq"]),
            event_type=event_type,
            payload=payload,
            subject_kind=subject_kind,
            subject_id=row["subject_id"],
            parent_subject_id=row["parent_subject_id"],
            attempt_id=row["attempt_id"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            visibility=visibility,
            occurred_at=row["occurred_at"],
            recorded_at=row["recorded_at"],
            idempotency_key=row["idempotency_key"],
            content_hash=row["content_hash"],
            producer=row["producer"],
            schema_version=schema_version,
            event_version=event_version,
        )

    def _content_hash(self, content: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            self._canonical_json(content).encode("utf-8")
        ).hexdigest()

    def _validate_draft(self, draft: RuntimeEventDraft) -> None:
        if draft.schema_version != self.SCHEMA_VERSION:
            raise RuntimeValidationError(
                f"unsupported event schema version: {draft.schema_version}"
            )
        if draft.event_version != 1:
            raise RuntimeValidationError(
                f"unsupported {draft.event_type.value} event version: "
                f"{draft.event_version}"
            )
        if not isinstance(draft.event_type, EventType):
            raise RuntimeValidationError("event_type must be an EventType")
        if not isinstance(draft.subject_kind, SubjectKind):
            raise RuntimeValidationError("subject_kind must be a SubjectKind")
        if not isinstance(draft.visibility, Visibility):
            raise RuntimeValidationError("visibility must be a Visibility")
        if not isinstance(draft.run_id, str) or not draft.run_id.strip():
            raise RuntimeValidationError("run_id is required")
        for label, value in (
            ("subject_id", draft.subject_id),
            ("parent_subject_id", draft.parent_subject_id),
            ("attempt_id", draft.attempt_id),
            ("correlation_id", draft.correlation_id),
            ("causation_id", draft.causation_id),
            ("idempotency_key", draft.idempotency_key),
        ):
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise RuntimeValidationError(f"{label} must be a non-empty string")
        if not isinstance(draft.occurred_at, str):
            raise RuntimeValidationError("occurred_at must be an RFC3339 string")
        try:
            occurred_at = datetime.fromisoformat(
                draft.occurred_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise RuntimeValidationError(
                "occurred_at must be an RFC3339 timestamp"
            ) from exc
        if occurred_at.tzinfo is None:
            raise RuntimeValidationError("occurred_at must include a timezone")
        expected_type = EVENT_PAYLOAD_TYPES.get(draft.event_type)
        if expected_type is None:
            raise RuntimeValidationError(
                f"unregistered event type: {draft.event_type.value}"
            )
        if not isinstance(draft.payload, expected_type):
            raise RuntimeValidationError(
                f"{draft.event_type.value} requires {expected_type.__name__}, "
                f"got {type(draft.payload).__name__}"
            )
        self._validate_payload_semantics(draft.event_type, draft.payload)

    def _validate_payload_semantics(
        self, event_type: EventType, payload: EventPayload
    ) -> None:
        if isinstance(payload, RuntimeActivityPayload):
            if (
                not isinstance(payload.stage, str)
                or not payload.stage.strip()
                or not isinstance(payload.summary, str)
                or not payload.summary.strip()
                or not isinstance(payload.details, Mapping)
            ):
                raise RuntimeValidationError(
                    "runtime activity requires stage, summary, and mapping details"
                )
        elif isinstance(payload, RunCreatedPayload):
            if not all(
                isinstance(value, str) and value.strip()
                for value in (
                    payload.owner_id,
                    payload.session_id,
                    payload.file_id,
                    payload.initial_state,
                )
            ):
                raise RuntimeValidationError(
                    "run.created payload fields must be non-empty strings"
                )
        elif isinstance(payload, TaskCreatedPayload):
            if (
                not isinstance(payload.name, str)
                or not payload.name.strip()
                or not isinstance(payload.role, str)
                or not payload.role.strip()
                or not isinstance(payload.initial_state, str)
                or not payload.initial_state.strip()
                or not isinstance(payload.depth, int)
                or isinstance(payload.depth, bool)
                or payload.depth < 0
                or not isinstance(payload.required, bool)
            ):
                raise RuntimeValidationError("invalid task.created payload")
        elif isinstance(payload, StateChangedPayload):
            if (
                not isinstance(payload.from_state, str)
                or not payload.from_state.strip()
                or not isinstance(payload.to_state, str)
                or not payload.to_state.strip()
                or not isinstance(payload.cause, str)
                or not payload.cause.strip()
                or (
                    payload.wait_reason is not None
                    and (
                        not isinstance(payload.wait_reason, str)
                        or not payload.wait_reason.strip()
                    )
                )
            ):
                raise RuntimeValidationError("invalid state changed payload")
        elif isinstance(payload, CancelRequestedPayload):
            if (
                not isinstance(payload.reason, str)
                or not payload.reason.strip()
                or (
                    payload.requested_by is not None
                    and (
                        not isinstance(payload.requested_by, str)
                        or not payload.requested_by.strip()
                    )
                )
            ):
                raise RuntimeValidationError("invalid cancel requested payload")
        elif isinstance(payload, ArtifactPayload):
            if (
                not isinstance(payload.artifact_id, str)
                or not payload.artifact_id.strip()
                or not isinstance(payload.kind, str)
                or not payload.kind.strip()
                or not isinstance(payload.display_name, str)
                or not payload.display_name.strip()
                or payload.state not in {"staging", "ready", "failed"}
                or (
                    payload.byte_size is not None
                    and (
                        not isinstance(payload.byte_size, int)
                        or isinstance(payload.byte_size, bool)
                        or payload.byte_size < 0
                    )
                )
                or (
                    payload.sha256 is not None
                    and (
                        not isinstance(payload.sha256, str)
                        or _SHA256.fullmatch(payload.sha256) is None
                    )
                )
                or (
                    payload.error is not None
                    and not isinstance(payload.error, Mapping)
                )
            ):
                raise RuntimeValidationError("invalid artifact payload")
        elif isinstance(payload, AttemptStartedPayload):
            if (
                not isinstance(payload.task_id, str)
                or not payload.task_id.strip()
                or not isinstance(payload.attempt_no, int)
                or isinstance(payload.attempt_no, bool)
                or payload.attempt_no < 1
                or not isinstance(payload.retry_policy, Mapping)
            ):
                raise RuntimeValidationError("invalid attempt.started payload")
        elif isinstance(payload, AttemptSucceededPayload):
            self._validate_attempt_identity(
                payload.task_id, payload.attempt_no, event_type
            )
        elif isinstance(payload, AttemptFailedPayload):
            self._validate_attempt_identity(
                payload.task_id, payload.attempt_no, event_type
            )
            if (
                not isinstance(payload.error, Mapping)
                or not isinstance(payload.retryable, bool)
            ):
                raise RuntimeValidationError("invalid attempt.failed payload")
        elif isinstance(payload, AttemptCancelledPayload):
            self._validate_attempt_identity(
                payload.task_id, payload.attempt_no, event_type
            )
            if not isinstance(payload.reason, str) or not payload.reason.strip():
                raise RuntimeValidationError("invalid attempt.cancelled payload")
        elif isinstance(payload, AttemptLeaseAcquiredPayload):
            self._validate_lease_identity(
                payload.task_id,
                payload.lease_id,
                payload.lease_epoch,
                payload.worker_id,
                event_type,
            )
            if (
                not isinstance(payload.fencing_token, int)
                or isinstance(payload.fencing_token, bool)
                or payload.fencing_token < 1
                or not isinstance(payload.expires_at, str)
                or not payload.expires_at.strip()
            ):
                raise RuntimeValidationError(
                    "invalid attempt.lease_acquired payload"
                )
        elif isinstance(payload, AttemptLeaseReleasedPayload):
            self._validate_lease_identity(
                payload.task_id,
                payload.lease_id,
                payload.lease_epoch,
                payload.worker_id,
                event_type,
            )
            if not isinstance(payload.reason, str) or not payload.reason.strip():
                raise RuntimeValidationError(
                    "invalid attempt.lease_released payload"
                )
        elif isinstance(payload, AttemptLeaseExpiredPayload):
            self._validate_lease_identity(
                payload.task_id,
                payload.lease_id,
                payload.lease_epoch,
                payload.worker_id,
                event_type,
            )
            if (
                not isinstance(payload.expired_at, str)
                or not payload.expired_at.strip()
            ):
                raise RuntimeValidationError(
                    "invalid attempt.lease_expired payload"
                )
        elif isinstance(payload, TaskRetryScheduledPayload):
            if (
                not isinstance(payload.task_id, str)
                or not payload.task_id.strip()
                or not isinstance(payload.failed_attempt_id, str)
                or not payload.failed_attempt_id.strip()
                or not isinstance(payload.failed_attempt_no, int)
                or isinstance(payload.failed_attempt_no, bool)
                or payload.failed_attempt_no < 1
                or not isinstance(payload.next_attempt_no, int)
                or isinstance(payload.next_attempt_no, bool)
                or payload.next_attempt_no != payload.failed_attempt_no + 1
                or not isinstance(payload.retry_at, str)
                or not payload.retry_at.strip()
                or not isinstance(payload.delay_seconds, (int, float))
                or isinstance(payload.delay_seconds, bool)
                or not math.isfinite(float(payload.delay_seconds))
                or payload.delay_seconds < 0
                or not isinstance(payload.reason, str)
                or not payload.reason.strip()
            ):
                raise RuntimeValidationError(
                    "invalid task.retry_scheduled payload"
                )

    @staticmethod
    def _validate_attempt_identity(
        task_id: Any, attempt_no: Any, event_type: EventType
    ) -> None:
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(attempt_no, int)
            or isinstance(attempt_no, bool)
            or attempt_no < 1
        ):
            raise RuntimeValidationError(
                f"invalid {event_type.value} payload"
            )

    @staticmethod
    def _validate_lease_identity(
        task_id: Any,
        lease_id: Any,
        lease_epoch: Any,
        worker_id: Any,
        event_type: EventType,
    ) -> None:
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(lease_id, str)
            or not lease_id.strip()
            or not isinstance(lease_epoch, int)
            or isinstance(lease_epoch, bool)
            or lease_epoch < 1
            or not isinstance(worker_id, str)
            or not worker_id.strip()
        ):
            raise RuntimeValidationError(
                f"invalid {event_type.value} payload"
            )

    def _reject_sensitive_fields(self, value: Any, path: str = "payload") -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                key_text = str(key)
                if _SENSITIVE_KEY.search(key_text):
                    raise RuntimeValidationError(
                        f"sensitive field is forbidden in runtime events: "
                        f"{path}.{key_text}"
                    )
                self._reject_sensitive_fields(nested, f"{path}.{key_text}")
        elif isinstance(value, (list, tuple)):
            for index, nested in enumerate(value):
                self._reject_sensitive_fields(nested, f"{path}[{index}]")

    def _validate_json_value(self, value: Any, path: str = "payload") -> None:
        if value is None or isinstance(value, (str, bool, int)):
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise RuntimeValidationError(f"non-finite JSON number at {path}")
            return
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise RuntimeValidationError(
                        f"JSON object key must be a string at {path}"
                    )
                self._validate_json_value(nested, f"{path}.{key}")
            return
        if isinstance(value, (list, tuple)):
            for index, nested in enumerate(value):
                self._validate_json_value(nested, f"{path}[{index}]")
            return
        if is_dataclass(value):
            self._validate_json_value(asdict(value), path)
            return
        raise RuntimeValidationError(
            f"unsupported JSON value {type(value).__name__} at {path}"
        )

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
