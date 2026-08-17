"""Выбор носителя для devices_history.db: USB → SD → eMMC.

Каталог на носителе: ``<mount>/sa02m-stand/devices_history.db``.
Указатель: ``/var/lib/sa02m-stand/devices_history.active``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ACTIVE_NAME = "devices_history.db"
ARCHIVE_GLOB = "devices_history_*.db"
SUBDIR = "sa02m-stand"

DEFAULT_USB = Path(os.environ.get("STAND_DEVICES_USB_MOUNT", "/media/usb"))
DEFAULT_SD = Path(os.environ.get("STAND_DEVICES_SD_MOUNT", "/media/sdcard"))
DEFAULT_EMMC = Path(
    os.environ.get("STAND_DEVICES_EMMC_DIR", "/var/lib/sa02m-stand")
)
POINTER_PATH = Path(
    os.environ.get(
        "STAND_DEVICES_HISTORY_POINTER",
        "/var/lib/sa02m-stand/devices_history.active",
    )
)

# Жёсткий override: абсолютный путь к файлу БД (как раньше STAND_DEVICES_HISTORY_DB)
ENV_FORCE_DB = "STAND_DEVICES_HISTORY_DB"

MIN_FREE_BYTES = int(
    os.environ.get("STAND_DEVICES_HISTORY_MIN_FREE", str(64 * 1024 * 1024))
)
RESOLVE_CACHE_S = float(os.environ.get("STAND_DEVICES_RESOLVE_CACHE_S", "15"))

_CACHE: dict[str, Any] = {"ts": 0.0, "result": None}


@dataclass(frozen=True)
class StorageTarget:
    backend: str  # usb | sd | emmc | force
    mount: Path
    directory: Path
    active_path: Path
    free_bytes: int
    fstype: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "history_backend": self.backend,
            "history_mount": str(self.mount),
            "history_db_dir": str(self.directory),
            "history_db_path": str(self.active_path),
            "history_free_bytes": self.free_bytes,
            "history_fstype": self.fstype,
            "history_archives_count": len(list_archive_dbs(self.directory)),
        }


def _env_force_db() -> Path | None:
    raw = str(os.environ.get(ENV_FORCE_DB) or "").strip()
    if not raw:
        return None
    # Пустое / сброс через STAND_DEVICES_HISTORY_DB=  в unit после удаления —
    # если путь задан явно, используем его как force.
    return Path(raw)


def mount_fstype(mount: Path) -> str:
    """FSTYPE из /proc/mounts (лучшее совпадение по длине mountpoint)."""
    try:
        text = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    best = ""
    best_len = -1
    target = str(mount.resolve()) if mount.exists() else str(mount)
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mp, fst = parts[1], parts[2]
        # unescape octal in mount path if needed — rare for /media/*
        if target == mp or target.startswith(mp.rstrip("/") + "/"):
            if len(mp) > best_len:
                best = fst
                best_len = len(mp)
    return best


def free_bytes(path: Path) -> int:
    try:
        st = os.statvfs(str(path))
        return int(st.f_bavail * st.f_frsize)
    except (OSError, AttributeError):
        # Windows/tests without statvfs
        try:
            import shutil

            return int(shutil.disk_usage(str(path)).free)
        except OSError:
            return 0


def is_mountpoint(path: Path) -> bool:
    try:
        if os.path.ismount(str(path)):
            return True
    except OSError:
        pass
    # Tests / offline: явная метка «смонтировано»
    return (path / ".sa02m_mount_ok").is_file()


def probe_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".sa02m_write_ok"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _candidate_ok(mount: Path, *, require_mount: bool) -> tuple[bool, int, str]:
    if not mount.is_dir():
        return False, 0, ""
    if require_mount and not is_mountpoint(mount):
        return False, 0, ""
    free = free_bytes(mount)
    fst = mount_fstype(mount)
    if free < MIN_FREE_BYTES:
        return False, free, fst
    directory = (mount / SUBDIR) if require_mount else mount
    if not probe_writable(directory):
        return False, free, fst
    return True, free, fst


def list_archive_dbs(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob(ARCHIVE_GLOB))


def list_history_dbs(directory: Path) -> list[Path]:
    """Active + archives, archives first (older), active last."""
    out: list[Path] = []
    for p in list_archive_dbs(directory):
        if p.is_file():
            out.append(p)
    active = directory / ACTIVE_NAME
    if active.is_file():
        out.append(active)
    return out


def write_active_pointer(target: StorageTarget) -> None:
    try:
        POINTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        text = f"{target.active_path}\nbackend={target.backend}\n"
        tmp = POINTER_PATH.with_suffix(".active.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(POINTER_PATH)
    except OSError:
        pass


def invalidate_resolve_cache() -> None:
    _CACHE["ts"] = 0.0
    _CACHE["result"] = None


def resolve_storage_target(*, force_refresh: bool = False) -> StorageTarget:
    """USB → SD → eMMC. Env STAND_DEVICES_HISTORY_DB — force path."""
    now = time.monotonic()
    if (
        not force_refresh
        and _CACHE["result"] is not None
        and (now - float(_CACHE["ts"])) < RESOLVE_CACHE_S
    ):
        return _CACHE["result"]

    forced = _env_force_db()
    if forced is not None:
        directory = forced.parent
        directory.mkdir(parents=True, exist_ok=True)
        target = StorageTarget(
            backend="force",
            mount=directory,
            directory=directory,
            active_path=forced,
            free_bytes=free_bytes(directory),
            fstype=mount_fstype(directory),
        )
        _CACHE["ts"] = now
        _CACHE["result"] = target
        write_active_pointer(target)
        return target

    # USB
    ok, free, fst = _candidate_ok(DEFAULT_USB, require_mount=True)
    if ok:
        directory = DEFAULT_USB / SUBDIR
        target = StorageTarget(
            backend="usb",
            mount=DEFAULT_USB,
            directory=directory,
            active_path=directory / ACTIVE_NAME,
            free_bytes=free,
            fstype=fst,
        )
        _CACHE["ts"] = now
        _CACHE["result"] = target
        write_active_pointer(target)
        return target

    # SD
    ok, free, fst = _candidate_ok(DEFAULT_SD, require_mount=True)
    if ok:
        directory = DEFAULT_SD / SUBDIR
        target = StorageTarget(
            backend="sd",
            mount=DEFAULT_SD,
            directory=directory,
            active_path=directory / ACTIVE_NAME,
            free_bytes=free,
            fstype=fst,
        )
        _CACHE["ts"] = now
        _CACHE["result"] = target
        write_active_pointer(target)
        return target

    # eMMC staging
    DEFAULT_EMMC.mkdir(parents=True, exist_ok=True)
    free = free_bytes(DEFAULT_EMMC)
    probe_writable(DEFAULT_EMMC)
    target = StorageTarget(
        backend="emmc",
        mount=DEFAULT_EMMC,
        directory=DEFAULT_EMMC,
        active_path=DEFAULT_EMMC / ACTIVE_NAME,
        free_bytes=free,
        fstype=mount_fstype(Path("/")) or "ext4",
    )
    _CACHE["ts"] = now
    _CACHE["result"] = target
    write_active_pointer(target)
    return target


def emmc_staging_path() -> Path:
    return DEFAULT_EMMC / ACTIVE_NAME


def journaling_for_fstype(fstype: str) -> tuple[str, str]:
    """Return (journal_mode, synchronous) for SQLite."""
    ft = (fstype or "").lower()
    if ft in ("vfat", "msdos", "exfat", "ntfs", "fuseblk", "ntfs3"):
        return "DELETE", "NORMAL"
    return "WAL", "NORMAL"
