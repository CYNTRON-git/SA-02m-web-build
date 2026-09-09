"""SQLite file, DDL, schema migrations, connection and rotation of the
device archive (USB → SD → eMMC, see stand_storage_path)."""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sa02m_devices.history_metrics import CAREL_COLUMNS, _DTV_SENSOR_COLUMNS


try:
    from sa02m_devices.stand_storage_path import (
        ACTIVE_NAME,
        free_bytes,
        journaling_for_fstype,
        list_history_dbs,
        mount_fstype,
        resolve_storage_target,
    )
except ImportError:  # hardpy_tests layout: /opt/hardpy_tests/lib/
    from lib.stand_storage_path import (  # type: ignore[no-redef]
        ACTIVE_NAME,
        free_bytes,
        journaling_for_fstype,
        list_history_dbs,
        mount_fstype,
        resolve_storage_target,
    )


log = logging.getLogger("sa02m-devices-history")

RETENTION_S = float(os.environ.get("STAND_DEVICES_RETENTION_S", str(30 * 86400)))
# Event rows (device_events) outlive the sample tables: Operator decision F3
# (Carel plan, 2026-09-03) — the journal is the evidence the feature exists to
# keep; purging it with the 30 d samples would discard exactly that.
EVENT_RETENTION_S = float(
    os.environ.get("STAND_DEVICES_EVENT_RETENTION_S", str(365 * 86400))
)
ROTATE_BYTES = int(
    os.environ.get("STAND_DEVICES_HISTORY_ROTATE_BYTES", str(3 * 1024**3))
)
ROTATE_HEADROOM = int(
    os.environ.get("STAND_DEVICES_HISTORY_ROTATE_HEADROOM", str(256 * 1024**2))
)


_CREATE_DTV = """
    CREATE TABLE IF NOT EXISTS dtv_samples (
        ts REAL NOT NULL,
        device_id TEXT NOT NULL DEFAULT '',
        room_temp REAL,
        humidity REAL,
        eco2_ppm REAL,
        tvoc_mg_m3 REAL,
        pressure_mmhg REAL,
        light_pct REAL,
        presence REAL,
        PRIMARY KEY (ts, device_id)
    );
"""
_CREATE_CE = """
    CREATE TABLE IF NOT EXISTS ce_samples (
        ts REAL NOT NULL,
        device_id TEXT NOT NULL DEFAULT '',
        voltage_a REAL,
        voltage_b REAL,
        voltage_c REAL,
        current_a REAL,
        current_b REAL,
        current_c REAL,
        power_w_a REAL,
        power_w_b REAL,
        power_w_c REAL,
        power_w_total REAL,
        frequency_hz REAL,
        energy_kwh_import REAL,
        PRIMARY KEY (ts, device_id)
    );
"""
# MR-02m analog «длинная» таблица: одна строка на канал за отсчёт. Число AI на
# модуль (6/12/…) и единица канала (°C / V / mA / …) динамические, поэтому НЕ
# столбцы-как-у-dtv/ce, а (ch, value, unit). Единица хранится на момент отсчёта —
# смена типа датчика в истории видна честно (ось идёт по последней единице окна).
_CREATE_MR = """
    CREATE TABLE IF NOT EXISTS mr_samples (
        ts REAL NOT NULL,
        device_id TEXT NOT NULL DEFAULT '',
        ch INTEGER NOT NULL,
        value REAL,
        unit TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (ts, device_id, ch)
    );
"""
# Carel AHU: a WIDE row per (ts, device_id), like dtv/ce — one row per 10 s tick
# instead of 13. Unlike MR channels the metric set is FIXED and its unit is a
# constant per metric (CAREL_METRIC_META), so there is no `unit` column: the long
# table's last-wins unit could never differ from the meta. The column list is
# built from CAREL_COLUMNS (code constants, never request input) so a metric
# added to METRICS reaches the table with no second edit here.
_CAREL_COL_DDL = ",\n        ".join(f"{c} REAL" for c in CAREL_COLUMNS)
_CREATE_CAREL = f"""
    CREATE TABLE IF NOT EXISTS carel_samples (
        ts REAL NOT NULL,
        device_id TEXT NOT NULL DEFAULT '',
        {_CAREL_COL_DDL},
        PRIMARY KEY (ts, device_id)
    );
"""


def _ensure_ce_power_phase_cols(conn: sqlite3.Connection) -> None:
    cols = {
        str(r[1])
        for r in conn.execute("PRAGMA table_info(ce_samples)").fetchall()
    }
    for col in ("power_w_a", "power_w_b", "power_w_c"):
        if col not in cols:
            conn.execute(f"ALTER TABLE ce_samples ADD COLUMN {col} REAL")


def _ensure_dtv_sensor_cols(conn: sqlite3.Connection) -> None:
    """Idempotent per-sensor column add to dtv_samples (mirrors the ce loop).

    An OLD scalar-only dtv_samples gains the 13 per-sensor REAL columns with no
    data loss; existing rows keep NULL there (charts skip the gap)."""
    cols = {
        str(r[1])
        for r in conn.execute("PRAGMA table_info(dtv_samples)").fetchall()
    }
    for col in _DTV_SENSOR_COLUMNS:
        if col not in cols:
            conn.execute(f"ALTER TABLE dtv_samples ADD COLUMN {col} REAL")


def db_path(path: Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    forced = str(os.environ.get("STAND_DEVICES_HISTORY_DB") or "").strip()
    if forced:
        return Path(forced)
    return resolve_storage_target().active_path


def storage_status() -> dict[str, Any]:
    """Метаданные носителя для API."""
    forced = str(os.environ.get("STAND_DEVICES_HISTORY_DB") or "").strip()
    if forced:
        p = Path(forced)
        return {
            "history_backend": "force",
            "history_mount": str(p.parent),
            "history_db_dir": str(p.parent),
            "history_db_path": str(p),
            "history_free_bytes": free_bytes(p.parent),
            "history_fstype": mount_fstype(p.parent),
            "history_archives_count": len(
                list(p.parent.glob("devices_history_*.db"))
            ),
        }
    return resolve_storage_target(force_refresh=True).as_dict()


def _needs_pk_migration(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not row or not row[0]:
        return False
    sql = row[0].upper().replace(" ", "")
    if "PRIMARYKEY(TS,DEVICE_ID)" in sql:
        return False
    if "TSPREALPRIMARYKEY" in sql or "TSREALPRIMARYKEY" in sql:
        return True
    return "PRIMARYKEY(TS," not in sql and "PRIMARY KEY" in row[0].upper()


def _migrate_table(conn: sqlite3.Connection, table: str, create_sql: str) -> None:
    tmp = f"{table}__mig"
    conn.executescript(
        f"""
        DROP TABLE IF EXISTS {tmp};
        {create_sql.replace(table, tmp, 1)}
        INSERT OR IGNORE INTO {tmp} SELECT * FROM {table};
        DROP TABLE {table};
        ALTER TABLE {tmp} RENAME TO {table};
        """
    )


def _carel_is_long(conn: sqlite3.Connection) -> bool:
    """A 1.0.6.40-and-older archive: `carel_samples` still has a `metric` column."""
    return "metric" in {
        str(r[1]) for r in conn.execute("PRAGMA table_info(carel_samples)").fetchall()
    }


# One-time long → wide pivot. Written as ONE script that opens its OWN
# transaction: `executescript` runs DDL in AUTOCOMMIT otherwise (which is why
# the legacy PK migration above is not atomic — do not copy it), and a crash or
# a full medium halfway would leave a board with a half-migrated archive.
# BEGIN IMMEDIATE takes the write lock up front, so the 1 Hz logger writing into
# the same file either finished before the pivot or blocks on it (its own
# `sqlite3.connect(timeout=30)`) and then writes into the wide table — a tick
# lost to the busy timeout is one 10 s sample, retried by the next tick. A third
# outcome — an opener that decided to migrate before the winner committed and
# reaches this script against an already-wide table — is handled by the caller.
# `MAX(CASE …)` over the long PK `(ts, device_id, metric)` sees exactly one value
# per cell, so the pivot is lossless; the old writer emitted every metric of a
# tick under one `ts`, so grouping by `(ts, device_id)` rebuilds the tick.
# Metric/column names come from CAREL_COLUMNS — code constants, not request input.
_MIGRATE_CAREL_SQL = """
BEGIN IMMEDIATE;
DROP TABLE IF EXISTS carel_samples__wide;
CREATE TABLE carel_samples__wide (
    ts REAL NOT NULL,
    device_id TEXT NOT NULL DEFAULT '',
    {col_ddl},
    PRIMARY KEY (ts, device_id)
);
INSERT OR REPLACE INTO carel_samples__wide (ts, device_id, {cols})
    SELECT ts, device_id, {pivot}
    FROM carel_samples GROUP BY ts, device_id;
DROP TABLE IF EXISTS carel_samples_v1;
ALTER TABLE carel_samples RENAME TO carel_samples_v1;
ALTER TABLE carel_samples__wide RENAME TO carel_samples;
DROP INDEX IF EXISTS idx_carel_device_metric_ts;
COMMIT;
""".format(
    col_ddl=_CAREL_COL_DDL,
    cols=", ".join(CAREL_COLUMNS),
    pivot=",\n           ".join(
        f"MAX(CASE WHEN metric = '{c}' THEN value END)" for c in CAREL_COLUMNS
    ),
)


def _migrate_carel_to_wide(conn: sqlite3.Connection) -> None:
    """Pivot a long `carel_samples` in place; keep the old table as the rollback.

    Idempotent both ways. Sequentially: a second open sees no `metric` column
    and returns. CONCURRENTLY: every caller opens its own connection (a logger
    tick, an archive read, an eMMC staging), so a second opener can evaluate the
    check below while the winner is still inside its transaction, block on the
    script's `BEGIN IMMEDIATE`, and take the write lock after the schema has
    already changed — its `SELECT … WHERE metric = …` then hits a table with no
    `metric` column. The loser's STALE DECISION is what is wrong there, not the
    archive, so the outcome is decided by re-reading committed state after the
    rollback: no `metric` column any more means the winner did our work.

    Why not take the lock before the check instead: `executescript` COMMITs any
    pending transaction before running its script (CPython sqlite3, verified on
    3.14), so a `BEGIN IMMEDIATE` issued here would be released by the very call
    it was meant to protect — holding it would mean driving the script's own
    statements by hand and betting the migration on pysqlite's implicit-BEGIN
    behaviour. The script's own `BEGIN IMMEDIATE` stays the one serialisation
    point; only the loser's verdict is re-taken against what it serialised on.

    Runs on EVERY file `_connect` opens. `carel_samples_v1` is the one-release
    rollback (plan 1.0.6.41 fork F4; 1.0.6.42 drops it) and `purge_old` never
    touches it.
    """
    if not _carel_is_long(conn):
        return
    try:
        conn.executescript(_MIGRATE_CAREL_SQL)
    except sqlite3.Error:
        # The script's own COMMIT never ran: drop the half-built wide table and
        # leave the long archive exactly as it was.
        conn.rollback()
        if not _carel_is_long(conn):
            # Lost the race (or waited out the busy timeout while the winner
            # committed): the archive IS wide now, which is all this call
            # wanted. The connection stays open and usable.
            log.info("carel_samples: already migrated by a concurrent opener")
            return
        # A genuine failure — the archive is still long. Raising (not
        # swallowing) is deliberate: a reader skips this file, the logger logs
        # the tick.
        conn.close()
        raise
    long_rows = conn.execute("SELECT count(*) FROM carel_samples_v1").fetchone()[0]
    wide_rows = conn.execute("SELECT count(*) FROM carel_samples").fetchone()[0]
    log.info(
        "carel_samples: migrated %s long rows → %s wide", long_rows, wide_rows
    )


def ensure_schema(path: Path | None = None) -> Path:
    """Создать файл БД и схему; вернуть путь."""
    p = db_path(path)
    _connect(p).close()
    return p


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = db_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0)
    fst = mount_fstype(p.parent)
    journal, sync = journaling_for_fstype(fst)
    conn.execute(f"PRAGMA journal_mode={journal}")
    conn.execute(f"PRAGMA synchronous={sync}")
    conn.executescript(_CREATE_DTV + _CREATE_CE + _CREATE_MR + _CREATE_CAREL)
    # Before the `with conn:` block: the pivot script owns its own transaction.
    _migrate_carel_to_wide(conn)
    with conn:
        if _needs_pk_migration(conn, "dtv_samples"):
            _migrate_table(conn, "dtv_samples", _CREATE_DTV)
        if _needs_pk_migration(conn, "ce_samples"):
            _migrate_table(conn, "ce_samples", _CREATE_CE)
        _ensure_ce_power_phase_cols(conn)
        _ensure_dtv_sensor_cols(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dtv_device_ts "
            "ON dtv_samples(device_id, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ce_device_ts "
            "ON ce_samples(device_id, ts)"
        )
        # mr_samples is a fresh table (no legacy PK to migrate); its read path
        # is (device_id, ch, ts) — one channel's series over a window.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mr_device_ch_ts "
            "ON mr_samples(device_id, ch, ts)"
        )
        # Wide since 1.0.6.41: the read path is (device_id, ts) — one device's
        # window, every metric in the row (like dtv/ce).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_carel_device_ts "
            "ON carel_samples(device_id, ts)"
        )
    return conn


def rotate_if_needed(path: Path | None = None) -> dict[str, Any]:
    """При размере active ≥ 3 ГиБ — архивировать и создать новый active."""
    p = db_path(path)
    if not p.is_file():
        return {"rotated": False, "reason": "no_file"}
    try:
        size = p.stat().st_size
    except OSError as exc:
        return {"rotated": False, "reason": str(exc)}
    if size < ROTATE_BYTES:
        return {"rotated": False, "size": size, "threshold": ROTATE_BYTES}
    free = free_bytes(p.parent)
    if free < ROTATE_HEADROOM:
        return {
            "rotated": False,
            "skipped": "low_space",
            "size": size,
            "free_bytes": free,
            "headroom": ROTATE_HEADROOM,
        }
    # checkpoint + rename
    try:
        conn = sqlite3.connect(str(p), timeout=60.0)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive = p.parent / f"devices_history_{stamp}.db"
    n = 0
    while archive.exists():
        n += 1
        archive = p.parent / f"devices_history_{stamp}_{n}.db"
    p.replace(archive)
    for suf in ("-wal", "-shm"):
        side = Path(str(p) + suf)
        if side.is_file():
            try:
                side.unlink()
            except OSError:
                pass
    ensure_schema(p)
    return {
        "rotated": True,
        "archive": str(archive),
        "active": str(p),
        "size": size,
    }


def _read_paths(path: Path | None) -> list[Path]:
    if path is not None:
        return [Path(path)]
    forced = str(os.environ.get("STAND_DEVICES_HISTORY_DB") or "").strip()
    if forced:
        return [Path(forced)]
    target = resolve_storage_target()
    paths = list_history_dbs(target.directory)
    return paths if paths else [target.active_path]


# совместимость: старое имя константы
DEFAULT_DB_PATH = Path("/var/lib/sa02m-stand") / ACTIVE_NAME
