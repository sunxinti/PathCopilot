"""Thread-safe Run/Task registry backed by the SQLite fact store."""

from __future__ import annotations

import threading
import uuid
from typing import Any, Mapping

from .attempts import RetryPolicy
from .cancellation import CancellationToken
from .errors import RuntimeValidationError
from .events import SubjectKind
from .models import (
    RunRecord,
    RunState,
    TaskRecord,
    TaskState,
    WaitReason,
    WorkflowCounts,
)
from .sqlite_event_store import SQLiteEventStore


class RunRegistry:
    """Own IDs, duplicate guards, cancellation tokens, and lifecycle commands."""

    def __init__(
        self,
        store: SQLiteEventStore,
        *,
        max_total_tasks_per_run: int = 256,
        max_depth: int = 8,
    ):
        if max_total_tasks_per_run < 1:
            raise ValueError("max_total_tasks_per_run must be >= 1")
        if max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        self.store = store
        self.max_total_tasks_per_run = max_total_tasks_per_run
        self.max_depth = max_depth
        self._lock = threading.RLock()
        self._run_tokens: dict[str, CancellationToken] = {}
        self._task_tokens: dict[str, CancellationToken] = {}

    def create_run(
        self,
        *,
        owner_id: str,
        session_id: str,
        file_id: str,
        idempotency_key: str | None = None,
        client_request_id: str | None = None,
        config: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[RunRecord, bool]:
        with self._lock:
            record = RunRecord(
                run_id=f"run_{uuid.uuid4().hex}",
                owner_id=owner_id,
                session_id=session_id,
                file_id=file_id,
                idempotency_key=idempotency_key,
                client_request_id=client_request_id,
                config=dict(config or {}),
                metadata=dict(metadata or {}),
            )
            saved, created = self.store.create_run(
                record,
                max_total_tasks=self.max_total_tasks_per_run,
                max_task_depth=self.max_depth,
            )
            if saved.is_terminal:
                self._run_tokens.pop(saved.run_id, None)
            else:
                self._run_tokens.setdefault(saved.run_id, CancellationToken())
            return saved, created

    def create_task(
        self,
        *,
        run_id: str,
        name: str,
        role: str,
        parent_task_id: str | None = None,
        continuation_of_task_id: str | None = None,
        required: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        """Create an Attempt-managed Task.

        This is the safe default for new Agent Runtime work: the Task may only
        execute and commit through an owned Attempt, Lease, and fencing token.
        """

        saved, _ = self.spawn_task(
            run_id=run_id,
            name=name,
            role=role,
            parent_task_id=parent_task_id,
            continuation_of_task_id=continuation_of_task_id,
            required=required,
            metadata=metadata,
        )
        return saved

    def create_legacy_task(
        self,
        *,
        run_id: str,
        name: str,
        role: str,
        parent_task_id: str | None = None,
        continuation_of_task_id: str | None = None,
        required: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        """Compatibility-only creation for the pre-Attempt 2A transition API."""

        saved, _ = self.spawn_legacy_task(
            run_id=run_id,
            name=name,
            role=role,
            parent_task_id=parent_task_id,
            continuation_of_task_id=continuation_of_task_id,
            required=required,
            metadata=metadata,
        )
        return saved

    def spawn_task(
        self,
        *,
        run_id: str,
        name: str,
        role: str,
        idempotency_key: str | None = None,
        parent_task_id: str | None = None,
        continuation_of_task_id: str | None = None,
        required: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[TaskRecord, bool]:
        """Atomically spawn an idempotent, Attempt-managed Task."""

        return self._spawn_task(
            run_id=run_id,
            name=name,
            role=role,
            idempotency_key=idempotency_key,
            parent_task_id=parent_task_id,
            continuation_of_task_id=continuation_of_task_id,
            required=required,
            metadata=metadata,
            attempt_managed=True,
        )

    def spawn_legacy_task(
        self,
        *,
        run_id: str,
        name: str,
        role: str,
        idempotency_key: str | None = None,
        parent_task_id: str | None = None,
        continuation_of_task_id: str | None = None,
        required: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[TaskRecord, bool]:
        """Compatibility-only spawn for the pre-Attempt 2A transition API."""

        return self._spawn_task(
            run_id=run_id,
            name=name,
            role=role,
            idempotency_key=idempotency_key,
            parent_task_id=parent_task_id,
            continuation_of_task_id=continuation_of_task_id,
            required=required,
            metadata=metadata,
            attempt_managed=False,
        )

    def _spawn_task(
        self,
        *,
        run_id: str,
        name: str,
        role: str,
        idempotency_key: str | None,
        parent_task_id: str | None,
        continuation_of_task_id: str | None,
        required: bool,
        metadata: Mapping[str, Any] | None,
        attempt_managed: bool,
    ) -> tuple[TaskRecord, bool]:
        with self._lock:
            record = self._new_task_record(
                run_id=run_id,
                name=name,
                role=role,
                idempotency_key=idempotency_key,
                parent_task_id=parent_task_id,
                continuation_of_task_id=continuation_of_task_id,
                required=required,
                metadata=metadata,
            )
            if attempt_managed:
                saved, created = self.store.spawn_task_with_retry_policy(
                    record, RetryPolicy()
                )
            else:
                saved, created = self.store.spawn_task(record)
            saved = self._install_task_token(saved, created=created)
            return saved, created

    @staticmethod
    def _new_task_record(
        *,
        run_id: str,
        name: str,
        role: str,
        idempotency_key: str | None = None,
        parent_task_id: str | None = None,
        continuation_of_task_id: str | None = None,
        required: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        return TaskRecord(
            task_id=f"task_{uuid.uuid4().hex}",
            run_id=run_id,
            name=name,
            role=role,
            parent_task_id=parent_task_id,
            continuation_of_task_id=continuation_of_task_id,
            depth=0,
            required=required,
            metadata=dict(metadata or {}),
            spawn_idempotency_key=idempotency_key,
        )

    def _install_task_token(
        self, saved: TaskRecord, *, created: bool
    ) -> TaskRecord:
        if saved.is_terminal:
            self._task_tokens.pop(saved.task_id, None)
        elif created:
            try:
                self.token_for_task(saved.task_id)
            except RuntimeValidationError:
                # Another process may cancel the just-committed subtree before
                # this process installs its in-memory token.
                saved = self.store.get_task(saved.task_id)
                if saved.is_terminal:
                    self._task_tokens.pop(saved.task_id, None)
                else:
                    raise
        return saved

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
        with self._lock:
            saved = self.store.transition_run(
                run_id,
                target,
                cause=cause,
                expected_version=expected_version,
                error=error,
                metadata_patch=metadata_patch,
            )
            if saved.is_terminal:
                self._run_tokens.pop(run_id, None)
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
        with self._lock:
            saved = self.store.transition_task(
                task_id,
                target,
                cause=cause,
                expected_version=expected_version,
                wait_reason=wait_reason,
                result=result,
                error=error,
                metadata_patch=metadata_patch,
            )
            if saved.is_terminal:
                self._task_tokens.pop(task_id, None)
                run = self.store.get_run(saved.run_id)
                if run.state is RunState.CANCELLING:
                    self.finalize_cancel_if_quiescent(saved.run_id)
            return saved

    def request_cancel(
        self,
        run_id: str,
        *,
        requested_by: str | None,
        reason: str = "user requested stop",
    ) -> RunRecord:
        """Request cancellation and truthfully distinguish request from exit."""

        with self._lock:
            run = self.store.get_run(run_id)
            if run.is_terminal:
                return run
            run_token = self.token_for_run(run_id)
            run, tasks = self.store.request_cancellation(
                run_id, requested_by=requested_by, reason=reason
            )
            # A Task-local request may have been persisted by another process
            # before this later Run cancellation. Synchronize cached leaves
            # first so ancestor token propagation cannot overwrite that first
            # persisted reason.
            for task in sorted(
                tasks, key=lambda item: item.depth, reverse=True
            ):
                task_token = self._task_tokens.get(task.task_id)
                if task_token is not None:
                    self._sync_task_token_from_persistence(task, task_token)
            persisted_request = self.store.cancellation_request_for(
                run_id,
                subject_kind=SubjectKind.RUN,
                subject_id=run_id,
            )
            run_token.request_cancel(
                reason=(
                    persisted_request.reason
                    if persisted_request is not None
                    else reason
                ),
                requested_by=(
                    persisted_request.requested_by
                    if persisted_request is not None
                    else requested_by
                ),
                requested_at=run.cancel_requested_at,
            )
            for task in tasks:
                if task.is_terminal:
                    self._task_tokens.pop(task.task_id, None)
            if run.is_terminal:
                self._run_tokens.pop(run_id, None)
            elif run.state is RunState.CANCELLING:
                run = self.finalize_cancel_if_quiescent(run_id)
            return run

    def request_task_cancel(
        self,
        task_id: str,
        *,
        requested_by: str | None,
        reason: str = "user requested stop",
    ) -> TaskRecord:
        """Cancel one non-root Task subtree without cancelling its Run."""

        with self._lock:
            current = self.store.get_task(task_id)
            if current.parent_task_id is None:
                raise RuntimeValidationError(
                    "the root Task represents the Run; cancel the Run instead"
                )
            if not current.is_terminal:
                self.token_for_task(task_id)

            tasks, _ = self.store.request_task_cancellation(
                task_id,
                requested_by=requested_by,
                reason=reason,
            )
            # Descendants must claim their own first persisted cancellation
            # before the target token propagates to them.
            for task in sorted(
                tasks, key=lambda item: item.depth, reverse=True
            ):
                task_token = self._task_tokens.get(task.task_id)
                if task_token is not None:
                    self._sync_task_token_from_persistence(task, task_token)
            saved = next(task for task in tasks if task.task_id == task_id)
            for task in tasks:
                if task.is_terminal:
                    self._task_tokens.pop(task.task_id, None)
            return saved

    def complete_task_cancellation(self, task_id: str) -> TaskRecord:
        with self._lock:
            task = self.store.get_task(task_id)
            if task.state is TaskState.CANCELLED:
                return task
            if task.state is not TaskState.CANCELLING:
                raise RuntimeValidationError(
                    f"Task is not cancelling: {task.state.value}"
                )
            task = self.store.complete_task_cancellation(task_id)
            for saved in self.store.list_tasks(task.run_id):
                if saved.is_terminal:
                    self._task_tokens.pop(saved.task_id, None)
            self.finalize_cancel_if_quiescent(task.run_id)
            return task

    def finalize_cancel_if_quiescent(self, run_id: str) -> RunRecord:
        with self._lock:
            run = self.store.get_run(run_id)
            if run.state is not RunState.CANCELLING:
                return run
            tasks = self.store.list_tasks(run_id)
            if any(not task.is_terminal for task in tasks):
                return run
            run = self.store.complete_run_cancellation(run_id)
            self._run_tokens.pop(run_id, None)
            return run

    def token_for_run(self, run_id: str) -> CancellationToken:
        with self._lock:
            run = self.store.get_run(run_id)
            if run.is_terminal:
                raise RuntimeValidationError(
                    f"terminal Run has no active token: {run.state.value}"
                )
            token = self._run_tokens.setdefault(run_id, CancellationToken())
            if run.cancel_requested_at is not None and not token.is_cancelled:
                request = self.store.cancellation_request_for(
                    run_id,
                    subject_kind=SubjectKind.RUN,
                    subject_id=run_id,
                )
                token.request_cancel(
                    reason=(
                        request.reason
                        if request is not None
                        else "restored persisted cancellation"
                    ),
                    requested_by=(
                        request.requested_by if request is not None else None
                    ),
                    requested_at=run.cancel_requested_at,
                )
            return token

    def token_for_task(self, task_id: str) -> CancellationToken:
        with self._lock:
            task = self.store.get_task(task_id)
            token = self._task_tokens.get(task_id)
            if task.is_terminal:
                if token is not None:
                    self._sync_task_token_from_persistence(task, token)
                    self._task_tokens.pop(task_id, None)
                raise RuntimeValidationError(
                    f"terminal Task has no active token: {task.state.value}"
                )
            if token is not None:
                self._sync_task_token_from_persistence(task, token)
                return token
            if task.cancel_requested_at is not None:
                # A Task's own first persisted cancellation must win over a
                # later ancestor/Run cancellation during process recovery.
                token = CancellationToken()
            elif task.parent_task_id:
                token = self.token_for_task(task.parent_task_id).child()
            else:
                token = self.token_for_run(task.run_id).child()
            self._task_tokens[task_id] = token
            self._sync_task_token_from_persistence(task, token)
            return token

    def _sync_task_token_from_persistence(
        self, task: TaskRecord, token: CancellationToken
    ) -> None:
        if task.cancel_requested_at is None or token.is_cancelled:
            return
        request = self.store.cancellation_request_for(
            task.run_id,
            subject_kind=SubjectKind.TASK,
            subject_id=task.task_id,
        )
        token.request_cancel(
            reason=(
                request.reason
                if request is not None
                else "restored persisted cancellation"
            ),
            requested_by=(
                request.requested_by if request is not None else None
            ),
            requested_at=task.cancel_requested_at,
        )

    def counts(self, run_id: str) -> WorkflowCounts:
        return self.store.counts(run_id)

    def assert_owner(self, run_id: str, owner_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run.owner_id != owner_id:
            raise RuntimeValidationError("Run is not owned by current principal")
        return run
