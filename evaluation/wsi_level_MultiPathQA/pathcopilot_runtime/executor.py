"""Durable, lease-aware worker execution for the PathCopilot Runtime.

The executor deliberately has no in-memory work queue.  Each fixed worker
slot claims at most one durable Task only when the slot is ready to execute
it.  This couples database ownership to actual executor capacity and avoids
leases expiring while work waits in a local queue.

Cancellation remains cooperative: handlers must use ``context.checkpoint()``
between bounded operations and register abort callbacks on the supplied
token for interruptible adapters.  A watchdog and fencing tokens guarantee
that a worker which stops heartbeating cannot later publish a result.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .attempts import TaskClaim
from .cancellation import CancellationToken
from .errors import (
    CancellationRequested,
    LeaseLost,
    RuntimeValidationError,
)
from .models import TaskRecord, TaskState, WaitReason
from .sqlite_event_store import SQLiteEventStore


TaskHandler = Callable[["ExecutionContext"], Mapping[str, Any]]


class YieldExecution(Exception):
    """Ask the Runtime to release the worker while keeping the Attempt alive."""

    def __init__(
        self,
        wait_reason: WaitReason,
        *,
        cause: str = "handler yielded execution",
    ):
        super().__init__(cause)
        self.wait_reason = WaitReason(wait_reason)
        self.cause = str(cause or "handler yielded execution")


class RetryableExecutionError(Exception):
    """Structured handler failure which may consume the Task retry policy."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ):
        if not isinstance(code, str) or not code.strip():
            raise RuntimeValidationError("retryable error code is required")
        if not isinstance(message, str) or not message.strip():
            raise RuntimeValidationError("retryable error message is required")
        super().__init__(message)
        self.code = code.strip()
        self.message = message.strip()
        self.details = dict(details or {})

    def as_error(self) -> Mapping[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "category": "handler",
            "retryable": True,
            "details": self.details,
        }


@dataclass(frozen=True)
class ExecutorSnapshot:
    workers: int
    running: int
    claimed: int
    completed: int
    failed: int
    yielded: int
    cancelled: int
    lease_lost: int
    watchdog_passes: int


class ExecutionContext:
    """Context passed to one role handler under a fenced Task claim."""

    def __init__(
        self,
        *,
        store: SQLiteEventStore,
        claim: TaskClaim,
        task: TaskRecord,
        token: CancellationToken,
        lease_ttl_seconds: float,
    ):
        self.store = store
        self.claim = claim
        self.task = task
        self.token = token
        self.lease_ttl_seconds = lease_ttl_seconds

    def checkpoint(self) -> None:
        """Synchronize persisted cancellation and raise when cancellation won."""

        current = self.store.get_task(self.claim.task_id)
        if (
            current.cancel_requested_at is not None
            or current.state in {TaskState.CANCELLING, TaskState.CANCELLED}
        ):
            self.token.request_cancel(
                reason="persisted Task cancellation requested",
                requested_by="runtime",
                requested_at=current.cancel_requested_at,
            )
        self.token.raise_if_cancelled()

    def heartbeat(self) -> None:
        """Renew the current claim immediately.

        An automatic heartbeat loop already runs while the handler executes;
        this method is useful before or after a known long bounded operation.
        """

        self.checkpoint()
        self.store.renew_attempt_lease(
            self.claim, self.lease_ttl_seconds
        )

    def yield_for(
        self,
        wait_reason: WaitReason,
        *,
        cause: str = "handler yielded execution",
    ) -> None:
        raise YieldExecution(wait_reason, cause=cause)


class DurableTaskExecutor:
    """Fixed-slot executor backed by SQLite Attempt leases and a watchdog."""

    def __init__(
        self,
        store: SQLiteEventStore,
        *,
        max_workers: int = 4,
        lease_ttl_seconds: float = 30.0,
        heartbeat_interval_seconds: float | None = None,
        poll_interval_seconds: float = 0.1,
        watchdog_interval_seconds: float = 1.0,
        instance_id: str | None = None,
        run_id: str | None = None,
    ):
        if type(max_workers) is not int or max_workers < 1:
            raise RuntimeValidationError("max_workers must be an integer >= 1")
        for label, value in (
            ("lease_ttl_seconds", lease_ttl_seconds),
            ("poll_interval_seconds", poll_interval_seconds),
            ("watchdog_interval_seconds", watchdog_interval_seconds),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or float(value) <= 0
            ):
                raise RuntimeValidationError(f"{label} must be > 0")
        lease_ttl_seconds = float(lease_ttl_seconds)
        if heartbeat_interval_seconds is None:
            heartbeat_interval_seconds = max(
                0.01, lease_ttl_seconds / 3.0
            )
        if (
            not isinstance(heartbeat_interval_seconds, (int, float))
            or isinstance(heartbeat_interval_seconds, bool)
            or float(heartbeat_interval_seconds) <= 0
            or float(heartbeat_interval_seconds) >= lease_ttl_seconds
        ):
            raise RuntimeValidationError(
                "heartbeat_interval_seconds must be > 0 and less than "
                "lease_ttl_seconds"
            )

        self.store = store
        self.max_workers = max_workers
        self.lease_ttl_seconds = lease_ttl_seconds
        self.heartbeat_interval_seconds = float(
            heartbeat_interval_seconds
        )
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.watchdog_interval_seconds = float(
            watchdog_interval_seconds
        )
        self.instance_id = instance_id or (
            f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
        )
        self.run_id = run_id

        self._handlers_lock = threading.RLock()
        self._handlers: dict[str, TaskHandler] = {}
        self._state_lock = threading.RLock()
        self._stop = threading.Event()
        self._started = False
        self._threads: list[threading.Thread] = []
        self._watchdog_thread: threading.Thread | None = None
        self._active_claims: dict[str, TaskClaim] = {}
        self._claimed = 0
        self._completed = 0
        self._failed = 0
        self._yielded = 0
        self._cancelled = 0
        self._lease_lost = 0
        self._watchdog_passes = 0

    def register_handler(self, role: str, handler: TaskHandler) -> None:
        if not isinstance(role, str) or not role.strip():
            raise RuntimeValidationError("handler role is required")
        if not callable(handler):
            raise RuntimeValidationError("handler must be callable")
        normalized = role.strip()
        with self._handlers_lock:
            self._handlers[normalized] = handler

    def unregister_handler(self, role: str) -> None:
        normalized = str(role).strip()
        with self._handlers_lock:
            self._handlers.pop(normalized, None)

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name=f"pathcopilot-watchdog-{self.instance_id}",
                daemon=True,
            )
            self._watchdog_thread.start()
            for slot in range(self.max_workers):
                thread = threading.Thread(
                    target=self._slot_loop,
                    args=(slot,),
                    name=f"pathcopilot-worker-{self.instance_id}-{slot}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)

    def _registered_roles(self) -> tuple[str, ...]:
        with self._handlers_lock:
            return tuple(self._handlers)

    def _handler_for(self, role: str) -> TaskHandler | None:
        with self._handlers_lock:
            return self._handlers.get(role)

    def _slot_loop(self, slot: int) -> None:
        worker_id = self.instance_id
        worker_slot_id = f"slot-{slot}"
        claim_request_id = f"claim_{uuid.uuid4().hex}"
        while not self._stop.is_set():
            roles = self._registered_roles()
            if not roles:
                self._stop.wait(self.poll_interval_seconds)
                continue
            try:
                claim = self.store.claim_next_task(
                    worker_id,
                    self.lease_ttl_seconds,
                    run_id=self.run_id,
                    roles=roles,
                    worker_slot_id=worker_slot_id,
                    claim_request_id=claim_request_id,
                )
            except Exception:
                # SQLite contention or a transient recovery pass must not kill
                # the fixed slot.  Retrying the same persisted request id also
                # makes a lost claim response safe: the store returns the
                # already committed claim rather than granting another lease.
                self._stop.wait(self.poll_interval_seconds)
                continue
            if claim is None:
                claim_request_id = f"claim_{uuid.uuid4().hex}"
                self._stop.wait(self.poll_interval_seconds)
                continue
            with self._state_lock:
                self._active_claims[worker_slot_id] = claim
                self._claimed += 1
            try:
                self._execute_claim(claim)
            finally:
                with self._state_lock:
                    self._active_claims.pop(worker_slot_id, None)
                claim_request_id = f"claim_{uuid.uuid4().hex}"

    def _execute_claim(self, claim: TaskClaim) -> None:
        task = self.store.get_task(claim.task_id)
        handler = self._handler_for(task.role)
        if handler is None:
            # A role can be unregistered between selection and execution.
            try:
                self.store.yield_attempt(
                    claim,
                    WaitReason.RESOURCE,
                    cause="handler was unregistered before execution",
                )
                with self._state_lock:
                    self._yielded += 1
            except LeaseLost:
                with self._state_lock:
                    self._lease_lost += 1
            return

        token = CancellationToken()
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(claim, token, heartbeat_stop, lease_lost),
            name=f"pathcopilot-heartbeat-{claim.lease_id}",
            daemon=True,
        )
        heartbeat.start()
        context = ExecutionContext(
            store=self.store,
            claim=claim,
            task=task,
            token=token,
            lease_ttl_seconds=self.lease_ttl_seconds,
        )
        try:
            context.checkpoint()
            result = handler(context)
            if not isinstance(result, Mapping):
                raise RuntimeValidationError(
                    "Task handler must return a result mapping"
                )
            context.checkpoint()
            self._stop_heartbeat(heartbeat_stop, heartbeat)
            if lease_lost.is_set():
                raise LeaseLost("worker lost its lease while handler ran")
            self.store.complete_attempt(claim, dict(result))
            with self._state_lock:
                self._completed += 1
        except YieldExecution as exc:
            self._stop_heartbeat(heartbeat_stop, heartbeat)
            if lease_lost.is_set():
                with self._state_lock:
                    self._lease_lost += 1
                return
            try:
                self.store.yield_attempt(
                    claim, exc.wait_reason, cause=exc.cause
                )
                with self._state_lock:
                    self._yielded += 1
            except LeaseLost:
                with self._state_lock:
                    self._lease_lost += 1
        except CancellationRequested:
            self._stop_heartbeat(heartbeat_stop, heartbeat)
            try:
                self.store.acknowledge_attempt_cancellation(claim)
                with self._state_lock:
                    self._cancelled += 1
            except LeaseLost:
                with self._state_lock:
                    self._lease_lost += 1
        except LeaseLost:
            self._stop_heartbeat(heartbeat_stop, heartbeat)
            with self._state_lock:
                self._lease_lost += 1
        except Exception as exc:
            self._stop_heartbeat(heartbeat_stop, heartbeat)
            if lease_lost.is_set():
                with self._state_lock:
                    self._lease_lost += 1
                return
            if isinstance(exc, RetryableExecutionError):
                error = exc.as_error()
                retryable = True
            else:
                error = {
                    "code": type(exc).__name__.upper(),
                    "message": str(exc) or type(exc).__name__,
                    "category": "handler",
                    "retryable": False,
                }
                retryable = False
            try:
                self.store.fail_attempt(
                    claim, error, retryable=retryable
                )
                with self._state_lock:
                    self._failed += 1
            except LeaseLost:
                with self._state_lock:
                    self._lease_lost += 1
        finally:
            self._stop_heartbeat(heartbeat_stop, heartbeat)

    @staticmethod
    def _stop_heartbeat(
        stop: threading.Event, thread: threading.Thread
    ) -> None:
        stop.set()
        if thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=2.0)

    def _heartbeat_loop(
        self,
        claim: TaskClaim,
        token: CancellationToken,
        stop: threading.Event,
        lease_lost: threading.Event,
    ) -> None:
        while not stop.wait(self.heartbeat_interval_seconds):
            try:
                task = self.store.get_task(claim.task_id)
                if (
                    task.cancel_requested_at is not None
                    or task.state
                    in {TaskState.CANCELLING, TaskState.CANCELLED}
                ):
                    token.request_cancel(
                        reason="persisted Task cancellation requested",
                        requested_by="runtime",
                        requested_at=task.cancel_requested_at,
                    )
                    return
                self.store.renew_attempt_lease(
                    claim, self.lease_ttl_seconds
                )
            except LeaseLost:
                lease_lost.set()
                token.request_cancel(
                    reason="worker lease lost", requested_by="runtime"
                )
                return
            except Exception:
                # A single transient database error must not immediately
                # revoke authority.  If it persists, the lease naturally
                # expires and fencing prevents a late result.
                continue

    def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.store.recover_runtime(run_id=self.run_id)
                with self._state_lock:
                    self._watchdog_passes += 1
            except Exception:
                pass
            self._stop.wait(self.watchdog_interval_seconds)

    def snapshot(self) -> ExecutorSnapshot:
        with self._state_lock:
            return ExecutorSnapshot(
                workers=self.max_workers,
                running=len(self._active_claims),
                claimed=self._claimed,
                completed=self._completed,
                failed=self._failed,
                yielded=self._yielded,
                cancelled=self._cancelled,
                lease_lost=self._lease_lost,
                watchdog_passes=self._watchdog_passes,
            )

    def shutdown(
        self,
        *,
        wait: bool = True,
        timeout: float | None = None,
    ) -> bool:
        """Stop claiming new work and optionally wait for active handlers.

        This method does not pretend to force-kill arbitrary Python or native
        code.  If a handler does not cooperate, its lease will eventually
        expire and the watchdog will fence any late result.
        """

        self._stop.set()
        if not wait:
            return not any(thread.is_alive() for thread in self._threads)
        deadline = None if timeout is None else time.monotonic() + timeout
        threads = list(self._threads)
        if self._watchdog_thread is not None:
            threads.append(self._watchdog_thread)
        for thread in threads:
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            thread.join(remaining)
        return not any(thread.is_alive() for thread in threads)

    def __enter__(self) -> "DurableTaskExecutor":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.shutdown(wait=True)
