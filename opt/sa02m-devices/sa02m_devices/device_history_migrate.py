"""Перенос/merge staging БД (eMMC или SD) на более приоритетный носитель."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from sa02m_devices.stand_storage_path import emmc_staging_path
from sa02m_devices import event_journal_state as journal

# THE roster of history tables a promote must carry — one home for both
# `_db_has_rows()` («is there anything to promote?») and `merge_db_into()`
# («copy every row»). A table added to the archive is added HERE, or the day
# it lands its rows are silently dropped on every eMMC→USB promote (plan D1:
# mr_samples and device_events were lost this way; carel_samples inherited it).
# Value = columns to leave out of the merge: device_events.id is AUTOINCREMENT
# and must be re-assigned by the destination, never copied.
HISTORY_TABLES: dict[str, tuple[str, ...]] = {
    "dtv_samples": (),
    "ce_samples": (),
    "mr_samples": (),
    "carel_samples": (),
    "mtd_samples": (),
    "device_events": ("id",),
}


def _checkpoint_and_close(path: Path) -> bool:
    if not path.is_file():
        return True
    conn = sqlite3.connect(str(path), timeout=0.2)
    try:
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        complete = result[0] == 0 and (result[1] < 0 or result[1] == result[2])
    finally:
        conn.close()
    return complete and not any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm"))


def _db_has_rows(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 100:
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
        try:
            for table in HISTORY_TABLES:
                try:
                    row = conn.execute(
                        f"SELECT 1 FROM {table} LIMIT 1"
                    ).fetchone()
                except sqlite3.OperationalError:
                    continue  # a staging file from before this table existed
                if row:
                    return True
        finally:
            conn.close()
    except sqlite3.Error:
        return path.stat().st_size > 0
    return False


def _merge_table(
    dst: sqlite3.Connection,
    src: sqlite3.Connection,
    table: str,
    skip_cols: tuple[str, ...] = (),
) -> int:
    cols_src = [
        r[1] for r in src.execute(f"PRAGMA table_info({table})").fetchall()
    ]
    cols_dst = [
        r[1] for r in dst.execute(f"PRAGMA table_info({table})").fetchall()
    ]
    if not cols_src or not cols_dst:
        return 0
    cols = [c for c in cols_src if c in cols_dst and c not in skip_cols]
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


_MERGED_KEY = {
    "dtv_samples": "dtv_merged",
    "ce_samples": "ce_merged",
    "mr_samples": "mr_merged",
    "carel_samples": "carel_merged",
    "mtd_samples": "mtd_merged",
    "device_events": "events_merged",
}


def merge_db_into(src_path: Path, dst_path: Path) -> dict[str, Any]:
    """Merge samples unchanged and eligible events with exact retry deduplication."""
    source, destination = journal.domain_for(src_path), journal.domain_for(dst_path)
    with journal.guards([source, destination], exclusive=True) as states:
        return _merge_db_into(src_path, dst_path, states[source], states[destination])


def _event_predicate(source: journal.State, destination: journal.State):
    from sa02m_devices.device_events import generation_filter

    left, left_args = generation_filter(source.generation)
    right, right_args = generation_filter(destination.generation)
    return f"({left}) AND ({right})", (*left_args, *right_args)


def _merge_events(dst, src, source, destination) -> int:
    from sa02m_devices.device_events import validate_event_tags

    validate_event_tags(src)
    predicate, args = _event_predicate(source, destination)
    columns = ("journal_generation", "device_id", "kind", "phase", "ts",
               "port_num", "addr", "value", "ref_value", "message")
    names = ", ".join(columns)
    values = ", ".join("?" for _ in columns)
    equal = " AND ".join(f"{name} IS ?" for name in columns)
    total = 0
    for row in src.execute(f"SELECT {names} FROM device_events WHERE {predicate}", args):
        total += dst.execute(
            f"INSERT INTO device_events ({names}) SELECT {values} "
            f"WHERE NOT EXISTS (SELECT 1 FROM device_events WHERE {equal})", (*row, *row)
        ).rowcount
    return total


def _merge_db_into(src_path, dst_path, source, destination):
    from sa02m_devices import device_events, device_history_db

    device_history_db.ensure_schema(dst_path)
    device_events.ensure_events_schema(dst_path)
    device_history_db.ensure_schema(src_path)
    device_events.ensure_events_schema(src_path)
    src = sqlite3.connect(str(src_path), timeout=60.0)
    dst = sqlite3.connect(str(dst_path), timeout=60.0)
    try:
        report: dict[str, Any] = {"ok": True}
        src.execute("BEGIN")
        dst.execute("PRAGMA synchronous=FULL")
        with dst:
            for table, skip in HISTORY_TABLES.items():
                report[_MERGED_KEY[table]] = (
                    _merge_events(dst, src, source, destination) if table == "device_events"
                    else _merge_table(dst, src, table, skip)
                )
            # Raw ce rows may have landed past a rollup that was already
            # marked caught-up. Drop the fold; the next read rebuilds it.
            from sa02m_devices.history_ce_roll import reset_ce_roll

            reset_ce_roll(dst)
        return report
    finally:
        src.close()
        dst.close()


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_db(src: Path, dst: Path) -> dict[str, Any]:
    """Publish a SQLite snapshot including WAL, without retiring its source."""
    from sa02m_devices import device_events, device_history_db

    source, destination = journal.domain_for(src), journal.domain_for(dst)
    with journal.guards([source, destination], exclusive=True) as states:
        if dst.exists():
            return {**_merge_db_into(src, dst, states[source], states[destination]), "action": "merge"}
        device_history_db.ensure_schema(src)
        device_events.ensure_events_schema(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".history-copy-", suffix=".db", dir=dst.parent)
        os.close(fd)
        temporary = Path(name)
        reader = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(temporary)
        try:
            reader.backup(target, pages=256)
            device_events.ensure_events_schema_on(target)
            device_events.validate_event_tags(target)
            predicate, args = _event_predicate(states[source], states[destination])
            target.execute("PRAGMA synchronous=FULL")
            with target:
                target.execute(f"DELETE FROM device_events WHERE NOT ({predicate})", args)
            if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise sqlite3.DatabaseError("copied archive failed integrity check")
        except BaseException:
            target.close()
            reader.close()
            temporary.unlink(missing_ok=True)
            raise
        target.close()
        reader.close()
        try:
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            if dst.exists():
                return {**_merge_db_into(src, dst, states[source], states[destination]), "action": "merge"}
            os.replace(temporary, dst)
            _sync_directory(dst.parent)
            return {"ok": True, "action": "copy"}
        finally:
            temporary.unlink(missing_ok=True)


def remove_db_files(path: Path) -> bool:
    """Retire only a quiescent archive; never unlink a live WAL or SHM."""
    if not _checkpoint_and_close(path):
        return False
    path.unlink(missing_ok=True)
    _sync_directory(path.parent)
    return True


def promote_to_media(
    source_path: Path,
    media_active_path: Path,
) -> dict[str, Any]:
    """Transfer the archive, retaining the source until retirement is safe."""
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
    source, destination = journal.domain_for(source_path), journal.domain_for(media_active_path)
    with journal.guards([source, destination], exclusive=True):
        if not _db_has_rows(source_path):
            result["ok"] = remove_db_files(source_path)
            result["action"] = "empty_source"
            return result
        if not media_active_path.is_file():
            result.update(copy_db(source_path, media_active_path))
        else:
            result.update(merge_db_into(source_path, media_active_path))
            result["action"] = "merge"
        if result["ok"]:
            try:
                retired = remove_db_files(source_path)
            except (OSError, sqlite3.Error) as exc:
                result.update(ok=False, error=f"source retirement failed: {exc}")
            else:
                if not retired:
                    result.update(ok=False, error="source checkpoint or readers still busy")
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
    attempted: set[Path] = set()

    # Always try to absorb eMMC staging when writing to removable
    if new_backend in ("usb", "sd"):
        staging = emmc_staging_path()
        if staging.is_file() and staging.resolve() != new_path.resolve():
            if _db_has_rows(staging):
                attempted.add(staging.resolve())
                reports.append(promote_to_media(staging, new_path))

    # Promote previous lower-priority active (e.g. SD → USB)
    rank = {"emmc": 0, "sd": 1, "usb": 2, "force": 3}
    if (
        prev_backend
        and prev_path
        and prev_path.is_file()
        and rank.get(new_backend, -1) > rank.get(prev_backend, -1)
        and prev_path.resolve() != new_path.resolve()
        and prev_path.resolve() not in attempted
    ):
        if _db_has_rows(prev_path):
            reports.append(promote_to_media(prev_path, new_path))

    if not reports:
        return None
    ok = all(r.get("ok") for r in reports)
    return {"ok": ok, "steps": reports, "ts": time.time()}
