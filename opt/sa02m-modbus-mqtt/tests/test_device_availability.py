#!/usr/bin/env python3
"""Device-level availability: stale retained meta/error clear at registration.

Regression net for the 1.0.5.63 fix — a device the bridge reports online must
not carry a retained /devices/<id>/meta/error="r" left by a prior process.
A healthy device never transitions, so without the register-time clear the
stale "r" would never be cleared and the cloud watchdog floods on false
incidents. The offline back-off machine must still assert "r" when a device is
actually failing now.
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


if __name__ == "__main__":
    unittest.main()
