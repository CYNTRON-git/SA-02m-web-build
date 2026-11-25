"""Real logger/SQLite history survives repeated storage promotions and retries."""

from __future__ import annotations

import signal
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import sa02m_devices_logger as logger
from sa02m_devices import device_history_migrate
from sa02m_devices.device_events import reset_carel_event_state


def _q(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("failure", [None, "exception", "report"])
def test_main_promotes_current_archive_through_two_media(
    tmp_path: Path, monkeypatch, failure
):
    """Buffered eMMC -> SD -> USB ticks stay visible in the active archive.

    The second promotion can fail once, either by raising or by reporting
    failure. Its source must remain the SD archive until a successful retry.
    Only storage discovery, time, snapshots and the injected failure are
    replaced: the logger, writer, migration and SQLite operations are real.
    """
    paths = {backend: tmp_path / backend / "devices_history.db"
             for backend in ("emmc", "sd", "usb")}
    for path in paths.values():
        path.parent.mkdir()
    targets = {backend: SimpleNamespace(backend=backend, active_path=path)
               for backend, path in paths.items()}
    staging = tmp_path / "staging" / "devices_history.db"
    monkeypatch.setattr(device_history_migrate, "emmc_staging_path", lambda: staging)
    monkeypatch.setattr(logger, "emmc_staging_path", lambda: staging)
    monkeypatch.setenv("STAND_DEVICES_WIDGETS_PATH", str(tmp_path / "none.json"))
    reset_carel_event_state()

    base_ts = time.time() - 3600.0
    tick_no = 0
    sleeps = 0
    second_attempts = 0
    sampled_backends = []

    def resolve(*, force_refresh=False):  # noqa: ARG001
        backend = "emmc" if tick_no < 2 else "sd" if tick_no < 4 else "usb"
        return targets[backend]

    def snap():
        nonlocal tick_no
        sample = {
            "ok": True, "ts": base_ts + tick_no,
            "dtv": [{"ok": True, "id": "dtv-COM4-3", "room_temp": 21.0}],
            "ce": [], "mr": [], "carel": [], "mtd": [],
            "devices": [], "alerts": [],
        }
        tick_no += 1
        if tick_no == 6:
            logger._stop = True
        return sample

    def sleep_until(end, stopped):  # noqa: ARG001
        nonlocal sleeps
        sleeps += 1
        if sleeps > 12:
            logger._stop = True  # bounded failure if a broken retry never progresses

    real_tick = logger.logger_tick

    def tick(target, state, *, now_m=None):
        sampled_backends.append(target.backend)
        return real_tick(target, state, now_m=now_m)

    real_promote = logger.promote_on_backend_change

    def promote(prev_backend, prev_path, new_backend, new_path):
        nonlocal second_attempts
        if prev_backend == "sd" and new_backend == "usb":
            second_attempts += 1
            # Both the failed attempt and its retry must still own the complete
            # SD source. New USB samples may already exist on a retry.
            assert [round(r[0] - base_ts) for r in _q(
                paths["sd"], "SELECT ts FROM dtv_samples ORDER BY ts"
            )] == [0, 1, 2, 3]
            if second_attempts == 1:
                assert not paths["usb"].exists()
            if failure and second_attempts == 1:
                if failure == "exception":
                    raise OSError("injected removable storage failure")
                return {"ok": False, "steps": [{"ok": False, "error": "injected"}]}
        return real_promote(prev_backend, prev_path, new_backend, new_path)

    monkeypatch.setattr(logger, "resolve_storage_target", resolve)
    monkeypatch.setattr(logger, "live_snapshot", snap)
    monkeypatch.setattr(logger, "logger_tick", tick)
    monkeypatch.setattr(logger, "promote_on_backend_change", promote)
    monkeypatch.setattr(logger, "_sleep_until", sleep_until)
    monkeypatch.setattr(logger.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(logger, "FLUSH_EVERY_S", 3600.0)
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

    assert second_attempts == (2 if failure else 1)
    assert sampled_backends == ["emmc", "emmc", "sd", "sd", "usb", "usb"]
    rows = _q(paths["usb"], "SELECT ts FROM dtv_samples ORDER BY ts")
    assert [round(r[0] - base_ts) for r in rows] == list(range(6))
    assert _q(paths["usb"], "PRAGMA integrity_check") == [("ok",)]
    assert not paths["emmc"].exists()
    assert not paths["sd"].exists()


@pytest.mark.parametrize("failure", ["exception", "report"])
@pytest.mark.parametrize("route", ["steady_usb", "removed_sd", "return_to_sd"])
def test_failed_history_transfer_does_not_stop_new_samples(
    tmp_path: Path, monkeypatch, failure, route
):
    """Four failed-transfer ticks still persist new samples, then recover.

    Wall and monotonic time advance on every scheduled iteration, regardless
    of whether the broken logger sampled. A missing tick remains a visible
    gap in the real SQLite timestamps instead of pausing the test's clock.
    """
    paths = {backend: tmp_path / backend / "devices_history.db"
             for backend in ("emmc", "sd", "usb")}
    for path in paths.values():
        path.parent.mkdir()
    targets = {backend: SimpleNamespace(backend=backend, active_path=path)
               for backend, path in paths.items()}
    monkeypatch.setattr(device_history_migrate, "emmc_staging_path", lambda: paths["emmc"])
    monkeypatch.setattr(logger, "emmc_staging_path", lambda: paths["emmc"])
    monkeypatch.setenv("STAND_DEVICES_WIDGETS_PATH", str(tmp_path / "none.json"))
    reset_carel_event_state()
    base_ts = time.time() - 3600.0
    clock = 0
    during_failure = None
    attempts = []
    detached_sd = tmp_path / "detached-sd.db"
    real_promote = device_history_migrate.promote_to_media

    def rows(path):
        if not path.is_file():
            return []
        return [round(r[0] - base_ts) for r in _q(
            path, "SELECT ts FROM dtv_samples ORDER BY ts"
        )]

    def resolve(*, force_refresh=False):  # noqa: ARG001
        if route == "return_to_sd" and 6 <= clock < 8:
            return targets["sd"]
        return targets["emmc" if clock < 2 else "sd" if clock < 4 else "usb"]

    def snap():
        return {
            "ok": True, "ts": base_ts + clock,
            "dtv": [{"ok": True, "id": "dtv-COM4-3", "room_temp": 21.0}],
            "ce": [], "mr": [], "carel": [], "mtd": [],
            "devices": [], "alerts": [],
        }

    def promote(source, dest):
        if source == paths["sd"] and dest == paths["usb"]:
            attempts.append(clock)
            if clock < 8:
                if failure == "exception":
                    raise OSError("injected sustained transfer failure")
                return {"ok": False, "source": str(source), "dest": str(dest)}
        return real_promote(source, dest)

    def sleep_until(end, stopped):  # noqa: ARG001
        nonlocal clock, during_failure
        if route == "removed_sd" and clock == 4:
            paths["sd"].replace(detached_sd)
        if clock == 7:
            source = detached_sd if route == "removed_sd" else paths["sd"]
            during_failure = (rows(source), rows(paths["usb"]))
            if route == "removed_sd":
                detached_sd.replace(paths["sd"])
        clock += 1
        if clock == 10:
            logger._stop = True

    monkeypatch.setattr(device_history_migrate, "promote_to_media", promote)
    monkeypatch.setattr(logger, "resolve_storage_target", resolve)
    monkeypatch.setattr(logger, "live_snapshot", snap)
    monkeypatch.setattr(logger, "_sleep_until", sleep_until)
    monkeypatch.setattr(logger.time, "monotonic", lambda: 1000.0 + clock)
    monkeypatch.setattr(logger, "FLUSH_EVERY_S", 0.0)
    monkeypatch.setattr(logger, "RESOLVE_EVERY_S", 2.0)
    monkeypatch.setattr(logger, "INTERVAL_S", 1.0)
    monkeypatch.setattr(logger, "INTERVAL_EMMC_S", 1.0)
    monkeypatch.setattr(logger, "_stop", False)
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    try:
        assert logger.main() == 0
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        logger._stop = False

    expected_during = ([0, 1, 2, 3, 6, 7], [4, 5]) if route == "return_to_sd" else (
        [0, 1, 2, 3], [4, 5, 6, 7]
    )
    assert during_failure == expected_during
    expected_attempts = [4, 6, 8] if route == "steady_usb" else [4, 8]
    assert attempts == expected_attempts, "retry must follow storage-resolution cadence"
    assert rows(paths["usb"]) == list(range(10))
    assert _q(paths["usb"], "PRAGMA integrity_check") == [("ok",)]
    assert not paths["sd"].exists()


@pytest.mark.parametrize("failure", ["exception", "report"])
def test_failed_first_promotion_is_retained_across_a_second_target(
    tmp_path: Path, monkeypatch, failure
):
    """eMMC -> SD fails, then USB appears before that history can transfer."""
    paths = {backend: tmp_path / backend / "devices_history.db"
             for backend in ("emmc", "sd", "usb")}
    for path in paths.values():
        path.parent.mkdir()
    targets = {backend: SimpleNamespace(backend=backend, active_path=path)
               for backend, path in paths.items()}
    monkeypatch.setattr(device_history_migrate, "emmc_staging_path", lambda: paths["emmc"])
    monkeypatch.setattr(logger, "emmc_staging_path", lambda: paths["emmc"])
    monkeypatch.setenv("STAND_DEVICES_WIDGETS_PATH", str(tmp_path / "none.json"))
    reset_carel_event_state()
    base_ts = time.time() - 3600.0
    clock = 0
    during_failure = None
    attempts = []
    real_promote = device_history_migrate.promote_to_media

    def rows(path):
        if not path.is_file():
            return []
        return [round(r[0] - base_ts) for r in _q(
            path, "SELECT ts FROM dtv_samples ORDER BY ts"
        )]

    def resolve(*, force_refresh=False):  # noqa: ARG001
        return targets["emmc" if clock < 2 else "sd" if clock < 4 else "usb"]

    def snap():
        return {
            "ok": True, "ts": base_ts + clock,
            "dtv": [{"ok": True, "id": "dtv-COM4-3", "room_temp": 21.0}],
            "ce": [], "mr": [], "carel": [], "mtd": [],
            "devices": [], "alerts": [],
        }

    def promote(source, dest):
        attempts.append(clock)
        if source == paths["emmc"] and clock < 6:
            if failure == "exception":
                raise OSError("injected first-media history failure")
            return {"ok": False, "source": str(source), "dest": str(dest)}
        return real_promote(source, dest)

    def sleep_until(end, stopped):  # noqa: ARG001
        nonlocal clock, during_failure
        if clock == 5:
            during_failure = tuple(rows(paths[b]) for b in ("emmc", "sd", "usb"))
        clock += 1
        if clock == 8:
            logger._stop = True

    monkeypatch.setattr(device_history_migrate, "promote_to_media", promote)
    monkeypatch.setattr(logger, "resolve_storage_target", resolve)
    monkeypatch.setattr(logger, "live_snapshot", snap)
    monkeypatch.setattr(logger, "_sleep_until", sleep_until)
    monkeypatch.setattr(logger.time, "monotonic", lambda: 1000.0 + clock)
    monkeypatch.setattr(logger, "FLUSH_EVERY_S", 0.0)
    monkeypatch.setattr(logger, "RESOLVE_EVERY_S", 2.0)
    monkeypatch.setattr(logger, "INTERVAL_S", 1.0)
    monkeypatch.setattr(logger, "INTERVAL_EMMC_S", 1.0)
    monkeypatch.setattr(logger, "_stop", False)
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    try:
        assert logger.main() == 0
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        logger._stop = False

    expected_during = ([0, 1], [2, 3], [4, 5]) if failure == "exception" else (
        [0, 1], [], [2, 3, 4, 5]
    )
    assert during_failure == expected_during
    assert set(attempts) == {2, 4, 6}, "failed transfers are retried only on resolution"
    assert rows(paths["usb"]) == list(range(8))
    assert _q(paths["usb"], "PRAGMA integrity_check") == [("ok",)]
    assert not paths["emmc"].exists()
    assert not paths["sd"].exists()
