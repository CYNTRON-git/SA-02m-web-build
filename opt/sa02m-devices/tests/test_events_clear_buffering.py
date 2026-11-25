"""Captured observations retain sample and detector continuity across clear."""

import sqlite3
import time

import pytest

from sa02m_devices import device_events, event_journal_state as journal
from sa02m_devices.history_writer import ArchiveWriter, ArchiveFlushError
from tests.test_events_clear_continuity import _snapshot
from tests.journal_support import private_authority


@pytest.fixture
def rig(tmp_path, monkeypatch, private_authority):
    monkeypatch.setenv("STAND_DEVICES_EVENTS_STATE_DIR", str(tmp_path / "authority"))
    domain = journal.domain_for()
    journal.bootstrap(domain, ready=True)
    device_events.reset_carel_event_state()
    path = tmp_path / "samples.db"
    writer = ArchiveWriter(path, flush_every_s=30)
    base = time.time() - 20
    writer.add(_snapshot(base, 0), mr_due=True)
    assert writer.flush() == []
    yield path, writer, domain, base
    writer.close()
    device_events.reset_carel_event_state()


def _kinds(path):
    return [e["kind"] for e in device_events.list_events(path=path)["events"]]


def _fail_commit_once(writer):
    original = writer.open()

    class CommitFailure:
        def __getattr__(self, name):
            return getattr(original, name)

        def execute(self, sql, *args):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("injected failed commit")
            return original.execute(sql, *args)

    writer.conn = CommitFailure()


@pytest.mark.parametrize("failed_commit", [False, True])
def test_mixed_batch_suppresses_old_edge_and_advances_baseline(rig, failed_commit):
    path, writer, _, base = rig
    writer.add(_snapshot(base + 1, 1), mr_due=True)
    device_events.clear_events(path)
    writer.add(_snapshot(base + 2, 0), mr_due=True)
    if failed_commit:
        _fail_commit_once(writer)
        with pytest.raises(ArchiveFlushError):
            writer.flush()
        assert writer.pending == 2
        assert _kinds(path) == []
    assert [e["kind"] for e in writer.flush()] == ["carel_alarm_off"]
    assert _kinds(path) == ["carel_alarm_off"]
    assert writer.open().execute("SELECT kind FROM device_events").fetchall() == [("carel_alarm_off",)]
    assert writer.open().execute("SELECT alarm FROM carel_samples ORDER BY ts").fetchall() == [
        (0.0,), (1.0,), (0.0,)
    ]
    writer.add(_snapshot(base + 3, 0), mr_due=True)
    assert writer.flush() == []


def test_failed_close_retarget_keeps_original_capture(rig, tmp_path):
    path, writer, _, base = rig
    writer.add(_snapshot(base + 1, 1), mr_due=True)
    device_events.clear_events(path)
    _fail_commit_once(writer)
    destination = tmp_path / "new-medium.db"
    writer.retarget(destination)
    assert writer.pending == 1
    writer.add(_snapshot(base + 2, 0), mr_due=True)
    assert [e["kind"] for e in writer.flush()] == ["carel_alarm_off"]
    assert _kinds(destination) == ["carel_alarm_off"]
    assert writer.open().execute("SELECT alarm FROM carel_samples ORDER BY ts").fetchall() == [
        (1.0,), (0.0,)
    ]


@pytest.mark.parametrize("missing", ["state.json", "guard.lock"])
def test_unknown_capture_stays_nonemitting_after_recovery(rig, missing):
    path, writer, domain, base = rig
    lost = domain.directory / missing
    original = lost.read_bytes()
    lost.unlink()
    writer.add(_snapshot(base + 1, 1), mr_due=True)
    assert writer.flush() == []
    assert writer.open().execute("SELECT count(*) FROM carel_samples").fetchone() == (2,)
    # Restoring a valid authority is not permission to recapture old snapshots.
    lost.write_bytes(original)
    lost.chmod(0o600)
    journal.recover(domain)
    journal.set_ready(domain, True)
    writer.add(_snapshot(base + 2, 0), mr_due=True)
    assert [e["kind"] for e in writer.flush()] == ["carel_alarm_off"]
    assert _kinds(path) == ["carel_alarm_off"]


def test_unknown_pending_capture_is_not_blessed_at_flush(rig):
    path, writer, domain, base = rig
    record = domain.directory / "state.json"
    original = record.read_bytes()
    record.write_bytes(b"corrupt")
    writer.add(_snapshot(base + 1, 1), mr_due=True)
    record.write_bytes(original)
    writer.add(_snapshot(base + 2, 0), mr_due=True)
    assert [e["kind"] for e in writer.flush()] == ["carel_alarm_off"]
    assert _kinds(path) == ["carel_alarm_off"]


def test_clear_changes_no_capture_eligibility_with_wall_clock_jumps(rig, monkeypatch):
    path, writer, _, base = rig
    writer.add(_snapshot(base + 1, 1), mr_due=True)
    monkeypatch.setattr(time, "time", lambda: base - 3600)
    device_events.clear_events(path)
    monkeypatch.setattr(time, "time", lambda: base + 86400)
    writer.add(_snapshot(base - 2, 0), mr_due=True)
    assert [e["kind"] for e in writer.flush()] == ["carel_alarm_off"]
    assert _kinds(path) == ["carel_alarm_off"]


def test_old_ce_events_do_not_suppress_current_events_and_samples_stay(rig):
    path, writer, _, base = rig

    def snapshot(ts, voltage):
        return {"ts": ts, "ce": [{"id": "ce02m3-COM2-14", "ok": True,
                                  "voltage": {"a": voltage}, "current": {"a": 1.0}}]}

    writer.add(snapshot(base, 230), mr_due=False)
    writer.flush()
    writer.add(snapshot(base + 1, 250), mr_due=False)
    device_events.clear_events(path)
    writer.add(snapshot(base + 2, 270), mr_due=False)
    created = writer.flush()
    assert len(created) == 1 and created[0]["ts"] == base + 2
    assert writer.open().execute("SELECT ts FROM ce_samples ORDER BY ts").fetchall() == [
        (base,), (base + 1,), (base + 2,)
    ]
    assert len(device_events.list_events(path=path)["events"]) == 1


def test_clear_locks_all_targets_before_deleting_any(rig, tmp_path):
    path, writer, domain, base = rig
    writer.add(_snapshot(base + 1, 1), mr_due=True)
    writer.flush()
    second = tmp_path / "zz-locked.db"
    second_writer = ArchiveWriter(second, flush_every_s=0)
    second_writer.add(_snapshot(base, 0), mr_due=True)
    second_writer.flush()
    holder = sqlite3.connect(second, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    before = journal.read_atomic(domain)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            journal.clear(domain, lambda: [path, second], timeout=0.1)
    finally:
        holder.close()
        second_writer.close()
    assert journal.read_atomic(domain) == before
    assert _kinds(path) == ["carel_alarm_on"]


def test_carel_sample_failure_after_detection_rolls_back_baseline(rig, monkeypatch):
    from sa02m_devices import history_writer

    path, writer, _, base = rig
    writer.add(_snapshot(base + 1, 1), mr_due=True)
    device_events.clear_events(path)
    writer.add(_snapshot(base + 2, 0), mr_due=True)
    original = history_writer.insert_carel_sample

    def fail(snapshot, **kwargs):
        if snapshot["ts"] == base + 2:
            raise sqlite3.OperationalError("injected Carel sample failure after detector")
        return original(snapshot, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(history_writer, "insert_carel_sample", fail)
        with pytest.raises(ArchiveFlushError):
            writer.flush()
    assert _kinds(path) == []
    assert writer.pending == 2
    assert [e["kind"] for e in writer.flush()] == ["carel_alarm_off"]
    assert _kinds(path) == ["carel_alarm_off"]


def test_invalid_config_does_not_redirect_authority_or_drop_samples(rig, monkeypatch):
    path, writer, domain, base = rig
    with monkeypatch.context() as patch:
        patch.setenv("STAND_DEVICES_EVENTS_STATE_DIR", "invalid-relative")
        writer.add(_snapshot(base + 1, 1), mr_due=True)
        assert writer.flush() == []
        with pytest.raises(journal.JournalStateError):
            device_events.list_events(path=path)
    assert journal.read_atomic(domain).generation == "legacy"
    writer.add(_snapshot(base + 2, 0), mr_due=True)
    assert [e["kind"] for e in writer.flush()] == ["carel_alarm_off"]
    assert writer.open().execute("SELECT alarm FROM carel_samples ORDER BY ts").fetchall() == [(0.0,), (1.0,), (0.0,)]
