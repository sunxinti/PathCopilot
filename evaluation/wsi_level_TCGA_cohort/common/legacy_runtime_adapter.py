"""Compatibility adapter between the durable Runtime and the mature Agent.

The pathology Agent remains the work function.  This module only translates
Runtime execution/cancellation into that function's existing call contract.
Invocation arguments are deliberately kept in memory: Socket.IO objects,
credentials, and other process-local dependencies must never be serialized
into the Runtime database.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from pathcopilot_runtime import (
    ExecutionContext,
    RuntimeValidationError,
    TaskRecord,
    WaitReason,
    YieldExecution,
)


LOGGER = logging.getLogger(__name__)

LegacyRunner = Callable[..., Any]
LegacyStopCallback = Callable[[str], Any]
InvocationLoader = Callable[[TaskRecord], Mapping[str, Any] | None]
ResultMapper = Callable[[Any], Mapping[str, Any]]


@dataclass(frozen=True)
class LegacyInvocation:
    """One process-local binding for an accepted Runtime root Task."""

    run_id: str
    task_id: str
    owner_id: str
    session_id: str
    file_id: str
    kwargs: Mapping[str, Any]


def _default_result_mapper(result: Any) -> Mapping[str, Any]:
    """Persist a bounded completion receipt, not the pathology payload.

    The mature Agent already writes reports/trees/images to its established
    stores.  Persisting its entire return object in ``runtime_tasks`` would
    duplicate potentially large or sensitive material.
    """

    return {
        "legacy_status": "completed",
        "returned_mapping": isinstance(result, Mapping),
    }


def _validate_result(result: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise RuntimeValidationError(
            "legacy result mapper must return a result mapping"
        )
    normalized = dict(result)
    try:
        json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeValidationError(
            "legacy result mapper returned non-JSON data"
        ) from exc
    return normalized


class LegacyRuntimeAdapter:
    """Execute the unchanged legacy runner under a fenced Runtime Attempt.

    ``ExecutionContext.checkpoint()`` runs immediately before and after the
    legacy call.  Therefore a result produced after cancellation or lease loss
    cannot be committed: the executor's existing Attempt fencing remains the
    sole authority for the terminal write.
    """

    def __init__(
        self,
        legacy_runner: LegacyRunner,
        *,
        stop_callback: LegacyStopCallback | None = None,
        invocation_loader: InvocationLoader | None = None,
        result_mapper: ResultMapper | None = None,
        inject_runtime_context: bool = False,
    ):
        if not callable(legacy_runner):
            raise RuntimeValidationError("legacy_runner must be callable")
        if stop_callback is not None and not callable(stop_callback):
            raise RuntimeValidationError("stop_callback must be callable")
        if invocation_loader is not None and not callable(invocation_loader):
            raise RuntimeValidationError("invocation_loader must be callable")
        if result_mapper is not None and not callable(result_mapper):
            raise RuntimeValidationError("result_mapper must be callable")
        if not isinstance(inject_runtime_context, bool):
            raise RuntimeValidationError(
                "inject_runtime_context must be a boolean"
            )

        self._legacy_runner = legacy_runner
        self._stop_callback = stop_callback
        self._invocation_loader = invocation_loader
        self._result_mapper = result_mapper or _default_result_mapper
        self._inject_runtime_context = inject_runtime_context
        self._lock = threading.RLock()
        self._invocations: dict[str, LegacyInvocation] = {}
        self._task_id_by_run_id: dict[str, str] = {}
        self._stop_requested: set[str] = set()
        self._stop_delivered: set[str] = set()
        self._stop_delivering: set[str] = set()

    def bind(
        self,
        *,
        run_id: str,
        task_id: str,
        owner_id: str,
        session_id: str,
        file_id: str,
        kwargs: Mapping[str, Any] | None = None,
    ) -> LegacyInvocation:
        """Bind process-local dependencies before the Task becomes READY."""

        values = (run_id, task_id, owner_id, session_id, file_id)
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise RuntimeValidationError(
                "run_id, task_id, owner_id, session_id, and file_id are required"
            )
        if kwargs is not None and not isinstance(kwargs, Mapping):
            raise RuntimeValidationError("legacy invocation kwargs must be a mapping")

        invocation = LegacyInvocation(
            run_id=run_id.strip(),
            task_id=task_id.strip(),
            owner_id=owner_id.strip(),
            session_id=session_id.strip(),
            file_id=file_id.strip(),
            kwargs=dict(kwargs or {}),
        )
        deliver_stop = False
        with self._lock:
            existing = self._invocations.get(invocation.task_id)
            if existing is not None:
                if (
                    existing.run_id != invocation.run_id
                    or existing.owner_id != invocation.owner_id
                    or existing.session_id != invocation.session_id
                    or existing.file_id != invocation.file_id
                ):
                    raise RuntimeValidationError(
                        "Task already has a different legacy invocation"
                    )
                return existing
            other_task_id = self._task_id_by_run_id.get(invocation.run_id)
            if other_task_id is not None and other_task_id != invocation.task_id:
                raise RuntimeValidationError(
                    "Run already has a different legacy root invocation"
                )
            self._invocations[invocation.task_id] = invocation
            self._task_id_by_run_id[invocation.run_id] = invocation.task_id
            deliver_stop = invocation.run_id in self._stop_requested
        if deliver_stop:
            self._deliver_stop(invocation)
        return invocation

    def has_binding(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._invocations

    def request_stop(self, run_id: str) -> bool:
        """Synchronize a persisted Runtime cancellation to ``stop_agent``.

        The callback is delivered at most once per bound invocation.  Returning
        ``False`` means no invocation was currently bound; the request is kept
        and will be delivered if a binding appears later.
        """

        if not isinstance(run_id, str) or not run_id.strip():
            raise RuntimeValidationError("run_id is required")
        normalized = run_id.strip()
        with self._lock:
            self._stop_requested.add(normalized)
            task_id = self._task_id_by_run_id.get(normalized)
            invocation = (
                self._invocations.get(task_id) if task_id is not None else None
            )
        if invocation is None:
            return False
        return self._deliver_stop(invocation)

    def _deliver_stop(self, invocation: LegacyInvocation) -> bool:
        with self._lock:
            if (
                invocation.task_id in self._stop_delivered
                or invocation.task_id in self._stop_delivering
            ):
                return False
            self._stop_delivering.add(invocation.task_id)
        if self._stop_callback is None:
            with self._lock:
                self._stop_delivering.discard(invocation.task_id)
            return False
        try:
            self._stop_callback(invocation.session_id)
        except Exception:
            # Runtime cancellation remains authoritative even if the legacy
            # cooperative stop hook is broken.
            LOGGER.exception(
                "Legacy stop callback failed for run %s", invocation.run_id
            )
            with self._lock:
                self._stop_delivering.discard(invocation.task_id)
            return False
        with self._lock:
            self._stop_delivering.discard(invocation.task_id)
            self._stop_delivered.add(invocation.task_id)
        return True

    def finalize(self, run_id: str, task_id: str | None = None) -> None:
        """Release all process-local state after a terminal Runtime outcome."""

        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return
        with self._lock:
            bound_task_id = (
                str(task_id).strip()
                if task_id is not None and str(task_id).strip()
                else self._task_id_by_run_id.get(normalized_run_id)
            )
            if bound_task_id:
                self._invocations.pop(bound_task_id, None)
                self._stop_delivered.discard(bound_task_id)
                self._stop_delivering.discard(bound_task_id)
            if self._task_id_by_run_id.get(normalized_run_id) == bound_task_id:
                self._task_id_by_run_id.pop(normalized_run_id, None)
            self._stop_requested.discard(normalized_run_id)

    def _load_invocation(
        self, context: ExecutionContext
    ) -> LegacyInvocation:
        with self._lock:
            invocation = self._invocations.get(context.task.task_id)
        if invocation is not None:
            return invocation
        if self._invocation_loader is None:
            raise YieldExecution(
                WaitReason.RESOURCE,
                cause="legacy invocation awaits process-local Web binding",
            )

        kwargs = self._invocation_loader(context.task)
        if kwargs is None:
            raise YieldExecution(
                WaitReason.RESOURCE,
                cause="legacy invocation loader awaits recoverable binding",
            )
        run = context.store.get_run(context.task.run_id)
        return self.bind(
            run_id=run.run_id,
            task_id=context.task.task_id,
            owner_id=run.owner_id,
            session_id=run.session_id,
            file_id=run.file_id,
            kwargs=kwargs,
        )

    def execute(self, context: ExecutionContext) -> Mapping[str, Any]:
        """DurableTaskExecutor role handler for the mature Agent."""

        invocation = self._load_invocation(context)
        remove_cancel_callback = context.token.add_callback(
            lambda _info: self._deliver_stop(invocation)
        )
        try:
            context.checkpoint()
            try:
                runner_kwargs = dict(invocation.kwargs)
                if self._inject_runtime_context:
                    # These values are process-local execution capabilities.
                    # They are never serialized into Task metadata or events.
                    runner_kwargs.update({
                        "_runtime_run_id": invocation.run_id,
                        "_runtime_task_id": invocation.task_id,
                        "_runtime_owner_id": invocation.owner_id,
                        "_runtime_context": context,
                    })
                raw_result = self._legacy_runner(**runner_kwargs)
            except Exception as exc:
                # Do not persist exception text: upstream SDK failures can
                # contain credentials or private request content.
                raise RuntimeValidationError(
                    f"legacy runner failed ({type(exc).__name__})"
                ) from exc
            context.checkpoint()
            return _validate_result(self._result_mapper(raw_result))
        finally:
            remove_cancel_callback()
            self.finalize(invocation.run_id, invocation.task_id)
