"""Project the mature pathology event stream into first-class Agent Tasks."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping
from typing import Any

from pathcopilot_runtime import TaskState, WaitReason

from .models import AgentMessageKind, AgentMessageRole
from .service import AgentLifecycleService


_SAFE_ROI = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_ROLE_BY_LEVEL = {
    "red": ("pathology.roi.red", 10),
    "blue": ("pathology.roi.blue", 40),
    "green": ("pathology.roi.green", 160),
}


class LegacyAgentProjector:
    """Keep legacy calculation intact while Runtime owns Agent identities."""

    def __init__(
        self,
        agents: AgentLifecycleService,
        *,
        run_id: str,
        owner_id: str,
    ):
        self.agents = agents
        self.run_id = run_id
        self.owner_id = owner_id
        self._lock = threading.RLock()
        self._task_by_roi: dict[str, str] = {}
        self._level_by_roi: dict[str, str] = {}
        self._load_existing()

    def project(self, event_name: str, data: Any) -> None:
        if not isinstance(data, Mapping):
            if event_name in {"agent.done", "agent.error"}:
                self._finalize_children(failed=event_name == "agent.error")
            return
        with self._lock:
            if event_name == "agent.message":
                self._project_message(data)
            elif event_name == "agent.red_analysis":
                self._project_red(data)
            elif event_name == "agent.blue_analysis":
                self._project_blue(data)
            elif event_name == "agent.green_analysis":
                self._project_green(data)
            elif event_name == "agent.done":
                self._finalize_children(failed=False)
            elif event_name == "agent.error":
                self._finalize_children(failed=True)

    def _project_message(self, data: Mapping[str, Any]) -> None:
        level = str(data.get("level") or "").strip().lower()
        roi_id = self._roi(data.get("roi_id"))
        parent_id = self._roi(
            data.get("parent_id")
            or (data.get("metadata") or {}).get("parent_id")
        )
        is_progress = bool(data.get("is_progress")) or data.get("type") == "progress"
        is_result = bool(data.get("is_result"))
        regions = data.get("regions")
        selection_parent = None
        if (
            level == "blue"
            and roi_id
            and isinstance(regions, (list, tuple))
        ):
            # Legacy emits a Blue-selection message before red_analysis. In
            # that event roi_id identifies the parent Red ROI, not a Blue ROI.
            selection_parent = self._ensure("red", roi_id, parent_id=parent_id)
            self.agents.start_agent(
                run_id=self.run_id,
                owner_id=self.owner_id,
                task_id=selection_parent.task_id,
            )
            # Temporary grid names such as ``R2_sub_r2_c0`` are not stable
            # Agent identities. Record this event as planning evidence and
            # let later analysis events create the stable child.
        elif (
            level == "green"
            and roi_id
            and isinstance(regions, (list, tuple))
        ):
            # Likewise roi_id is the parent Blue ROI for Green selection.
            selection_parent = self._ensure("blue", roi_id, parent_id=parent_id)
            self.agents.start_agent(
                run_id=self.run_id,
                owner_id=self.owner_id,
                task_id=selection_parent.task_id,
            )
            # Temporary green grid names must not create Runtime Agents.

        if (
            selection_parent is None
            and level in _ROLE_BY_LEVEL
            and roi_id
            and not roi_id.startswith("AI-")
        ):
            task = self._ensure(level, roi_id, parent_id=parent_id)
            if is_progress:
                self.agents.start_agent(
                    run_id=self.run_id,
                    owner_id=self.owner_id,
                    task_id=task.task_id,
                )
            elif is_result:
                self.agents.start_agent(
                    run_id=self.run_id,
                    owner_id=self.owner_id,
                    task_id=task.task_id,
                )
                # A legacy result message is presentation output, not a
                # lifecycle boundary.  Blue/Green child events can arrive
                # afterwards, so completion is driven by explicit analysis
                # events or the terminal reconciliation pass.

        content = self._public_text(data.get("content"))
        if (
            content
            and data.get("type") in {"assistant", "system"}
            and not is_progress
        ):
            run = self.agents.runtime_store.get_run(self.run_id)
            root_task_id = run.root_task_id
            if root_task_id:
                target_task_id = (
                    selection_parent.task_id
                    if selection_parent is not None
                    else (
                        self._task_by_roi.get(roi_id, root_task_id)
                        if roi_id
                        else root_task_id
                    )
                )
                self.agents.agent_store.append_message(
                    run_id=self.run_id,
                    task_id=target_task_id,
                    sender_task_id=target_task_id,
                    role=AgentMessageRole.ASSISTANT,
                    kind=(
                        AgentMessageKind.EVIDENCE
                        if is_result
                        else AgentMessageKind.MESSAGE
                    ),
                    content_text=content,
                    payload={
                        "source": "legacy.agent.message",
                        "level": level or None,
                        "roi_id": roi_id,
                    },
                    idempotency_key=(
                        "legacy-message:"
                        f"{roi_id or 'root'}:{self._digest({'content': content, 'level': level, 'is_result': is_result})}"
                    ),
                )

    def _project_red(self, data: Mapping[str, Any]) -> None:
        roi_id = self._roi(data.get("roi_id"))
        if not roi_id:
            return
        red = self._ensure("red", roi_id)
        red = self.agents.start_agent(
            run_id=self.run_id,
            owner_id=self.owner_id,
            task_id=red.task_id,
        )
        # ``blue_ids`` here are still temporary grid names such as
        # ``R8_sub_r2_c0``. Creating Tasks from them would duplicate the
        # later stable ``R8-B11`` Agent. Record the planned count only.
        planned_blue_count = self._planned_child_count(
            data.get("blue_ids"), data.get("blue_count")
        )
        observation = self._public_text(data.get("observation"))
        if observation:
            self.agents.agent_store.snapshot_context(
                run_id=self.run_id,
                task_id=red.task_id,
                context={
                    **dict(
                        (
                            self.agents.agent_store.latest_context(
                                self.run_id, red.task_id
                            )
                            or _EmptyContext()
                        ).context
                    ),
                    "evidence_summary": observation,
                },
                idempotency_key=(
                    f"red-evidence:{roi_id}:{self._digest(observation)}"
                ),
            )
        if planned_blue_count > 0:
            self._await_declared_children(
                red,
                planned_child_count=planned_blue_count,
                planned_child_level="blue",
                cause=(
                    "Red Agent planned "
                    f"{planned_blue_count} Blue child Agent(s)"
                ),
            )
        else:
            self.agents.complete_agent(
                run_id=self.run_id,
                owner_id=self.owner_id,
                task_id=red.task_id,
                summary=observation,
                result={"status": "completed", "roi_id": roi_id},
            )

    def _project_blue(self, data: Mapping[str, Any]) -> None:
        blue_id = self._roi(data.get("blue_id"))
        parent_id = self._roi(data.get("parent_roi_id"))
        if not blue_id:
            return
        blue = self._ensure("blue", blue_id, parent_id=parent_id)
        blue = self.agents.start_agent(
            run_id=self.run_id,
            owner_id=self.owner_id,
            task_id=blue.task_id,
        )
        summary = self._public_text(
            data.get("findings") or data.get("observation")
        )
        if bool(data.get("no_green_needed")):
            self.agents.complete_agent(
                run_id=self.run_id,
                owner_id=self.owner_id,
                task_id=blue.task_id,
                summary=summary,
                result={"status": "completed", "roi_id": blue_id},
            )
            return
        if summary:
            latest = self.agents.agent_store.latest_context(
                self.run_id, blue.task_id
            )
            self.agents.agent_store.snapshot_context(
                run_id=self.run_id,
                task_id=blue.task_id,
                parent_snapshot_id=(
                    latest.snapshot_id if latest is not None else None
                ),
                context={
                    **dict(latest.context if latest is not None else {}),
                    "evidence_summary": summary,
                },
                idempotency_key=(
                    f"blue-evidence:{blue_id}:{self._digest(summary)}"
                ),
            )
        planned_green_count = self._positive_int(data.get("green_count"))
        if planned_green_count is not None:
            self._await_declared_children(
                blue,
                planned_child_count=planned_green_count,
                planned_child_level="green",
                cause=(
                    "Blue Agent planned "
                    f"{planned_green_count} Green child Agent(s)"
                ),
            )
            return
        self._await_declared_children(
            blue,
            planned_child_count_unknown=True,
            cause="Blue Agent planned Green child Agents of unknown count",
        )

    def _project_green(self, data: Mapping[str, Any]) -> None:
        green_id = self._roi(data.get("green_id"))
        parent_id = self._roi(data.get("parent_blue_id"))
        if not green_id:
            return
        green = self._ensure("green", green_id, parent_id=parent_id)
        self.agents.start_agent(
            run_id=self.run_id,
            owner_id=self.owner_id,
            task_id=green.task_id,
        )
        summary = self._public_text(
            data.get("findings") or data.get("observation")
        )
        self.agents.complete_agent(
            run_id=self.run_id,
            owner_id=self.owner_id,
            task_id=green.task_id,
            summary=summary,
            result={"status": "completed", "roi_id": green_id},
        )

    def _await_declared_children(
        self,
        task,
        *,
        cause: str,
        planned_child_count: int | None = None,
        planned_child_level: str | None = None,
        planned_child_count_unknown: bool = False,
    ) -> None:
        metadata_patch: dict[str, Any] = {}
        if planned_child_count_unknown:
            metadata_patch["planned_child_count_unknown"] = True
        elif planned_child_count is not None:
            metadata_patch["planned_child_count"] = planned_child_count
            if planned_child_level:
                metadata_patch["planned_child_level"] = planned_child_level
        if not metadata_patch:
            return
        current = self.agents.runtime_store.get_task(task.task_id)
        if current.state is TaskState.RUNNING:
            self.agents.registry.transition_task(
                current.task_id,
                TaskState.WAITING,
                cause=cause,
                wait_reason=WaitReason.CHILDREN,
                metadata_patch=metadata_patch,
            )

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        if type(value) is int and value > 0:
            return value
        return None

    @classmethod
    def _planned_child_count(cls, child_ids: Any, child_count: Any) -> int:
        if isinstance(child_ids, (list, tuple)):
            return len(child_ids)
        return cls._positive_int(child_count) or 0

    def _ensure(
        self, level: str, roi_id: str, *, parent_id: str | None = None
    ):
        existing_id = self._task_by_roi.get(roi_id)
        if existing_id is not None:
            existing_level = self._level_by_roi.get(roi_id)
            if existing_level and existing_level != level:
                raise RuntimeError(
                    f"ROI {roi_id!r} was projected as both "
                    f"{existing_level!r} and {level!r}"
                )
            return self.agents.runtime_store.get_task(existing_id)
        run = self.agents.runtime_store.get_run(self.run_id)
        parent_task_id = (
            self._task_by_roi.get(parent_id) if parent_id else run.root_task_id
        )
        if parent_task_id is None:
            raise RuntimeError("Runtime Run has no root Agent")
        role, magnification = _ROLE_BY_LEVEL[level]
        task, _ = self.agents.spawn_agent(
            run_id=self.run_id,
            owner_id=self.owner_id,
            parent_task_id=parent_task_id,
            role=role,
            name=f"{level.capitalize()} ROI Agent {roi_id}",
            context={
                "roi_id": roi_id,
                "parent_roi_id": parent_id,
                "level": level,
                "magnification": magnification,
            },
            idempotency_key=f"legacy-roi-agent:{level}:{roi_id}",
            projected=True,
        )
        self._task_by_roi[roi_id] = task.task_id
        self._level_by_roi[roi_id] = level
        return task

    def _finalize_children(self, *, failed: bool) -> None:
        run = self.agents.runtime_store.get_run(self.run_id)
        # Reconcile repeatedly from leaves to roots.  One pass is insufficient:
        # settling a leaf can make its parent eligible later in the same
        # terminal event, while optimistic versions may have changed.
        for _ in range(8):
            changed = False
            all_tasks = self.agents.runtime_store.list_tasks(self.run_id)
            tasks = sorted(all_tasks, key=lambda item: item.depth, reverse=True)
            for task in tasks:
                if task.task_id == run.root_task_id or task.is_terminal:
                    continue
                current = self.agents.runtime_store.get_task(task.task_id)
                if current.is_terminal:
                    continue
                children = [
                    candidate
                    for candidate in self.agents.runtime_store.list_tasks(self.run_id)
                    if candidate.parent_task_id == current.task_id
                ]
                if any(not child.is_terminal for child in children):
                    continue
                child_failed = any(
                    child.state in {TaskState.FAILED, TaskState.CANCELLED}
                    for child in children
                )
                try:
                    if failed or child_failed:
                        self.agents.fail_agent(
                            run_id=self.run_id,
                            owner_id=self.owner_id,
                            task_id=current.task_id,
                            code="LEGACY_AGENT_ABORTED",
                            message=(
                                "Pathology Agent stopped or a required child "
                                "did not complete successfully"
                            ),
                        )
                    else:
                        self.agents.complete_agent(
                            run_id=self.run_id,
                            owner_id=self.owner_id,
                            task_id=current.task_id,
                            summary=None,
                            result={
                                "status": "completed",
                                "projected_at_terminal": True,
                            },
                        )
                    changed = True
                except Exception:
                    # A concurrently settling ancestor may have advanced the
                    # version.  The next pass refetches the authoritative row.
                    continue
            if not changed:
                break

    def _load_existing(self) -> None:
        for task in self.agents.runtime_store.list_tasks(self.run_id):
            roi_id = self._roi(task.metadata.get("roi_id"))
            if roi_id:
                self._task_by_roi[roi_id] = task.task_id
                level = str(task.metadata.get("level") or "").strip().lower()
                if level in _ROLE_BY_LEVEL:
                    self._level_by_roi[roi_id] = level

    @staticmethod
    def _roi(value: Any) -> str | None:
        if isinstance(value, str):
            value = value.strip()
            if _SAFE_ROI.fullmatch(value):
                return value
        return None

    @staticmethod
    def _public_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        return text[:16000]

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]


class _EmptyContext:
    context: Mapping[str, Any] = {}
