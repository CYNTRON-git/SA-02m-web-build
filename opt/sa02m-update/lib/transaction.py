# -*- coding: utf-8 -*-
"""Atomic transaction.json journal + flock for SA-02m update runner."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Set

try:
    from . import PackageError
except ImportError:  # pragma: no cover
    from __init__ import PackageError  # type: ignore

DEFAULT_STATEDIR = Path("/var/lib/sa02m-update")
TRANSACTION_NAME = "transaction.json"
LOCK_NAME = "update.lock"

STAGES: Set[str] = {
    "idle",
    "uploaded",
    "validating",
    "backing_up",
    "applying",
    "verifying",
    "committing",
    "done",
    "rolling_back",
    "rolled_back",
    "error",
    "cancelled",
}

RESULTS: Set[str] = {"pending", "success", "failed", "rolled_back", "cancelled"}
SOURCES: Set[str] = {"file", "github"}
OPERATIONS: Set[str] = {"update", "factory_reset"}

# Cancel allowed until backing_up inclusive (§2.10).
CANCEL_ALLOWED_STAGES: Set[str] = {"idle", "uploaded", "validating", "backing_up"}

# Power-loss recovery actions (§2.5).
RECOVERY_WIPE_STAGES: Set[str] = {"uploaded", "validating", "cancelled"}
RECOVERY_ROLLBACK_STAGES: Set[str] = {"backing_up", "applying", "verifying", "committing", "rolling_back"}
RECOVERY_NOOP_STAGES: Set[str] = {"done", "error", "idle"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_transaction(
    *,
    operation: str = "update",
    source: str = "file",
    package_path: str = "/var/lib/sa02m-update/incoming/package.sa02m",
    target_version: str = "",
    target_commit: str = "",
    previous_version: str = "",
    stage: str = "idle",
) -> Dict[str, Any]:
    if operation not in OPERATIONS:
        raise PackageError("E_INTERNAL", f"invalid operation: {operation}")
    if source not in SOURCES:
        raise PackageError("E_INTERNAL", f"invalid source: {source}")
    if stage not in STAGES:
        raise PackageError("E_INTERNAL", f"invalid stage: {stage}")
    now = utc_now_iso()
    return {
        "schema_version": 1,
        "id": str(uuid.uuid4()),
        "operation": operation,
        "source": source,
        "package_path": package_path,
        "target_version": target_version,
        "target_commit": target_commit,
        "previous_version": previous_version,
        "stage": stage,
        "progress_pct": 0,
        "files_total": 0,
        "files_done": 0,
        "result": "pending",
        "error_code": None,
        "error_message": None,
        "rollback_archive": None,
        "imaging_lock": False,
        "signature_ok": False,
        "started_at": now,
        "updated_at": now,
        "finished_at": None,
    }


def validate_transaction(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise PackageError("E_INTERNAL", "transaction must be object")
    if obj.get("schema_version") != 1:
        raise PackageError("E_INTERNAL", "transaction.schema_version must be 1")
    if obj.get("stage") not in STAGES:
        raise PackageError("E_INTERNAL", f"invalid stage: {obj.get('stage')!r}")
    if obj.get("result") not in RESULTS:
        raise PackageError("E_INTERNAL", f"invalid result: {obj.get('result')!r}")
    if obj.get("operation") not in OPERATIONS:
        raise PackageError("E_INTERNAL", f"invalid operation: {obj.get('operation')!r}")
    if obj.get("source") not in SOURCES:
        raise PackageError("E_INTERNAL", f"invalid source: {obj.get('source')!r}")
    return obj


def transaction_path(statedir: Path = DEFAULT_STATEDIR) -> Path:
    return Path(statedir) / TRANSACTION_NAME


def lock_path(statedir: Path = DEFAULT_STATEDIR) -> Path:
    return Path(statedir) / LOCK_NAME


def atomic_write_json(path: Path, obj: Dict[str, Any], *, mode: int = 0o644) -> None:
    """temp → fsync → rename → fsync(dir)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".txn-", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fdatasync(fh.fileno())
            except (AttributeError, OSError):
                os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(str(tmp_path), str(path))
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)  # type: ignore[call-arg]
        except OSError:
            pass
        raise


def load_transaction(statedir: Path = DEFAULT_STATEDIR) -> Optional[Dict[str, Any]]:
    path = transaction_path(statedir)
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError("E_INTERNAL", f"cannot read transaction.json: {exc}") from exc
    return validate_transaction(obj)


def save_transaction(obj: Dict[str, Any], statedir: Path = DEFAULT_STATEDIR) -> Dict[str, Any]:
    obj = validate_transaction(dict(obj))
    obj["updated_at"] = utc_now_iso()
    atomic_write_json(transaction_path(statedir), obj, mode=0o644)
    return obj


def update_stage(
    statedir: Path,
    stage: str,
    *,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    result: Optional[str] = None,
    progress_pct: Optional[int] = None,
    files_done: Optional[int] = None,
    files_total: Optional[int] = None,
    **fields: Any,
) -> Dict[str, Any]:
    if stage not in STAGES:
        raise PackageError("E_INTERNAL", f"invalid stage: {stage}")
    txn = load_transaction(statedir)
    if txn is None:
        txn = default_transaction(stage=stage)
    txn["stage"] = stage
    if error_code is not None:
        txn["error_code"] = error_code
    if error_message is not None:
        txn["error_message"] = error_message
    if result is not None:
        if result not in RESULTS:
            raise PackageError("E_INTERNAL", f"invalid result: {result}")
        txn["result"] = result
    if progress_pct is not None:
        txn["progress_pct"] = int(progress_pct)
    if files_done is not None:
        txn["files_done"] = int(files_done)
    if files_total is not None:
        txn["files_total"] = int(files_total)
    for key, value in fields.items():
        txn[key] = value
    if stage in {"done", "error", "rolled_back", "cancelled"}:
        txn["finished_at"] = utc_now_iso()
    return save_transaction(txn, statedir)


def cancel_allowed(stage: str) -> bool:
    return stage in CANCEL_ALLOWED_STAGES


def recovery_action(stage: str) -> str:
    """Return wipe|rollback|complete_or_rollback|continue_rollback|noop for boot recovery."""
    if stage in RECOVERY_WIPE_STAGES:
        return "wipe"
    if stage == "backing_up":
        return "rollback_if_archive_else_error"
    if stage in {"applying", "verifying"}:
        return "rollback"
    if stage == "committing":
        return "complete_or_rollback"
    if stage == "rolling_back":
        return "continue_rollback"
    if stage in RECOVERY_NOOP_STAGES:
        return "noop"
    return "noop"


class UpdateLock:
    """Exclusive flock on statedir/update.lock (fcntl on Unix, msvcrt on Windows)."""

    def __init__(self, statedir: Path = DEFAULT_STATEDIR) -> None:
        self.statedir = Path(statedir)
        self.path = lock_path(self.statedir)
        self._fh: Any = None

    def acquire(self, *, blocking: bool = False) -> None:
        self.statedir.mkdir(parents=True, exist_ok=True)
        try:
            self._fh = open(self.path, "a+b")
            self._fh.seek(0)
            if self._fh.read(1) == b"":
                self._fh.write(b"0")
                self._fh.flush()
            self._fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                flags = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                try:
                    msvcrt.locking(self._fh.fileno(), flags, 1)
                except OSError as exc:
                    self.release()
                    raise PackageError("E_LOCK", "update.lock is held") from exc
            else:
                import fcntl

                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                try:
                    fcntl.flock(self._fh.fileno(), flags)
                except BlockingIOError as exc:
                    self.release()
                    raise PackageError("E_LOCK", "update.lock is held") from exc
            # Record PID for diagnostics
            self._fh.seek(0)
            self._fh.truncate(0)
            self._fh.write(f"{os.getpid()}\n".encode("ascii"))
            self._fh.flush()
        except PackageError:
            raise
        except PermissionError as exc:
            # Windows: second open/read of an msvcrt-locked file.
            self.release()
            raise PackageError("E_LOCK", "update.lock is held") from exc
        except Exception as exc:
            self.release()
            raise PackageError("E_LOCK", f"cannot acquire update.lock: {exc}") from exc

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                try:
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def __enter__(self) -> "UpdateLock":
        self.acquire(blocking=False)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@contextmanager
def held_lock(statedir: Path = DEFAULT_STATEDIR, *, blocking: bool = False) -> Iterator[UpdateLock]:
    lock = UpdateLock(statedir)
    lock.acquire(blocking=blocking)
    try:
        yield lock
    finally:
        lock.release()
