"""Safe bridge from the mature Socket.IO Agent stream to Runtime events.

The legacy pathology Agent already emits a rich UI stream.  Runtime 2C must
not reinterpret that stream as lifecycle authority: Run/Task/Attempt events
remain authoritative.  This adapter records a small, versioned activity fact
*before* forwarding the original Socket.IO event, which gives the Web layer a
replay cursor without persisting images, reports, prompts, paths, secrets, or
hidden model reasoning.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from pathcopilot_agent import LegacyAgentProjector
from pathcopilot_runtime import RuntimeEvent, Visibility
from runtime_integration import PathCopilotRuntimeService, RuntimeMode


LOGGER = logging.getLogger(__name__)

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_SECRET_TEXT = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]+|"
    r"(?:api[_ -]?key|access[_ -]?token|authorization|password|secret)"
    r"\s*[:=]\s*\S+)"
)
_WINDOWS_PATH = re.compile(r"(?i)(?:[A-Z]:\\|\\\\)[^\s,;]+")
_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:[^/\s]+/)+[^/\s,;]*")

_COUNT_FIELDS = frozenset(
    {
        "total",
        "completed",
        "running",
        "pending",
        "failed",
        "cancelled",
        "selected",
        "count",
        "current",
        "step",
        "max_steps",
        "depth",
        "level",
        "zoom",
        "percent",
        "progress",
        "query_length",
    }
)
_LABEL_FIELDS = frozenset(
    {
        "status",
        "phase",
        "mode",
        "strategy",
        "kind",
        "role",
        "state",
        "color",
        "action",
        "server",
        "tool",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_id",
        "artifact_ids",
        "thumbnail_artifact_id",
        "report_artifact_id",
        "overlay_artifact_id",
    }
)

_ACTIVITY_CONTRACTS: dict[str, tuple[str, str, Visibility]] = {
    "agent.progress": (
        "legacy.v1.progress",
        "Legacy Agent progress updated",
        Visibility.USER,
    ),
    "agent.phase2_progress": (
        "legacy.v1.roi.blue.progress",
        "Blue ROI analysis progress updated",
        Visibility.USER,
    ),
    "agent.phase3_progress": (
        "legacy.v1.roi.green.progress",
        "Green ROI analysis progress updated",
        Visibility.USER,
    ),
    "agent.phase_result": (
        "legacy.v1.phase.result",
        "An analysis phase produced a result",
        Visibility.USER,
    ),
    "agent.viewport": (
        "legacy.v1.viewport.updated",
        "Pathology viewport overlay updated",
        Visibility.USER,
    ),
    "agent.trace": (
        "legacy.v1.trace",
        "Internal Agent trace recorded",
        Visibility.INTERNAL,
    ),
    "agent.message": (
        "legacy.v1.message",
        "Agent message emitted",
        Visibility.USER,
    ),
    "agent.orchestrator_reply": (
        "legacy.v1.orchestrator.reply",
        "Orchestrator response emitted",
        Visibility.USER,
    ),
    "agent.red_analysis": (
        "legacy.v1.roi.red.completed",
        "Red ROI analysis updated",
        Visibility.USER,
    ),
    "agent.blue_analysis": (
        "legacy.v1.roi.blue.completed",
        "Blue ROI analysis updated",
        Visibility.USER,
    ),
    "agent.green_analysis": (
        "legacy.v1.roi.green.completed",
        "Green ROI analysis updated",
        Visibility.USER,
    ),
    "agent.confirm_selection": (
        "legacy.v1.confirmation.selection.requested",
        "ROI selection confirmation requested",
        Visibility.USER,
    ),
    "agent.confirm_analysis": (
        "legacy.v1.confirmation.analysis.requested",
        "Analysis confirmation requested",
        Visibility.USER,
    ),
    "agent.all_blue_selected": (
        "legacy.v1.confirmation.blue.requested",
        "Blue ROI confirmation requested",
        Visibility.USER,
    ),
    "agent.all_green_selected": (
        "legacy.v1.confirmation.green.requested",
        "Green ROI confirmation requested",
        Visibility.USER,
    ),
    "agent.knowledge_retrieval": (
        "legacy.v1.knowledge.progress",
        "Knowledge retrieval updated",
        Visibility.INTERNAL,
    ),
    "agent.mcp_tool": (
        "agent.v1.mcp.tool",
        "Agent MCP tool call updated",
        Visibility.INTERNAL,
    ),
    "agent.native_tool": (
        "agent.v1.native.tool",
        "Agent API-native tool call updated",
        Visibility.INTERNAL,
    ),
    "agent.wsi_started": (
        "legacy.v1.wsi.started",
        "Whole-slide analysis started",
        Visibility.USER,
    ),
    "agent.wsi_completed": (
        "legacy.v1.wsi.completed",
        "Whole-slide analysis completed",
        Visibility.USER,
    ),
    "agent.report_ready": (
        "legacy.v1.report.ready",
        "Pathology report artifact is ready",
        Visibility.USER,
    ),
    "agent.done": (
        "legacy.v1.completed",
        "Legacy Agent completed",
        Visibility.USER,
    ),
    "agent.error": (
        "legacy.v1.error",
        "Legacy Agent reported an error",
        Visibility.USER,
    ),
}

_LATE_AFTER_TERMINAL = frozenset(
    {
        "agent.progress",
        "agent.phase2_progress",
        "agent.phase3_progress",
        "agent.trace",
        "agent.knowledge_retrieval",
        "agent.mcp_tool",
        "agent.native_tool",
    }
)
_TERMINAL_EVENTS = frozenset({"agent.done", "agent.error"})


def _safe_identifier(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        if _SAFE_IDENTIFIER.fullmatch(value):
            return value
    return None


def _redact_text(value: Any, *, maximum: int = 240) -> str | None:
    """Return a short display-safe label, never a free-form persisted report."""

    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    if not text:
        return None
    text = _SECRET_TEXT.sub("[REDACTED]", text)
    text = _WINDOWS_PATH.sub("[PATH]", text)
    text = _POSIX_PATH.sub("[PATH]", text)
    return text[:maximum]


def _safe_details(payload: Any, *, source_event: str) -> dict[str, Any]:
    """Extract only bounded metrics, labels, and opaque Artifact references."""

    details: dict[str, Any] = {
        "source_event": source_event,
        "source_version": 1,
    }
    if not isinstance(payload, Mapping):
        return details

    for key in _COUNT_FIELDS:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            details[key] = value

    for key in _LABEL_FIELDS:
        label = _redact_text(payload.get(key), maximum=80)
        if label is not None:
            details[key] = label

    for key in _ARTIFACT_FIELDS:
        value = payload.get(key)
        if isinstance(value, str):
            identifier = _safe_identifier(value)
            if identifier is not None:
                details[key] = identifier
        elif isinstance(value, (list, tuple)):
            identifiers = [
                identifier
                for identifier in (_safe_identifier(item) for item in value[:64])
                if identifier is not None
            ]
            if identifiers:
                details[key] = identifiers

    logical_id = None
    for key in ("logical_event_id", "activity_id", "event_id"):
        logical_id = _safe_identifier(payload.get(key))
        if logical_id is not None:
            break
    if logical_id is not None:
        details["logical_event_id"] = logical_id
    return details


class RuntimeEventBridge:
    """Socket.IO-compatible proxy with durable, privacy-bounded activities."""

    def __init__(
        self,
        socketio: Any,
        service: PathCopilotRuntimeService,
        *,
        run_id: str,
        owner_id: str,
        strict_persistence: bool | None = None,
        authority_guard: Callable[[], bool] | None = None,
        bridge_id: str | None = None,
        root_task_id: str | None = None,
        projector_scope: str | None = None,
    ):
        self._socketio = socketio
        self._service = service
        self.run_id = str(run_id)
        self.owner_id = str(owner_id)
        self._strict = (
            service.mode is RuntimeMode.MANAGED
            if strict_persistence is None
            else bool(strict_persistence)
        )
        self._authority_guard = authority_guard
        self._bridge_id = bridge_id or uuid.uuid4().hex
        self._lock = threading.RLock()
        self._emission_no = 0
        self._terminal_seen = False
        self._terminal_event_name: str | None = None
        self._agent_projector = LegacyAgentProjector(
            service.agents,
            run_id=self.run_id,
            owner_id=self.owner_id,
            root_task_id=root_task_id,
            scope=projector_scope,
        )

    @property
    def terminal_event_name(self) -> str | None:
        with self._lock:
            return self._terminal_event_name

    def sleep(self, *args: Any, **kwargs: Any) -> Any:
        return self._socketio.sleep(*args, **kwargs)

    def emit(
        self,
        event_name: str,
        data: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Persist an Agent activity before forwarding its legacy UI event."""

        if not isinstance(event_name, str) or not event_name.startswith("agent."):
            return self._socketio.emit(event_name, data, *args, **kwargs)

        with self._lock:
            if self._terminal_seen and event_name in _LATE_AFTER_TERMINAL:
                LOGGER.debug("Dropped late Agent event after terminal: %s", event_name)
                return None
            self._emission_no += 1
            emission_no = self._emission_no

            if self._authority_guard is not None and not self._authority_guard():
                LOGGER.warning(
                    "Skipped Runtime projection for non-authoritative Agent event "
                    "while preserving the legacy UI event for run %s: %s",
                    self.run_id,
                    event_name,
                )
                return self._socketio.emit(event_name, data, *args, **kwargs)

            runtime_event: RuntimeEvent | None = None
            try:
                # Child Agent identity and state are committed before the
                # corresponding UI event is visible.  Socket.IO remains a
                # presentation stream, never lifecycle authority.
                self._agent_projector.project(event_name, data)
            except Exception:
                LOGGER.exception(
                    "Runtime Agent projection failed; preserving the mature "
                    "pathology calculation and legacy Socket event"
                )
            try:
                runtime_event = self._record(event_name, data, emission_no)
            except Exception:
                if self._strict:
                    raise
                LOGGER.exception(
                    "Runtime shadow activity persistence failed; preserving "
                    "the mature pathology calculation and legacy Socket event"
                )

            forwarded = data
            if runtime_event is not None and isinstance(data, Mapping):
                forwarded = dict(data)
                forwarded["runtime"] = {
                    "event_id": runtime_event.event_id,
                    "run_id": runtime_event.run_id,
                    "seq": runtime_event.seq,
                    "schema_version": runtime_event.schema_version,
                }

            if event_name in _TERMINAL_EVENTS:
                self._terminal_seen = True
                self._terminal_event_name = event_name
            return self._socketio.emit(
                event_name,
                forwarded,
                *args,
                **kwargs,
            )

    def _record(
        self,
        event_name: str,
        data: Any,
        emission_no: int,
    ) -> RuntimeEvent:
        stage, summary, visibility = _ACTIVITY_CONTRACTS.get(
            event_name,
            (
                "legacy.v1.notice",
                "Legacy Agent UI event emitted",
                Visibility.INTERNAL,
            ),
        )
        details = _safe_details(data, source_event=event_name)
        logical_id = details.get("logical_event_id")
        suffix = logical_id or f"{self._bridge_id}:{emission_no}"
        return self._service.record_activity(
            self.run_id,
            owner_id=self.owner_id,
            stage=stage,
            summary=summary,
            details=details,
            visibility=visibility,
            idempotency_key=f"legacy-activity:v1:{suffix}",
        )


__all__ = ["RuntimeEventBridge"]
