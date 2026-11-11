"""What the batch writer promises when things go wrong (plan fix-1.0.7.36-logger G-D2).

- A process killed mid-batch leaves a whole database with none of that batch.
- A process killed between batches loses only the buffer.
- A failed batch rolls back the Carel edge baselines with it, so the retry
  still lands the edge exactly once; the buffer is kept, bounded and reported.
- A clean stop (SIGTERM) writes the buffer before exit.
- The writer releases the file before a rotation and before a promote moves it.
- An API read during a batch gets the last committed state, no error.
- The legacy `carel_samples_v1` table is dropped once when the writer opens.

A power cut is NOT tested here: that rests on SQLite's WAL guarantee with
synchronous=FULL (asserted in test_logger_write_amplification.py).
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import sa02m_devices_logger as logger
from sa02m_devices import history_store, history_write
from sa02m_devices.device_events import list_events, reset_carel_event_state
from sa02m_devices.history_mr import history_mr

try:
    from sa02m_devices.history_writer import ArchiveFlushError
except ImportError:  # the pre-fix tree: fail on behaviour, not on the import
    class ArchiveFlushError(Exception):  # type: ignore[no-redef]
        pass

# Spelled out, not imported: the test pins the table the owner chose to drop.
LEGACY_CAREL_TABLE = "carel_samples_v1"

PKG = Path(__file__).resolve().parents[1]
STEP_S = 5.0

# A child process: the rig of test_logger_write_amplification, standalone (no
# pytest), so the test can kill it with os._exit at a chosen point.
_CHILD_PRELUDE = textwrap.dedent('''
    import os, sys, time
    from types import SimpleNamespace
    sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
    import sa02m_devices_logger as logger
    from sa02m_devices import history_write
    from test_logger_crash_safety import _snapshot

    db = sys.argv[1]
    base_ts = float(sys.argv[2])
    alarm_from = int(sys.argv[3])
    os.environ["STAND_DEVICES_WIDGETS_PATH"] = db + ".widgets.json"
    tick_no = [0]
    logger.live_snapshot = lambda: _snapshot(
        base_ts + STEP * tick_no[0], alarm=1 if tick_no[0] >= alarm_from else 0)
    STEP = 5.0
    target = SimpleNamespace(backend="emmc", active_path=__import__("pathlib").Path(db))
    state = logger.LoggerState(flush_every_s=30.0)

    def tick():
        logger.logger_tick(target, state, now_m=1000.0 + STEP * tick_no[0])
        tick_no[0] += 1
''')


def _snapshot(ts: float, *, alarm: int = 0) -> dict:
    return {
        "ok": True,
        "ts": ts,
        "dtv": [{"ok": True, "id": "dtv-COM4-3", "room_temp": 21.0, "humidity": 40.0}],
        "ce": [{
            "ok": True, "id": "ce02m3-COM2-14", "kind": "ce",
            "voltage": {"a": 230.0, "b": 230.0, "c": 230.0},
            "current": {"a": 1.0, "b": 1.0, "c": 1.0},
            "power_w": {"a": 200.0, "b": 200.0, "c": 200.0, "total": 600.0},
            "frequency_hz": 50.0, "energy_kwh_import": 1.0,
        }],
        "mr": [{
            "id": "mr02m-COM3-7", "kind": "mr", "ai_count": 2,
            "channels": [
                {"ch": 1, "value": 22.5, "unit": "°C", "enabled": True, "ok": True},
                {"ch": 2, "value": 23.5, "unit": "°C", "enabled": True, "ok": True},
            ],
        }],
        "carel": [{
            "id": "carel-COM3-1", "kind": "carel", "alarm": alarm,
            "plant_state": "run", "unit_on": 1, "alarm_count": alarm,
            "supply_temp": 26.5, "setpoint": 27.0,
        }],
        "mtd": [{"id": "mtdx62-mb-COM3-20", "kind": "mtd", "presence": 1,
                 "illuminance_lux": 300.0, "target_distance_m": 1.0}],
        "devices": [],
        "alerts": [],
    }


def _q(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _dtv_ts(db: Path, base_ts: float) -> list[int]:
    return [round((r[0] - base_ts) / STEP_S)
            for r in _q(db, "SELECT ts FROM dtv_samples ORDER BY ts")]


def _child(db: Path, base_ts: float, alarm_from: int, body: str) -> subprocess.CompletedProcess:
    code = _CHILD_PRELUDE + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", code, str(db), repr(base_ts), str(alarm_from)],
        cwd=str(PKG), capture_output=True, text=True, timeout=120,
    )


def _integrity(db: Path) -> str:
    return _q(db, "PRAGMA integrity_check")[0][0]


# -- process death -----------------------------------------------------------

def test_process_kill_mid_flush_leaves_db_whole(tmp_path: Path):
    db = tmp_path / "devices_history.db"
    base_ts = time.time() - 3600.0
    # Ticks 0..6 (+0..+30 s): batch 1 = tick 0, batch 2 = ticks 1..6 at +30 s,
    # with MR due at ticks 2, 4, 6 and a Carel alarm edge at tick 3. The second
    # _insert_mr call inside batch 2 kills the process mid-transaction.
    r = _child(db, base_ts, 3, '''
        real = history_write._insert_mr
        calls = [0]
        def dying(conn, ts, mr):
            calls[0] += 1
            if calls[0] == 3:  # 1 in batch 1, then the 2nd in batch 2
                os._exit(9)
            return real(conn, ts, mr)
        history_write._insert_mr = dying
        for _ in range(7):
            tick()
        print("NOT KILLED")
    ''')
    assert r.returncode == 9, (r.returncode, r.stdout, r.stderr)
    assert _integrity(db) == "ok"
    # All or nothing: batch 2 left no row in any table; batch 1 is intact.
    assert _dtv_ts(db, base_ts) == [0]
    assert _q(db, "SELECT count(*) FROM ce_samples")[0][0] == 1
    assert _q(db, "SELECT count(*) FROM mr_samples")[0][0] == 2
    assert _q(db, "SELECT count(*) FROM carel_samples")[0][0] == 1
    assert _q(db, "SELECT count(*) FROM device_events")[0][0] == 0

    # Restart; the alarm rises on the restarted logger's 2nd tick and lands once.
    # (The +15 edge died with batch 2 — the documented loss of a crash.)
    r2 = _child(db, base_ts + 60.0, 1, '''
        for _ in range(2):
            tick()
        state.writer.close()
    ''')
    assert r2.returncode == 0, r2.stderr
    kinds = [k for (k,) in _q(db, "SELECT kind FROM device_events ORDER BY id")]
    assert kinds.count("carel_alarm_on") == 1, kinds


def test_process_kill_between_flushes_loses_only_buffer(tmp_path: Path):
    db = tmp_path / "devices_history.db"
    base_ts = time.time() - 3600.0
    r = _child(db, base_ts, 99, '''
        for _ in range(8):  # batches at tick 0 and tick 6 (+30 s); tick 7 buffered
            tick()
        os._exit(9)
    ''')
    assert r.returncode == 9, (r.returncode, r.stderr)
    assert _integrity(db) == "ok"
    assert _dtv_ts(db, base_ts) == [0, 1, 2, 3, 4, 5, 6]


# -- failed batch ------------------------------------------------------------

class _Rig:
    def __init__(self, tmp_path: Path, monkeypatch, *, backend: str = "emmc"):
        self.db = tmp_path / "devices_history.db"
        self.base_ts = time.time() - 3600.0
        self.n = 0
        self.alarm = 0
        monkeypatch.setattr(logger, "live_snapshot", self._snap)
        monkeypatch.setenv("STAND_DEVICES_WIDGETS_PATH", str(tmp_path / "none.json"))
        reset_carel_event_state()
        self.target = SimpleNamespace(backend=backend, active_path=self.db)
        self.state = logger.LoggerState(flush_every_s=30.0)

    def _snap(self) -> dict:
        return _snapshot(self.base_ts + STEP_S * self.n, alarm=self.alarm)

    def tick(self) -> None:
        try:
            logger.logger_tick(self.target, self.state, now_m=1000.0 + STEP_S * self.n)
        finally:
            self.n += 1


def _fail_ce(monkeypatch, flag: dict) -> None:
    """Fail the CE insert while flag["on"], for snapshots at/after flag["from_ts"]."""
    real = history_write._insert_ce

    def maybe(conn, ts, ce):
        if flag["on"] and ts >= flag.get("from_ts", float("-inf")):
            raise sqlite3.OperationalError("database or disk is full")
        return real(conn, ts, ce)

    monkeypatch.setattr(history_write, "_insert_ce", maybe)


def test_retry_after_failed_flush_keeps_carel_edge(tmp_path: Path, monkeypatch):
    rig = _Rig(tmp_path, monkeypatch)
    flag = {"on": False}
    _fail_ce(monkeypatch, flag)
    try:
        rig.tick()  # +0: batch 1, Carel baseline alarm=0
        rig.alarm = 1
        for _ in range(5):  # +5..+25 buffered, the edge is at +5
            rig.tick()
        # Fail on the LAST snapshot of batch 2, after its detectors already
        # moved the in-memory baseline to alarm=1 for the +5 edge.
        flag["on"] = True
        flag["from_ts"] = rig.base_ts + 30.0
        with pytest.raises(ArchiveFlushError):
            rig.tick()  # +30: batch 2 fails and rolls back
        flag["on"] = False
        for _ in range(6):  # +35..+60: the retry is batch 3 at +60
            rig.tick()
        kinds = [k for (k,) in _q(rig.db, "SELECT kind FROM device_events")]
        assert kinds.count("carel_alarm_on") == 1, kinds
        assert _dtv_ts(rig.db, rig.base_ts) == list(range(13))
    finally:
        rig.state.writer.close()


def test_failed_flush_keeps_a_bounded_reported_buffer(tmp_path: Path, monkeypatch, capsys):
    rig = _Rig(tmp_path, monkeypatch)
    flag = {"on": False}
    _fail_ce(monkeypatch, flag)
    rig.tick()
    flag["on"] = True
    errors: list[str] = []
    for _ in range(40):  # 200 s of an unwritable archive
        try:
            rig.tick()
        except ArchiveFlushError as exc:
            errors.append(str(exc))
    writer = rig.state.writer
    assert writer.max_pending == 24  # 4 batches of 30 s at the 5 s eMMC tick
    assert writer.pending == 24
    assert writer.dropped == 40 - 24
    # Once per batch interval, each naming what is held and what was lost.
    assert len(errors) == 6, errors
    assert "pending=24 dropped=" in errors[-1], errors[-1]
    assert "database or disk is full" in errors[-1]
    flag["on"] = False
    writer.close()
    assert len(_dtv_ts(rig.db, rig.base_ts)) == 1 + 24
    assert "16 snapshot(s) were dropped" in capsys.readouterr().err


# -- file replaced under the writer -----------------------------------------

def test_writer_is_closed_before_rotation(tmp_path: Path, monkeypatch):
    rig = _Rig(tmp_path, monkeypatch)
    rig.tick()
    for _ in range(5):
        rig.tick()
    monkeypatch.setattr(history_store, "ROTATE_BYTES", 1)
    monkeypatch.setattr(history_store, "ROTATE_HEADROOM", 0)
    rig.tick()  # +30: batch, then rotation
    monkeypatch.setattr(history_store, "ROTATE_BYTES", 1 << 40)
    archives = sorted(tmp_path.glob("devices_history_*.db"))
    assert len(archives) == 1, list(tmp_path.iterdir())
    # The whole batch went to the file BEFORE it was renamed, nothing after.
    assert _dtv_ts(archives[0], rig.base_ts) == list(range(7))
    assert _dtv_ts(rig.db, rig.base_ts) == []
    for _ in range(6):
        rig.tick()
    rig.state.writer.close()
    assert _dtv_ts(rig.db, rig.base_ts) == list(range(7, 13))
    assert _dtv_ts(archives[0], rig.base_ts) == list(range(7))


def test_main_flushes_to_old_media_before_promote_and_on_stop(tmp_path: Path, monkeypatch):
    """main() end to end: eMMC → USB switch with a buffer pending, then a stop.

    The buffered ticks must land in the OLD file before the promote copies it
    (else they are written after the copy, into a file the promote removed),
    and the ticks after the switch must be written when the loop stops."""
    from sa02m_devices import device_history_migrate

    emmc = tmp_path / "emmc" / "devices_history.db"
    usb = tmp_path / "usb" / "devices_history.db"
    emmc.parent.mkdir()
    usb.parent.mkdir()
    staging = tmp_path / "staging" / "devices_history.db"
    monkeypatch.setattr(device_history_migrate, "emmc_staging_path", lambda: staging)
    monkeypatch.setattr(logger, "emmc_staging_path", lambda: staging)
    monkeypatch.setenv("STAND_DEVICES_WIDGETS_PATH", str(tmp_path / "none.json"))
    reset_carel_event_state()

    t_emmc = SimpleNamespace(backend="emmc", active_path=emmc)
    t_usb = SimpleNamespace(backend="usb", active_path=usb)
    base_ts = time.time() - 3600.0
    n = [0]
    SWITCH_AT, STOP_AT = 4, 7

    def resolve(*, force_refresh=False):  # noqa: ARG001
        return t_emmc if n[0] < SWITCH_AT else t_usb

    def snap():
        s = _snapshot(base_ts + n[0])
        n[0] += 1
        if n[0] >= STOP_AT:
            logger._stop = True
        return s

    monkeypatch.setattr(logger, "resolve_storage_target", resolve)
    monkeypatch.setattr(logger, "live_snapshot", snap)
    monkeypatch.setattr(logger, "FLUSH_EVERY_S", 3600.0)  # only switch/stop write
    monkeypatch.setattr(logger, "INTERVAL_EMMC_S", 0.2)
    monkeypatch.setattr(logger, "INTERVAL_S", 0.2)
    monkeypatch.setattr(logger, "RESOLVE_EVERY_S", 0.0)
    monkeypatch.setattr(logger, "_stop", False)
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    try:
        assert logger.main() == 0
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        logger._stop = False
    got = [round(r[0] - base_ts) for r in _q(usb, "SELECT ts FROM dtv_samples ORDER BY ts")]
    assert got == list(range(STOP_AT)), got
    assert not emmc.exists(), "the promote did not take the eMMC file"


# -- concurrent reader -------------------------------------------------------

def test_api_read_during_flush_gets_committed_state(tmp_path: Path, monkeypatch):
    rig = _Rig(tmp_path, monkeypatch)
    rig.base_ts = time.time() - 120.0  # inside the 1 h chart window
    rig.tick()  # batch 1 committed: 2 MR channels at +0
    seen: list[dict] = []
    real = history_write._insert_mr

    def read_mid_batch(conn, ts, mr):
        real(conn, ts, mr)
        # The batch holds the write lock right now (BEGIN IMMEDIATE).
        seen.append(history_mr("mr02m-COM3-7", "1h", ch=1, path=rig.db))
        seen.append(list_events(path=rig.db))

    monkeypatch.setattr(history_write, "_insert_mr", read_mid_batch)
    t0 = time.monotonic()
    for _ in range(6):
        rig.tick()  # batch 2 at +30
    elapsed = time.monotonic() - t0
    rig.state.writer.close()
    assert len(seen) == 2 * 3, "the reads inside the batch never ran"
    assert elapsed < 10.0, f"a reader waited on the batch's lock ({elapsed:.1f} s)"
    for res in seen:
        assert res.get("ok", True) is True and not res.get("error"), res
    # The reader sees the last COMMITTED state: batch 1 only.
    assert seen[1]["count"] == 0
    # A reader that skipped the file would answer ok with no series at all.
    points = sum(len(x.get("points") or []) for x in seen[0]["series"])
    assert points == 1, seen[0]


# -- clean stop ----------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM delivery is POSIX-only")
def test_sigterm_flushes(tmp_path: Path):
    db = tmp_path / "devices_history.db"
    base_ts = time.time() - 3600.0
    code = textwrap.dedent('''
        import os, sys, time
        sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
        import sa02m_devices_logger as logger
        from test_logger_crash_safety import _snapshot
        base_ts = float(sys.argv[1])
        n = [0]
        def snap():
            if n[0] >= 3:
                while not logger._stop:  # hold tick 4 until SIGTERM arrives
                    time.sleep(0.02)
                raise RuntimeError("stopping")
            s = _snapshot(base_ts + 5.0 * n[0])
            n[0] += 1
            print("tick", n[0], flush=True)
            return s
        logger.live_snapshot = snap
        raise SystemExit(logger.main())
    ''')
    env = dict(os.environ)
    env.update({
        "STAND_DEVICES_HISTORY_DB": str(db),
        "STAND_DEVICES_LOG_INTERVAL_S": "0.2",
        "STAND_DEVICES_FLUSH_EVERY_S": "30",
        "STAND_DEVICES_WIDGETS_PATH": str(tmp_path / "none.json"),
    })
    proc = subprocess.Popen(
        [sys.executable, "-c", code, repr(base_ts)], cwd=str(PKG), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for line in proc.stdout:
            if line.strip() == "tick 3":
                break
        time.sleep(0.5)  # tick 4 is now parked in snap()
        before = _q(db, "SELECT count(*) FROM dtv_samples")[0][0]
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    assert before == 1, f"{before} ticks were on disk before the stop (want the first batch only)"
    assert _q(db, "SELECT count(*) FROM dtv_samples")[0][0] == 3, err


# -- legacy carel_samples_v1 ----------------------------------------------------

def _seed_legacy(db: Path, rows: int) -> None:
    history_store.ensure_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            f"CREATE TABLE {LEGACY_CAREL_TABLE} (ts REAL NOT NULL, device_id TEXT,"
            " metric TEXT NOT NULL, value REAL, PRIMARY KEY (ts, device_id, metric))"
        )
        conn.executemany(
            f"INSERT INTO {LEGACY_CAREL_TABLE} VALUES (?,?,?,?)",
            ((float(i), "carel-COM3-1", "alarm", 0.0) for i in range(rows)),
        )
        conn.commit()
    finally:
        conn.close()


def _tables(db: Path) -> set[str]:
    return {r[0] for r in _q(db, "SELECT name FROM sqlite_master WHERE type='table'")}


def test_writer_drops_legacy_carel_table_once(tmp_path: Path, monkeypatch):
    db = tmp_path / "devices_history.db"
    _seed_legacy(db, 500)
    rig = _Rig(tmp_path, monkeypatch)
    rig.tick()
    rig.state.writer.close()
    assert LEGACY_CAREL_TABLE not in _tables(db)
    assert {"dtv_samples", "ce_samples", "mr_samples", "carel_samples",
            "mtd_samples", "device_events"} <= _tables(db)
    assert _dtv_ts(db, rig.base_ts) == [0]
    assert _q(db, "SELECT count(*) FROM carel_samples")[0][0] == 1
    # A second start on the same file, and a start on a file that never had it.
    rig2 = _Rig(tmp_path, monkeypatch)
    rig2.base_ts = rig.base_ts
    rig2.n = 1
    rig2.tick()
    rig2.state.writer.close()
    assert _dtv_ts(db, rig.base_ts) == [0, 1]
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    rig3 = _Rig(fresh, monkeypatch)
    rig3.tick()
    rig3.state.writer.close()
    assert _dtv_ts(rig3.db, rig3.base_ts) == [0]


def test_failed_legacy_drop_does_not_stop_the_writer(tmp_path: Path, monkeypatch, capsys):
    db = tmp_path / "devices_history.db"
    _seed_legacy(db, 10)
    real_connect = history_store._connect

    def deny_drop(path=None):
        conn = real_connect(path)
        conn.set_authorizer(
            lambda action, arg1, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_DROP_TABLE and arg1 == LEGACY_CAREL_TABLE
            else sqlite3.SQLITE_OK
        )
        return conn

    monkeypatch.setattr(history_store, "_connect", deny_drop)
    rig = _Rig(tmp_path, monkeypatch)
    rig.tick()  # the drop is refused; the batch is written all the same
    assert "carel_samples_v1 not dropped" in capsys.readouterr().err
    assert LEGACY_CAREL_TABLE in _tables(db)
    assert _dtv_ts(db, rig.base_ts) == [0]
    rig.state.writer.close()
    # The next open tries again.
    monkeypatch.setattr(history_store, "_connect", real_connect)
    rig.tick()
    rig.state.writer.close()
    assert LEGACY_CAREL_TABLE not in _tables(db)
    assert _dtv_ts(db, rig.base_ts) == [0, 1]


def test_staging_absorb_closes_the_writer_only_when_it_will_merge(tmp_path: Path, monkeypatch):
    """The writer is released before a staging absorb only when the absorb
    will really run: an empty staging file must not cost a checkpoint per tick."""
    staging = tmp_path / "staging" / "devices_history.db"
    usb = SimpleNamespace(backend="usb", active_path=tmp_path / "usb" / "devices_history.db")
    monkeypatch.setattr(logger, "emmc_staging_path", lambda: staging)
    assert logger._staging_absorb_due(usb) is False  # no staging file
    history_store.ensure_schema(staging)
    assert logger._staging_absorb_due(usb) is False  # schema only, no rows
    history_write.insert_sample(_snapshot(time.time() - 60.0), path=staging)
    assert logger._staging_absorb_due(usb) is True
    same = SimpleNamespace(backend="usb", active_path=staging)
    assert logger._staging_absorb_due(same) is False  # writing to staging itself
