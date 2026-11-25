"""External connections prepare authority before acquiring any SQLite lock."""

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sa02m_devices import device_events as events, device_history_db as history
from sa02m_devices import event_journal_state as journal
from sa02m_devices import history_store
from tests.journal_support import private_authority
from tests.test_device_events import _ce_snap, _carel_snap


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.delenv("STAND_DEVICES_EVENTS_STATE_DIR", raising=False)
    monkeypatch.setattr(events, "_CAREL_PREV", {})
    path = tmp_path / "standalone.db"
    history.ensure_schema(path)
    now = time.time()
    for ts in (now - 2, now - 1):
        history.insert_sample(_ce_snap(ts, ua=230), path=path)
        history.insert_carel_sample(_carel_snap(ts, alarm=0, plant="run"), path=path)
    return path, now


def detector(kind, now):
    if kind == "ce":
        return events.detect_ce_events, _ce_snap(now, ua=250), "voltage_up"
    return events.detect_carel_events, _carel_snap(now, alarm=1, plant="run"), "carel_alarm_on"


def connection_mode(conn, mode):
    if mode == "transaction":
        conn.execute("BEGIN IMMEDIATE")
    if mode == "cursor":
        cursor = conn.execute("SELECT ts FROM carel_samples ORDER BY ts")
        assert cursor.fetchone() is not None
        assert not conn.in_transaction
        return cursor
    return None


def forbidden(*args, **kwargs):
    raise AssertionError("borrowed connection must not acquire a guard, bootstrap or open SQLite")


@pytest.mark.parametrize("kind", ["ce", "carel"])
@pytest.mark.parametrize("mode", ["idle", "transaction", "cursor"])
def test_released_preparation_sequence_first_use_and_caller_rollback(archive, kind, mode, monkeypatch):
    path, now = archive
    domain = journal.domain_for(path)
    assert not domain.directory.exists()
    assert events.ensure_events_schema(path) == path
    detect, snapshot, expected_kind = detector(kind, now)
    with sqlite3.connect(path) as conn:
        cursor = connection_mode(conn, mode)
        trace = []
        conn.set_trace_callback(trace.append)
        with monkeypatch.context() as check:
            check.setattr(journal, "guard", forbidden)
            check.setattr(journal, "bootstrap", forbidden)
            check.setattr(sqlite3, "connect", forbidden)
            created = detect(snapshot, path=path, conn=conn)
        assert [event["kind"] for event in created] == [expected_kind]
        assert conn.in_transaction
        assert conn.execute("SELECT kind,journal_generation FROM device_events").fetchall() == [(expected_kind, "legacy")]
        assert not any(sql.upper().startswith(("COMMIT", "ROLLBACK")) for sql in trace)
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM device_events").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM carel_samples").fetchone() == (2,)
        if cursor is not None:
            assert len(cursor.fetchall()) == 1
    conn.close()
    assert journal.read_atomic(domain).ready


@pytest.mark.parametrize("kind", ["ce", "carel"])
@pytest.mark.parametrize("mode", ["idle", "transaction", "cursor"])
def test_unprepared_raw_connection_refuses_without_side_effects(archive, kind, mode, monkeypatch):
    path, now = archive
    domain = journal.domain_for(path)
    detect, snapshot, _ = detector(kind, now)
    with sqlite3.connect(path) as conn:
        events.ensure_events_schema_on(conn)
        assert not domain.directory.exists(), "schema-only helper must not initialize authority"
        cursor = connection_mode(conn, mode)
        active = conn.in_transaction
        total = conn.total_changes
        trace = []
        conn.set_trace_callback(trace.append)
        with monkeypatch.context() as check:
            check.setattr(journal, "guard", forbidden)
            check.setattr(journal, "bootstrap", forbidden)
            check.setattr(sqlite3, "connect", forbidden)
            with pytest.raises(journal.JournalStateError):
                detect(snapshot, path=path, conn=conn)
        assert conn.in_transaction == active
        assert conn.total_changes == total
        assert trace == []
        assert not domain.directory.exists()
        assert not list(path.parent.glob(".journal-init-*"))
        if cursor is not None:
            assert len(cursor.fetchall()) == 1
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM device_events").fetchone() == (0,)
    conn.close()


def test_path_preparation_takes_guard_before_its_schema_connection(archive, monkeypatch):
    path, _ = archive
    domain = journal.domain_for(path)
    original = events._connect
    seen = []
    def connect(value):
        held = getattr(journal._local, "held", {})
        assert domain.directory in held
        assert held[domain.directory][0].generation == "legacy"
        seen.append(value)
        return original(value)
    monkeypatch.setattr(events, "_connect", connect)
    events.ensure_events_schema(path)
    assert seen == [path]


def test_path_preparation_never_bootstraps_missing_shared_authority(archive, monkeypatch):
    path, _ = archive
    shared = path.parent / "shared-authority"
    monkeypatch.setenv("STAND_DEVICES_EVENTS_STATE_DIR", str(shared))
    with monkeypatch.context() as check:
        check.setattr(events, "_connect", forbidden)
        check.setattr(journal, "bootstrap", forbidden)
        with pytest.raises(journal.JournalStateError):
            events.ensure_events_schema(path)
    assert not shared.exists()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM carel_samples").fetchone() == (2,)


@pytest.fixture
def scope_paths(tmp_path, monkeypatch, private_authority):
    for name in ("STAND_DEVICES_EVENTS_STATE_DIR", "STAND_DEVICES_HISTORY_DB",
                 "STAND_DEVICES_EVENTS_UPDATE_DIR"):
        monkeypatch.delenv(name, raising=False)
    paths = SimpleNamespace(automatic=tmp_path / "automatic.db", explicit=tmp_path / "explicit.db",
                            forced=tmp_path / "forced.db", production=tmp_path / "production-authority",
                            shared=tmp_path / "shared-authority")
    monkeypatch.setattr(journal, "DEFAULT_DIRECTORY", paths.production)
    monkeypatch.setattr(history_store, "resolve_storage_target", lambda: SimpleNamespace(active_path=paths.automatic))
    return paths


def test_omitted_path_missing_production_authority_refuses_before_archive_creation(scope_paths):
    paths = scope_paths
    with pytest.raises(journal.JournalStateError):
        events.ensure_events_schema()
    assert not paths.production.exists()
    assert not paths.automatic.exists()
    assert not Path(str(paths.automatic) + ".events-state").exists()


def test_omitted_path_reuses_production_authority_and_default_reads(scope_paths, monkeypatch):
    paths = scope_paths
    domain = journal.domain_for()
    assert domain.directory == paths.production and not domain.standalone
    before = journal.bootstrap(domain, ready=True)
    files = {p.name: (p.stat().st_ino, p.read_bytes()) for p in paths.production.iterdir()}
    original = events._connect
    held = []
    def connect(value):
        held.append(set(journal._local.held))
        return original(value)
    monkeypatch.setattr(events, "_connect", connect)
    assert events.ensure_events_schema() == paths.automatic
    assert held == [{paths.production}]
    assert not Path(str(paths.automatic) + ".events-state").exists()
    assert events.list_events()["events"] == []
    assert journal.read_atomic(domain) == before
    assert {p.name: (p.stat().st_ino, p.read_bytes()) for p in paths.production.iterdir()} == files


@pytest.mark.parametrize("selection", ["explicit", "forced", "explicit-forced", "shared",
                                      "shared-explicit", "shared-forced", "shared-explicit-forced"])
def test_preparation_retains_explicit_forced_and_shared_scope_precedence(scope_paths, monkeypatch, selection):
    paths = scope_paths
    if "forced" in selection:
        monkeypatch.setenv("STAND_DEVICES_HISTORY_DB", str(paths.forced))
    if "shared" in selection:
        monkeypatch.setenv("STAND_DEVICES_EVENTS_STATE_DIR", str(paths.shared))
    argument = paths.explicit if "explicit" in selection else None
    database = argument or (paths.forced if "forced" in selection else paths.automatic)
    authority = paths.shared if "shared" in selection else Path(str(database) + ".events-state")
    domain = journal.domain_for(argument)
    assert domain.directory == authority
    assert domain.standalone == ("shared" not in selection)
    before = journal.bootstrap(domain, ready=True) if "shared" in selection else None
    assert events.ensure_events_schema(argument) == database
    assert database.is_file()
    after = journal.read_atomic(domain)
    assert after.ready and after.generation == "legacy"
    if before is not None:
        assert after == before
        assert not Path(str(database) + ".events-state").exists()
    assert not paths.production.exists()
    assert events.list_events(path=argument)["events"] == []


def test_whole_standalone_authority_removal_is_outside_persistence_guarantee(scope_paths):
    path = scope_paths.explicit
    events.ensure_events_schema(path)
    domain = journal.domain_for(path)
    assert events.clear_events(path) == 0
    cleared = journal.read_atomic(domain)
    assert cleared.generation != "legacy"
    assert {p.name for p in domain.directory.iterdir()} == {"guard.lock", "state.json"}
    for child in domain.directory.iterdir():
        child.unlink()
    domain.directory.rmdir()
    assert events.list_events(path=path)["events"] == []
    recreated = journal.read_atomic(domain)
    assert recreated.generation == "legacy"
    assert recreated.identity != cleared.identity


def test_absent_standalone_clear_is_noop_without_future_arrival_tombstone(scope_paths):
    path = scope_paths.explicit
    domain = journal.domain_for(path)
    assert events.clear_events(path) == 0
    assert not path.exists() and not domain.directory.exists()
    with sqlite3.connect(path) as conn:
        events.ensure_events_schema_on(conn)
        conn.execute("INSERT INTO device_events(ts,device_id,kind,message) VALUES(1,'old','carel_alarm_on','returned')")
    conn.close()
    assert [row["message"] for row in events.list_events(path=path)["events"]] == ["returned"]
