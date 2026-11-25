"""Process barriers pin journal lock order and observation linearization."""

from contextlib import contextmanager
import multiprocessing as mp
import os
from pathlib import Path
import queue
import sqlite3
import time

import pytest

from sa02m_devices import device_events, device_history_migrate as migrate, history_store
from sa02m_devices import event_journal_state as journal
from sa02m_devices.history_writer import ArchiveWriter
from tests.test_events_clear_continuity import _snapshot, _seed_alarm
from tests.journal_support import private_authority, private_process


@pytest.fixture
def domain_paths(tmp_path, monkeypatch, private_authority):
    monkeypatch.setenv("STAND_DEVICES_EVENTS_STATE_DIR", str(tmp_path / "authority"))
    domain = journal.domain_for()
    journal.bootstrap(domain, ready=True)
    source, destination = tmp_path / "source.db", tmp_path / "dest.db"
    _seed_alarm(source, time.time() - 100)
    history_store.ensure_schema(destination)
    device_events.ensure_events_schema(destination)
    return domain, source, destination


@contextmanager
def _process(ctx, function, *args):
    process = ctx.Process(target=function, args=args)
    process.start()
    try:
        yield process
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)


@private_process
def _clear(path, output, barrier=None, acquired=None, timeout=0.3):
    if acquired is not None:
        original = device_events._clear_targets

        def targets(value):
            acquired.set()
            return original(value)

        device_events._clear_targets = targets
    if barrier is not None:
        barrier.wait(10)
    start = time.monotonic()
    try:
        output.put(("ok", device_events.clear_events(path, busy_timeout_s=timeout)))
    except device_events.EventsBusy:
        output.put(("busy", time.monotonic() - start))


@pytest.mark.parametrize("method", ["spawn", "fork"])
def test_guard_timeout_and_fork_never_unlock_parent(domain_paths, method):
    domain, source, _ = domain_paths
    ctx = mp.get_context(method)
    output = mp.get_context("spawn").Queue()
    original = journal.read_atomic(domain)
    with journal.guard(domain, exclusive=True):
        with _process(ctx, _clear, source, output):
            result = output.get(timeout=5)
            assert result[0] == "busy" and result[1] < 5
        # A second independent contender proves the fork child did not unlock
        # the parent's shared open-file-description while resetting its cache.
        with _process(mp.get_context("spawn"), _clear, source, output):
            assert output.get(timeout=5)[0] == "busy"
    assert journal.read_atomic(domain) == original
    assert len(device_events.list_events(path=source)["events"]) == 1


def test_two_clears_serialize_without_double_counting(domain_paths):
    domain, source, _ = domain_paths
    ctx = mp.get_context("spawn")
    barrier, output = ctx.Barrier(3), ctx.Queue()
    original = journal.read_atomic(domain)
    with _process(ctx, _clear, source, output, barrier), _process(ctx, _clear, source, output, barrier):
        barrier.wait(10)
        results = [output.get(timeout=5), output.get(timeout=5)]
    assert sorted(results) == [("ok", 0), ("ok", 1)]
    assert journal.read_atomic(domain).generation != original.generation
    assert device_events.list_events(path=source)["events"] == []


@private_process
def _logger_capture(path, inside_snapshot, allow_snapshot, filtered, cleared, output):
    import sa02m_devices_logger as logger

    base = time.time()

    def snapshot():
        inside_snapshot.set()
        assert allow_snapshot.wait(10)
        return _snapshot(base, 0)

    def filter_snapshot(value):
        filtered.set()
        assert cleared.wait(10)
        return value

    logger.live_snapshot = snapshot
    logger.filter_for_archive = filter_snapshot
    state = logger.LoggerState(last_purge=time.time(), flush_every_s=0)
    target = type("Target", (), {"active_path": path, "backend": "usb"})()
    try:
        logger.logger_tick(target, state, now_m=1000.0)
        output.put(state.writer.open().execute("SELECT count(*) FROM carel_samples").fetchone()[0])
    finally:
        logger.close_writer(state)


def test_logger_capture_blocks_clear_then_old_buffer_stays_suppressed(domain_paths):
    _, source, _ = domain_paths
    ctx = mp.get_context("spawn")
    inside, allow, filtered, cleared = (ctx.Event() for _ in range(4))
    output = ctx.Queue()
    with _process(ctx, _logger_capture, source, inside, allow, filtered, cleared, output):
        assert inside.wait(5)
        with pytest.raises(device_events.EventsBusy):
            device_events.clear_events(source, busy_timeout_s=0.1)
        allow.set()
        assert filtered.wait(5)
        assert device_events.clear_events(source) == 1
        cleared.set()
        assert output.get(timeout=5) == 3
    assert device_events.list_events(path=source)["events"] == []


@private_process
def _flush_hold(path, entered, release, output):
    writer = ArchiveWriter(path, flush_every_s=0)
    original = writer._replay

    def replay(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    writer._replay = replay
    writer.add(_snapshot(time.time(), 0), mr_due=False)
    try:
        output.put([e["kind"] for e in writer.flush()])
    finally:
        writer.close()


def test_clear_waits_for_flush_and_wal_read_remains_available(domain_paths):
    _, source, _ = domain_paths
    ctx = mp.get_context("spawn")
    entered, release, output = ctx.Event(), ctx.Event(), ctx.Queue()
    with _process(ctx, _flush_hold, source, entered, release, output):
        assert entered.wait(5)
        assert len(device_events.list_events(path=source)["events"]) == 1
        with pytest.raises(device_events.EventsBusy):
            device_events.clear_events(source, busy_timeout_s=0.1)
        release.set()
        assert output.get(timeout=5) == ["carel_alarm_off"]
    assert device_events.clear_events(source) == 2


@private_process
def _structural_hold(source, destination, operation, entered, release, output):
    if operation == "rotation":
        original = migrate._checkpoint_and_close

        def checkpoint(path):
            entered.set()
            assert release.wait(10)
            return original(path)

        migrate._checkpoint_and_close = checkpoint
        history_store.ROTATE_BYTES = 1
        history_store.ROTATE_HEADROOM = 0
        output.put(history_store.rotate_if_needed(source))
    else:
        original = migrate._event_predicate

        def predicate(*args):
            entered.set()
            assert release.wait(10)
            return original(*args)

        migrate._event_predicate = predicate
        output.put(migrate.copy_db(source, destination) if operation == "copy"
                   else migrate.merge_db_into(source, destination))


@pytest.mark.parametrize("operation", ["copy", "merge", "rotation"])
def test_clear_serializes_with_structural_archive_operation(domain_paths, operation):
    domain, source, destination = domain_paths
    if operation == "copy":
        destination.unlink()
    ctx = mp.get_context("spawn")
    entered, release, output = ctx.Event(), ctx.Event(), ctx.Queue()
    before = journal.read_atomic(domain)
    with _process(ctx, _structural_hold, source, destination, operation, entered, release, output):
        assert entered.wait(5)
        with pytest.raises(device_events.EventsBusy):
            device_events.clear_events(source, busy_timeout_s=0.1)
        assert journal.read_atomic(domain) == before
        release.set()
        result = output.get(timeout=5)
        assert result.get("ok", result.get("rotated")), result
    target = source if operation == "rotation" else destination
    device_events.clear_events(target)
    assert device_events.list_events(path=target)["events"] == []


@private_process
def _legacy_conn(path, write_locked, may_detect, detected, may_commit, output):
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        write_locked.set()
        assert may_detect.wait(10)
        created = device_events.detect_carel_events(_snapshot(time.time(), 0), path=path, conn=conn)
        detected.set()
        assert may_commit.wait(10)
        conn.execute("COMMIT")
        output.put([e["kind"] for e in created])
    finally:
        conn.close()


def test_legacy_conn_never_waits_for_guard_held_by_clear(domain_paths):
    _, source, _ = domain_paths
    ctx = mp.get_context("spawn")
    locked, acquired, detected, commit = (ctx.Event() for _ in range(4))
    output, clear_output = ctx.Queue(), ctx.Queue()
    with _process(ctx, _legacy_conn, source, locked, acquired, detected, commit, output):
        assert locked.wait(5)
        with _process(ctx, _clear, source, clear_output, None, acquired, 3.0):
            assert acquired.wait(5)
            assert detected.wait(2), "conn= detector waited for journal guard after SQLite lock"
            commit.set()
            assert clear_output.get(timeout=5) == ("ok", 2)
        assert output.get(timeout=5) == ["carel_alarm_off"]
    assert device_events.list_events(path=source)["events"] == []


def test_late_legacy_conn_commit_keeps_pre_clear_tag_invisible(domain_paths):
    domain, source, destination = domain_paths
    ctx = mp.get_context("spawn")
    locked, detect, detected, commit = (ctx.Event() for _ in range(4))
    output = ctx.Queue()
    with _process(ctx, _legacy_conn, source, locked, detect, detected, commit, output):
        assert locked.wait(5)
        detect.set()
        assert detected.wait(5)
        assert device_events.clear_events(destination) == 0
        commit.set()
        assert output.get(timeout=5) == ["carel_alarm_off"]
    conn = sqlite3.connect(source)
    try:
        assert conn.execute("SELECT DISTINCT journal_generation FROM device_events").fetchall() == [("legacy",)]
    finally:
        conn.close()
    assert journal.read_atomic(domain).generation != "legacy"
    assert device_events.list_events(path=source)["events"] == []


def test_unaware_insert_and_generic_merge_cannot_bless_old_rows(domain_paths):
    _, source, destination = domain_paths
    device_events.clear_events(destination)
    writer = ArchiveWriter(source, flush_every_s=0)
    writer.add(_snapshot(time.time(), 0), mr_due=False)
    writer.flush()
    writer.close()
    src, dst = sqlite3.connect(source), sqlite3.connect(destination)
    try:
        dst.execute("INSERT INTO device_events(ts,device_id,kind,message) VALUES(?,?,?,?)",
                    (time.time(), "old-client", "carel_alarm_on", "unaware insert"))
        migrate._merge_table(dst, src, "device_events", ("id",))
        dst.commit()
        assert dst.execute("SELECT count(*) FROM device_events").fetchone() == (3,)
    finally:
        src.close()
        dst.close()
    assert [e["kind"] for e in device_events.list_events(path=destination)["events"]] == ["carel_alarm_off"]


@private_process
def _purge_hold(path, entered, release, output):
    original = device_events._connect

    def connect(value):
        entered.set()
        assert release.wait(10)
        return original(value)

    device_events._connect = connect
    output.put(device_events.purge_events(path, cutoff=time.time() + 1))


def test_standalone_event_purge_acquires_guard_before_sqlite(domain_paths):
    domain, source, _ = domain_paths
    ctx = mp.get_context("spawn")
    entered, release, output = ctx.Event(), ctx.Event(), ctx.Queue()
    before = journal.read_atomic(domain)
    with _process(ctx, _purge_hold, source, entered, release, output):
        assert entered.wait(5)
        with pytest.raises(device_events.EventsBusy):
            device_events.clear_events(source, busy_timeout_s=0.1)
        release.set()
        assert output.get(timeout=5) == 1
    assert journal.read_atomic(domain) == before


def test_legacy_conn_migrates_old_event_schema_without_committing(domain_paths, tmp_path, monkeypatch):
    from sa02m_devices.device_history_db import insert_carel_sample

    _, _, _ = domain_paths
    path = tmp_path / "old-client.db"
    base = time.time() - 100
    insert_carel_sample(_snapshot(base, 0), path=path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE device_events(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "ts REAL NOT NULL, device_id TEXT NOT NULL, kind TEXT NOT NULL, "
                 "phase TEXT NOT NULL DEFAULT '', port_num INTEGER, addr INTEGER, "
                 "value REAL, ref_value REAL, message TEXT NOT NULL)")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")

    def unexpected_connect(*args, **kwargs):
        raise AssertionError("conn= detector opened a second SQLite connection")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", unexpected_connect)
            created = device_events.detect_carel_events(_snapshot(base + 1, 1), path=path, conn=conn)
        assert [event["kind"] for event in created] == ["carel_alarm_on"]
        assert conn.in_transaction
        assert conn.execute("SELECT journal_generation FROM device_events").fetchall() == [("legacy",)]
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM device_events").fetchone() == (0,)
        assert "journal_generation" not in {row[1] for row in conn.execute("PRAGMA table_info(device_events)")}
    finally:
        conn.close()
