"""Explicit Web-facing service boundary for Runtime integration stage 2C.

No singleton is created and no worker starts at import time.  Flask/Electron
must construct the service and call :meth:`start` during their own lifecycle.
The three modes support a reversible rollout:

``off``
    Existing Web/Agent behavior is untouched and no Runtime database is used.
``shadow``
    Persist a Run/root Task while the existing thread remains the executor.
    The caller mirrors lifecycle completion through the ``shadow_*`` methods.
``managed``
    A DurableTaskExecutor owns the root Task Attempt and invokes the mature
    Agent through :class:`legacy_runtime_adapter.LegacyRuntimeAdapter`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from legacy_runtime_adapter import LegacyRuntimeAdapter
from pathcopilot_agent import AgentLifecycleService
from pathcopilot_runtime import (
    ConcurrencyConflict,
    DurableTaskExecutor,
    EventType,
    IllegalTransition,
    RunRecord,
    RunRegistry,
    RunState,
    RuntimeActivityPayload,
    RuntimeEvent,
    RuntimeEventDraft,
    RuntimeValidationError,
    SQLiteEventStore,
    SubjectKind,
    TaskRecord,
    TaskState,
    Visibility,
    WaitReason,
)


class RuntimeMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    MANAGED = "managed"

    @classmethod
    def parse(cls, value: "RuntimeMode | str") -> "RuntimeMode":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise RuntimeValidationError(
                "runtime mode must be off, shadow, or managed"
            ) from exc


@dataclass(frozen=True)
class RuntimeSubmission:
    mode: RuntimeMode
    accepted: bool
    created: bool
    run_id: str | None = None
    root_task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "accepted": self.accepted,
            "created": self.created,
            "run_id": self.run_id,
            "root_task_id": self.root_task_id,
        }


@dataclass(frozen=True)
class RuntimeStatus:
    mode: RuntimeMode
    run_id: str
    root_task_id: str | None
    run_state: RunState
    root_task_state: TaskState | None
    counts: Mapping[str, int]
    last_seq: int
    cancel_requested: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "run_id": self.run_id,
            "root_task_id": self.root_task_id,
            "run_state": self.run_state.value,
            "root_task_state": (
                self.root_task_state.value
                if self.root_task_state is not None
                else None
            ),
            "counts": dict(self.counts),
            "last_seq": self.last_seq,
            "cancel_requested": self.cancel_requested,
        }


class PathCopilotRuntimeService:
    """One explicit lifecycle owner for Web Runtime integration."""

    ROOT_ROLE = "pathology.orchestrator"
    LEGACY_ROOT_ROLE = "legacy.pathology.run"
    INTEGRATION_VERSION = "2D"

    def __init__(
        self,
        db_path: str | Path,
        *,
        mode: RuntimeMode | str = RuntimeMode.SHADOW,
        legacy_adapter: LegacyRuntimeAdapter | None = None,
        max_workers: int = 2,
        max_total_tasks_per_run: int = 256,
        executor_options: Mapping[str, Any] | None = None,
    ):
        if type(max_total_tasks_per_run) is not int or max_total_tasks_per_run < 1:
            raise ValueError("max_total_tasks_per_run must be an integer >= 1")
        self.db_path = Path(db_path)
        self.mode = RuntimeMode.parse(mode)
        self.legacy_adapter = legacy_adapter
        self.max_workers = max_workers
        self.max_total_tasks_per_run = max_total_tasks_per_run
        self.executor_options = dict(executor_options or {})

        self._lock = threading.RLock()
        self._settle_lock = threading.RLock()
        self._started = False
        self._store: SQLiteEventStore | None = None
        self._registry: RunRegistry | None = None
        self._agents: AgentLifecycleService | None = None
        self._executor: DurableTaskExecutor | None = None
        self._unsubscribe = None

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started

    @property
    def store(self) -> SQLiteEventStore:
        with self._lock:
            if self._store is None:
                raise RuntimeValidationError("Runtime service has not been started")
            return self._store

    @property
    def registry(self) -> RunRegistry:
        with self._lock:
            if self._registry is None:
                raise RuntimeValidationError("Runtime service has not been started")
            return self._registry

    @property
    def agents(self) -> AgentLifecycleService:
        with self._lock:
            if self._agents is None:
                raise RuntimeValidationError("Runtime service has not been started")
            return self._agents

    def start(self) -> None:
        """Initialize persistence and workers exactly once per start cycle."""

        with self._lock:
            if self._started:
                return
            if self.mode is RuntimeMode.OFF:
                self._started = True
                return
            if (
                self.mode is RuntimeMode.MANAGED
                and self.legacy_adapter is None
            ):
                raise RuntimeValidationError(
                    "managed mode requires a LegacyRuntimeAdapter"
                )

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            store = SQLiteEventStore(self.db_path)
            registry = RunRegistry(
                store,
                max_total_tasks_per_run=self.max_total_tasks_per_run,
            )
            agents = AgentLifecycleService(store, registry)
            unsubscribe = store.subscribe(self._on_runtime_event)
            executor = None
            if self.mode is RuntimeMode.MANAGED:
                options = dict(self.executor_options)
                options.setdefault("max_workers", self.max_workers)
                executor = DurableTaskExecutor(store, **options)
                executor.register_handler(
                    self.ROOT_ROLE, self.legacy_adapter.execute
                )
                # Existing databases can contain a 2C root role.  Keep it
                # recoverable while all new submissions use the 2D
                # Orchestrator identity.
                executor.register_handler(
                    self.LEGACY_ROOT_ROLE, self.legacy_adapter.execute
                )

            self._store = store
            self._registry = registry
            self._agents = agents
            self._unsubscribe = unsubscribe
            self._executor = executor
            self._started = True
            if executor is not None:
                executor.start()

    def stop(
        self,
        *,
        wait: bool = True,
        timeout: float | None = 10.0,
    ) -> bool:
        """Stop claiming work; return ``False`` if a handler is still alive."""

        with self._lock:
            if not self._started:
                return True
            executor = self._executor
            if executor is not None:
                stopped = executor.shutdown(wait=wait, timeout=timeout)
                if not stopped:
                    return False
            if self._unsubscribe is not None:
                self._unsubscribe()
            self._unsubscribe = None
            self._executor = None
            self._agents = None
            self._registry = None
            self._store = None
            self._started = False
            return True

    def _require_started(self) -> None:
        if not self.started:
            raise RuntimeValidationError("Runtime service has not been started")

    @staticmethod
    def _required_text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise RuntimeValidationError(f"{label} is required")
        return value.strip()

    def submit(
        self,
        *,
        owner_id: str,
        session_id: str,
        file_id: str,
        request_id: str,
        legacy_kwargs: Mapping[str, Any] | None = None,
        activate: bool = True,
    ) -> RuntimeSubmission:
        """Idempotently accept one Web request and create its root Task."""

        self._require_started()
        if self.mode is RuntimeMode.OFF:
            return RuntimeSubmission(
                mode=self.mode,
                accepted=False,
                created=False,
            )

        owner_id = self._required_text(owner_id, "owner_id")
        session_id = self._required_text(session_id, "session_id")
        file_id = self._required_text(file_id, "file_id")
        request_id = self._required_text(request_id, "request_id")
        if legacy_kwargs is not None and not isinstance(legacy_kwargs, Mapping):
            raise RuntimeValidationError("legacy_kwargs must be a mapping")
        if not isinstance(activate, bool):
            raise RuntimeValidationError("activate must be a boolean")

        run, run_created = self.registry.create_run(
            owner_id=owner_id,
            session_id=session_id,
            file_id=file_id,
            idempotency_key=(
                f"web-runtime:{owner_id}:{session_id}:{request_id}"
            ),
            client_request_id=request_id,
            config={
                "runtime_mode": self.mode.value,
                "integration_version": self.INTEGRATION_VERSION,
            },
            metadata={
                "request_id": request_id,
                "integration_version": self.INTEGRATION_VERSION,
            },
        )
        spawn = (
            self.registry.spawn_task
            if self.mode is RuntimeMode.MANAGED
            else self.registry.spawn_legacy_task
        )
        task, task_created = spawn(
            run_id=run.run_id,
            name="PathCopilot pathology analysis",
            role=self.ROOT_ROLE,
            idempotency_key=f"web-root:{owner_id}:{request_id}",
            metadata={
                "request_id": request_id,
                "integration_version": self.INTEGRATION_VERSION,
            },
        )
        safe_context = {
            "file_id": file_id,
            "session_id": session_id,
            "request_id": request_id,
            "runtime_mode": self.mode.value,
        }
        question = None
        if legacy_kwargs is not None:
            for key in ("mode", "lang", "strategy", "max_steps"):
                value = legacy_kwargs.get(key)
                if isinstance(value, (str, int, float, bool)) or value is None:
                    safe_context[key] = value
            raw_question = legacy_kwargs.get("diagnostic_question")
            if isinstance(raw_question, str) and raw_question.strip():
                question = raw_question.strip()[:65536]
        self.agents.seed_root(
            run_id=run.run_id,
            owner_id=owner_id,
            root_task_id=task.task_id,
            question=question,
            context=safe_context,
            request_id=request_id,
        )

        # Bind process-local dependencies before READY makes the Task claimable.
        if (
            self.mode is RuntimeMode.MANAGED
            and not task.is_terminal
            and not self.legacy_adapter.has_binding(task.task_id)
        ):
            self.legacy_adapter.bind(
                run_id=run.run_id,
                task_id=task.task_id,
                owner_id=owner_id,
                session_id=session_id,
                file_id=file_id,
                kwargs=legacy_kwargs,
            )

        if activate:
            self._prepare_submission(run.run_id, task.task_id)
        return RuntimeSubmission(
            mode=self.mode,
            accepted=True,
            created=run_created or task_created,
            run_id=run.run_id,
            root_task_id=task.task_id,
        )

    def activate(
        self,
        run_id: str,
        *,
        owner_id: str,
    ) -> RuntimeStatus:
        """Make a bound submission claimable after Web state is registered."""

        run = self.assert_owner(run_id, owner_id)
        if run.root_task_id is None:
            raise RuntimeValidationError("Runtime Run has no root Task")
        if (
            self.mode is RuntimeMode.MANAGED
            and self.legacy_adapter is not None
            and not self.legacy_adapter.has_binding(run.root_task_id)
        ):
            raise RuntimeValidationError(
                "managed root Task requires a process-local legacy binding"
            )
        self._prepare_submission(run.run_id, run.root_task_id)
        return self.status(run.run_id, owner_id=owner_id)

    def _prepare_submission(self, run_id: str, task_id: str) -> None:
        run = self.store.get_run(run_id)
        if run.state is RunState.CREATED:
            try:
                self.registry.transition_run(
                    run_id, RunState.QUEUED, cause="Web request accepted"
                )
            except (ConcurrencyConflict, IllegalTransition):
                pass
        task = self.store.get_task(task_id)
        if task.state is TaskState.CREATED:
            try:
                self.registry.transition_task(
                    task_id,
                    TaskState.READY,
                    cause="root dependencies satisfied",
                )
            except (ConcurrencyConflict, IllegalTransition):
                pass
        elif (
            task.state is TaskState.WAITING
            and task.wait_reason is WaitReason.RESOURCE
        ):
            try:
                self.registry.transition_task(
                    task_id,
                    TaskState.READY,
                    cause="process-local legacy binding restored",
                )
            except (ConcurrencyConflict, IllegalTransition):
                pass
        if self.mode is RuntimeMode.MANAGED:
            run = self.store.get_run(run_id)
            if run.state is RunState.QUEUED:
                try:
                    self.registry.transition_run(
                        run_id,
                        RunState.RUNNING,
                        cause="managed executor enabled",
                    )
                except (ConcurrencyConflict, IllegalTransition):
                    pass

    def assert_owner(self, run_id: str, owner_id: str) -> RunRecord:
        self._require_started()
        if self.mode is RuntimeMode.OFF:
            raise RuntimeValidationError("Runtime integration is disabled")
        return self.registry.assert_owner(
            self._required_text(run_id, "run_id"),
            self._required_text(owner_id, "owner_id"),
        )

    def active_run_for_session(self, session_id: str) -> RunRecord | None:
        """Resolve durable Socket-room ownership after a Web restart.

        This is a server-internal authorization primitive.  Callers compare
        the returned owner with the authenticated principal and never expose
        the record directly to an untrusted client.
        """

        self._require_started()
        if self.mode is RuntimeMode.OFF:
            return None
        return self.store.find_active_run_by_session(
            session_id=self._required_text(session_id, "session_id")
        )

    def existing_submission(
        self,
        *,
        owner_id: str,
        session_id: str,
        file_id: str,
        request_id: str,
    ) -> RuntimeSubmission | None:
        """Return a previously accepted Web command without creating work."""

        self._require_started()
        if self.mode is RuntimeMode.OFF:
            return None
        owner_id = self._required_text(owner_id, "owner_id")
        session_id = self._required_text(session_id, "session_id")
        file_id = self._required_text(file_id, "file_id")
        request_id = self._required_text(request_id, "request_id")
        run = self.store.find_run_by_idempotency(
            owner_id=owner_id,
            idempotency_key=(
                f"web-runtime:{owner_id}:{session_id}:{request_id}"
            ),
        )
        if run is None:
            return None
        if (
            run.session_id != session_id
            or run.file_id != file_id
            or run.client_request_id != request_id
        ):
            raise RuntimeValidationError(
                "idempotency key conflicts with a different Web request"
            )
        return RuntimeSubmission(
            mode=self.mode,
            accepted=True,
            created=False,
            run_id=run.run_id,
            root_task_id=run.root_task_id,
        )

    def status(self, run_id: str, *, owner_id: str) -> RuntimeStatus:
        run = self.assert_owner(run_id, owner_id)
        if self.mode is RuntimeMode.MANAGED:
            self._settle_managed_run(run.run_id)
            run = self.registry.assert_owner(run.run_id, owner_id)
        root = (
            self.store.get_task(run.root_task_id)
            if run.root_task_id is not None
            else None
        )
        return RuntimeStatus(
            mode=self.mode,
            run_id=run.run_id,
            root_task_id=run.root_task_id,
            run_state=run.state,
            root_task_state=root.state if root is not None else None,
            counts=self.registry.counts(run.run_id).to_dict(),
            last_seq=run.last_seq,
            cancel_requested=run.cancel_requested_at is not None,
        )

    def events(
        self,
        run_id: str,
        *,
        owner_id: str,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[RuntimeEvent]:
        run = self.assert_owner(run_id, owner_id)
        return self.store.read_events(
            run.run_id, after_seq=after_seq, limit=limit
        )

    def cancel(
        self,
        run_id: str,
        *,
        owner_id: str,
        requested_by: str | None = None,
        reason: str = "user requested stop",
    ) -> RuntimeStatus:
        run = self.assert_owner(run_id, owner_id)
        saved = self.registry.request_cancel(
            run.run_id,
            requested_by=requested_by or owner_id,
            reason=reason,
        )
        if (
            self.legacy_adapter is not None
            and not saved.is_terminal
        ):
            self.legacy_adapter.request_stop(saved.run_id)
        status = self.status(saved.run_id, owner_id=owner_id)
        self._release_terminal_binding(saved.run_id)
        return status

    def record_activity(
        self,
        run_id: str,
        *,
        owner_id: str,
        stage: str,
        summary: str,
        details: Mapping[str, Any] | None = None,
        visibility: Visibility = Visibility.USER,
        idempotency_key: str | None = None,
    ) -> RuntimeEvent:
        """Persist one bridge-safe activity fact for replay."""

        run = self.assert_owner(run_id, owner_id)
        stage = self._required_text(stage, "stage")
        summary = self._required_text(summary, "summary")
        if details is not None and not isinstance(details, Mapping):
            raise RuntimeValidationError("activity details must be a mapping")
        return self.store.append(
            RuntimeEventDraft(
                run_id=run.run_id,
                event_type=EventType.RUNTIME_ACTIVITY_RECORDED,
                payload=RuntimeActivityPayload(
                    stage=stage,
                    summary=summary,
                    details=dict(details or {}),
                ),
                subject_kind=SubjectKind.TASK,
                subject_id=run.root_task_id,
                visibility=Visibility(visibility),
                idempotency_key=idempotency_key,
            )
        )

    def _require_shadow(self) -> None:
        self._require_started()
        if self.mode is not RuntimeMode.SHADOW:
            raise RuntimeValidationError(
                "shadow lifecycle commands require shadow mode"
            )

    def shadow_started(self, run_id: str, *, owner_id: str) -> RuntimeStatus:
        self._require_shadow()
        run = self.assert_owner(run_id, owner_id)
        if run.is_terminal:
            return self.status(run_id, owner_id=owner_id)
        if run.state is RunState.QUEUED:
            run = self.registry.transition_run(
                run.run_id,
                RunState.RUNNING,
                cause="legacy Agent thread started",
            )
        task = self.store.get_task(run.root_task_id)
        if task.state is TaskState.READY:
            self.registry.transition_task(
                task.task_id,
                TaskState.RUNNING,
                cause="legacy Agent entered run_agent",
            )
        return self.status(run.run_id, owner_id=owner_id)

    def shadow_succeeded(
        self,
        run_id: str,
        *,
        owner_id: str,
        result: Mapping[str, Any] | None = None,
    ) -> RuntimeStatus:
        self._require_shadow()
        self.shadow_started(run_id, owner_id=owner_id)
        run = self.assert_owner(run_id, owner_id)
        if run.is_terminal:
            return self.status(run_id, owner_id=owner_id)
        task = self.store.get_task(run.root_task_id)
        if task.state is TaskState.RUNNING:
            self.registry.transition_task(
                task.task_id,
                TaskState.SUCCEEDED,
                cause="legacy Agent completed",
                result=dict(result or {"legacy_status": "completed"}),
            )
        run = self.store.get_run(run.run_id)
        if run.state is RunState.RUNNING:
            run = self.registry.transition_run(
                run.run_id,
                RunState.FINALIZING,
                cause="legacy root Task completed",
            )
        if run.state is RunState.FINALIZING:
            self.registry.transition_run(
                run.run_id,
                RunState.SUCCEEDED,
                cause="shadow lifecycle reconciled",
            )
        return self.status(run.run_id, owner_id=owner_id)

    def shadow_failed(
        self,
        run_id: str,
        *,
        owner_id: str,
        code: str = "LEGACY_AGENT_FAILED",
        message: str = "legacy Agent failed",
    ) -> RuntimeStatus:
        self._require_shadow()
        self.shadow_started(run_id, owner_id=owner_id)
        run = self.assert_owner(run_id, owner_id)
        if run.is_terminal:
            return self.status(run_id, owner_id=owner_id)
        error = {
            "code": self._required_text(code, "code"),
            "message": self._required_text(message, "message"),
            "category": "legacy_adapter",
            "retryable": False,
        }
        task = self.store.get_task(run.root_task_id)
        if task.state is TaskState.RUNNING:
            self.registry.transition_task(
                task.task_id,
                TaskState.FAILED,
                cause="legacy Agent failed",
                error=error,
            )
        run = self.store.get_run(run.run_id)
        if run.state is RunState.RUNNING:
            self.registry.transition_run(
                run.run_id,
                RunState.FAILED,
                cause="legacy root Task failed",
                error=error,
            )
        return self.status(run.run_id, owner_id=owner_id)

    def shadow_cancelled(
        self,
        run_id: str,
        *,
        owner_id: str,
        requested_by: str | None = None,
        reason: str = "legacy Agent stopped",
    ) -> RuntimeStatus:
        self._require_shadow()
        run = self.assert_owner(run_id, owner_id)
        if not run.is_terminal and run.state is not RunState.CANCELLING:
            self.registry.request_cancel(
                run.run_id,
                requested_by=requested_by or owner_id,
                reason=reason,
            )
        run = self.store.get_run(run.run_id)
        if run.root_task_id is not None:
            task = self.store.get_task(run.root_task_id)
            if task.state is TaskState.CANCELLING:
                self.registry.complete_task_cancellation(task.task_id)
        self.registry.finalize_cancel_if_quiescent(run.run_id)
        return self.status(run.run_id, owner_id=owner_id)

    def _on_runtime_event(self, event: RuntimeEvent) -> None:
        if (
            self.mode is RuntimeMode.MANAGED
            and event.event_type is EventType.TASK_STATUS_CHANGED
        ):
            self._settle_managed_run(event.run_id)

    def _release_terminal_binding(self, run_id: str) -> None:
        if self.legacy_adapter is None:
            return
        try:
            run = self.store.get_run(run_id)
        except Exception:
            return
        if run.is_terminal:
            self.legacy_adapter.finalize(run.run_id, run.root_task_id)

    def _settle_managed_run(self, run_id: str) -> None:
        """Project the terminal root Attempt into its owning Run."""

        with self._settle_lock:
            run = self.store.get_run(run_id)
            if run.is_terminal:
                self._release_terminal_binding(run.run_id)
                return
            if run.root_task_id is None:
                return
            task = self.store.get_task(run.root_task_id)
            if not task.is_terminal:
                return
            if run.state is RunState.CANCELLING:
                self.registry.finalize_cancel_if_quiescent(run.run_id)
                self._release_terminal_binding(run.run_id)
                return
            if task.state is TaskState.SUCCEEDED:
                if run.state is RunState.QUEUED:
                    run = self.registry.transition_run(
                        run.run_id,
                        RunState.RUNNING,
                        cause="root Attempt completed",
                    )
                if run.state is RunState.RUNNING:
                    run = self.registry.transition_run(
                        run.run_id,
                        RunState.FINALIZING,
                        cause="managed root Task completed",
                    )
                if run.state is RunState.FINALIZING:
                    self.registry.transition_run(
                        run.run_id,
                        RunState.SUCCEEDED,
                        cause="managed lifecycle reconciled",
                    )
            elif task.state is TaskState.FAILED:
                # 2B recovery normally settles required Task failure in the
                # same transaction.  This fallback covers externally imported
                # legacy states without weakening the core state machine.
                current = self.store.get_run(run.run_id)
                if not current.is_terminal:
                    error = dict(task.error or {
                        "code": "LEGACY_ROOT_FAILED",
                        "message": "managed legacy root Task failed",
                        "category": "legacy_adapter",
                        "retryable": False,
                    })
                    self.registry.transition_run(
                        current.run_id,
                        RunState.FAILED,
                        cause="managed root Task failed",
                        error=error,
                    )
            self._release_terminal_binding(run.run_id)
