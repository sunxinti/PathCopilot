"""Durable Agent control plane built on Runtime Run/Task authority."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from pathcopilot_runtime import (
    ConcurrencyConflict,
    IllegalTransition,
    RunRegistry,
    SQLiteEventStore,
    TaskRecord,
    TaskState,
    WaitReason,
)
from pathcopilot_runtime.errors import RuntimeValidationError

from .models import AgentMessageKind, AgentMessageRole
from .store import AgentStore


_TERMINAL = {
    TaskState.SUCCEEDED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.SKIPPED,
}


class AgentLifecycleService:
    """Expose Agent primitives without a second state machine."""

    def __init__(
        self,
        runtime_store: SQLiteEventStore,
        registry: RunRegistry,
    ):
        self.runtime_store = runtime_store
        self.registry = registry
        self.agent_store = AgentStore(runtime_store)

    def assert_owner(self, run_id: str, owner_id: str):
        run = self.runtime_store.get_run(run_id)
        if run.owner_id != owner_id:
            raise RuntimeValidationError("Runtime Run was not found")
        return run

    def seed_root(
        self,
        *,
        run_id: str,
        owner_id: str,
        root_task_id: str,
        question: str | None,
        context: Mapping[str, Any] | None = None,
        request_id: str,
    ) -> None:
        self.assert_owner(run_id, owner_id)
        task = self.runtime_store.get_task(root_task_id)
        if task.run_id != run_id:
            raise RuntimeValidationError("root Task does not belong to Run")
        merged = dict(context or {})
        if question:
            merged["question"] = str(question)
        self.agent_store.snapshot_context(
            run_id=run_id,
            task_id=root_task_id,
            context=merged,
            idempotency_key=f"root-context:{request_id}",
        )
        if question:
            self.agent_store.append_message(
                run_id=run_id,
                task_id=root_task_id,
                recipient_task_id=root_task_id,
                role=AgentMessageRole.USER,
                kind=AgentMessageKind.INSTRUCTION,
                content_text=str(question),
                payload={"source": "web"},
                idempotency_key=f"root-question:{request_id}",
            )

    def spawn_agent(
        self,
        *,
        run_id: str,
        owner_id: str,
        parent_task_id: str,
        role: str,
        name: str,
        context: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        required: bool = True,
        projected: bool = False,
    ) -> tuple[TaskRecord, bool]:
        self.assert_owner(run_id, owner_id)
        parent = self.runtime_store.get_task(parent_task_id)
        if parent.run_id != run_id:
            raise RuntimeValidationError("parent Task does not belong to Run")
        inherited = self.agent_store.latest_context(run_id, parent_task_id)
        merged = dict(inherited.context if inherited is not None else {})
        merged.update(dict(context or {}))
        spawn = (
            self.registry.spawn_legacy_task
            if projected
            else self.registry.spawn_task
        )
        task, created = spawn(
            run_id=run_id,
            name=name,
            role=role,
            parent_task_id=parent_task_id,
            idempotency_key=idempotency_key,
            required=required,
            metadata={
                "agent": True,
                "projected": projected,
                "roi_id": merged.get("roi_id"),
                "level": merged.get("level"),
                "magnification": merged.get("magnification"),
            },
        )
        self.agent_store.snapshot_context(
            run_id=run_id,
            task_id=task.task_id,
            parent_snapshot_id=(
                inherited.snapshot_id if inherited is not None else None
            ),
            context=merged,
            idempotency_key=(
                f"agent-context:{idempotency_key}"
                if idempotency_key is not None
                else None
            ),
        )
        if task.state is TaskState.CREATED:
            task = self._transition(
                task, TaskState.READY, cause="Agent was spawned"
            )
        return task, created

    def start_agent(
        self, *, run_id: str, owner_id: str, task_id: str
    ) -> TaskRecord:
        self.assert_owner(run_id, owner_id)
        task = self.runtime_store.get_task(task_id)
        if task.run_id != run_id:
            raise RuntimeValidationError("Agent Task does not belong to Run")
        if task.state is TaskState.CREATED:
            task = self._transition(
                task, TaskState.READY, cause="Agent dependencies satisfied"
            )
        if task.state is TaskState.READY and task.metadata.get("projected"):
            task = self._transition(
                task, TaskState.RUNNING, cause="Projected Agent started"
            )
        return task

    def complete_agent(
        self,
        *,
        run_id: str,
        owner_id: str,
        task_id: str,
        summary: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        self.assert_owner(run_id, owner_id)
        task = self.runtime_store.get_task(task_id)
        if task.run_id != run_id:
            raise RuntimeValidationError("Agent Task does not belong to Run")
        # Legacy event streams can report the same logical completion through
        # both a structured analysis event and a later human-readable result
        # message. Once the Agent is terminal, completion is an idempotent
        # no-op; attempting to replace its persisted summary would create a
        # false idempotency conflict.
        if task.is_terminal:
            return task
        if summary:
            self.agent_store.append_message(
                run_id=run_id,
                task_id=task_id,
                sender_task_id=task_id,
                role=AgentMessageRole.ASSISTANT,
                kind=AgentMessageKind.SUMMARY,
                content_text=summary,
                payload={"public": True},
                idempotency_key=f"agent-summary:{task_id}",
            )
        children = self._children(task)
        if any(not child.is_terminal for child in children):
            if task.state is TaskState.RUNNING:
                return self._transition(
                    task,
                    TaskState.WAITING,
                    cause="Agent is waiting for child Agents",
                    wait_reason=WaitReason.CHILDREN,
                )
            return task
        if task.state is TaskState.CREATED:
            task = self._transition(task, TaskState.READY, cause="Agent completed")
        if task.state is TaskState.WAITING:
            task = self._transition(
                task, TaskState.READY, cause="All child Agents completed"
            )
        if task.state is TaskState.READY and task.metadata.get("projected"):
            task = self._transition(
                task, TaskState.RUNNING, cause="Projected Agent resumed"
            )
        if task.state is TaskState.RUNNING and task.metadata.get("projected"):
            task = self._transition(
                task,
                TaskState.SUCCEEDED,
                cause="Projected Agent completed",
                result=dict(result or {"status": "completed"}),
            )
        self._settle_ancestors(task)
        return task

    def fail_agent(
        self,
        *,
        run_id: str,
        owner_id: str,
        task_id: str,
        code: str,
        message: str,
    ) -> TaskRecord:
        self.assert_owner(run_id, owner_id)
        task = self.runtime_store.get_task(task_id)
        if task.run_id != run_id:
            raise RuntimeValidationError("Agent Task does not belong to Run")
        error = {
            "code": str(code)[:120],
            "message": str(message)[:500],
            "category": "agent",
            "retryable": False,
        }
        children = self._children(task)
        if any(not child.is_terminal for child in children):
            for child in sorted(children, key=lambda item: item.depth, reverse=True):
                if not child.is_terminal:
                    self.stop_agent(
                        run_id=run_id,
                        owner_id=owner_id,
                        task_id=child.task_id,
                        reason="parent Agent failed",
                    )
        current = self.runtime_store.get_task(task_id)
        if current.state is TaskState.CREATED:
            current = self._transition(
                current, TaskState.READY, cause="Agent failed before start"
            )
        if current.state is TaskState.READY and current.metadata.get("projected"):
            current = self._transition(
                current, TaskState.RUNNING, cause="Projected Agent failure"
            )
        if current.state is TaskState.RUNNING and current.metadata.get("projected"):
            current = self._transition(
                current,
                TaskState.FAILED,
                cause="Projected Agent failed",
                error=error,
            )
        self._settle_ancestors(current)
        return current

    def send_message(
        self,
        *,
        run_id: str,
        owner_id: str,
        sender_task_id: str,
        recipient_task_id: str,
        message: str,
        idempotency_key: str | None = None,
    ):
        self.assert_owner(run_id, owner_id)
        sender = self.runtime_store.get_task(sender_task_id)
        recipient = self.runtime_store.get_task(recipient_task_id)
        if sender.run_id != run_id or recipient.run_id != run_id:
            raise RuntimeValidationError("Agent message crosses a Run boundary")
        return self.agent_store.append_message(
            run_id=run_id,
            task_id=recipient_task_id,
            sender_task_id=sender_task_id,
            recipient_task_id=recipient_task_id,
            role=AgentMessageRole.USER,
            kind=AgentMessageKind.FOLLOWUP,
            content_text=message,
            payload={"source": "agent"},
            idempotency_key=idempotency_key,
        )

    def followup(
        self,
        *,
        run_id: str,
        owner_id: str,
        task_id: str,
        message: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.assert_owner(run_id, owner_id)
        target = self.runtime_store.get_task(task_id)
        if target.run_id != run_id:
            raise RuntimeValidationError("Agent Task does not belong to Run")
        if target.is_terminal:
            context = self.agent_store.latest_context(run_id, task_id)
            continuation, created = self.registry.spawn_legacy_task(
                run_id=run_id,
                name=f"{target.name} (follow-up)",
                role=target.role,
                parent_task_id=target.parent_task_id,
                continuation_of_task_id=target.task_id,
                idempotency_key=f"followup-task:{idempotency_key}",
                metadata={
                    **dict(target.metadata),
                    "projected": True,
                    "continuation": True,
                },
            )
            self.agent_store.snapshot_context(
                run_id=run_id,
                task_id=continuation.task_id,
                parent_snapshot_id=(
                    context.snapshot_id if context is not None else None
                ),
                context=dict(context.context if context is not None else {}),
                idempotency_key=f"followup-context:{idempotency_key}",
            )
            if continuation.state is TaskState.CREATED:
                continuation = self._transition(
                    continuation,
                    TaskState.READY,
                    cause="Follow-up Agent created",
                )
            recipient_task_id = continuation.task_id
        else:
            continuation = target
            created = False
            recipient_task_id = target.task_id
        envelope = self.agent_store.append_message(
            run_id=run_id,
            task_id=recipient_task_id,
            sender_task_id=target.parent_task_id,
            recipient_task_id=recipient_task_id,
            role=AgentMessageRole.USER,
            kind=AgentMessageKind.FOLLOWUP,
            content_text=message,
            payload={"source": "user"},
            idempotency_key=f"followup-message:{idempotency_key}",
        )
        return {
            "task": self.task_view(continuation),
            "created": created,
            "message": envelope.to_dict(),
        }

    def stop_agent(
        self,
        *,
        run_id: str,
        owner_id: str,
        task_id: str,
        reason: str = "user requested stop",
    ) -> TaskRecord:
        self.assert_owner(run_id, owner_id)
        task = self.runtime_store.get_task(task_id)
        if task.run_id != run_id:
            raise RuntimeValidationError("Agent Task does not belong to Run")
        if task.parent_task_id is None:
            self.registry.request_cancel(
                run_id, requested_by=owner_id, reason=reason
            )
            return self.runtime_store.get_task(task_id)
        return self.registry.request_task_cancel(
            task_id, requested_by=owner_id, reason=reason
        )

    def wait_agents(
        self,
        *,
        run_id: str,
        owner_id: str,
        task_ids: Sequence[str],
        timeout_seconds: float = 0.0,
    ) -> dict[str, Any]:
        self.assert_owner(run_id, owner_id)
        deadline = (
            time.monotonic() + float(timeout_seconds)
            if timeout_seconds and timeout_seconds > 0
            else None
        )
        while True:
            tasks = [self.runtime_store.get_task(task_id) for task_id in task_ids]
            if any(task.run_id != run_id for task in tasks):
                raise RuntimeValidationError("Agent Task crosses a Run boundary")
            if all(task.is_terminal for task in tasks):
                break
            if deadline is None or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        return {
            "complete": all(task.is_terminal for task in tasks),
            "agents": [self.task_view(task) for task in tasks],
        }

    def agent_tree(self, *, run_id: str, owner_id: str) -> dict[str, Any]:
        run = self.assert_owner(run_id, owner_id)
        tasks = self.runtime_store.list_tasks(run_id)
        by_parent: dict[str | None, list[TaskRecord]] = {}
        for task in tasks:
            by_parent.setdefault(task.parent_task_id, []).append(task)
        for siblings in by_parent.values():
            siblings.sort(key=lambda item: (item.created_at, item.task_id))

        def node(task: TaskRecord) -> dict[str, Any]:
            return {
                **self.task_view(task),
                "children": [
                    node(child) for child in by_parent.get(task.task_id, [])
                ],
            }

        roots = by_parent.get(None, [])
        return {
            "run_id": run_id,
            "root_task_id": run.root_task_id,
            "agents": [node(task) for task in roots],
        }

    def messages(
        self,
        *,
        run_id: str,
        owner_id: str,
        task_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        self.assert_owner(run_id, owner_id)
        return [
            item.to_dict()
            for item in self.agent_store.list_messages(
                run_id, task_id=task_id, limit=limit
            )
        ]

    @staticmethod
    def task_view(task: TaskRecord) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "run_id": task.run_id,
            "parent_task_id": task.parent_task_id,
            "continuation_of_task_id": task.continuation_of_task_id,
            "name": task.name,
            "role": task.role,
            "state": task.state.value,
            "depth": task.depth,
            "required": task.required,
            "wait_reason": (
                task.wait_reason.value if task.wait_reason is not None else None
            ),
            "attempt": task.attempt,
            "metadata": dict(task.metadata),
            "result": dict(task.result) if task.result is not None else None,
            "error": dict(task.error) if task.error is not None else None,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "updated_at": task.updated_at,
            "ended_at": task.ended_at,
        }

    def _children(self, task: TaskRecord) -> list[TaskRecord]:
        return [
            candidate
            for candidate in self.runtime_store.list_tasks(task.run_id)
            if candidate.parent_task_id == task.task_id
        ]

    def _settle_ancestors(self, task: TaskRecord) -> None:
        parent_id = task.parent_task_id
        while parent_id is not None:
            parent = self.runtime_store.get_task(parent_id)
            if not parent.metadata.get("projected"):
                return
            children = self._children(parent)
            # Planned counts come from analysis events. Children are spawned
            # later, under stable ROI ids, so "all attached children are
            # terminal" is not the same as "the declared plan is complete".
            # ``agent.done`` finalization ignores this gate on purpose.
            if parent.metadata.get("planned_child_count_unknown"):
                return
            planned = parent.metadata.get("planned_child_count")
            if type(planned) is int and planned > 0 and len(children) < planned:
                return
            if any(not child.is_terminal for child in children):
                return
            if parent.state is TaskState.WAITING:
                parent = self._transition(
                    parent,
                    TaskState.READY,
                    cause="All child Agents became terminal",
                )
            if parent.state is TaskState.READY:
                parent = self._transition(
                    parent, TaskState.RUNNING, cause="Projected Agent resumed"
                )
            if parent.state is TaskState.RUNNING:
                parent = self._transition(
                    parent,
                    TaskState.SUCCEEDED,
                    cause="Projected Agent and children completed",
                    result={"status": "completed"},
                )
            parent_id = parent.parent_task_id

    def _transition(
        self,
        task: TaskRecord,
        target: TaskState,
        *,
        cause: str,
        wait_reason: WaitReason | None = None,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        if task.state is target:
            return task
        if task.is_terminal:
            return task
        try:
            return self.registry.transition_task(
                task.task_id,
                target,
                cause=cause,
                expected_version=task.state_version,
                wait_reason=wait_reason,
                result=result,
                error=error,
            )
        except (ConcurrencyConflict, IllegalTransition):
            current = self.runtime_store.get_task(task.task_id)
            if current.state is target or current.is_terminal:
                return current
            raise
