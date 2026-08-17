"""Unit tests for DeviceRegistry discovery/query/action."""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402


DOC = {
    "rooms": [{"id": "r1", "name": "Lab", "devices": ["d1"]}],
    "devices": [
        {
            "id": "d1",
            "name": "Pump",
            "room_id": "r1",
            "type": "devices.types.switch",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "mqtt": "/devices/sa02m-SA-02/controls/do",
                    "parameters": {"instance": "on"},
                }
            ],
            "properties": [],
        }
    ],
}


class TestDeviceRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = DeviceRegistry(DOC)

    def test_discovery(self):
        devices = self.reg.discovery_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["name"], "Pump")
        self.assertEqual(devices[0]["room"], "Lab")

    def test_query_unreachable_without_cache(self):
        out = self.reg.query_devices(["d1"])
        self.assertEqual(out[0].get("error_code"), C.ERR_DEVICE_UNREACHABLE)

    def test_query_after_mqtt(self):
        self.reg.note_mqtt("/devices/sa02m-SA-02/controls/do", "1")
        out = self.reg.query_devices(["d1"])
        self.assertEqual(out[0]["capabilities"][0]["state"]["value"], True)

    def test_action_publishes_on_suffix(self):
        self.reg.note_mqtt("/devices/sa02m-SA-02/controls/do", "0")
        results, pubs = self.reg.apply_actions(
            [
                {
                    "id": "d1",
                    "capabilities": [
                        {
                            "type": "devices.capabilities.on_off",
                            "state": {"instance": "on", "value": True},
                        }
                    ],
                }
            ]
        )
        self.assertEqual(results[0]["capabilities"][0]["status"], C.STATUS_DONE)
        self.assertEqual(pubs, [("/devices/sa02m-SA-02/controls/do/on", "1")])

    def test_ignore_retained_flag(self):
        ok = self.reg.note_mqtt("/devices/sa02m-SA-02/controls/do", "1", retained=True)
        self.assertFalse(ok)
        self.assertIsNone(self.reg.get_cached("/devices/sa02m-SA-02/controls/do"))


if __name__ == "__main__":
    unittest.main()
