"""Hierarchical cooperative cancellation.

The token makes cancellation state truthful and observable. It intentionally
does not claim that Python can force-kill a blocked third-party call; adapters
must register abort callbacks and check the token between bounded operations.
"""

from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import CancellationRequested
from .models import utc_now_iso


@dataclass(frozen=True)
class CancellationInfo:
    reason: str
    requested_by: Optional[str]
    requested_at: str


CancelCallback = Callable[[CancellationInfo], None]


class CancellationToken:
    def __init__(self, parent: "CancellationToken | None" = None):
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._info: CancellationInfo | None = None
        self._callbacks: dict[int, CancelCallback] = {}
        self._next_callback_id = 1
        self._children: weakref.WeakSet[CancellationToken] = weakref.WeakSet()
        self._parent_ref: weakref.ReferenceType[CancellationToken] | None = None

        if parent is not None:
            self._parent_ref = weakref.ref(parent)
            parent._register_child(self)

    def _register_child(self, child: "CancellationToken") -> None:
        with self._lock:
            self._children.add(child)
            info = self._info
        if info is not None:
            child.request_cancel(
                reason=info.reason,
                requested_by=info.requested_by,
                requested_at=info.requested_at,
            )

    def child(self) -> "CancellationToken":
        return CancellationToken(parent=self)

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def info(self) -> CancellationInfo | None:
        with self._lock:
            return self._info

    def request_cancel(
        self,
        reason: str = "cancelled",
        requested_by: str | None = None,
        requested_at: str | None = None,
    ) -> bool:
        """Request cancellation once and propagate it to current descendants."""

        with self._lock:
            if self._info is not None:
                return False
            info = CancellationInfo(
                reason=str(reason or "cancelled"),
                requested_by=requested_by,
                requested_at=requested_at or utc_now_iso(),
            )
            self._info = info
            self._event.set()
            callbacks = list(self._callbacks.values())
            children = list(self._children)

        for child in children:
            child.request_cancel(
                reason=info.reason,
                requested_by=info.requested_by,
                requested_at=info.requested_at,
            )
        for callback in callbacks:
            try:
                callback(info)
            except Exception:
                # Cancellation must continue even if one adapter cleanup fails.
                continue
        return True

    def add_callback(self, callback: CancelCallback) -> Callable[[], None]:
        """Register an abort hook and return an explicit unsubscribe function."""

        with self._lock:
            callback_id = self._next_callback_id
            self._next_callback_id += 1
            info = self._info
            if info is None:
                self._callbacks[callback_id] = callback

        if info is not None:
            try:
                callback(info)
            except Exception:
                # Match normal propagation: one adapter cleanup failure must
                # not make cancellation registration fail.
                pass

        def unsubscribe() -> None:
            with self._lock:
                self._callbacks.pop(callback_id, None)

        return unsubscribe

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        info = self.info
        if info is not None:
            raise CancellationRequested(
                reason=info.reason, requested_by=info.requested_by
            )
