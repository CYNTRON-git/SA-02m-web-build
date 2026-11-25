"""A failed startup staging transfer does not pause recording on USB."""

import signal
import sqlite3
import time
from types import SimpleNamespace

import sa02m_devices_logger as logger
from sa02m_devices import device_history_migrate as migrate, event_journal_state as journal
from sa02m_devices.history_writer import ArchiveWriter
from tests.test_events_clear_continuity import _snapshot
from tests.journal_support import private_authority


def test_startup_staging_retries_while_new_samples_continue(tmp_path, monkeypatch, private_authority):
    monkeypatch.setenv("STAND_DEVICES_EVENTS_STATE_DIR", str(tmp_path / "authority"))
    journal.bootstrap(journal.domain_for(), ready=True)
    staging, active = tmp_path / "emmc.db", tmp_path / "usb.db"
    base = time.time() - 100
    writer = ArchiveWriter(staging, flush_every_s=0)
    writer.add(_snapshot(base, 0), mr_due=True)
    writer.close()
    monkeypatch.setattr(logger, "emmc_staging_path", lambda: staging)
    monkeypatch.setattr(migrate, "emmc_staging_path", lambda: staging)
    monkeypatch.setenv("STAND_DEVICES_WIDGETS_PATH", str(tmp_path / "none.json"))
    clock, attempts = 0, []
    target = SimpleNamespace(backend="usb", active_path=active)
    real_promote = migrate.promote_to_media

    def promote(source, dest):
        attempts.append(clock)
        if clock < 4:
            return {"ok": False, "error": "injected startup failure"}
        return real_promote(source, dest)

    def sleep_until(*_):
        nonlocal clock
        clock += 1
        if clock == 6:
            logger._stop = True

    monkeypatch.setattr(migrate, "promote_to_media", promote)
    monkeypatch.setattr(logger, "resolve_storage_target", lambda **_: target)
    monkeypatch.setattr(logger, "live_snapshot", lambda: _snapshot(base + clock + 1, 0))
    monkeypatch.setattr(logger, "_sleep_until", sleep_until)
    monkeypatch.setattr(logger.time, "monotonic", lambda: 1000.0 + clock)
    monkeypatch.setattr(logger, "FLUSH_EVERY_S", 0.0)
    monkeypatch.setattr(logger, "RESOLVE_EVERY_S", 2.0)
    monkeypatch.setattr(logger, "_stop", False)
    old_term, old_int = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        assert logger.main() == 0
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        logger._stop = False
    assert attempts == [0, 2, 4]
    assert not staging.exists()
    conn = sqlite3.connect(active)
    try:
        assert [round(row[0] - base) for row in conn.execute(
            "SELECT ts FROM dtv_samples ORDER BY ts"
        )] == list(range(7))
    finally:
        conn.close()
