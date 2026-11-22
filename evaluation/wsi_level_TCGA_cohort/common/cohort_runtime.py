"""TCGA cohort adapter for the open-source PathCopilot Agent Runtime.

The mature cancer-specific ``run_agent`` remains the only pathology executor.
This adapter mirrors the application's default shadow mode: it creates a
durable Run/root Agent, projects red/blue/green events into Agent Tasks, and
records a privacy-bounded SQLite event stream without changing model prompts.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from runtime_event_bridge import RuntimeEventBridge
from runtime_integration import PathCopilotRuntimeService, RuntimeMode


LOGGER = logging.getLogger(__name__)


class CohortAgentRuntime:
    """Lifecycle owner for sequential cohort runs using Runtime shadow mode."""

    def __init__(self, db_path: str | Path, *, mode: str = "shadow"):
        parsed = RuntimeMode.parse(mode)
        if parsed is RuntimeMode.MANAGED:
            raise ValueError(
                "The headless cohort runner supports off/shadow only; managed "
                "mode is reserved for the application's worker-owned Web path."
            )
        self.service = PathCopilotRuntimeService(db_path, mode=parsed)
        self.mode = parsed
        self.available = parsed is RuntimeMode.OFF
        self.start_error: str | None = None

    def start(self) -> None:
        if self.mode is RuntimeMode.OFF:
            self.service.start()
            self.available = True
            return
        try:
            self.service.start()
            self.available = True
        except Exception as exc:
            # Match the open-source application's shadow fail-open contract.
            self.available = False
            self.start_error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception(
                "Agent Runtime shadow initialization failed; preserving the "
                "mature cohort execution path"
            )

    def stop(self) -> None:
        if not self.service.started:
            return
        try:
            self.service.stop(wait=True, timeout=10.0)
        except Exception:
            LOGGER.exception("Agent Runtime shutdown failed")

    def run(
        self,
        runner: Callable[..., Any],
        *,
        socketio: Any,
        room: str,
        file_id: str,
        runner_kwargs: Mapping[str, Any],
        request_id: str | None = None,
        owner_id: str = "tcga-cohort-cli",
    ) -> tuple[Any, dict[str, Any]]:
        """Run the legacy Agent exactly once and return a Runtime receipt."""

        kwargs = dict(runner_kwargs)
        kwargs.setdefault("file_id", file_id)
        if self.mode is RuntimeMode.OFF or not self.available:
            result = runner(socketio=socketio, room=room, **kwargs)
            return result, {
                "mode": self.mode.value,
                "enabled": False,
                "run_id": "",
                "run_state": "",
                "error": self.start_error or "",
            }

        request_id = request_id or uuid.uuid4().hex
        try:
            submission = self.service.submit(
                owner_id=owner_id,
                session_id=room,
                file_id=file_id,
                request_id=request_id,
                legacy_kwargs={"socketio": socketio, "room": room, **kwargs},
                activate=True,
            )
            bridge = RuntimeEventBridge(
                socketio,
                self.service,
                run_id=submission.run_id,
                owner_id=owner_id,
                strict_persistence=False,
            )
            self.service.shadow_started(submission.run_id, owner_id=owner_id)
        except Exception as exc:
            # Setup failed before the pathology call, so legacy fallback is safe
            # and cannot duplicate model work.
            LOGGER.exception(
                "Agent Runtime shadow submission failed; running the mature "
                "cohort Agent without Runtime projection"
            )
            result = runner(socketio=socketio, room=room, **kwargs)
            return result, {
                "mode": self.mode.value,
                "enabled": False,
                "run_id": "",
                "run_state": "",
                "error": f"{type(exc).__name__}: {exc}",
            }

        try:
            result = runner(socketio=bridge, room=room, **kwargs)
        except Exception:
            try:
                self.service.shadow_failed(
                    submission.run_id,
                    owner_id=owner_id,
                    code="LEGACY_AGENT_FAILED",
                    message="cohort pathology Agent raised an exception",
                )
            except Exception:
                LOGGER.exception("Agent Runtime failure reconciliation failed")
            raise
        except BaseException:
            try:
                projector = getattr(bridge, "_agent_projector", None)
                finalize = getattr(projector, "_finalize_children", None)
                if callable(finalize):
                    finalize(failed=True)
            except Exception:
                LOGGER.exception(
                    "Agent Runtime interrupt child finalization failed"
                )
            try:
                self.service.shadow_failed(
                    submission.run_id,
                    owner_id=owner_id,
                    code="LEGACY_AGENT_INTERRUPTED",
                    message="cohort pathology Agent was interrupted",
                )
            except Exception:
                LOGGER.exception(
                    "Agent Runtime interrupt reconciliation failed"
                )
            raise

        try:
            if bridge.terminal_event_name == "agent.error":
                status = self.service.shadow_failed(
                    submission.run_id,
                    owner_id=owner_id,
                    code="LEGACY_AGENT_ERROR_EVENT",
                    message="cohort pathology Agent emitted agent.error",
                )
            else:
                status = self.service.shadow_succeeded(
                    submission.run_id,
                    owner_id=owner_id,
                    result={
                        "legacy_status": "completed",
                        "returned_mapping": isinstance(result, Mapping),
                    },
                )
            return result, {
                "mode": self.mode.value,
                "enabled": True,
                "run_id": submission.run_id,
                "run_state": status.run_state.value,
                "error": "",
            }
        except Exception as exc:
            # The pathology result is authoritative in shadow mode.
            LOGGER.exception(
                "Agent Runtime completion reconciliation failed; preserving "
                "the completed pathology result"
            )
            return result, {
                "mode": self.mode.value,
                "enabled": True,
                "run_id": submission.run_id,
                "run_state": "unknown",
                "error": f"{type(exc).__name__}: {exc}",
            }


__all__ = ["CohortAgentRuntime"]
