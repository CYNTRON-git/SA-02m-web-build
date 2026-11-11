"""The archive logger's batch writer: one connection, one transaction per batch.

Opening and closing a connection per archive function cost two forced writes
each — closing the LAST connection to a WAL database is a full checkpoint
(fsync WAL, copy pages, fsync the database, delete the WAL). The writer keeps
one connection for its whole life on a path and commits a batch of buffered
snapshots at once (audit 2026-11-11, IO-01; plan fix-1.0.7.36-logger).

A batch replays the snapshots in tick order through the same functions the
per-tick logger called, so the CE and Carel edge detectors see exactly the
«previous row» they saw before: rows of earlier snapshots of the batch are
visible inside the transaction.
"""

from __future__ import annotations

import math
import sqlite3
import sys
from pathlib import Path
from typing import Any

from sa02m_devices import history_store
from sa02m_devices.device_events import (
    carel_state_restore,
    carel_state_save,
    detect_carel_events,
    detect_ce_events,
    ensure_events_schema_on,
)
from sa02m_devices.history_write import (
    insert_carel_sample,
    insert_mr_sample,
    insert_mtd_sample,
    insert_sample,
    purge_old,
)

# After a checkpoint SQLite truncates the WAL back to this size, so one long
# chart read that held the WAL open does not leave a large file behind.
JOURNAL_SIZE_LIMIT = 16 * 1024 * 1024


# The long-format Carel table the 1.0.6.41 pivot kept as a one-release rollback
# to 1.0.6.40 (history_store._migrate_carel_to_wide). Nothing reads it; the
# owner decided on 2026-11-11 to drop it in 1.0.7.36.
LEGACY_CAREL_TABLE = "carel_samples_v1"


class ArchiveFlushError(RuntimeError):
    """A batch was not written; its snapshots are still buffered."""


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def drop_legacy_carel_table(conn: sqlite3.Connection) -> bool:
    """Drop LEGACY_CAREL_TABLE once, on the writer's open; True when dropped.

    Idempotent: a file without the table costs one sqlite_master read. A failed
    drop is logged and the writer carries on — the table is dead weight, not
    something an archive write depends on; the next open tries again. Its pages
    go to the freelist and are reused; the file does not shrink (no VACUUM).
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (LEGACY_CAREL_TABLE,),
        ).fetchone()
        if row is None:
            return False
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(f"DROP TABLE IF EXISTS {LEGACY_CAREL_TABLE}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        _err(f"archive: {LEGACY_CAREL_TABLE} not dropped: {exc}")
        return False
    print(f"archive: dropped legacy table {LEGACY_CAREL_TABLE}", flush=True)
    return True


class ArchiveWriter:
    """Buffer logger snapshots and write them to `path` once per `flush_every_s`.

    `interval_s` is the logger tick; it sizes the buffer bound only.
    """

    def __init__(
        self,
        path: Path,
        *,
        flush_every_s: float,
        interval_s: float = 1.0,
    ) -> None:
        self.path = Path(path)
        self.flush_every_s = float(flush_every_s)
        self.interval_s = float(interval_s)
        self.conn: sqlite3.Connection | None = None
        self.last_flush_m: float | None = None
        self.dropped = 0
        self._pending: list[tuple[dict[str, Any], bool]] = []

    # -- buffer -----------------------------------------------------------

    @property
    def pending(self) -> int:
        return len(self._pending)

    @property
    def max_pending(self) -> int:
        """Four batches' worth of ticks: an unwritable archive (disk full, a
        long lock) keeps ~2 min of eMMC ticks, then drops the oldest — counted
        and reported, never unbounded."""
        iv = max(0.2, self.interval_s)
        return 4 * math.ceil(max(self.flush_every_s, iv) / iv)

    def add(self, snapshot: dict[str, Any], *, mr_due: bool) -> None:
        self._pending.append((snapshot, bool(mr_due)))
        over = len(self._pending) - self.max_pending
        if over > 0:
            del self._pending[:over]
            self.dropped += over

    def due(self, now_m: float) -> bool:
        return self.last_flush_m is None or now_m - self.last_flush_m >= self.flush_every_s

    # -- connection -------------------------------------------------------

    def open(self) -> sqlite3.Connection:
        if self.conn is not None:
            return self.conn
        # Schema, migrations and the ce_roll DDL run once per connection life.
        conn = history_store._connect(self.path)
        try:
            conn.isolation_level = None  # explicit BEGIN IMMEDIATE / COMMIT
            ensure_events_schema_on(conn)
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode == "wal":
                # NORMAL on WAL syncs only at checkpoints: a committed batch
                # could then be lost with minutes of earlier ones on a power
                # cut, and the «at most one batch lost» promise would be false.
                # FULL syncs the WAL at every COMMIT — one fsync per batch.
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
        except BaseException:
            conn.close()
            raise
        drop_legacy_carel_table(conn)
        self.conn = conn
        return conn

    def _drop_conn(self) -> None:
        conn, self.conn = self.conn, None
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error as exc:
            _err(f"archive close error: {exc}")

    def size_at_least(self, threshold: int) -> bool:
        try:
            return self.path.stat().st_size >= int(threshold)
        except OSError:
            return False

    # -- writing ----------------------------------------------------------

    def _replay(
        self, conn: sqlite3.Connection, snap: dict[str, Any], mr_due: bool
    ) -> list[dict[str, Any]]:
        insert_sample(snap, conn=conn)
        if mr_due:
            insert_mr_sample(snap, conn=conn)
            insert_carel_sample(snap, conn=conn)
            insert_mtd_sample(snap, conn=conn)
        # A detector failure costs its own events only, never the samples —
        # as when each ran in its own connection.
        before = carel_state_save()
        conn.execute("SAVEPOINT ev")
        try:
            created = detect_ce_events(snap, path=self.path, conn=conn)
            created += detect_carel_events(snap, path=self.path, conn=conn)
        except Exception as exc:  # noqa: BLE001
            conn.execute("ROLLBACK TO ev")
            conn.execute("RELEASE ev")
            carel_state_restore(before)
            _err(f"events error: {exc}")
            return []
        conn.execute("RELEASE ev")
        return created

    def flush(self, *, now_m: float | None = None) -> list[dict[str, Any]]:
        """Write every buffered snapshot in one transaction; return new events.

        On failure the transaction and the Carel baselines roll back, the
        connection is dropped (the file may have been replaced), the snapshots
        stay buffered and ArchiveFlushError is raised."""
        if now_m is not None:
            # Set on the attempt, not on success: a failing archive is retried
            # once per batch interval, not on every tick.
            self.last_flush_m = now_m
        if not self._pending:
            return []
        saved = carel_state_save()
        try:
            conn = self.open()
            conn.execute("BEGIN IMMEDIATE")
            try:
                created: list[dict[str, Any]] = []
                for snap, mr_due in self._pending:
                    created += self._replay(conn, snap, mr_due)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error as exc:
                        _err(f"archive rollback error: {exc}")
                raise
        except (sqlite3.Error, OSError) as exc:
            carel_state_restore(saved)
            self._drop_conn()
            raise ArchiveFlushError(
                f"archive batch not written: {exc} "
                f"pending={len(self._pending)} dropped={self.dropped}"
            ) from exc
        except BaseException:
            carel_state_restore(saved)
            self._drop_conn()
            raise
        self._pending.clear()
        if self.dropped:
            _err(
                f"archive: {self.dropped} snapshot(s) were dropped while the "
                "archive was unwritable"
            )
            self.dropped = 0
        return created

    def purge(self) -> dict[str, int]:
        """The retention purge in its own transaction on the writer's connection."""
        conn = self.open()
        conn.execute("BEGIN IMMEDIATE")
        try:
            stats = purge_old(self.path, conn=conn)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            self._drop_conn()
            raise
        return stats

    def close(self) -> None:
        """Write what is buffered, then close — the last close is the one
        checkpoint (and WAL removal) of the writer's life on this path. A
        failed final batch is reported here, not raised: the caller is about
        to replace the file or exit, and the snapshots stay buffered."""
        try:
            if self._pending:
                self.flush()
        except Exception as exc:  # noqa: BLE001
            _err(f"logger error: {exc}")
        finally:
            self._drop_conn()

    def retarget(self, path: Path) -> None:
        """Switch to another archive file; the old one is flushed and closed."""
        new = Path(path)
        if new == self.path:
            return
        self.close()
        self.path = new
