"""Real filesystem contracts for journal authority publication and scope."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest

from sa02m_devices import event_journal_state as journal


def _domain(tmp_path):
    return journal.Domain(tmp_path / "authority", True, tmp_path / "history.db")


def test_bootstrap_is_private_complete_and_idempotent(tmp_path):
    domain = _domain(tmp_path)
    state = journal.bootstrap(domain, ready=True)
    assert state.generation == "legacy" and state.ready is True
    assert journal.bootstrap(domain) == state
    assert journal.read_atomic(domain) == state
    assert stat.S_IMODE(domain.directory.stat().st_mode) == 0o700
    for name in ("guard.lock", "state.json"):
        assert stat.S_IMODE((domain.directory / name).stat().st_mode) == 0o600
    assert not domain.archive.exists()


def test_concurrent_first_operations_converge(tmp_path):
    domain = _domain(tmp_path)
    with ThreadPoolExecutor(max_workers=6) as pool:
        states = list(pool.map(lambda _: journal.bootstrap(domain, ready=True), range(12)))
    assert all(state == states[0] for state in states)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["authority"]


@pytest.mark.parametrize("record", [
    b"{}", b"[]", b"\xff", b" " * 1025,
    b'{"version":1,"version":1}',
    b'{"version":2,"identity":"' + b"a" * 32 + b'","generation":"legacy","ready":true}',
])
def test_invalid_state_is_never_a_bootstrap(tmp_path, record):
    domain = _domain(tmp_path)
    journal.bootstrap(domain, ready=True)
    (domain.directory / "state.json").write_bytes(record)
    with pytest.raises(journal.JournalStateError):
        with journal.guard(domain):
            pass
    assert (domain.directory / "state.json").read_bytes() == record


@pytest.mark.parametrize("field,value", [
    ("version", True), ("generation", "A" * 32), ("generation", None),
    ("ready", 1), ("identity", "b" * 32), ("extra", 1),
])
def test_strict_types_identity_and_token_grammar(tmp_path, field, value):
    domain = _domain(tmp_path)
    journal.bootstrap(domain)
    path = domain.directory / "state.json"
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(journal.JournalStateError):
        journal.read_atomic(domain)


@pytest.mark.parametrize("target", ["state.json", "guard.lock", "directory"])
def test_symlinks_and_public_modes_are_refused(tmp_path, target):
    domain = _domain(tmp_path)
    journal.bootstrap(domain)
    path = domain.directory if target == "directory" else domain.directory / target
    path.chmod(0o777 if target == "directory" else 0o666)
    with pytest.raises(journal.JournalStateError):
        journal.read_atomic(domain)
    path.chmod(0o700 if target == "directory" else 0o600)
    moved = path.with_name(path.name + ".original")
    path.rename(moved)
    path.symlink_to(moved)
    with pytest.raises(journal.JournalStateError):
        journal.read_atomic(domain)


@pytest.mark.parametrize("missing", ["state.json", "guard.lock"])
def test_partial_authority_is_not_recreated(tmp_path, missing):
    domain = _domain(tmp_path)
    journal.bootstrap(domain)
    (domain.directory / missing).unlink()
    with pytest.raises(journal.JournalStateError):
        journal.bootstrap(domain)
    assert not (domain.directory / missing).exists()


def test_removed_whole_authority_with_tagged_rows_is_refused(tmp_path):
    domain = _domain(tmp_path)
    conn = sqlite3.connect(domain.archive)
    conn.execute("CREATE TABLE device_events(journal_generation TEXT)")
    conn.execute("INSERT INTO device_events VALUES (?)", ("a" * 32,))
    conn.commit()
    conn.close()
    with pytest.raises(journal.JournalStateError, match="lost its journal authority"):
        with journal.guard(domain):
            pass
    assert not domain.directory.exists()


def test_capture_and_read_are_read_only_after_initialization(tmp_path, monkeypatch):
    monkeypatch.delenv("STAND_DEVICES_EVENTS_STATE_DIR", raising=False)
    path = tmp_path / "samples.db"
    capture = journal.capture(path)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
              for p in capture.domain.directory.iterdir()}
    for _ in range(5):
        assert journal.capture(path) == capture
        journal.read_atomic(capture.domain)
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
            for p in capture.domain.directory.iterdir()} == before


def test_scopes_explicit_forced_and_shared(tmp_path, monkeypatch):
    monkeypatch.delenv("STAND_DEVICES_EVENTS_STATE_DIR", raising=False)
    first = journal.domain_for(tmp_path / "first.db")
    second = journal.domain_for(tmp_path / "second.db")
    assert first != second
    monkeypatch.setenv("STAND_DEVICES_HISTORY_DB", str(first.archive))
    assert journal.domain_for() == first
    monkeypatch.setenv("STAND_DEVICES_EVENTS_STATE_DIR", str(tmp_path / "shared"))
    assert journal.domain_for(first.archive) == journal.domain_for(second.archive)
    assert not journal.domain_for().standalone
    monkeypatch.setenv("STAND_DEVICES_EVENTS_STATE_DIR", "relative")
    with pytest.raises(journal.JournalStateError):
        journal.domain_for(first.archive)


def test_absent_clear_creates_nothing_but_pending_capture_is_cleared(tmp_path, monkeypatch):
    monkeypatch.delenv("STAND_DEVICES_EVENTS_STATE_DIR", raising=False)
    path = tmp_path / "never.db"
    domain = journal.domain_for(path)
    assert journal.clear(domain, lambda: [path], timeout=0.2) == 0
    assert list(tmp_path.iterdir()) == []
    captured = journal.capture(path)
    assert journal.clear(domain, lambda: [path], timeout=0.2) == 0
    assert journal.read_atomic(domain).generation != captured.generation
    assert not path.exists()


def test_publish_fsyncs_file_before_replace_and_directory_after(tmp_path, monkeypatch):
    domain = _domain(tmp_path)
    journal.bootstrap(domain, ready=True)
    guard_inode = (domain.directory / "guard.lock").stat().st_ino
    calls = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        calls.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real_fsync(fd)

    def replace(*args, **kwargs):
        calls.append("replace")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    journal.clear(domain, lambda: [], timeout=0.2)
    assert calls == ["file", "replace", "directory"]
    assert (domain.directory / "guard.lock").stat().st_ino == guard_inode


@pytest.mark.parametrize("failure", ["file", "replace", "directory"])
def test_publication_failure_never_reports_success_or_restores_old_state(tmp_path, monkeypatch, failure):
    domain = _domain(tmp_path)
    before = journal.bootstrap(domain, ready=True)
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        if kind == failure:
            raise OSError("injected publication failure")
        return real_fsync(fd)

    def replace(*args, **kwargs):
        if failure == "replace":
            raise OSError("injected replace failure")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises((OSError, journal.JournalStateError)):
        journal.clear(domain, lambda: [], timeout=0.2)
    after = journal.read_atomic(domain)
    assert (after.generation != before.generation) is (failure == "directory")
    assert sorted(p.name for p in domain.directory.iterdir()) == ["guard.lock", "state.json"]


def test_readiness_and_recovery_keep_guard_inode_and_never_restore_legacy(tmp_path):
    domain = _domain(tmp_path)
    original = journal.bootstrap(domain)
    inode = (domain.directory / "guard.lock").stat().st_ino
    with pytest.raises(journal.JournalStateError, match="not ready"):
        journal.clear(domain, lambda: [], timeout=0.2)
    assert journal.set_ready(domain, True).generation == original.generation
    journal.clear(domain, lambda: [], timeout=0.2)
    cleared = journal.read_atomic(domain)
    recovered = journal.recover(domain)
    assert recovered.identity != cleared.identity
    assert recovered.generation not in ("legacy", cleared.generation)
    assert recovered.ready is False
    assert (domain.directory / "guard.lock").stat().st_ino == inode


def test_legacy_accepts_foreign_tags_but_cleared_authority_does_not():
    initial = journal.State("a" * 32, "legacy", True)
    cleared = journal.State("a" * 32, "b" * 32, True)
    assert journal.accepts(initial, "legacy")
    assert journal.accepts(initial, "c" * 32)
    assert journal.accepts(cleared, "b" * 32)
    assert not journal.accepts(cleared, "legacy")
    assert not journal.accepts(cleared, "c" * 32)
    with pytest.raises(journal.JournalStateError):
        journal.accepts(initial, "corrupt")


def test_schema_is_additive_and_does_not_commit_callers_transaction(tmp_path):
    from sa02m_devices.device_events import ensure_events_schema_on

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE device_events(id INTEGER PRIMARY KEY, ts REAL, "
                 "device_id TEXT, kind TEXT, phase TEXT)")
    conn.execute("INSERT INTO device_events VALUES(1, 123, 'd', 'k', '')")
    conn.commit()
    conn.execute("INSERT INTO device_events VALUES(2, 124, 'd', 'k', '')")
    ensure_events_schema_on(conn)
    assert conn.in_transaction
    assert conn.execute("SELECT journal_generation FROM device_events").fetchall() == [
        ("legacy",), ("legacy",)
    ]
    conn.rollback()
    assert conn.execute("SELECT id FROM device_events").fetchall() == [(1,)]
    ensure_events_schema_on(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE device_events SET journal_generation='INVALID'")
    conn.close()


@pytest.mark.parametrize("partial", [False, True])
def test_recorded_first_identity_can_finish_but_cannot_reset_cleared_state(tmp_path, partial):
    domain = _domain(tmp_path)
    if partial:
        domain.directory.mkdir(mode=0o700)
        marker = domain.directory / "guard.lock"
        marker.touch(mode=0o600)
    initial = journal.initialize_recorded(domain, "b" * 32)
    assert initial == journal.State("b" * 32, "legacy", False)
    assert journal.initialize_recorded(domain, "b" * 32) == initial
    with pytest.raises(journal.JournalStateError):
        journal.initialize_recorded(domain, "c" * 32)
    journal.set_ready(domain, True)
    journal.clear(domain, lambda: [], timeout=0.2)
    before = journal.read_atomic(domain)
    with pytest.raises(journal.JournalStateError):
        journal.initialize_recorded(domain, "b" * 32)
    assert journal.read_atomic(domain) == before


def test_explicit_recovery_repairs_a_missing_guard_without_accepting_old_rows(tmp_path):
    domain = _domain(tmp_path)
    before = journal.bootstrap(domain, ready=True)
    (domain.directory / "guard.lock").unlink()
    after = journal.recover(domain)
    assert journal.read_atomic(domain) == after
    assert after.identity != before.identity and after.generation != "legacy"
    assert after.ready is False


@pytest.mark.parametrize("name", ["guard.lock", "state.json"])
def test_nonregular_authority_is_refused_without_blocking(tmp_path, name):
    domain = _domain(tmp_path)
    journal.bootstrap(domain, ready=True)
    target = domain.directory / name
    target.unlink()
    os.mkfifo(target, 0o600)
    result = subprocess.run([sys.executable, "-B", "-c", '''
import sys
from pathlib import Path
from sa02m_devices import event_journal_state as journal
try:
    journal.read_atomic(journal.Domain(Path(sys.argv[1]), True))
except journal.JournalStateError:
    pass
else:
    raise RuntimeError("FIFO accepted as journal state")
''', str(domain.directory)], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
