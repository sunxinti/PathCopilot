"""Patient-level adapter for the PathCopilot Agent Runtime.

The established ``run_patient_level_agent`` remains the only pathology
executor.  Shadow Runtime owns durable Run/Task identity and projects the
existing event stream into this hierarchy without changing prompts, model
calls, ROI selection, cache behavior, concurrency, or report synthesis::

    Patient Orchestrator
      +-- WSI Agent 1
      |     +-- Red ROI Agents -> Blue ROI Agents -> Green ROI Agents
      +-- WSI Agent 2
            +-- Red ROI Agents -> Blue ROI Agents -> Green ROI Agents

Repeated ROI identifiers are scoped to their WSI Agent, which is essential
because concurrently analysed slides each begin with identifiers such as R1.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from runtime_event_bridge import RuntimeEventBridge
from runtime_integration import PathCopilotRuntimeService, RuntimeMode
from pathcopilot_runtime import TaskState


LOGGER = logging.getLogger(__name__)
PATIENT_EVALUATION_MAX_TOTAL_TASKS = 2048


class PatientRuntimeSocket:
    """Socket-compatible Runtime projector for one Patient-Level Run."""

    def __init__(
        self,
        socketio: Any,
        service: PathCopilotRuntimeService,
        *,
        run_id: str,
        root_task_id: str,
        owner_id: str,
        patient_room: str,
        patient_id: str,
        file_infos: Sequence[Mapping[str, Any]],
    ) -> None:
        self._socketio = socketio
        self._service = service
        self.run_id = str(run_id)
        self.root_task_id = str(root_task_id)
        self.owner_id = str(owner_id)
        self.patient_room = str(patient_room)
        self.patient_id = str(patient_id)
        self._lock = threading.RLock()
        self._active = True
        self._slide_tasks: dict[int, str] = {}
        self._slide_file_ids: dict[int, str] = {}
        self._worker_bridges: dict[str, RuntimeEventBridge] = {}
        self._patient_bridge = RuntimeEventBridge(
            socketio,
            service,
            run_id=self.run_id,
            owner_id=self.owner_id,
            strict_persistence=False,
            root_task_id=self.root_task_id,
            projector_scope=f"patient:{self.patient_id}",
            authority_guard=self._is_active,
        )
        self._prepare_slides(file_infos)

    def sleep(self, *args: Any, **kwargs: Any) -> Any:
        return self._socketio.sleep(*args, **kwargs)

    def emit(
        self,
        event_name: str,
        data: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        with self._lock:
            if not self._active:
                return self._socketio.emit(event_name, data, *args, **kwargs)

            target_room = kwargs.get("to")
            if isinstance(target_room, str):
                worker_bridge = self._worker_bridges.get(target_room)
                if worker_bridge is not None:
                    return worker_bridge.emit(event_name, data, *args, **kwargs)

            try:
                if event_name == "agent.wsi_started" and isinstance(data, Mapping):
                    self._start_slide(data)
                elif event_name == "agent.wsi_completed" and isinstance(data, Mapping):
                    self._finish_slide(data)
            except Exception:
                # Runtime is an additive shadow control plane.  Projection failure
                # must never suppress or duplicate the mature pathology execution.
                LOGGER.exception(
                    "Patient Runtime WSI lifecycle projection failed; preserving "
                    "the established Patient-Level event stream"
                )
            return self._patient_bridge.emit(event_name, data, *args, **kwargs)

    def _is_active(self) -> bool:
        with self._lock:
            return self._active

    def deactivate(self) -> None:
        """Atomically detach late worker events from the Runtime control plane."""

        with self._lock:
            self._active = False

    @property
    def terminal_event_name(self) -> str | None:
        return self._patient_bridge.terminal_event_name

    @property
    def slide_task_ids(self) -> dict[int, str]:
        with self._lock:
            return dict(self._slide_tasks)

    def _prepare_slides(self, file_infos: Sequence[Mapping[str, Any]]) -> None:
        for fallback_index, raw_info in enumerate(file_infos):
            info = dict(raw_info)
            index = int(info.get("index", fallback_index))
            file_id = str(info.get("file_id") or "").strip()
            if not file_id:
                raise ValueError(f"Missing file_id for Patient-Level slide {index}")
            filename = Path(str(info.get("filename") or f"WSI_{index + 1}")).name
            part = str(info.get("part") or f"Slide {index + 1}").strip()
            task, _ = self._service.agents.spawn_agent(
                run_id=self.run_id,
                owner_id=self.owner_id,
                parent_task_id=self.root_task_id,
                role="pathology.wsi",
                name=f"WSI Agent {index + 1}: {part}",
                context={
                    "patient_id": self.patient_id,
                    "wsi_index": index,
                    "file_id": file_id,
                    "filename": filename,
                    "part": part,
                    "level": "wsi",
                },
                idempotency_key=f"patient-wsi:{self.patient_id}:{index}:{file_id}",
                projected=True,
            )
            worker_room = self._worker_room(index)
            bridge = RuntimeEventBridge(
                self._socketio,
                self._service,
                run_id=self.run_id,
                owner_id=self.owner_id,
                strict_persistence=False,
                root_task_id=task.task_id,
                projector_scope=f"patient:{self.patient_id}:wsi:{index}:{file_id}",
                authority_guard=self._is_active,
            )
            self._slide_tasks[index] = task.task_id
            self._slide_file_ids[index] = file_id
            self._worker_bridges[worker_room] = bridge

    def _worker_room(self, index: int) -> str:
        return f"{self.patient_room}:patient:{self.patient_id}:wsi:{index}"

    def _task_for_payload(self, data: Mapping[str, Any]) -> str:
        index = int(data.get("index", -1))
        task_id = self._slide_tasks.get(index)
        if task_id is None:
            raise KeyError(f"Unknown Patient-Level WSI index: {index}")
        observed_file_id = str(data.get("file_id") or "").strip()
        expected_file_id = self._slide_file_ids[index]
        if observed_file_id and observed_file_id != expected_file_id:
            raise ValueError(
                f"WSI lifecycle file_id mismatch at index {index}: "
                f"{observed_file_id!r} != {expected_file_id!r}"
            )
        return task_id

    def _start_slide(self, data: Mapping[str, Any]) -> None:
        with self._lock:
            task_id = self._task_for_payload(data)
            self._service.agents.start_agent(
                run_id=self.run_id,
                owner_id=self.owner_id,
                task_id=task_id,
            )

    def _finish_slide(self, data: Mapping[str, Any]) -> None:
        with self._lock:
            task_id = self._task_for_payload(data)
            task = self._service.agents.runtime_store.get_task(task_id)
            if task.is_terminal:
                return
            self._service.agents.start_agent(
                run_id=self.run_id,
                owner_id=self.owner_id,
                task_id=task_id,
            )
            error = str(data.get("error") or "").strip()
            if error:
                self._service.agents.fail_agent(
                    run_id=self.run_id,
                    owner_id=self.owner_id,
                    task_id=task_id,
                    code="WSI_ANALYSIS_FAILED",
                    message=error,
                )
            else:
                self._service.agents.complete_agent(
                    run_id=self.run_id,
                    owner_id=self.owner_id,
                    task_id=task_id,
                    summary=None,
                    result={
                        "status": "completed",
                        "file_id": self._slide_file_ids[int(data.get("index", -1))],
                        "from_cache": bool(data.get("from_cache")),
                    },
                )

    def reconcile_slides(self, result: Any) -> None:
        """Settle WSI Tasks when a cache-only path emitted no slide events."""

        result_map = dict(result) if isinstance(result, Mapping) else {}
        error_file_ids = {
            str(value) for value in result_map.get("error_file_ids", [])
        }
        overall_failed = result_map.get("status") not in {"completed", "partial"}
        with self._lock:
            for index, task_id in self._slide_tasks.items():
                task = self._service.agents.runtime_store.get_task(task_id)
                if task.is_terminal:
                    continue
                file_id = self._slide_file_ids[index]
                self._service.agents.start_agent(
                    run_id=self.run_id,
                    owner_id=self.owner_id,
                    task_id=task_id,
                )
                if overall_failed or file_id in error_file_ids:
                    self._service.agents.fail_agent(
                        run_id=self.run_id,
                        owner_id=self.owner_id,
                        task_id=task_id,
                        code="WSI_RESULT_INCOMPLETE",
                        message=(
                            "Patient-Level result marked this WSI incomplete "
                            "without a terminal slide event"
                        ),
                    )
                else:
                    self._service.agents.complete_agent(
                        run_id=self.run_id,
                        owner_id=self.owner_id,
                        task_id=task_id,
                        summary=None,
                        result={
                            "status": "completed",
                            "file_id": file_id,
                            "reconciled_from_patient_result": True,
                            "from_cache": bool(result_map.get("cached")),
                        },
                    )


    def reconcile_interrupt(self) -> None:
        """Terminalize every open Task from the leaves upward after Ctrl+C."""

        error = {
            "code": "EVALUATION_INTERRUPTED",
            "message": "Patient-Level evaluation was interrupted",
            "category": "legacy_adapter",
            "retryable": False,
        }

        def child_tasks(task_id: str):
            return [
                child
                for child in self._service.store.list_tasks(self.run_id)
                if child.parent_task_id == task_id
            ]

        def force_fail(task_id: str, seen: set[str]) -> None:
            if task_id in seen:
                return
            seen.add(task_id)
            for child in sorted(
                child_tasks(task_id),
                key=lambda item: item.depth,
                reverse=True,
            ):
                force_fail(child.task_id, seen)
            task = self._service.store.get_task(task_id)
            if task.is_terminal:
                return
            try:
                if task.state is TaskState.CREATED:
                    self._service.agents.start_agent(
                        run_id=self.run_id,
                        owner_id=self.owner_id,
                        task_id=task_id,
                    )
                    task = self._service.store.get_task(task_id)
                    if task.is_terminal:
                        return
                if task.state in {
                    TaskState.READY,
                    TaskState.RUNNING,
                    TaskState.WAITING,
                    TaskState.CANCELLING,
                }:
                    self._service.registry.transition_task(
                        task.task_id,
                        TaskState.FAILED,
                        cause="evaluation interrupted",
                        error=error,
                    )
            except Exception:
                LOGGER.exception(
                    "Patient Runtime could not terminalize task %s "
                    "during interrupt; continuing with remaining tasks",
                    task_id,
                )

        with self._lock:
            seen: set[str] = set()
            tasks = list(self._service.store.list_tasks(self.run_id))
            roots = [task for task in tasks if task.parent_task_id is None]
            for root in roots or tasks:
                force_fail(root.task_id, seen)
            for task in sorted(tasks, key=lambda item: item.depth, reverse=True):
                if not task.is_terminal:
                    force_fail(task.task_id, seen)


class PatientLevelAgentRuntime:
    """Lifecycle owner for terminal Patient-Level Auto evaluation."""

    def __init__(self, db_path: str | Path, *, mode: str = "shadow") -> None:
        parsed = RuntimeMode.parse(mode)
        if parsed is RuntimeMode.MANAGED:
            raise ValueError(
                "The terminal Patient-Level evaluator supports off/shadow only; "
                "managed mode is reserved for worker-owned application requests."
            )
        self.service = PathCopilotRuntimeService(
            db_path,
            mode=parsed,
            max_total_tasks_per_run=PATIENT_EVALUATION_MAX_TOTAL_TASKS,
        )
        self.mode = parsed
        self.available = parsed is RuntimeMode.OFF
        self.start_error: str | None = None
        self.last_run_id: str | None = None

    def start(self) -> None:
        if self.mode is RuntimeMode.OFF:
            self.service.start()
            self.available = True
            return
        try:
            self.service.start()
            self.available = True
        except Exception as exc:
            self.available = False
            self.start_error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception(
                "Patient Agent Runtime shadow initialization failed; "
                "preserving the established evaluation path"
            )

    def stop(self) -> None:
        if not self.service.started:
            return
        try:
            self.service.stop(wait=True, timeout=10.0)
        except Exception:
            LOGGER.exception("Patient Agent Runtime shutdown failed")

    def run(
        self,
        runner: Callable[..., Any],
        *,
        socketio: Any,
        room: str,
        patient_id: str,
        file_infos: Sequence[Mapping[str, Any]],
        runner_kwargs: Mapping[str, Any],
        request_id: str | None = None,
        owner_id: str = "pumch-patient-cli",
    ) -> tuple[Any, dict[str, Any]]:
        """Run the established Patient-Level Agent exactly once."""

        kwargs = dict(runner_kwargs)
        if self.mode is RuntimeMode.OFF or not self.available:
            result = runner(socketio=socketio, room=room, **kwargs)
            return result, self._receipt(
                enabled=False,
                error=self.start_error or "",
            )

        request_id = request_id or uuid.uuid4().hex
        try:
            submission = self.service.submit(
                owner_id=owner_id,
                session_id=room,
                file_id=f"patient:{patient_id}",
                request_id=request_id,
                legacy_kwargs={"lang": kwargs.get("lang", "zh")},
                activate=True,
            )
            if not submission.run_id or not submission.root_task_id:
                raise RuntimeError("Patient Runtime submission has no Run/root Task")
            self.last_run_id = submission.run_id
            self.service.agents.seed_root(
                run_id=submission.run_id,
                owner_id=owner_id,
                root_task_id=submission.root_task_id,
                question=None,
                context={
                    "patient_id": patient_id,
                    "patient_identifier": str(kwargs.get("folder_name") or ""),
                    "total_wsi": len(file_infos),
                    "mode": "ai",
                    "lang": kwargs.get("lang", "zh"),
                    "wsi_schedule": str(kwargs.get("wsi_schedule") or "parallel"),
                    "api_max_concurrency": kwargs.get("api_max_concurrency"),
                },
                request_id=f"{request_id}:patient-context",
            )
            bridge = PatientRuntimeSocket(
                socketio,
                self.service,
                run_id=submission.run_id,
                root_task_id=submission.root_task_id,
                owner_id=owner_id,
                patient_room=room,
                patient_id=patient_id,
                file_infos=file_infos,
            )
            self.service.shadow_started(submission.run_id, owner_id=owner_id)
        except Exception as exc:
            # Setup failed before the pathology call, so legacy fallback cannot
            # duplicate model work.
            LOGGER.exception(
                "Patient Agent Runtime shadow submission failed; running the "
                "established Patient-Level Agent without Runtime projection"
            )
            result = runner(socketio=socketio, room=room, **kwargs)
            return result, self._receipt(
                enabled=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        try:
            result = runner(socketio=bridge, room=room, **kwargs)
        except KeyboardInterrupt:
            try:
                from agent.gpt_client import request_api_interrupt
                request_api_interrupt()
            except Exception:
                LOGGER.debug("API interrupt signal was unavailable", exc_info=True)
            try:
                bridge.deactivate()
                bridge.reconcile_interrupt()
                self.service.shadow_failed(
                    submission.run_id,
                    owner_id=owner_id,
                    code="PATIENT_AGENT_INTERRUPTED",
                    message="Patient-Level evaluation was interrupted",
                )
            except Exception:
                LOGGER.exception("Patient Runtime interrupt reconciliation failed")
            raise
        except Exception:
            try:
                self.service.shadow_failed(
                    submission.run_id,
                    owner_id=owner_id,
                    code="PATIENT_AGENT_FAILED",
                    message="Patient-Level pathology Agent raised an exception",
                )
            except Exception:
                LOGGER.exception("Patient Runtime failure reconciliation failed")
            raise

        try:
            bridge.reconcile_slides(result)
            completed = (
                isinstance(result, Mapping)
                and result.get("status") == "completed"
                and bool(result.get("success", False))
            )
            if completed and bridge.terminal_event_name != "agent.error":
                status = self.service.shadow_succeeded(
                    submission.run_id,
                    owner_id=owner_id,
                    result={
                        "legacy_status": "completed",
                        "patient_id": patient_id,
                        "wsi_count": len(file_infos),
                    },
                )
            else:
                status = self.service.shadow_failed(
                    submission.run_id,
                    owner_id=owner_id,
                    code="PATIENT_AGENT_INCOMPLETE",
                    message="Patient-Level evaluation did not complete successfully",
                )
            return result, self._receipt(
                enabled=True,
                run_id=submission.run_id,
                run_state=status.run_state.value,
                task_count=len(
                    self.service.store.list_tasks(submission.run_id)
                ),
            )
        except Exception as exc:
            # Shadow reconciliation never changes an already completed report.
            LOGGER.exception(
                "Patient Agent Runtime completion reconciliation failed; "
                "preserving the pathology result"
            )
            return result, self._receipt(
                enabled=True,
                run_id=submission.run_id,
                run_state="unknown",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _receipt(
        self,
        *,
        enabled: bool,
        run_id: str = "",
        run_state: str = "",
        task_count: int = 0,
        error: str = "",
    ) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "enabled": enabled,
            "run_id": run_id,
            "run_state": run_state,
            "task_count": task_count,
            "error": error,
        }


__all__ = ["PatientLevelAgentRuntime", "PatientRuntimeSocket"]
