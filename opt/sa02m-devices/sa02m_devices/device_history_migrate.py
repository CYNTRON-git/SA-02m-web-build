"""Перенос/merge staging БД (eMMC или SD) на более приоритетный носитель."""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from sa02m_devices.stand_storage_path import emmc_staging_path


def _checkpoint_and_close(path: Path) -> None:
    if not path.is_file():
        return
    try:
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    # drop sidecar WAL/SHM if present
    for suf in ("-wal", "-shm"):
        side = Path(str(path) + suf)
        try:
            if side.is_file():
                side.unlink()
        except OSError:
            pass


def _db_has_rows(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 100:
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
        try:
            for table in ("dtv_samples", "ce_samples"):
                row = conn.execute(
                    f"SELECT 1 FROM {table} LIMIT 1"
                ).fetchone()
                if row:
                    return True
        finally:
            conn.close()
    except sqlite3.Error:
        return path.stat().st_size > 0
    return False


def _merge_table(dst: sqlite3.Connection, src: sqlite3.Connection, table: str) -> int:
    cols_src = [
        r[1] for r in src.execute(f"PRAGMA table_info({table})").fetchall()
    ]
    cols_dst = [
        r[1] for r in dst.execute(f"PRAGMA table_info({table})").fetchall()
    ]
    if not cols_src or not cols_dst:
        return 0
    cols = [c for c in cols_src if c in cols_dst]
    if not cols:
        return 0
    col_list = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    rows = src.execute(f"SELECT {col_list} FROM {table}").fetchall()
    n = 0
    for row in rows:
        dst.execute(
            f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})",
            row,
        )
        n += 1
    return n


def merge_db_into(src_path: Path, dst_path: Path) -> dict[str, Any]:
    """INSERT OR IGNORE всех строк src → dst. dst должен существовать (schema)."""
    from sa02m_devices import device_history_db

    device_history_db.ensure_schema(dst_path)
    _checkpoint_and_close(src_path)
    src = sqlite3.connect(str(src_path), timeout=60.0)
    dst = sqlite3.connect(str(dst_path), timeout=60.0)
    try:
        with dst:
            n_dtv = _merge_table(dst, src, "dtv_samples")
            n_ce = _merge_table(dst, src, "ce_samples")
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst.commit()
        return {"ok": True, "dtv_merged": n_dtv, "ce_merged": n_ce}
    finally:
        src.close()
        dst.close()


def copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    shutil.copy2(src, tmp)
    src_size = src.stat().st_size
    if tmp.stat().st_size != src_size:
        tmp.unlink(missing_ok=True)
        raise OSError(f"copy size mismatch {tmp.stat().st_size} != {src_size}")
    tmp.replace(dst)


def remove_db_files(path: Path) -> None:
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass


def promote_to_media(
    source_path: Path,
    media_active_path: Path,
) -> dict[str, Any]:
    """Перенести/влить source в media active, затем удалить source.

    - нет active на носителе → copy + verify + unlink source
    - есть active → merge + unlink source
    """
    result: dict[str, Any] = {
        "ok": False,
        "action": "none",
        "source": str(source_path),
        "dest": str(media_active_path),
    }
    if source_path.resolve() == media_active_path.resolve():
        result["ok"] = True
        result["action"] = "same"
        return result
    if not _db_has_rows(source_path):
        remove_db_files(source_path)
        result["ok"] = True
        result["action"] = "empty_source"
        return result

    _checkpoint_and_close(source_path)
    media_active_path.parent.mkdir(parents=True, exist_ok=True)

    if not media_active_path.is_file():
        copy_db(source_path, media_active_path)
        remove_db_files(source_path)
        result["ok"] = True
        result["action"] = "copy"
        return result

    merged = merge_db_into(source_path, media_active_path)
    if not merged.get("ok"):
        result["error"] = "merge failed"
        return result
    remove_db_files(source_path)
    result["ok"] = True
    result["action"] = "merge"
    result.update(merged)
    return result


def promote_on_backend_change(
    prev_backend: str | None,
    prev_path: Path | None,
    new_backend: str,
    new_path: Path,
) -> dict[str, Any] | None:
    """При повышении приоритета (emmc→sd/usb, sd→usb) перенести данные.

    Также: если пишем на media, а eMMC staging непустой — влить staging.
    """
    reports: list[dict[str, Any]] = []

    # Always try to absorb eMMC staging when writing to removable
    if new_backend in ("usb", "sd"):
        staging = emmc_staging_path()
        if staging.is_file() and staging.resolve() != new_path.resolve():
            if _db_has_rows(staging):
                reports.append(promote_to_media(staging, new_path))

    # Promote previous lower-priority active (e.g. SD → USB)
    rank = {"emmc": 0, "sd": 1, "usb": 2, "force": 3}
    if (
        prev_backend
        and prev_path
        and prev_path.is_file()
        and rank.get(new_backend, -1) > rank.get(prev_backend, -1)
        and prev_path.resolve() != new_path.resolve()
    ):
        if _db_has_rows(prev_path):
            reports.append(promote_to_media(prev_path, new_path))

    if not reports:
        return None
    ok = all(r.get("ok") for r in reports)
    return {"ok": ok, "steps": reports, "ts": time.time()}
