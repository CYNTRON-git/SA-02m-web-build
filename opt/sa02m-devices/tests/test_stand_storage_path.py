"""Тесты выбора носителя и миграции/ротации devices_history."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from sa02m_devices import device_history_db
from sa02m_devices import device_history_migrate as migrate
from sa02m_devices import stand_storage_path as ssp


@pytest.fixture
def storage_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    usb = tmp_path / "media" / "usb"
    sd = tmp_path / "media" / "sdcard"
    emmc = tmp_path / "var" / "lib" / "sa02m-stand"
    usb.mkdir(parents=True)
    sd.mkdir(parents=True)
    emmc.mkdir(parents=True)
    (usb / ".sa02m_mount_ok").write_text("1", encoding="utf-8")
    (sd / ".sa02m_mount_ok").write_text("1", encoding="utf-8")
    monkeypatch.setenv("STAND_DEVICES_USB_MOUNT", str(usb))
    monkeypatch.setenv("STAND_DEVICES_SD_MOUNT", str(sd))
    monkeypatch.setenv("STAND_DEVICES_EMMC_DIR", str(emmc))
    monkeypatch.setenv("STAND_DEVICES_HISTORY_POINTER", str(emmc / "devices_history.active"))
    monkeypatch.setenv("STAND_DEVICES_HISTORY_MIN_FREE", "1")
    monkeypatch.delenv("STAND_DEVICES_HISTORY_DB", raising=False)
    # reload module-level paths
    monkeypatch.setattr(ssp, "DEFAULT_USB", usb)
    monkeypatch.setattr(ssp, "DEFAULT_SD", sd)
    monkeypatch.setattr(ssp, "DEFAULT_EMMC", emmc)
    monkeypatch.setattr(ssp, "POINTER_PATH", emmc / "devices_history.active")
    monkeypatch.setattr(ssp, "MIN_FREE_BYTES", 1)
    ssp.invalidate_resolve_cache()
    return {"usb": usb, "sd": sd, "emmc": emmc}


def _snap(ts: float, temp: float = 21.0) -> dict:
    return {
        "ts": ts,
        "dtv": [{
            "ok": True,
            "id": "dtv-COM4-3",
            "room_temp": temp,
            "humidity": 40.0,
            "eco2_ppm": 400.0,
            "tvoc_mg_m3": 0.1,
            "pressure_mmhg": 750.0,
            "light_pct": 5.0,
            "presence": 0.0,
        }],
        "ce": [],
    }


def test_resolve_prefers_usb(storage_roots):
    t = ssp.resolve_storage_target(force_refresh=True)
    assert t.backend == "usb"
    assert t.active_path == storage_roots["usb"] / "sa02m-stand" / "devices_history.db"


def test_resolve_falls_back_sd_then_emmc(storage_roots, monkeypatch):
    # USB not a mount
    (storage_roots["usb"] / ".sa02m_mount_ok").unlink()
    ssp.invalidate_resolve_cache()
    t = ssp.resolve_storage_target(force_refresh=True)
    assert t.backend == "sd"
    (storage_roots["sd"] / ".sa02m_mount_ok").unlink()
    ssp.invalidate_resolve_cache()
    t2 = ssp.resolve_storage_target(force_refresh=True)
    assert t2.backend == "emmc"
    assert t2.active_path == storage_roots["emmc"] / "devices_history.db"


def test_promote_copy_and_merge(storage_roots):
    emmc_db = storage_roots["emmc"] / "devices_history.db"
    usb_db = storage_roots["usb"] / "sa02m-stand" / "devices_history.db"
    now = time.time()
    device_history_db.insert_sample(_snap(now - 10, temp=10.0), path=emmc_db)
    # copy when no media db
    r = migrate.promote_to_media(emmc_db, usb_db)
    assert r["ok"] and r["action"] == "copy"
    assert usb_db.is_file()
    assert not emmc_db.is_file()
    h = device_history_db.history("room_temp", "1h", path=usb_db, device_id="dtv-COM4-3")
    assert h["ok"] and h["series"]

    # staging again + merge into existing
    device_history_db.insert_sample(_snap(now - 5, temp=11.0), path=emmc_db)
    device_history_db.insert_sample(_snap(now - 1, temp=12.0), path=usb_db)
    r2 = migrate.promote_to_media(emmc_db, usb_db)
    assert r2["ok"] and r2["action"] == "merge"
    assert not emmc_db.is_file()
    h2 = device_history_db.history("room_temp", "1h", path=usb_db, device_id="dtv-COM4-3")
    temps = [p[1] for p in h2["series"][0]["points"]]
    assert 10.0 in temps and 11.0 in temps and 12.0 in temps


def test_rotate_keeps_archive(storage_roots, monkeypatch):
    db = storage_roots["emmc"] / "devices_history.db"
    device_history_db.insert_sample(_snap(time.time()), path=db)
    monkeypatch.setattr(device_history_db, "ROTATE_BYTES", 1)
    monkeypatch.setattr(device_history_db, "ROTATE_HEADROOM", 1)
    r = device_history_db.rotate_if_needed(path=db)
    assert r["rotated"] is True
    archives = list((storage_roots["emmc"]).glob("devices_history_*.db"))
    assert len(archives) == 1
    assert db.is_file()  # new active


def test_history_merges_archive_and_active(storage_roots, monkeypatch):
    d = storage_roots["emmc"]
    arch = d / "devices_history_20200101_000000.db"
    active = d / "devices_history.db"
    now = time.time()
    device_history_db.insert_sample(_snap(now - 100, temp=1.0), path=arch)
    device_history_db.insert_sample(_snap(now - 1, temp=2.0), path=active)
    monkeypatch.setenv("STAND_DEVICES_HISTORY_DB", str(active))
    # With force path, history only reads that file — use explicit multi via dir helper
    paths = ssp.list_history_dbs(d)
    assert arch in paths and active in paths
    # Query both manually through history with path=None after pointing resolve to emmc only
    monkeypatch.delenv("STAND_DEVICES_HISTORY_DB", raising=False)
    (storage_roots["usb"] / ".sa02m_mount_ok").unlink(missing_ok=True)
    (storage_roots["sd"] / ".sa02m_mount_ok").unlink(missing_ok=True)
    ssp.invalidate_resolve_cache()
    h = device_history_db.history("room_temp", "1h", device_id="dtv-COM4-3")
    temps = [p[1] for p in h["series"][0]["points"]]
    assert 1.0 in temps and 2.0 in temps
