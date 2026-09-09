"""SQLite opens per logger tick are bounded — the edge detectors open ONCE.

Ship review 1.0.6.39 (advisory, backlog 2026-09-08): `detect_carel_events`
opened the archive twice on EVERY 1 Hz tick of a board with a Carel unit —
`history_db.ensure_schema()` (a full connect + DDL + PK-migration probe, then
close) followed by its own `_connect()`. `detect_ce_events` had the same
shape. The fix checks `sqlite_master` on the connection it already holds and
calls `ensure_schema()` only when the seed table is really absent (a fresh
file, a rotation) — zero extra opens on the steady state, still correct on a
new file.

Method: a counting double around `sqlite3.connect` (both modules resolve it
through the `sqlite3` module attribute at call time). Before the fix the
steady-state Carel call cost 2 opens and a steady logger tick 5; after, 1 and
3. The tick bound is the measured post-fix number so a re-introduced open is
caught, and the fresh-file case proves the seed table is still created.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import sa02m_devices_logger as logger
from sa02m_devices.device_events import detect_carel_events, detect_ce_events, reset_carel_event_state


def _carel_snap(ts: float, alarm: int, plant: str) -> dict:
    return {
        "ts": ts,
        "dtv": [], "ce": [], "mr": [],
        "carel": [{"id": "carel-COM3-1", "kind": "carel", "alarm": alarm,
                   "plant_state": plant, "unit_on": 1, "alarm_count": alarm,
                   "supply_temp": 26.5, "setpoint": 27.0}],
        "devices": [], "alerts": [],
    }


def _ce_snap(ts: float) -> dict:
    return {
        "ts": ts, "dtv": [], "mr": [], "carel": [],
        "ce": [{"ok": True, "id": "ce02m3-COM2-14", "kind": "ce",
                "voltage": {"a": 230.0, "b": 230.0, "c": 230.0},
                "current": {"a": 1.0, "b": 1.0, "c": 1.0},
                "power_w": {"a": 200.0, "b": 200.0, "c": 200.0, "total": 600.0},
                "frequency_hz": 50.0, "energy_kwh_import": 1.0}],
        "devices": [], "alerts": [],
    }


def _count_connects(monkeypatch, fn) -> int:
    real = sqlite3.connect
    calls: list[tuple] = []

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", counting)
    try:
        fn()
    finally:
        monkeypatch.setattr(sqlite3, "connect", real)
    return len(calls)


def test_detect_carel_events_opens_sqlite_once_on_the_steady_state(tmp_path: Path, monkeypatch):
    reset_carel_event_state()
    db = tmp_path / "hist.db"
    base = time.time() - 30
    # First call on a fresh file: the seed table must be created (whatever it costs).
    detect_carel_events(_carel_snap(base, 0, "run"), path=db)
    n = _count_connects(monkeypatch, lambda: detect_carel_events(_carel_snap(base + 1, 0, "run"), path=db))
    assert n == 1, f"detect_carel_events opened SQLite {n}x on a steady tick (want 1)"


def test_detect_ce_events_opens_sqlite_once_on_the_steady_state(tmp_path: Path, monkeypatch):
    db = tmp_path / "hist.db"
    base = time.time() - 30
    detect_ce_events(_ce_snap(base), path=db)
    n = _count_connects(monkeypatch, lambda: detect_ce_events(_ce_snap(base + 1), path=db))
    assert n == 1, f"detect_ce_events opened SQLite {n}x on a steady tick (want 1)"


def test_carel_seed_table_is_created_on_a_fresh_file(tmp_path: Path):
    # The fast path must not skip schema creation where it is really needed:
    # a brand-new file (first boot, a rotation) has no carel_samples yet.
    reset_carel_event_state()
    db = tmp_path / "fresh.db"
    assert detect_carel_events(_carel_snap(time.time() - 5, 0, "run"), path=db) == []
    conn = sqlite3.connect(str(db))
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"carel_samples", "device_events"} <= names, names


def test_steady_logger_tick_opens_sqlite_at_most_three_times(tmp_path: Path, monkeypatch):
    """One open per writer: insert_sample, the CE detector, the Carel detector.

    Measured on the pre-fix tree: 5 (each detector paid ensure_schema + its
    own connect). The second tick is inside MR_INTERVAL_S and after the first
    tick's purge, so neither the MR/Carel sample writes nor purge_old run."""
    db = tmp_path / "devices_history.db"
    now = time.time()
    snaps = iter([
        _tick_snap(now - 2, alarm=0, plant="run"),
        _tick_snap(now - 1, alarm=0, plant="run"),
    ])
    monkeypatch.setattr(logger, "live_snapshot", lambda: next(snaps))
    monkeypatch.setenv("STAND_DEVICES_WIDGETS_PATH", str(tmp_path / "none.json"))
    reset_carel_event_state()
    target = SimpleNamespace(backend="usb", active_path=db)
    state = logger.LoggerState()
    logger.logger_tick(target, state, now_m=100.0)
    n = _count_connects(monkeypatch, lambda: logger.logger_tick(target, state, now_m=101.0))
    assert n <= 3, f"a steady logger tick opened SQLite {n}x (want <= 3)"


def _tick_snap(ts: float, *, alarm: int, plant: str) -> dict:
    snap = _carel_snap(ts, alarm, plant)
    snap["ce"] = _ce_snap(ts)["ce"]
    snap["ok"] = True
    return snap
