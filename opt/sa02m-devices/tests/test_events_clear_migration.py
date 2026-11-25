"""Generation-aware imports preserve committed samples and safe retirement."""

import sqlite3
import time

import pytest

from sa02m_devices import device_events, device_history_migrate as migrate, history_store
from sa02m_devices import event_journal_state as journal
from sa02m_devices.history_writer import ArchiveWriter
from tests.test_events_clear_continuity import _snapshot, _samples, _seed_alarm
from tests.journal_support import private_authority


@pytest.fixture
def shared(tmp_path, monkeypatch, private_authority):
    monkeypatch.setenv("STAND_DEVICES_EVENTS_STATE_DIR", str(tmp_path / "authority"))
    journal.bootstrap(journal.domain_for(), ready=True)
    device_events.reset_carel_event_state()
    yield tmp_path / "source.db", tmp_path / "dest.db"
    device_events.reset_carel_event_state()


@pytest.mark.parametrize("copy", [False, True])
@pytest.mark.parametrize("tag", ["stale", "current", "untagged"])
def test_import_filters_old_rows_and_preserves_samples(shared, copy, tag):
    source, destination = shared
    base = time.time() - 100
    _seed_alarm(source, base)
    device_events.clear_events(destination)
    if tag == "current":
        writer = ArchiveWriter(source, flush_every_s=0)
        writer.add(_snapshot(base + 2, 0), mr_due=True)
        writer.flush()
        writer.close()
    elif tag == "untagged":
        conn = sqlite3.connect(source)
        conn.execute("INSERT INTO device_events(ts,device_id,kind,phase,message) "
                     "VALUES(?,?,?,?,?)", (base + 2, "old-client", "carel_alarm_on", "", "old"))
        conn.commit()
        conn.close()
    before = _samples(source)
    if not copy:
        history_store.ensure_schema(destination)
    report = migrate.promote_to_media(source, destination)
    assert report["ok"], report
    assert _samples(destination) == before
    conn = sqlite3.connect(destination)
    try:
        actual = conn.execute("SELECT kind FROM device_events ORDER BY ts").fetchall()
    finally:
        conn.close()
    assert actual == ([("carel_alarm_off",)] if tag == "current" else [])


def test_initial_standalone_destination_keeps_foreign_current_tag(tmp_path, monkeypatch):
    monkeypatch.delenv("STAND_DEVICES_EVENTS_STATE_DIR", raising=False)
    source, destination = tmp_path / "source.db", tmp_path / "dest.db"
    base = time.time() - 100
    _seed_alarm(source, base)
    device_events.clear_events(source)
    writer = ArchiveWriter(source, flush_every_s=0)
    writer.add(_snapshot(base + 2, 0), mr_due=True)
    writer.flush()
    writer.close()
    conn = sqlite3.connect(source)
    conn.execute("INSERT INTO device_events(ts,device_id,kind,phase,message) "
                 "VALUES(?,?,?,?,?)", (base + 3, "unaware-client", "carel_alarm_on", "", "old"))
    conn.commit()
    conn.close()
    source_generation = journal.read_atomic(journal.domain_for(source)).generation
    assert migrate.promote_to_media(source, destination)["ok"]
    assert journal.read_atomic(journal.domain_for(destination)).generation == "legacy"
    conn = sqlite3.connect(destination)
    try:
        assert conn.execute("SELECT journal_generation FROM device_events").fetchall() == [(source_generation,)]
    finally:
        conn.close()
    assert [e["kind"] for e in device_events.list_events(path=destination)["events"]] == ["carel_alarm_off"]


def test_pinned_wal_reader_keeps_source_until_retry_and_never_duplicates(shared):
    source, destination = shared
    base = time.time() - 100
    writer = ArchiveWriter(source, flush_every_s=0)
    writer.add(_snapshot(base, 0), mr_due=True)
    writer.flush()
    assert writer.open().execute("PRAGMA journal_mode").fetchone() == ("wal",)
    reader = sqlite3.connect(source, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM carel_samples").fetchone()
    writer.add(_snapshot(base + 1, 1), mr_due=True)
    writer.flush()
    writer.close()
    before = _samples(source)
    try:
        report = migrate.promote_to_media(source, destination)
        assert report["ok"] is False, report
        assert source.is_file()
        assert _samples(destination) == before
        assert _samples(source) == before
        assert len(device_events.list_events(path=destination)["events"]) == 1
    finally:
        reader.close()
    report = migrate.promote_to_media(source, destination)
    assert report["ok"], report
    assert not source.exists()
    assert _samples(destination) == before
    assert len(device_events.list_events(path=destination)["events"]) == 1
    assert report["events_merged"] == 0


def test_rotation_keeps_new_event_tags(shared, monkeypatch):
    source, _ = shared
    base = time.time() - 100
    writer = ArchiveWriter(source, flush_every_s=0)
    try:
        writer.add(_snapshot(base, 0), mr_due=True)
        writer.flush()
        moved = source.with_name("moved.db")
        writer.close()
        source.rename(moved)
        writer.add(_snapshot(base + 1, 1), mr_due=True)
        writer.flush()
        writer.close()
        monkeypatch.setattr(history_store, "ROTATE_BYTES", 1)
        monkeypatch.setattr(history_store, "ROTATE_HEADROOM", 0)
        result = history_store.rotate_if_needed(source)
        assert result["rotated"]
        writer.add(_snapshot(base + 2, 0), mr_due=True)
        assert [e["kind"] for e in writer.flush()] == ["carel_alarm_off"]
        assert [e["kind"] for e in device_events.list_events(path=source)["events"]] == ["carel_alarm_off"]
    finally:
        writer.close()


def test_idle_writer_reopens_a_replaced_inode(shared, monkeypatch):
    source, destination = shared
    # DELETE mode permits an ordinary rename of an idle database without
    # manufacturing an unsafe WAL-sidecar relocation in the test itself.
    monkeypatch.setattr(history_store, "journaling_for_fstype", lambda _: ("DELETE", "FULL"))
    writer = ArchiveWriter(source, flush_every_s=0)
    base = time.time() - 100
    try:
        writer.add(_snapshot(base, 0), mr_due=True)
        writer.flush()
        old_connection = writer.conn
        source.rename(destination)
        writer.add(_snapshot(base + 1, 1), mr_due=True)
        writer.flush()
        assert writer.conn is not old_connection
        assert [row[0] for row in _samples(source)["carel_samples"]] == [base + 1]
        assert [row[0] for row in _samples(destination)["carel_samples"]] == [base]
    finally:
        writer.close()


def test_rotation_refuses_pinned_wal_without_removing_samples(shared, monkeypatch):
    source, _ = shared
    writer = ArchiveWriter(source, flush_every_s=0)
    base = time.time() - 100
    writer.add(_snapshot(base, 0), mr_due=True)
    writer.flush()
    reader = sqlite3.connect(source, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM carel_samples").fetchall()
    writer.add(_snapshot(base + 1, 1), mr_due=True)
    writer.flush()
    writer.close()
    before = _samples(source)
    monkeypatch.setattr(history_store, "ROTATE_BYTES", 1)
    monkeypatch.setattr(history_store, "ROTATE_HEADROOM", 0)
    try:
        result = history_store.rotate_if_needed(source)
        assert result["rotated"] is False and result["skipped"] == "busy"
        assert _samples(source) == before
    finally:
        reader.close()
    result = history_store.rotate_if_needed(source)
    assert result["rotated"] is True
