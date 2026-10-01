"""device_events.clear_events — the «Очистить» button's storage half.

Pins: every event row goes (the count is returned), the sample tables stay
(including MTD chart rows and the CE power/energy rollup), a DB that does not
exist is not created, the eMMC staging file a later promote would merge back
is emptied too, a cleared journal is not re-seeded from carel_samples, and a
held write lock answers EventsBusy within the bound instead of hanging.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from sa02m_devices import device_events
from sa02m_devices.device_events import (
    EventsBusy,
    clear_events,
    detect_carel_events,
    list_events,
    reset_carel_event_state,
)
from sa02m_devices.device_history_db import insert_carel_sample, insert_mtd_sample, insert_sample
from sa02m_devices.history_ce_roll import ensure_ce_roll

_SAMPLE_TABLES = (
    "dtv_samples",
    "ce_samples",
    "mr_samples",
    "carel_samples",
    "mtd_samples",
)
_CHART_TABLES = _SAMPLE_TABLES + ("ce_roll",)


def _carel_snap(ts: float, *, alarm: int, plant: str, did: str = "carel-COM3-1") -> dict:
    return {
        "ts": ts,
        "carel": [{
            "ok": True,
            "id": did,
            "kind": "carel",
            "plant_state": plant,
            "unit_on": 1.0,
            "alarm": float(alarm),
            "alarm_count": float(alarm),
            "alarm_text": "",
            "supply_temp": 26.5,
        }],
    }


def _ce_snap(ts: float) -> dict:
    return {
        "ts": ts,
        "dtv": [],
        "ce": [{
            "ok": True,
            "id": "ce02m3-COM2-14",
            "kind": "ce",
            "voltage": {"a": 230.0, "b": 230.0, "c": 230.0},
            "current": {"a": 1.0, "b": 1.0, "c": 1.0},
            "power_w": {"a": 200.0, "b": 200.0, "c": 200.0, "total": 600.0},
            "frequency_hz": 50.0,
            "energy_kwh_import": 1.0,
        }],
    }


def _mtd_snap(ts: float) -> dict:
    return {
        "ts": ts,
        "mtd": [{
            "ok": True,
            "id": "mtdx62-COM3-20",
            "kind": "mtd",
            "illuminance_lux": 120.0,
            "target_distance_m": 1.5,
            "presence": 1.0,
        }],
    }


def _seed_ce_roll(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        ensure_ce_roll(conn)
        conn.execute(
            "INSERT INTO ce_roll("
            " device_id, grain, bucket_ts,"
            " pa_sum, pa_n, pb_sum, pb_n, pc_sum, pc_n, pt_sum, pt_n, e_delta"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ce02m3-COM2-14", 3600, 1_700_000_000.0,
                200.0, 1, 200.0, 1, 200.0, 1, 600.0, 1, 0.01,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed(db: Path, steps=((0, "run"), (1, "alarm"), (0, "run"))) -> float:
    """Archive samples AND produce real edge events through the detector:
    by default alarm 0 → 1 → 0 with plant changes = four rows. Returns the
    last ts."""
    reset_carel_event_state()
    base = time.time() - 100
    insert_sample(_ce_snap(base), path=db)
    insert_mtd_sample(_mtd_snap(base), path=db)
    _seed_ce_roll(db)
    for i, (alarm, plant) in enumerate(steps):
        snap = _carel_snap(base + i, alarm=alarm, plant=plant)
        insert_carel_sample(snap, path=db)
        detect_carel_events(snap, path=db)
    return base + len(steps) - 1


def _count(db: Path, table: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _no_real_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Never let a test reach the board's real /var/lib staging file.
    monkeypatch.setattr(device_events, "emmc_staging_path", lambda: tmp_path / "no-staging.db")
    reset_carel_event_state()
    yield
    reset_carel_event_state()


def test_clear_deletes_every_event_and_returns_the_count(tmp_path: Path):
    db = tmp_path / "hist.db"
    _seed(db)
    before = _count(db, "device_events")
    assert before == 4, before  # on, plant, off, plant
    assert clear_events(path=db) == before
    assert _count(db, "device_events") == 0
    assert list_events(path=db)["events"] == []
    # Idempotent: a second clear deletes nothing and does not fail.
    assert clear_events(path=db) == 0


def test_clear_leaves_chart_samples_intact(tmp_path: Path):
    """Events go. Metric history, MTD samples and the CE power rollup stay."""
    db = tmp_path / "hist.db"
    _seed(db)
    before = {t: _count(db, t) for t in _CHART_TABLES}
    assert before["ce_samples"] == 1 and before["carel_samples"] == 3, before
    assert before["mtd_samples"] == 1 and before["ce_roll"] >= 1, before
    assert _count(db, "device_events") == 4
    clear_events(path=db)
    assert {t: _count(db, t) for t in _CHART_TABLES} == before
    assert _count(db, "device_events") == 0


def test_clear_on_an_absent_db_creates_nothing(tmp_path: Path):
    db = tmp_path / "never.db"
    assert clear_events(path=db) == 0
    assert not db.exists()


def test_clear_on_a_db_without_the_events_table_is_zero(tmp_path: Path):
    db = tmp_path / "samples-only.db"
    insert_sample(_ce_snap(time.time()), path=db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP TABLE IF EXISTS device_events")
        conn.commit()
    finally:
        conn.close()
    assert clear_events(path=db) == 0
    assert _count(db, "ce_samples") == 1


def test_clear_also_empties_the_emmc_staging_a_promote_would_merge_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The events read path reads the ACTIVE file only, but a non-empty eMMC
    staging file is merged INTO the active one on the next backend change
    (device_history_migrate.promote_on_backend_change, device_events included):
    left alone, its rows would come back into a journal the Operator cleared."""
    active = tmp_path / "usb" / "devices_history.db"
    staging = tmp_path / "emmc" / "devices_history.db"
    _seed(active)
    _seed(staging)
    monkeypatch.setenv("STAND_DEVICES_HISTORY_DB", str(active))
    monkeypatch.setattr(device_events, "emmc_staging_path", lambda: staging)
    charts = {
        p: {t: _count(p, t) for t in _CHART_TABLES}
        for p in (active, staging)
    }
    assert charts[staging]["ce_roll"] >= 1 and charts[staging]["mtd_samples"] == 1
    assert clear_events() == 8
    assert _count(active, "device_events") == 0
    assert _count(staging, "device_events") == 0
    assert {p: {t: _count(p, t) for t in _CHART_TABLES} for p in (active, staging)} == charts


def test_clear_with_staging_equal_to_active_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = tmp_path / "devices_history.db"
    _seed(db)
    monkeypatch.setenv("STAND_DEVICES_HISTORY_DB", str(db))
    monkeypatch.setattr(device_events, "emmc_staging_path", lambda: db)
    assert clear_events() == 4


def test_a_cleared_journal_is_not_reseeded_from_carel_samples(tmp_path: Path):
    """The Carel detector seeds its baseline from carel_samples on first sight.
    After a clear, neither the running logger (baseline in memory) nor a
    restarted one (baseline re-read from the archive) may write a row for a
    state that has not changed — the cleared edges stay gone."""
    db = tmp_path / "hist.db"
    last = _seed(db, steps=((0, "run"), (1, "alarm")))
    assert _count(db, "device_events") == 2
    clear_events(path=db)
    assert detect_carel_events(_carel_snap(last + 1, alarm=1, plant="alarm"), path=db) == []
    reset_carel_event_state()
    assert detect_carel_events(_carel_snap(last + 2, alarm=1, plant="alarm"), path=db) == []
    assert _count(db, "device_events") == 0
    created = detect_carel_events(_carel_snap(last + 3, alarm=0, plant="run"), path=db)
    assert [e["kind"] for e in created] == ["carel_alarm_off", "carel_plant_state"]


def test_a_held_write_lock_answers_busy_within_the_bound(tmp_path: Path):
    db = tmp_path / "hist.db"
    _seed(db)
    holder = sqlite3.connect(str(db), isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        t0 = time.monotonic()
        with pytest.raises(EventsBusy):
            clear_events(path=db, busy_timeout_s=0.2)
        assert time.monotonic() - t0 < 5.0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _count(db, "device_events") == 4
    assert clear_events(path=db) == 4


def test_busy_bound_comes_from_the_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STAND_DEVICES_EVENTS_CLEAR_BUSY_S", "1.5")
    assert device_events.events_clear_busy_s() == 1.5
    monkeypatch.delenv("STAND_DEVICES_EVENTS_CLEAR_BUSY_S")
    assert device_events.events_clear_busy_s() == 5.0
