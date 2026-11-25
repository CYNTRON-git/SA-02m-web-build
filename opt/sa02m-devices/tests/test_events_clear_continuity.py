"""A successful journal clear survives offline media and buffered snapshots."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sa02m_devices import device_events, device_history_migrate, history_store
from sa02m_devices import event_journal_state as journal
from sa02m_devices.device_events import clear_events, list_events, reset_carel_event_state
from sa02m_devices.history_writer import ArchiveWriter
from tests.journal_support import private_authority


def _snapshot(ts: float, alarm: int) -> dict:
    return {
        "ts": ts,
        "dtv": [{"ok": True, "id": "dtv-COM4-3", "room_temp": 21.0}],
        "carel": [{
            "ok": True, "id": "carel-COM3-1", "kind": "carel",
            "alarm": alarm, "alarm_count": alarm, "plant_state": "run",
            "unit_on": 1, "supply_temp": 26.5,
        }],
    }


def _samples(path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(str(path))
    try:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY ts").fetchall()
            for table in ("dtv_samples", "carel_samples")
        }
    finally:
        conn.close()


def _events() -> list[tuple[float, str]]:
    result = list_events()
    assert result["ok"] is True
    return [(row["ts"], row["kind"]) for row in result["events"]]


def _seed_alarm(path: Path, ts: float) -> None:
    reset_carel_event_state()
    writer = ArchiveWriter(path, flush_every_s=30.0)
    try:
        writer.add(_snapshot(ts, 0), mr_due=True)
        assert writer.flush() == []
        writer.add(_snapshot(ts + 1, 1), mr_due=True)
        assert [row["kind"] for row in writer.flush()] == ["carel_alarm_on"]
    finally:
        writer.close()


@pytest.fixture
def archives(tmp_path: Path, monkeypatch, private_authority):
    """Use the production resolver boundary without reaching device paths."""
    paths = {backend: tmp_path / backend / "devices_history.db"
             for backend in ("sd", "usb", "emmc")}
    active = SimpleNamespace(backend="usb", active_path=paths["usb"])
    monkeypatch.delenv("STAND_DEVICES_HISTORY_DB", raising=False)
    monkeypatch.setenv("STAND_DEVICES_EVENTS_STATE_DIR", str(tmp_path / "journal-state"))
    journal.bootstrap(journal.domain_for(), ready=True)
    monkeypatch.setattr(history_store, "resolve_storage_target", lambda **_: active)
    monkeypatch.setattr(device_events, "emmc_staging_path", lambda: paths["emmc"])
    monkeypatch.setattr(device_history_migrate, "emmc_staging_path", lambda: paths["emmc"])
    reset_carel_event_state()
    yield paths, active
    reset_carel_event_state()


@pytest.mark.parametrize("return_mode", ["select_sd", "promote_to_usb"])
def test_clear_excludes_old_events_when_offline_sd_returns(
    tmp_path: Path, archives, return_mode: str
):
    paths, active = archives
    base = time.time() - 100.0
    for offset, backend in enumerate(("sd", "usb", "emmc")):
        _seed_alarm(paths[backend], base + 10 * offset)
    before = {backend: _samples(path) for backend, path in paths.items()}
    assert _events() == [(base + 11, "carel_alarm_on")]

    # All connections are closed, so renaming the database models an absent
    # medium without leaving an open WAL or a reachable source at its path.
    detached = tmp_path / "detached-sd.db"
    paths["sd"].replace(detached)
    assert not paths["sd"].exists()
    assert clear_events() == 2  # the available USB and eMMC event rows
    assert _events() == []
    assert _samples(paths["usb"]) == before["usb"]
    assert _samples(paths["emmc"]) == before["emmc"]
    assert _samples(detached) == before["sd"]
    assert list_events(path=paths["emmc"])["events"] == []

    # A restart must not rely on the detector's pre-clear in-memory baseline.
    reset_carel_event_state()
    detached.replace(paths["sd"])
    if return_mode == "select_sd":
        active.backend, active.active_path = "sd", paths["sd"]
        expected_samples = before["sd"]
    else:
        report = device_history_migrate.promote_to_media(paths["sd"], paths["usb"])
        assert report["ok"] is True, report
        expected_samples = {table: before["sd"][table] + before["usb"][table]
                            for table in before["sd"]}
    after_return = _events()
    assert _samples(active.active_path) == expected_samples
    assert _samples(paths["emmc"]) == before["emmc"]

    later = time.time() + 1.0
    writer = ArchiveWriter(active.active_path, flush_every_s=30.0)
    try:
        writer.add(_snapshot(later, 1), mr_due=True)
        assert writer.flush() == []  # unchanged state after reopening
        writer.add(_snapshot(later + 1, 0), mr_due=True)
        assert [row["kind"] for row in writer.flush()] == ["carel_alarm_off"]
        writer.add(_snapshot(later + 2, 0), mr_due=True)
        assert writer.flush() == []
    finally:
        writer.close()

    assert [event for event in _events() if event[1] == "carel_alarm_off"] == [
        (later + 1, "carel_alarm_off")
    ]
    assert after_return == [], "offline SD restored an event after successful clear"
    assert _events() == [(later + 1, "carel_alarm_off")]


def test_clear_excludes_buffered_old_carel_edge_but_keeps_samples(archives):
    paths, _ = archives
    base = time.time() - 100.0
    writer = ArchiveWriter(paths["usb"], flush_every_s=30.0)
    try:
        writer.add(_snapshot(base, 0), mr_due=True)
        assert writer.flush() == []
        before = _samples(paths["usb"])
        writer.add(_snapshot(base + 1, 1), mr_due=True)
        assert writer.pending == 1
        assert _samples(paths["usb"]) == before

        assert clear_events() == 0  # the old edge is still only in memory
        assert _events() == []
        writer.flush()
        assert writer.pending == 0
        after_flush = _events()
        samples = _samples(paths["usb"])
        for table, rows in samples.items():
            assert rows[:1] == before[table]
            assert [row[0] for row in rows] == [base, base + 1]
        assert writer.open().execute(
            "SELECT alarm FROM carel_samples ORDER BY ts"
        ).fetchall() == [(0.0,), (1.0,)]

        later = time.time() + 1.0
        writer.add(_snapshot(later, 0), mr_due=True)
        assert [row["kind"] for row in writer.flush()] == ["carel_alarm_off"]
        writer.add(_snapshot(later + 1, 0), mr_due=True)
        assert writer.flush() == []
        for table, rows in _samples(paths["usb"]).items():
            assert rows[:2] == samples[table]
            assert [row[0] for row in rows] == [base, base + 1, later, later + 1]

        assert [event for event in _events() if event[1] == "carel_alarm_off"] == [
            (later, "carel_alarm_off")
        ]
        assert after_flush == [], "buffer replay restored a pre-clear Carel alarm edge"
        assert _events() == [(later, "carel_alarm_off")]
    finally:
        writer.close()
