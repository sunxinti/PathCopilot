"""Durable Agent Runtime adapter for the WSI evaluation backend.

This module adds auditability around the established pathology Agent without
changing its prompts, ROI selection policy, model calls, report generation, or
red/blue/green analysis order.  The original ``run_agent`` remains the only
pathology executor.

The Runtime records:

* one Run per evaluated WSI question;
* one root Orchestrator Task;
* dynamically projected red, blue, and green ROI Agent Tasks;
* explicit parent Task IDs (never inferred from list position);
* durable lifecycle events in SQLite.

It intentionally contains no Flask, Electron, Windows launcher, CLI, or MCP
server code.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from pathcopilot_agent import AgentLifecycleService, LegacyAgentProjector
from pathcopilot_runtime import (
    RunRegistry,
    RunState,
    SQLiteEventStore,
    TaskState,
)


_SAFE_PART = re.compile(r"[^A-Za-z0-9._-]+")
_OWNER_ID = "multipathqa-evaluator"


def _safe_part(value: Any, fallback: str) -> str:
    normalized = _SAFE_PART.sub("-", str(value or "")).strip("-")
    return normalized[:120] or fallback


class RuntimeSocket:
    """Socket.IO-compatible event observer used by the legacy Agent.

    Events are projected into durable Agent Tasks before being forwarded to the
    original socket object.  The observer never alters event payloads.
    """

    def __init__(
        self,
        socketio: Any,
        projector: LegacyAgentProjector,
        on_projection_error: Callable[[Exception], None],
    ) -> None:
        self._socketio = socketio
        self._projector = projector
        self._on_projection_error = on_projection_error

    def emit(
        self,
        event_name: str,
        data: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if isinstance(event_name, str) and event_name.startswith("agent."):
            try:
                self._projector.project(event_name, data)
            except Exception as exc:
                # Runtime is a shadow audit observer.  A persistence failure
                # must never alter the legacy Agent event stream or answer.
                self._on_projection_error(exc)
        return self._socketio.emit(event_name, data, *args, **kwargs)

    def sleep(self, *args: Any, **kwargs: Any) -> Any:
        return self._socketio.sleep(*args, **kwargs)


class EvaluationAgentRuntime:
    """Lifecycle owner for one benchmark sample.

    The class is deliberately synchronous because ``run_multipathqa_eval.py``
    already owns process scheduling and timeout enforcement.  Runtime state is
    an additive audit layer, not a second executor.
    """

    def __init__(
        self,
        *,
        db_path: str | Path,
        benchmark: str,
        sample_id: str,
        file_id: str,
        task_prompt: str,
        wsi_name: str,
        max_steps: int,
        language: str,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = SQLiteEventStore(self.db_path)
        self.registry = RunRegistry(
            self.store,
            max_total_tasks_per_run=2048,
            max_depth=16,
        )
        self.agents = AgentLifecycleService(self.store, self.registry)

        benchmark_part = _safe_part(benchmark, "benchmark")
        sample_part = _safe_part(sample_id, "sample")
        execution_id = uuid.uuid4().hex
        session_id = f"eval-{benchmark_part}-{sample_part}-{execution_id[:12]}"
        request_id = f"{benchmark_part}:{sample_part}:{execution_id}"

        run, _ = self.registry.create_run(
            owner_id=_OWNER_ID,
            session_id=session_id,
            file_id=str(file_id),
            client_request_id=request_id,
            idempotency_key=f"evaluation-run:{request_id}",
            config={
                "runtime_mode": "evaluation-shadow",
                "max_steps": int(max_steps),
                "language": str(language),
            },
            metadata={
                "benchmark": str(benchmark),
                "sample_id": str(sample_id),
                "wsi_name": str(wsi_name),
                "pathology_executor": "agent.agent_loop.run_agent",
            },
        )
        root, _ = self.registry.spawn_legacy_task(
            run_id=run.run_id,
            name="PathCopilot pathology Orchestrator",
            role="pathology.orchestrator",
            idempotency_key=f"evaluation-root:{request_id}",
            metadata={
                "agent": True,
                "projected": False,
                "benchmark": str(benchmark),
                "sample_id": str(sample_id),
            },
        )
        self.run_id = run.run_id
        self.root_task_id = root.task_id
        self.agents.seed_root(
            run_id=self.run_id,
            owner_id=_OWNER_ID,
            root_task_id=self.root_task_id,
            question=str(task_prompt),
            context={
                "benchmark": str(benchmark),
                "sample_id": str(sample_id),
                "file_id": str(file_id),
                "wsi_name": str(wsi_name),
                "max_steps": int(max_steps),
                "language": str(language),
            },
            request_id=request_id,
        )
        self.projector = LegacyAgentProjector(
            self.agents,
            run_id=self.run_id,
            owner_id=_OWNER_ID,
        )
        self._started = False
        self._terminal = False
        self._projection_error_count = 0
        self._last_projection_error_type: str | None = None
        self._projection_errors: list[dict[str, str]] = []

    def wrap_socket(self, socketio: Any) -> RuntimeSocket:
        return RuntimeSocket(
            socketio,
            self.projector,
            self._record_projection_error,
        )

    def _record_projection_error(self, exc: Exception) -> None:
        self._projection_error_count += 1
        self._last_projection_error_type = type(exc).__name__
        if len(self._projection_errors) < 20:
            self._projection_errors.append(
                {
                    "type": type(exc).__name__,
                }
            )

    def start(self) -> None:
        if self._started:
            return
        run = self.store.get_run(self.run_id)
        if run.state is RunState.CREATED:
            self.registry.transition_run(
                self.run_id,
                RunState.QUEUED,
                cause="evaluation sample accepted",
            )
        run = self.store.get_run(self.run_id)
        if run.state is RunState.QUEUED:
            self.registry.transition_run(
                self.run_id,
                RunState.RUNNING,
                cause="legacy pathology Agent started",
            )
        root = self.store.get_task(self.root_task_id)
        if root.state is TaskState.CREATED:
            root = self.registry.transition_task(
                self.root_task_id,
                TaskState.READY,
                cause="evaluation inputs validated",
            )
        if root.state is TaskState.READY:
            self.registry.transition_task(
                self.root_task_id,
                TaskState.RUNNING,
                cause="entered agent.agent_loop.run_agent",
            )
        self._started = True

    def succeed(self, result: Mapping[str, Any] | None = None) -> None:
        if self._terminal:
            return
        self.start()
        self.projector.project("agent.done", {"status": "completed"})
        root = self.store.get_task(self.root_task_id)
        if root.state is TaskState.RUNNING:
            self.registry.transition_task(
                self.root_task_id,
                TaskState.SUCCEEDED,
                cause="legacy pathology Agent completed",
                result=dict(result or {"status": "completed"}),
            )
        run = self.store.get_run(self.run_id)
        if run.state is RunState.RUNNING:
            run = self.registry.transition_run(
                self.run_id,
                RunState.FINALIZING,
                cause="root Agent completed",
            )
        if run.state is RunState.FINALIZING:
            self.registry.transition_run(
                self.run_id,
                RunState.SUCCEEDED,
                cause="evaluation artifacts verified",
            )
        self._terminal = True

    def fail(self, exc: BaseException) -> None:
        if self._terminal:
            return
        self.start()
        self.projector.project("agent.error", {"status": "failed"})
        error = {
            "code": "PATHOLOGY_AGENT_FAILED",
            "message": f"legacy pathology Agent failed ({type(exc).__name__})",
            "category": "evaluation",
            "retryable": False,
        }
        root = self.store.get_task(self.root_task_id)
        if root.state is TaskState.RUNNING:
            self.registry.transition_task(
                self.root_task_id,
                TaskState.FAILED,
                cause="legacy pathology Agent failed",
                error=error,
            )
        run = self.store.get_run(self.run_id)
        if run.state is RunState.RUNNING:
            self.registry.transition_run(
                self.run_id,
                RunState.FAILED,
                cause="root Agent failed",
                error=error,
            )
        self._terminal = True

    def receipt(self) -> dict[str, Any]:
        run = self.store.get_run(self.run_id)
        counts = self.registry.counts(self.run_id)
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "root_task_id": self.root_task_id,
            "run_state": run.state.value,
            "workflow_counts": counts.to_dict(),
            "event_count": run.last_seq,
            "runtime_db": str(self.db_path),
            "integrity": (
                "complete"
                if self._projection_error_count == 0
                else "degraded"
            ),
            "projection_error_count": self._projection_error_count,
            "last_projection_error_type": self._last_projection_error_type,
            "projection_errors": list(self._projection_errors),
        }
