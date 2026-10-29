#!/usr/bin/env python3
"""Device-level availability: stale retained meta/error clear at registration.

Regression net for the 1.0.5.63 fix — a device the bridge reports online must
not carry a retained /devices/<id>/meta/error="r" left by a prior process.
A healthy device never transitions, so without the register-time clear the
stale "r" would never be cleared and the cloud watchdog floods on false
incidents. The offline back-off machine must still assert "r" when a device is
actually failing now.

Also the back-off machine itself (1.0.7.12): a device that stays silent for
hours must keep backing off — bench 1.135 (2026-10-29) ran into mark_fail()
raising OverflowError once ``2 ** over`` outgrew a float (~1024 failures past
the threshold), which escaped BEFORE _backoff_until was set and turned every
silent device into a full-rate poll with one WARNING per attempt (3 lines/s,
23 % CPU). TestBackoffExponentCap pins the cap; TestPollFailLogLevel pins the
log_poll_fail helper (WARNING while online, DEBUG once offline) and that the
five per-attempt pollers route their failure line through it.
"""
from __future__ import annotations

import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))


def _stub_missing(name: str, module: types.ModuleType) -> None:
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module


_stub_missing("yaml", types.ModuleType("yaml"))
_stub_missing("serial", types.ModuleType("serial"))
try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    _paho = types.ModuleType("paho")
    _paho_mqtt = types.ModuleType("paho.mqtt")
    _paho_client = types.ModuleType("paho.mqtt.client")
    _paho_client.Client = object
    _paho_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    _paho.mqtt = _paho_mqtt
    _paho_mqtt.client = _paho_client
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _paho_mqtt
    sys.modules["paho.mqtt.client"] = _paho_client

import modbus_mqtt_bridge as bridge  # noqa: E402
import bridge_carel  # noqa: E402
import bridge_device  # noqa: E402
import bridge_dtv_ce  # noqa: E402
import bridge_led  # noqa: E402
import bridge_mr02m  # noqa: E402
from sa02m_carel import controls as cc  # noqa: E402


def _make_publisher():
    """Build an MQTTPublisher without __init__ (which builds a real mqtt.Client,
    impossible under the paho stub). Set only the attributes the availability
    methods touch, and capture the two publish helpers with mocks."""
    pub = bridge.MQTTPublisher.__new__(bridge.MQTTPublisher)
    pub._lock = threading.Lock()
    pub._device_online = {}
    pub._availability = True
    pub._poll_errors = 0
    pub._bridge_id = "sa02m-bridge"
    pub.pub_device_error = mock.Mock()
    pub._publish_bridge_status = mock.Mock()
    return pub


class TestDeviceAvailability(unittest.TestCase):
    def test_registration_clears_stale_retained_error(self):
        """Case 1: registering a device publishes meta/error="" exactly once —
        clearing any stale retained "r" from a previous process."""
        pub = _make_publisher()
        pub.register_device("d1")
        pub.pub_device_error.assert_called_once_with("d1", "")

    def test_healthy_poll_does_not_republish(self):
        """Case 2: a healthy poll (no online→offline flip) must not republish —
        the clear fires once at registration, not on every poll (no per-poll
        spam; pub_device_error has no dedup)."""
        pub = _make_publisher()
        pub.register_device("d1")
        self.assertEqual(pub.pub_device_error.call_count, 1)
        pub.device_online("d1", True)  # already online — no state change
        self.assertEqual(pub.pub_device_error.call_count, 1)

    def test_going_offline_still_asserts_r(self):
        """Case 3: a device that actually fails now still gets meta/error="r" —
        the fix must not over-clear and hide a real offline device."""
        pub = _make_publisher()
        pub.register_device("d1")
        pub.pub_device_error.reset_mock()
        pub.device_online("d1", False)
        pub.pub_device_error.assert_called_once_with("d1", "r")

    def test_recovery_clears_error_again(self):
        """A device that recovers (offline→online transition) re-publishes ""
        via the existing state machine — confirms the round trip."""
        pub = _make_publisher()
        pub.register_device("d1")
        pub.device_online("d1", False)   # offline
        pub.pub_device_error.reset_mock()
        pub.device_online("d1", True)    # back online — transition fires
        pub.pub_device_error.assert_called_once_with("d1", "")

    def test_availability_disabled_no_clear(self):
        """With availability publishing disabled, registration publishes no
        device-level error at all (guarded by self._availability)."""
        pub = _make_publisher()
        pub._availability = False
        pub.register_device("d1")
        pub.pub_device_error.assert_not_called()


class _RaisingPort:
    """A Modbus port whose every read fails — the silent device on the bus."""

    def __init__(self, exc=None):
        self.exc = exc or IOError("timeout")

    def _raise(self, *a, **k):
        raise self.exc

    read_coils = read_discrete_inputs = _raise
    read_holding_registers = read_input_registers = _raise


class TestBackoffExponentCap(unittest.TestCase):
    """mark_fail() stays total however long a device is silent."""

    def test_1200_failures_never_raise_and_keep_backing_off(self):
        pub = _make_publisher()
        pub.register_device("d1")
        pub.pub_device_error.reset_mock()
        p = bridge_device.DevicePoller({"id": "d1"}, pub)
        now = 1000.0
        with mock.patch.object(bridge_device.time, "monotonic", return_value=now):
            for n in range(1, 1201):
                p.mark_fail()        # OverflowError here on the uncapped code
                if n >= p._fail_threshold:
                    self.assertFalse(p._online, n)
                    self.assertLessEqual(p._backoff_until, now + p._backoff_max_s, n)
                    self.assertTrue(p.in_backoff(), n)
                if n >= p._fail_threshold + 4:
                    # 2 s * 2^4 > 30 s: every call past here sits at the cap.
                    self.assertEqual(p._backoff_until, now + p._backoff_max_s, n)
        self.assertEqual(p._fail_count, 1200)
        # One offline transition — meta/error="r" published exactly once.
        pub.pub_device_error.assert_called_once_with("d1", "r")

    def test_recovery_after_a_long_silence(self):
        pub = _make_publisher()
        pub.register_device("d1")
        p = bridge_device.DevicePoller({"id": "d1"}, pub)
        with mock.patch.object(bridge_device.time, "monotonic", return_value=5.0):
            for _ in range(1100):
                p.mark_fail()
            pub.pub_device_error.reset_mock()
            p.mark_ok()
        self.assertTrue(p._online)
        self.assertEqual(p._fail_count, 0)
        self.assertFalse(p.in_backoff())
        pub.pub_device_error.assert_called_once_with("d1", "")


def _records(cm, text):
    """(levelname, message) of the captured records whose message carries text."""
    return [(r.levelname, r.getMessage()) for r in cm.records if text in r.getMessage()]


class TestPollFailLogLevel(unittest.TestCase):
    """log_poll_fail: WARNING while the device counts as online, DEBUG once it
    is flagged offline, WARNING again after a recovery."""

    def test_helper_levels_follow_the_online_flag(self):
        pub = _make_publisher()
        pub.register_device("d1")
        p = bridge_device.DevicePoller({"id": "d1"}, pub)   # offline_after_fails=3
        exc = IOError("timeout")
        with self.assertLogs(p.log, level="DEBUG") as cm:
            for _ in range(5):
                p.mark_fail()
                p.log_poll_fail("x poll", exc)
            p.mark_ok()
            p.mark_fail()
            p.log_poll_fail("x poll", exc)
        got = _records(cm, "x poll: timeout")
        self.assertEqual(
            got,
            [("WARNING", "x poll: timeout")] * 2      # fails 1, 2: still online
            + [("DEBUG", "x poll: timeout")] * 3      # fails 3 (transition), 4, 5
            + [("WARNING", "x poll: timeout")],       # first fail after recovery
            got)
        # The transition lines themselves keep their levels.
        self.assertEqual(
            [lvl for lvl, m in _records(cm, "device offline after")], ["WARNING"])
        self.assertEqual(
            [lvl for lvl, m in _records(cm, "device back online")], ["INFO"])

    def _call_sites(self):
        """(label, poller, method) for the five per-attempt failure lines."""
        pub = mock.Mock()
        rtu = {"port": "/dev/COMtest", "baudrate": 9600, "offline_after_fails": 3}
        led = bridge_led.LedPoller(
            dict(rtu, id="led-COM3-13", type="led", address=13), pub)
        carel = bridge_carel.CarelPoller(
            dict(rtu, id="carel-COM3-1", type="carel", family=cc.FAMILY_CRST,
                 address=1, app_version="2.03.00.46"), pub)
        dtv = bridge_dtv_ce.DTVPoller(
            dict(rtu, id="dtv-COM4-3", type="dtv", address=3,
                 sensors_present=["temp_hdc1080"]), pub)
        ce = bridge_dtv_ce.CE02M3Poller(
            dict(rtu, id="ce02m3-COM2-14", type="ce02m3", address=14), pub)
        mr = bridge_mr02m.MR02mPoller(
            dict(rtu, id="mr02m-COM3-6", address=6, module_type=1), pub)
        return [
            ("led poll", led, led._read_snapshot),
            ("carel poll", carel, carel._read_snapshot),
            ("sensor poll", dtv, dtv._poll_sensors),
            ("power poll", ce, ce._poll_power),
            ("init_module", mr, mr._init_module),
        ]

    def test_five_pollers_route_their_failure_line_through_the_helper(self):
        for label, p, method in self._call_sites():
            with self.subTest(label=label):
                p.get_port = lambda: _RaisingPort(IOError("boom"))
                with mock.patch.object(bridge_dtv_ce.time, "sleep"), \
                        mock.patch.object(
                            bridge_device.DeviceLiveCache, "flush_file"):
                    # Online: the first failure is a WARNING.
                    with self.assertLogs(p.log, level="DEBUG") as cm:
                        method()
                    self.assertEqual(_records(cm, label + ": boom"),
                                     [("WARNING", label + ": boom")])
                    # Past the offline transition: the same line is DEBUG.
                    for _ in range(3):
                        p.mark_fail()
                    self.assertFalse(p._online)
                    with self.assertLogs(p.log, level="DEBUG") as cm:
                        method()
                    self.assertEqual(_records(cm, label + ": boom"),
                                     [("DEBUG", label + ": boom")])


if __name__ == "__main__":
    unittest.main()
