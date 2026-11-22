"""A fixed-worker, bounded-queue scheduler.

Unlike the Fable prototype, task count never creates an equal number of
threads. Parent Agents must yield while waiting for children; a later Runtime
orchestrator will reschedule them instead of blocking a worker.
"""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from .cancellation import CancellationToken
from .errors import CancellationRequested, SchedulerOverloaded


T = TypeVar("T")
WorkFunction = Callable[[CancellationToken], T]


@dataclass(frozen=True)
class SchedulerSnapshot:
    workers: int
    queued: int
    executing: int
    peak_executing: int
    accepted: int
    completed: int
    failed: int
    cancelled: int


@dataclass
class _WorkItem(Generic[T]):
    work_id: str
    function: WorkFunction[T]
    token: CancellationToken
    future: Future[T]


class BoundedTaskScheduler:
    """Execute work with a fixed number of daemon workers and bounded queue."""

    def __init__(
        self,
        *,
        max_workers: int = 4,
        max_pending: int = 64,
        thread_name_prefix: str = "pathcopilot-runtime",
    ):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if max_pending < 1:
            raise ValueError("max_pending must be >= 1")

        # Queue needs room for pending items in addition to executing workers.
        self._queue: queue.Queue[_WorkItem[Any]] = queue.Queue(
            maxsize=max_pending
        )
        self._max_workers = max_workers
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_changed = threading.Condition(self._lifecycle_lock)
        self._lock = threading.RLock()
        self._shutdown = False
        self._cancel_pending = False
        self._executing = 0
        self._peak_executing = 0
        self._accepted = 0
        self._completed = 0
        self._failed = 0
        self._cancelled = 0
        self._threads: list[threading.Thread] = []

        for index in range(max_workers):
            thread = threading.Thread(
                target=self._worker,
                name=f"{thread_name_prefix}-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def submit(
        self,
        work_id: str,
        function: WorkFunction[T],
        *,
        token: CancellationToken | None = None,
        block: bool = False,
        timeout: float | None = None,
    ) -> Future[T]:
        if not block and timeout is not None:
            raise ValueError("timeout is only valid when block=True")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be >= 0")

        future: Future[T] = Future()
        item = _WorkItem(
            work_id=work_id,
            function=function,
            token=token or CancellationToken(),
            future=future,
        )
        deadline = (
            None if timeout is None else time.monotonic() + timeout
        )
        with self._lifecycle_changed:
            while True:
                if self._shutdown:
                    raise RuntimeError("scheduler is shut down")
                try:
                    self._queue.put_nowait(item)
                    break
                except queue.Full as exc:
                    if not block:
                        raise SchedulerOverloaded(
                            f"scheduler queue is full; rejected {work_id}"
                        ) from exc
                    remaining = (
                        None
                        if deadline is None
                        else deadline - time.monotonic()
                    )
                    if remaining is not None and remaining <= 0:
                        raise SchedulerOverloaded(
                            f"scheduler queue is full; rejected {work_id}"
                        ) from exc
                    self._lifecycle_changed.wait(remaining)
            with self._lock:
                self._accepted += 1
        return future

    def _worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                with self._lifecycle_changed:
                    if self._shutdown and self._queue.empty():
                        return
                continue
            try:
                with self._lifecycle_changed:
                    self._lifecycle_changed.notify_all()
                    cancel_before_claim = self._cancel_pending
                    if cancel_before_claim:
                        item.token.request_cancel(
                            "scheduler shutdown", requested_by="scheduler"
                        )
                        item.future.cancel()
                        claimed = False
                    else:
                        claimed = item.future.set_running_or_notify_cancel()
                if not claimed:
                    with self._lock:
                        self._cancelled += 1
                    continue
                if item.token.is_cancelled:
                    info = item.token.info
                    raise CancellationRequested(
                        reason=info.reason if info else "cancelled",
                        requested_by=info.requested_by if info else None,
                    )

                with self._lock:
                    self._executing += 1
                    self._peak_executing = max(
                        self._peak_executing, self._executing
                    )
                try:
                    result = item.function(item.token)
                    item.token.raise_if_cancelled()
                finally:
                    with self._lock:
                        self._executing -= 1

                item.future.set_result(result)
                with self._lock:
                    self._completed += 1
            except CancellationRequested as exc:
                if not item.future.cancelled():
                    item.future.set_exception(exc)
                with self._lock:
                    self._cancelled += 1
            except BaseException as exc:
                if not item.future.cancelled():
                    item.future.set_exception(exc)
                with self._lock:
                    self._failed += 1
            finally:
                self._queue.task_done()

    def snapshot(self) -> SchedulerSnapshot:
        with self._lock:
            return SchedulerSnapshot(
                workers=self._max_workers,
                queued=self._queue.qsize(),
                executing=self._executing,
                peak_executing=self._peak_executing,
                accepted=self._accepted,
                completed=self._completed,
                failed=self._failed,
                cancelled=self._cancelled,
            )

    def shutdown(self, *, wait: bool = True, cancel_pending: bool = False) -> None:
        with self._lifecycle_changed:
            self._shutdown = True
            if cancel_pending:
                self._cancel_pending = True
            if self._cancel_pending:
                while True:
                    try:
                        pending = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        pending.token.request_cancel(
                            "scheduler shutdown", requested_by="scheduler"
                        )
                        pending.future.cancel()
                        with self._lock:
                            self._cancelled += 1
                    finally:
                        self._queue.task_done()
            self._lifecycle_changed.notify_all()
        if wait:
            for thread in self._threads:
                thread.join()

    def __enter__(self) -> "BoundedTaskScheduler":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.shutdown(wait=True, cancel_pending=exc is not None)
