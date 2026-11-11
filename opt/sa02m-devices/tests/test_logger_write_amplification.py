"""The archive logger writes in batches, not on every tick (audit 2026-11-11 IO-01).

Before the fix every archive function on every tick opened its own SQLite
connection and closed it again. Closing the LAST connection to a WAL database
runs a full checkpoint: fsync the WAL, copy its pages into the database, fsync
the database, delete the WAL — two forced writes per writing function, about
ten per 10 s on the bench eMMC (`.ai-dev/audit/20261111-IO.md`).

Every test here drives the real `logger.logger_tick()` with a stubbed
live_snapshot(), so on the pre-fix tree they fail on the defect itself (the
count of opens / commits / WAL deletions), not on a missing name. The batch
interval is set by assignment (`state.flush_every_s = 30.0`); the pre-fix
LoggerState simply carries the extra attribute and ignores it.

Commits are counted from the WAL file itself: a frame whose header carries a
non-zero «database size after commit» field ends a transaction. A reader pinned
in an open read transaction keeps the WAL from being reset, so every frame
written during the measurement is still in the file to be counted.
"""

from __future__ import annotations

import sqlite3
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import sa02m_devices_logger as logger
from sa02m_devices.device_events import reset_carel_event_state

STEP_S = 5.0  # the eMMC tick
START_M = 1000.0


def _snapshot(ts: float, i: int = 0) -> dict:
    """A small fleet: one of every archived kind, MR with 12 enabled channels."""
    return {
        "ok": True,
        "ts": ts,
        "dtv": [{
            "ok": True, "id": "dtv-COM4-3", "room_temp": 21.0 + 0.01 * i,
            "humidity": 40.0, "temp_sensors": [{"t": "HDC1080", "v": 21.0}],
        }],
        "ce": [{
            "ok": True, "id": "ce02m3-COM2-14", "kind": "ce",
            "voltage": {"a": 230.0, "b": 230.0, "c": 230.0},
            "current": {"a": 1.0, "b": 1.0, "c": 1.0},
            "power_w": {"a": 200.0, "b": 200.0, "c": 200.0, "total": 600.0},
            "frequency_hz": 50.0, "energy_kwh_import": 1.0 + 0.001 * i,
        }],
        "mr": [{
            "id": "mr02m-COM3-7", "kind": "mr", "ai_count": 12,
            "channels": [
                {"ch": ch, "value": 20.0 + ch + 0.01 * i, "unit": "°C",
                 "enabled": True, "ok": True}
                for ch in range(1, 13)
            ],
        }],
        "carel": [{
            "id": "carel-COM3-1", "kind": "carel", "alarm": 0,
            "plant_state": "run", "unit_on": 1, "alarm_count": 0,
            "supply_temp": 26.5, "setpoint": 27.0,
        }],
        "mtd": [{
            "id": "mtdx62-mb-COM3-20", "kind": "mtd",
            "presence": 1, "illuminance_lux": 346.5, "target_distance_m": 1.4,
        }],
        "devices": [],
        "alerts": [],
    }


class _Rig:
    """One archive file, one LoggerState, a monotonic clock in STEP_S steps."""

    def __init__(self, tmp_path: Path, monkeypatch, *, flush_every_s: float):
        self.db = tmp_path / "devices_history.db"
        self.base_ts = time.time() - 3600.0  # inside retention, never purged
        self.i = 0
        self.now_m = START_M
        monkeypatch.setattr(logger, "live_snapshot", self._snap)
        monkeypatch.setenv("STAND_DEVICES_WIDGETS_PATH", str(tmp_path / "none.json"))
        reset_carel_event_state()
        self.target = SimpleNamespace(backend="emmc", active_path=self.db)
        self.state = logger.LoggerState()
        self.state.flush_every_s = flush_every_s

    def _snap(self) -> dict:
        return _snapshot(self.base_ts + (self.now_m - START_M), self.i)

    def tick(self) -> None:
        logger.logger_tick(self.target, self.state, now_m=self.now_m)
        self.i += 1
        self.now_m += STEP_S

    def close(self) -> None:
        writer = getattr(self.state, "writer", None)
        if writer is not None:
            writer.close()


def _count(db: Path, table: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def wal_commit_frames(db: Path) -> tuple[int, int]:
    """(commit frames, all frames) of the current WAL generation."""
    wal = Path(str(db) + "-wal")
    if not wal.is_file():
        return 0, 0
    data = wal.read_bytes()
    if len(data) < 32:
        return 0, 0
    page = struct.unpack(">I", data[8:12])[0]
    salt = data[16:24]
    commits = frames = 0
    off = 32
    while off + 24 + page <= len(data):
        hdr = data[off:off + 24]
        if hdr[8:16] != salt:
            break  # a stale frame of an older WAL generation
        frames += 1
        if struct.unpack(">I", hdr[4:8])[0] != 0:
            commits += 1
        off += 24 + page
    return commits, frames


def _pin_reader(db: Path) -> sqlite3.Connection:
    reader = sqlite3.connect(str(db), isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM dtv_samples").fetchone()
    return reader


def test_steady_ticks_do_not_open_sqlite(tmp_path: Path, monkeypatch):
    rig = _Rig(tmp_path, monkeypatch, flush_every_s=30.0)
    rig.tick()  # warm-up: opens the writer, first batch, first purge
    real = sqlite3.connect
    calls: list[tuple] = []

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", counting)
    try:
        for _ in range(12):  # 60 s of eMMC ticks, 6 of them with an MR sample
            rig.tick()
    finally:
        monkeypatch.setattr(sqlite3, "connect", real)
        rig.close()
    assert len(calls) == 0, f"12 steady ticks opened SQLite {len(calls)}x (want 0)"


def _commits_over_12_ticks(tmp_path: Path, monkeypatch, flush_every_s: float) -> int:
    rig = _Rig(tmp_path, monkeypatch, flush_every_s=flush_every_s)
    rig.tick()
    reader = _pin_reader(rig.db)
    try:
        c0, _ = wal_commit_frames(rig.db)
        for _ in range(12):
            rig.tick()
        c1, _ = wal_commit_frames(rig.db)
    finally:
        reader.close()
        rig.close()
    return c1 - c0


def test_commits_follow_flush_cadence(tmp_path: Path, monkeypatch):
    n = _commits_over_12_ticks(tmp_path, monkeypatch, 30.0)
    assert n == 2, f"60 s of ticks at a 30 s batch made {n} commits (want 2)"


def test_unbatched_writer_commits_once_per_tick(tmp_path: Path, monkeypatch):
    # flush_every_s=0 (the LoggerState default): still ONE transaction per
    # tick, not one per archive function.
    n = _commits_over_12_ticks(tmp_path, monkeypatch, 0.0)
    assert n == 12, f"12 unbatched ticks made {n} commits (want 12)"


def test_wal_survives_between_ticks(tmp_path: Path, monkeypatch):
    rig = _Rig(tmp_path, monkeypatch, flush_every_s=30.0)
    wal = Path(str(rig.db) + "-wal")
    missing: list[int] = []
    try:
        rig.tick()
        for k in range(7):  # crosses the batch at +30 s
            rig.tick()
            if not wal.is_file():
                missing.append(k)
    finally:
        rig.close()
    # Each deletion is a last-connection checkpoint: two forced writes.
    assert missing == [], f"the WAL was checkpointed away after ticks {missing}"


def test_writer_connection_is_full_sync_on_wal(tmp_path: Path, monkeypatch):
    rig = _Rig(tmp_path, monkeypatch, flush_every_s=30.0)
    try:
        rig.tick()
        conn = rig.state.writer.conn
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        # FULL: a committed batch is durable at COMMIT, not at the next checkpoint.
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        rig.close()


def test_close_flushes_pending(tmp_path: Path, monkeypatch):
    rig = _Rig(tmp_path, monkeypatch, flush_every_s=30.0)
    for _ in range(4):  # +0 batch, then +5/+10/+15 pending; MR due at +0 and +10
        rig.tick()
    assert _count(rig.db, "dtv_samples") == 1, "ticks after the batch were written early"
    rig.state.writer.close()
    # The writer was the last connection: its close is the one checkpoint.
    assert not Path(str(rig.db) + "-wal").is_file(), "close() left the WAL behind"
    assert _count(rig.db, "dtv_samples") == 4
    assert _count(rig.db, "ce_samples") == 4
    assert _count(rig.db, "mr_samples") == 2 * 12
    assert _count(rig.db, "carel_samples") == 2
    assert _count(rig.db, "mtd_samples") == 2
