"""Run-isolated Artifact storage with staged, atomic commits."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping

from .attempts import TaskClaim
from .errors import (
    ArtifactSecurityError,
    AttemptConflict,
    ConcurrencyConflict,
    LeaseLost,
    RuntimeValidationError,
)
from .sqlite_event_store import SQLiteEventStore


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_HASH_CHUNK_BYTES = 1024 * 1024
_ROOT_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[str, threading.Lock] = {}


class ArtifactStore:
    def __init__(
        self,
        root: str | Path,
        event_store: SQLiteEventStore,
        *,
        operation_timeout_seconds: float = 30.0,
    ):
        if operation_timeout_seconds < 0:
            raise ValueError("operation_timeout_seconds must be >= 0")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.event_store = event_store
        self.operation_timeout_seconds = float(operation_timeout_seconds)
        self._operation_lock_path = (
            self.root / ".pathcopilot-artifacts.lock"
        )

    def stage_bytes(
        self,
        *,
        run_id: str,
        task_id: str | None,
        kind: str,
        display_name: str,
        data: bytes,
        mime_type: str = "application/octet-stream",
    ) -> Mapping[str, Any]:
        if not isinstance(data, bytes):
            raise RuntimeValidationError("Artifact data must be bytes")
        self._validate_id(run_id, "run_id")
        self._validate_display_name(display_name)
        if (
            not isinstance(kind, str)
            or not isinstance(mime_type, str)
            or not kind.strip()
            or not mime_type.strip()
        ):
            raise RuntimeValidationError("Artifact kind and mime_type are required")

        with _exclusive_root_operation(
            self._operation_lock_path,
            timeout_seconds=self.operation_timeout_seconds,
        ):
            return self._stage_bytes_locked(
                run_id=run_id,
                task_id=task_id,
                kind=kind,
                display_name=display_name,
                data=data,
                mime_type=mime_type,
            )

    def stage_bytes_for_claim(
        self,
        claim: TaskClaim,
        *,
        kind: str,
        display_name: str,
        data: bytes,
        mime_type: str = "application/octet-stream",
        run_id: str | None = None,
        task_id: str | None = None,
        now: str | datetime | None = None,
    ) -> Mapping[str, Any]:
        """Stage and publish bytes under an authoritative Task claim."""

        if not isinstance(claim, TaskClaim):
            raise RuntimeValidationError("a TaskClaim is required")
        resolved_run_id = claim.run_id if run_id is None else run_id
        resolved_task_id = claim.task_id if task_id is None else task_id
        if (
            resolved_run_id != claim.run_id
            or resolved_task_id != claim.task_id
        ):
            raise LeaseLost(
                "Artifact identity must match the supplied TaskClaim"
            )
        if not isinstance(data, bytes):
            raise RuntimeValidationError("Artifact data must be bytes")
        self._validate_id(resolved_run_id, "run_id")
        self._validate_display_name(display_name)
        if (
            not isinstance(kind, str)
            or not isinstance(mime_type, str)
            or not kind.strip()
            or not mime_type.strip()
        ):
            raise RuntimeValidationError("Artifact kind and mime_type are required")

        with _exclusive_root_operation(
            self._operation_lock_path,
            timeout_seconds=self.operation_timeout_seconds,
        ):
            return self._stage_bytes_for_claim_locked(
                claim=claim,
                run_id=resolved_run_id,
                task_id=resolved_task_id,
                kind=kind,
                display_name=display_name,
                data=data,
                mime_type=mime_type,
                now=now,
            )

    def _stage_bytes_locked(
        self,
        *,
        run_id: str,
        task_id: str | None,
        kind: str,
        display_name: str,
        data: bytes,
        mime_type: str,
    ) -> Mapping[str, Any]:
        artifact_id = f"art_{uuid.uuid4().hex}"
        relative_path = (
            Path("runs") / run_id / "artifacts" / artifact_id / display_name
        ).as_posix()
        staging_path = self._contained_path(
            Path("runs") / run_id / "staging" / artifact_id / display_name
        )
        final_path = self._contained_path(Path(relative_path))

        # All path validation must happen before a staging database row exists.
        self.event_store.create_artifact_metadata(
            run_id=run_id,
            artifact_id=artifact_id,
            task_id=task_id,
            kind=kind,
            relative_path=relative_path,
            display_name=display_name,
            mime_type=mime_type,
        )

        final_moved = False
        try:
            staging_path.parent.mkdir(parents=True, exist_ok=False)
            with staging_path.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())

            digest = hashlib.sha256(data).hexdigest()
            final_path.parent.mkdir(parents=True, exist_ok=False)
            os.replace(staging_path, final_path)
            final_moved = True
            self._fsync_directory(final_path.parent)
            try:
                staging_path.parent.rmdir()
            except OSError:
                pass

            return self.event_store.mark_artifact_ready(
                run_id=run_id,
                artifact_id=artifact_id,
                byte_size=len(data),
                sha256=digest,
            )
        except Exception as exc:
            if not final_moved:
                self._cleanup_tree(staging_path.parent)
                try:
                    self.event_store.mark_artifact_failed(
                        run_id=run_id,
                        artifact_id=artifact_id,
                        error={
                            "code": "ARTIFACT_WRITE_FAILED",
                            "message": str(exc),
                            "category": "artifact",
                            "retryable": False,
                        },
                    )
                except AttemptConflict:
                    try:
                        self.event_store._mark_unbound_artifact_failed_for_recovery(
                            run_id=run_id,
                            artifact_id=artifact_id,
                            error=self._unbound_attempt_error(task_id),
                        )
                    except Exception:
                        pass
                except Exception:
                    pass
            elif isinstance(exc, AttemptConflict):
                try:
                    self.event_store._mark_unbound_artifact_failed_for_recovery(
                        run_id=run_id,
                        artifact_id=artifact_id,
                        error=self._unbound_attempt_error(task_id),
                    )
                except Exception:
                    pass
                finally:
                    self._cleanup_tree(final_path.parent)
            # Other database failures after rename remain staging so recovery
            # can verify the durable file and finish the legacy commit.
            raise

    def _stage_bytes_for_claim_locked(
        self,
        *,
        claim: TaskClaim,
        run_id: str,
        task_id: str,
        kind: str,
        display_name: str,
        data: bytes,
        mime_type: str,
        now: str | datetime | None,
    ) -> Mapping[str, Any]:
        artifact_id = f"art_{uuid.uuid4().hex}"
        relative_path = (
            Path("runs") / run_id / "artifacts" / artifact_id / display_name
        ).as_posix()
        staging_path = self._contained_path(
            Path("runs") / run_id / "staging" / artifact_id / display_name
        )
        final_path = self._contained_path(Path(relative_path))

        self.event_store.create_artifact_metadata_for_claim(
            claim,
            run_id=run_id,
            task_id=task_id,
            artifact_id=artifact_id,
            kind=kind,
            relative_path=relative_path,
            display_name=display_name,
            mime_type=mime_type,
            now=now,
        )

        final_moved = False
        try:
            staging_path.parent.mkdir(parents=True, exist_ok=False)
            with staging_path.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())

            digest = hashlib.sha256(data).hexdigest()
            final_path.parent.mkdir(parents=True, exist_ok=False)
            os.replace(staging_path, final_path)
            final_moved = True
            self._fsync_directory(final_path.parent)
            try:
                staging_path.parent.rmdir()
            except OSError:
                pass

            return self.event_store.mark_artifact_ready_for_claim(
                claim,
                run_id=run_id,
                artifact_id=artifact_id,
                byte_size=len(data),
                sha256=digest,
                now=now,
            )
        except Exception as exc:
            failure = {
                "code": "ARTIFACT_WRITE_FAILED",
                "message": str(exc),
                "category": "artifact",
                "retryable": False,
            }
            if final_moved:
                if isinstance(exc, LeaseLost):
                    try:
                        self.event_store.mark_stale_artifact_failed(
                            run_id=run_id,
                            artifact_id=artifact_id,
                            error=self._stale_claim_error(
                                {
                                    "attempt_id": claim.attempt_id,
                                    "lease_id": claim.lease_id,
                                    "fencing_token": claim.fencing_token,
                                }
                            ),
                            now=now,
                        )
                    except Exception:
                        pass
                    finally:
                        self._cleanup_tree(final_path.parent)
                # A non-fencing database failure after rename remains staging;
                # recovery can verify the durable file and finish the commit.
                raise

            self._cleanup_tree(staging_path.parent)
            try:
                self.event_store.mark_artifact_failed_for_claim(
                    claim,
                    run_id=run_id,
                    artifact_id=artifact_id,
                    error=failure,
                    now=now,
                )
            except LeaseLost:
                try:
                    self.event_store.mark_stale_artifact_failed(
                        run_id=run_id,
                        artifact_id=artifact_id,
                        error=self._stale_claim_error(
                            {
                                "attempt_id": claim.attempt_id,
                                "lease_id": claim.lease_id,
                                "fencing_token": claim.fencing_token,
                            }
                        ),
                        now=now,
                    )
                except Exception:
                    pass
            except Exception:
                pass
            raise

    def open(
        self, run_id: str, artifact_id: str, *, verify_sha256: bool = True
    ) -> BinaryIO:
        metadata = self.event_store.get_artifact(run_id, artifact_id)
        if metadata["state"] != "ready":
            raise RuntimeValidationError(
                f"Artifact is not ready: {metadata['state']}"
            )
        path = self._contained_path(Path(metadata["relative_path"]))
        if not path.is_file():
            raise RuntimeValidationError("Artifact file is missing")
        handle = path.open("rb")
        try:
            if verify_sha256:
                digest = self._sha256_handle(handle)
                if digest != metadata["sha256"]:
                    raise RuntimeValidationError(
                        "Artifact SHA256 verification failed"
                    )
                handle.seek(0)
            return handle
        except Exception:
            handle.close()
            raise

    def path_for(
        self,
        run_id: str,
        artifact_id: str,
        *,
        verify_sha256: bool = True,
    ) -> Path:
        metadata = self.event_store.get_artifact(run_id, artifact_id)
        if metadata["state"] != "ready":
            raise RuntimeValidationError("Artifact is not ready")
        path = self._contained_path(Path(metadata["relative_path"]))
        if not path.is_file():
            raise RuntimeValidationError("Artifact file is missing")
        if verify_sha256:
            with path.open("rb") as handle:
                digest = self._sha256_handle(handle)
            if digest != metadata["sha256"]:
                raise RuntimeValidationError(
                    "Artifact SHA256 verification failed"
                )
        return path

    def recover_staging(
        self,
        *,
        timeout_seconds: float = 5.0,
        now: str | datetime | None = None,
    ) -> int:
        """Recover staging rows without publishing stale claim output."""

        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be >= 0")
        with _exclusive_root_operation(
            self._operation_lock_path,
            timeout_seconds=timeout_seconds,
        ):
            return self._recover_staging_locked(now=now)

    def _recover_staging_locked(
        self, *, now: str | datetime | None
    ) -> int:
        recovered = 0
        for metadata in self.event_store.list_staging_artifacts():
            current = self.event_store.get_artifact(
                metadata["run_id"], metadata["artifact_id"]
            )
            if current["state"] != "staging":
                continue
            final_path = self._contained_path(Path(metadata["relative_path"]))
            staging_path = self._contained_path(
                Path("runs")
                / metadata["run_id"]
                / "staging"
                / metadata["artifact_id"]
                / metadata["display_name"]
            )
            if self._is_claim_bound_artifact(current):
                self._recover_claim_bound_artifact(
                    current,
                    final_path=final_path,
                    staging_path=staging_path,
                    now=now,
                )
                recovered += 1
                continue
            if final_path.is_file():
                with final_path.open("rb") as handle:
                    digest = self._sha256_handle(handle)
                try:
                    self.event_store.mark_artifact_ready(
                        run_id=metadata["run_id"],
                        artifact_id=metadata["artifact_id"],
                        byte_size=final_path.stat().st_size,
                        sha256=digest,
                    )
                except AttemptConflict:
                    try:
                        self.event_store._mark_unbound_artifact_failed_for_recovery(
                            run_id=metadata["run_id"],
                            artifact_id=metadata["artifact_id"],
                            error=self._unbound_attempt_error(
                                metadata["task_id"]
                            ),
                        )
                    finally:
                        self._cleanup_tree(staging_path.parent)
                        self._cleanup_tree(final_path.parent)
            else:
                self._cleanup_tree(staging_path.parent)
                try:
                    self.event_store.mark_artifact_failed(
                        run_id=metadata["run_id"],
                        artifact_id=metadata["artifact_id"],
                        error={
                            "code": "ARTIFACT_INTERRUPTED",
                            "message": (
                                "Artifact staging was interrupted before commit"
                            ),
                            "category": "artifact",
                            "retryable": True,
                        },
                    )
                except AttemptConflict:
                    self.event_store._mark_unbound_artifact_failed_for_recovery(
                        run_id=metadata["run_id"],
                        artifact_id=metadata["artifact_id"],
                        error=self._unbound_attempt_error(
                            metadata["task_id"]
                        ),
                    )
            recovered += 1
        return recovered

    def _recover_claim_bound_artifact(
        self,
        metadata: Mapping[str, Any],
        *,
        final_path: Path,
        staging_path: Path,
        now: str | datetime | None,
    ) -> None:
        claim = self._claim_for_artifact(metadata)
        interrupted_error = {
            "code": "ARTIFACT_INTERRUPTED",
            "message": "Artifact staging was interrupted before commit",
            "category": "artifact",
            "retryable": True,
        }
        try:
            if final_path.is_file():
                with final_path.open("rb") as handle:
                    digest = self._sha256_handle(handle)
                self.event_store.mark_artifact_ready_for_claim(
                    claim,
                    run_id=metadata["run_id"],
                    artifact_id=metadata["artifact_id"],
                    byte_size=final_path.stat().st_size,
                    sha256=digest,
                    now=now,
                )
                self._cleanup_tree(staging_path.parent)
            else:
                self._cleanup_tree(staging_path.parent)
                self.event_store.mark_artifact_failed_for_claim(
                    claim,
                    run_id=metadata["run_id"],
                    artifact_id=metadata["artifact_id"],
                    error=interrupted_error,
                    now=now,
                )
        except LeaseLost:
            try:
                self.event_store.mark_stale_artifact_failed(
                    run_id=metadata["run_id"],
                    artifact_id=metadata["artifact_id"],
                    error=self._stale_claim_error(metadata),
                    now=now,
                )
            finally:
                self._cleanup_tree(staging_path.parent)
                self._cleanup_tree(final_path.parent)

    def _claim_for_artifact(
        self, metadata: Mapping[str, Any]
    ) -> TaskClaim:
        attempt_id = metadata["attempt_id"]
        lease_id = metadata["lease_id"]
        task_id = metadata["task_id"]
        fencing_token = metadata["fencing_token"]
        if any(
            value is None
            for value in (
                attempt_id,
                lease_id,
                task_id,
                fencing_token,
            )
        ):
            raise RuntimeValidationError(
                "claim-bound Artifact metadata is incomplete"
            )
        attempt = self.event_store.get_attempt(attempt_id)
        lease = self.event_store.get_lease(lease_id)
        if (
            attempt.run_id != metadata["run_id"]
            or attempt.task_id != task_id
            or lease.run_id != metadata["run_id"]
            or lease.task_id != task_id
            or lease.attempt_id != attempt_id
            or lease.fencing_token != int(fencing_token)
        ):
            raise RuntimeValidationError(
                "claim-bound Artifact metadata is inconsistent"
            )
        return TaskClaim(
            run_id=metadata["run_id"],
            task_id=task_id,
            attempt_no=attempt.attempt_no,
            attempt_id=attempt_id,
            lease_id=lease_id,
            worker_id=lease.worker_id,
            fencing_token=int(fencing_token),
            acquired_at=lease.acquired_at,
            expires_at=lease.expires_at,
        )

    @staticmethod
    def _is_claim_bound_artifact(metadata: Mapping[str, Any]) -> bool:
        values = (
            metadata["attempt_id"],
            metadata["lease_id"],
            metadata["fencing_token"],
        )
        if all(value is None for value in values):
            return False
        if any(value is None for value in values):
            raise RuntimeValidationError(
                "claim-bound Artifact metadata is incomplete"
            )
        return True

    @staticmethod
    def _stale_claim_error(
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {
            "code": "ARTIFACT_STALE_CLAIM",
            "message": (
                "Artifact staging belongs to an expired or superseded claim"
            ),
            "category": "artifact",
            "retryable": False,
            "attempt_id": metadata["attempt_id"],
            "lease_id": metadata["lease_id"],
            "fencing_token": metadata["fencing_token"],
        }

    @staticmethod
    def _unbound_attempt_error(
        task_id: str | None,
    ) -> Mapping[str, Any]:
        return {
            "code": "ARTIFACT_UNBOUND_ATTEMPT_MANAGED",
            "message": (
                "Unbound Artifact staging cannot cross into an "
                "Attempt-managed Task"
            ),
            "category": "artifact",
            "retryable": False,
            "task_id": task_id,
        }

    def _contained_path(self, relative_path: Path) -> Path:
        if relative_path.is_absolute() or relative_path.drive:
            raise ArtifactSecurityError("absolute Artifact paths are forbidden")
        if any(part in {"", ".", ".."} for part in relative_path.parts):
            raise ArtifactSecurityError("Artifact path traversal is forbidden")
        candidate = (self.root / relative_path).resolve(strict=False)
        try:
            common = Path(os.path.commonpath([str(self.root), str(candidate)]))
        except ValueError as exc:
            raise ArtifactSecurityError("Artifact path is on another volume") from exc
        if common != self.root:
            raise ArtifactSecurityError("Artifact path escapes storage root")
        return candidate

    @staticmethod
    def _validate_display_name(display_name: str) -> None:
        if (
            not isinstance(display_name, str)
            or not display_name
            or display_name in {".", ".."}
            or Path(display_name).name != display_name
            or "/" in display_name
            or "\\" in display_name
            or ":" in display_name
            or "\x00" in display_name
            or display_name != display_name.rstrip(" .")
            or any(ord(character) < 32 for character in display_name)
        ):
            raise ArtifactSecurityError("unsafe Artifact display_name")
        reserved_stem = display_name.split(".", 1)[0].upper()
        if reserved_stem in _WINDOWS_RESERVED_NAMES:
            raise ArtifactSecurityError("reserved Windows Artifact display_name")

    @staticmethod
    def _validate_id(value: str, label: str) -> None:
        if (
            not isinstance(value, str)
            or value in {".", ".."}
            or not _SAFE_ID.fullmatch(value)
        ):
            raise ArtifactSecurityError(f"unsafe {label}")

    @staticmethod
    def _sha256_handle(handle: BinaryIO) -> str:
        digest = hashlib.sha256()
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _cleanup_tree(path: Path) -> None:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError:
            # Cleanup is best-effort; metadata remains non-ready.
            pass

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Best-effort directory sync; Windows may not allow directory handles."""

        try:
            descriptor = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)


def _local_root_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _ROOT_LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ROOT_LOCKS[key] = lock
        return lock


@contextmanager
def _exclusive_root_operation(
    lock_path: Path, *, timeout_seconds: float
) -> Iterator[None]:
    """Serialize writers and recovery across threads and local processes."""

    deadline = time.monotonic() + timeout_seconds
    local_lock = _local_root_lock(lock_path)
    if not local_lock.acquire(timeout=timeout_seconds):
        raise ConcurrencyConflict("Artifact storage is busy")

    handle: BinaryIO | None = None
    file_locked = False
    try:
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()

        while True:
            try:
                _try_lock_file(handle)
                file_locked = True
                break
            except OSError as exc:
                busy = exc.errno in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EDEADLK,
                } or getattr(exc, "winerror", None) in {33, 36}
                if not busy:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConcurrencyConflict(
                        "Artifact storage is busy"
                    ) from exc
                time.sleep(min(0.02, remaining))
        yield
    finally:
        if handle is not None:
            if file_locked:
                try:
                    _unlock_file(handle)
                except OSError:
                    # Closing the handle releases the kernel lock even if an
                    # explicit unlock is interrupted.
                    pass
            handle.close()
        local_lock.release()


def _try_lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
