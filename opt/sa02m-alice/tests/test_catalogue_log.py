"""C1 (1.0.6.51): after an install, the client's journal states positively
what catalogue it built.

Shipping 1.0.6.50, whether a board's Alice client had picked up the new
Carel cards could only be inferred from the absence of errors. One INFO line
per catalogue build — at construction and on every reload — names the
profile, the device count and each device's capability types as THAT profile
sees them (a cloud-only item is not in the Yandex line).
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402

LOGGER = "sa02m_alice.registry"


def _doc():
    return {
        "rooms": [],
        "devices": [
            {"id": "carel-pcomini", "name": "Вентиляция",
             "type": "devices.types.thermostat.ac",
             "capabilities": [
                 {"type": "devices.capabilities.on_off",
                  "mqtt": "/devices/carel-COM3-1/controls/on"},
                 {"type": "devices.capabilities.mode",
                  "parameters": {"instance": "fan_speed", "modes": []},
                  "mqtt": "/devices/carel-COM3-1/controls/fan"},
                 {"type": "devices.capabilities.range",
                  "cloud_only": True,
                  "mqtt": "/devices/carel-COM3-1/controls/x"},
             ],
             "properties": []},
            {"id": "led-strip", "name": "Лента",
             "type": "devices.types.light",
             "capabilities": [
                 {"type": "devices.capabilities.on_off",
                  "mqtt": "/devices/led-COM3-13/controls/on"},
             ],
             "properties": []},
        ],
    }


def _built_lines(cm):
    return [r.getMessage() for r in cm.records
            if "catalogue built" in r.getMessage()]


class TestCatalogueLogLine(unittest.TestCase):
    def test_construct_logs_the_yandex_catalogue(self):
        with self.assertLogs(LOGGER, level="INFO") as cm:
            DeviceRegistry(_doc(), profile=C.PROFILE_YANDEX)
        self.assertEqual(_built_lines(cm), [
            "catalogue built (yandex): 2 devices — "
            "carel-pcomini[on_off,mode] led-strip[on_off]"])

    def test_cloud_profile_sees_the_cloud_only_item(self):
        with self.assertLogs(LOGGER, level="INFO") as cm:
            DeviceRegistry(_doc(), profile=C.PROFILE_CLOUD)
        self.assertEqual(_built_lines(cm), [
            "catalogue built (cloud): 2 devices — "
            "carel-pcomini[on_off,mode,range] led-strip[on_off]"])

    def test_reload_logs_again(self):
        with self.assertLogs(LOGGER, level="INFO"):
            reg = DeviceRegistry(_doc(), profile=C.PROFILE_YANDEX)
        doc = _doc()
        doc["devices"] = doc["devices"][1:]
        with self.assertLogs(LOGGER, level="INFO") as cm:
            reg.reload(doc)
        self.assertEqual(_built_lines(cm), [
            "catalogue built (yandex): 1 devices — led-strip[on_off]"])


if __name__ == "__main__":
    unittest.main()
