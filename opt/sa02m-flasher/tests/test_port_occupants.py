# -*- coding: utf-8 -*-
"""Port occupancy: one fuser per port per /ports request, unknown is not free.

H5-05 (1.0.7.36). The /ports description asked for the occupants and then for
their labels, and each asked `sudo fuser` again (~0.3 s per call on a loaded
board, 3-4 s per request). And a fuser that timed out answered "nobody holds
the port", which is_port_poll_free read as FREE. `_run` is the only boundary
replaced here: the parsing, the cache and the decision are the shipped code.
"""
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from sa02m_flasher import mplc_lease


def _done(rc, out="", err=""):
    return subprocess.CompletedProcess(args=["fuser"], returncode=rc, stdout=out, stderr=err)


class PortOccupants(unittest.TestCase):
    def setUp(self):
        cache = getattr(mplc_lease, "_OCCUPANTS_CACHE", None)
        if cache is not None:
            cache.clear()
        self.patches = [
            patch.object(mplc_lease, "_fuser", return_value="/usr/bin/fuser"),
            patch.object(mplc_lease, "_sudo", return_value=None),
            patch.object(mplc_lease, "_timeout_cmd", return_value=None),
            patch.object(mplc_lease, "device_path_exists", return_value=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def free(self, run_effect):
        with patch.object(mplc_lease, "_run", side_effect=run_effect):
            return mplc_lease.is_port_poll_free("/dev/COM4")

    def test_timeout_is_not_free(self):
        self.assertFalse(self.free(subprocess.TimeoutExpired(["fuser"], 2)),
                         "a fuser that timed out read as a free port")

    def test_refusal_and_failure_are_not_free(self):
        self.assertFalse(self.free(PermissionError("sudo")))
        self.assertFalse(self.free([_done(137)]), "the outer timeout -s KILL fired")
        self.assertFalse(self.free([_done(1, err="sudo: a password is required")]))

    def test_nobody_and_somebody(self):
        self.assertTrue(self.free([_done(1)]), "fuser rc 1 with no output = nobody")
        self.assertFalse(self.free([_done(0, out=" 1234", err="/dev/COM4:")]))

    def test_one_fuser_per_port_per_request_and_reuse(self):
        with patch.object(mplc_lease, "_run", return_value=_done(0, out=" 4242", err="/dev/COM3:")) as run, \
                patch.object(mplc_lease, "_pid_cmdline",
                             return_value="/usr/bin/python3 /opt/sa02m-modbus-mqtt/modbus_mqtt_bridge.py"):
            # What service.py _describe_port does for one port.
            occupants = mplc_lease.port_occupants("/dev/COM3")
            labels = mplc_lease.port_poll_service_labels("/dev/COM3")
            self.assertEqual(occupants, ["4242"])
            self.assertEqual(labels, ["MQTT"])
            self.assertEqual(run.call_count, 1, "occupants + labels ran fuser twice for one port")
            # A repeat inside the 2 s window starts nothing new.
            mplc_lease.port_occupants("/dev/COM3")
            mplc_lease.port_poll_service_labels("/dev/COM3")
            self.assertEqual(run.call_count, 1)
            # A decision never trusts the cache.
            mplc_lease.is_port_poll_free("/dev/COM3")
            self.assertEqual(run.call_count, 2)

    def test_cache_expires(self):
        clock = [100.0]
        with patch.object(mplc_lease, "_run", return_value=_done(1)) as run, \
                patch.object(mplc_lease.time, "monotonic", side_effect=lambda: clock[0]):
            mplc_lease.port_occupants("/dev/COM5")
            clock[0] += 1.0
            mplc_lease.port_occupants("/dev/COM5")
            self.assertEqual(run.call_count, 1)
            clock[0] += 1.5
            mplc_lease.port_occupants("/dev/COM5")
            self.assertEqual(run.call_count, 2)

    def test_unknown_reads_as_empty_in_answers(self):
        # The API fields stay as they were: busy_pids is a list, never null.
        with patch.object(mplc_lease, "_run", side_effect=subprocess.TimeoutExpired(["fuser"], 2)):
            self.assertEqual(mplc_lease.port_occupants("/dev/COM2"), [])


if __name__ == "__main__":
    unittest.main()
