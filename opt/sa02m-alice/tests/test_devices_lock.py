#!/usr/bin/env python3
"""The device document has ONE advisory lock and every writer holds it.

Three processes read-modify-write `sa02m-alice-devices.conf`: the CGI
(`config.api.dispatch` as www-data), both client units (`sio_handlers` →
`config_api.rename_device` / `apply_rooms` / `apply_groups`), and the
Yandex unit's auto-provisioner. `_atomic_write` makes each replace atomic
but nothing made load→save a critical section, so two writers could
interleave and one edit silently vanish (weekly audit 2026-09-08, B3).

Part A runs on every host: each writer's `save_devices` must happen INSIDE
`config_store.devices_lock()`. Part B needs `fcntl` (Linux): a second
thread's writer really blocks while the lock is held, and the same thread
may nest the lock without deadlocking. Skips are loud, never silent.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_alice.client import auto_provision as ap  # noqa: E402
from sa02m_alice.common import config_store  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import api  # noqa: E402


def _doc() -> dict:
    return {
        "rooms": [{"id": "r1", "name": "Lab", "devices": ["d1"]}],
        "groups": [{"id": "g1", "name": "Hall", "device_ids": ["d1"]}],
        "devices": [{
            "id": "d1", "name": "Pump", "room_id": "r1",
            "type": "devices.types.switch",
            "capabilities": [{
                "type": "devices.capabilities.on_off",
                "mqtt": "/devices/SA-02m/controls/do",
                "retrievable": True, "reportable": True,
                "parameters": {"instance": "on"},
            }],
            "properties": [],
        }],
    }


class _Probe:
    """Records whether each save happened while the lock was held."""

    def __init__(self) -> None:
        self.depth = 0
        self.enters = 0
        self.saves_inside = 0
        self.saves_outside = 0

    @contextlib.contextmanager
    def lock(self, path=None):
        self.enters += 1
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def save(self, doc, path=None):
        if self.depth > 0:
            self.saves_inside += 1
        else:
            self.saves_outside += 1


class TestEveryWriterHoldsTheLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sa02m-alice-devices.conf")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(_doc(), fh)
        self.probe = _Probe()
        patches = [
            mock.patch.object(C, "DEVICES_CONF", self.path),
            mock.patch.object(api, "devices_lock", self.probe.lock),
            mock.patch.object(api, "save_devices", self.probe.save),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _assert_locked_write(self, result):
        self.assertTrue(result.get("ok"), result)
        self.assertGreaterEqual(self.probe.enters, 1)
        self.assertEqual(self.probe.saves_outside, 0,
                         "a save ran outside devices_lock()")
        self.assertGreaterEqual(self.probe.saves_inside, 1)

    def test_apply_groups_upsert(self):
        self._assert_locked_write(api.apply_groups({"id": "g1", "name": "Hall2"}))

    def test_apply_groups_delete(self):
        self._assert_locked_write(api.apply_groups({"id": "g1", "delete": True}))

    def test_apply_rooms_upsert(self):
        self._assert_locked_write(api.apply_rooms({"id": "r1", "name": "Lab2",
                                                   "devices": ["d1"]}))

    def test_apply_rooms_delete(self):
        self._assert_locked_write(api.apply_rooms({"id": "r1", "delete": True}))

    def test_upsert_room(self):
        self._assert_locked_write(api.upsert_room({"id": "r1", "name": "Lab3"}))

    def test_delete_room(self):
        self._assert_locked_write(api.delete_room("r1"))

    def test_upsert_device(self):
        dev = _doc()["devices"][0]
        dev["name"] = "Pump2"
        self._assert_locked_write(api.upsert_device(dev))

    def test_rename_device(self):
        self._assert_locked_write(api.rename_device("d1", "Pump3"))

    def test_delete_device(self):
        self._assert_locked_write(api.delete_device("d1"))

    def test_reset_mappings(self):
        self._assert_locked_write(api.reset_mappings())

    def test_a_refused_body_takes_no_lock_and_saves_nothing(self):
        # Validation runs before the critical section — a bad body costs no
        # lock round-trip and persists nothing.
        result = api.apply_rooms({"id": "r1", "name": ""})
        self.assertFalse(result["ok"])
        self.assertEqual(self.probe.saves_inside + self.probe.saves_outside, 0)


class TestProvisionerHoldsTheLock(unittest.TestCase):
    def test_commit_provision_and_tick_write_inside_the_lock(self):
        probe = _Probe()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sa02m-alice-devices.conf")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"rooms": [], "groups": [], "devices": []}, fh)
            topics = ["/devices/dtv-COM9-99/controls/temp_bme680"]
            with mock.patch.object(ap, "devices_lock", probe.lock), \
                    mock.patch.object(ap, "save_devices", probe.save):
                added = ap.commit_provision("dtv-COM9-99", topics, path=path,
                                            yaml_ids=set())
            self.assertEqual(len(added), 1)
            self.assertEqual(probe.saves_outside, 0)
            self.assertEqual(probe.saves_inside, 1)

        probe = _Probe()
        store = {"rooms": [], "groups": [], "devices": []}
        clock = {"t": 0.0}
        with mock.patch.object(ap, "devices_lock", probe.lock):
            prov = ap.AutoProvisioner(
                load=lambda: store, save=probe.save,
                clock=lambda: clock["t"], settle_s=0.0, yaml_ids=lambda: set(),
            )
            prov.note("/devices/dtv-COM9-98/meta/name", "DTV")
            prov.note("/devices/dtv-COM9-98/controls/temp_bme680", "21.0")
            clock["t"] = 1.0
            self.assertTrue(prov.tick())
        self.assertEqual(probe.saves_outside, 0)
        self.assertEqual(probe.saves_inside, 1)


@unittest.skipUnless(config_store.fcntl is not None,
                     "fcntl unavailable on this host (Windows dev box) — the "
                     "real flock contention is unverified HERE; the daemon and "
                     "CGI run on Linux where CI/board cover it")
class TestRealFlock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sa02m-alice-devices.conf")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(_doc(), fh)
        p = mock.patch.object(C, "DEVICES_CONF", self.path)
        p.start()
        self.addCleanup(p.stop)

    def test_second_thread_blocks_until_release(self):
        held = threading.Event()
        release = threading.Event()
        done = threading.Event()

        def holder():
            with config_store.devices_lock():
                held.set()
                release.wait(5.0)

        def writer():
            api.upsert_room({"id": "r1", "name": "Lab-late"})
            done.set()

        th = threading.Thread(target=holder)
        tw = threading.Thread(target=writer)
        th.start()
        self.assertTrue(held.wait(5.0))
        tw.start()
        self.assertFalse(done.wait(0.4), "writer finished while the lock was held")
        release.set()
        th.join(5.0)
        self.assertTrue(done.wait(5.0), "writer never got the lock back")
        tw.join(5.0)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["rooms"][0]["name"], "Lab-late")

    def test_same_thread_nesting_does_not_deadlock(self):
        t0 = time.monotonic()
        with config_store.devices_lock():
            with config_store.devices_lock():
                pass
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_missing_directory_is_a_no_op_not_an_error(self):
        with config_store.devices_lock(os.path.join(self.tmp.name, "absent",
                                                    "devices.conf")):
            pass


if __name__ == "__main__":
    unittest.main()
