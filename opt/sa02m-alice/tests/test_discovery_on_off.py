"""C2 (1.0.6.51): Yandex Discovery carries only documented `on_off` parameters.

The «Умный дом» window stores an `on_off` capability with
`parameters: {"instance": "on"}` (smarthome.js shItemFromRow), and Discovery
copied `parameters` verbatim. Yandex documents exactly one `on_off` parameter,
`split` (bool); `instance` is not part of it. The Yandex profile now sends a
bool `split` only, and omits `parameters` when nothing is left. The cloud
profile's bytes are the cloud team's contract and stay as stored (Operator
decision F-C2, 2026-09-23).
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

ON_OFF = "devices.capabilities.on_off"


def _doc(params):
    cap = {"type": ON_OFF, "mqtt": "/devices/mr02m-COM3-10/controls/do_1"}
    if params is not None:
        cap["parameters"] = params
    range_cap = {"type": "devices.capabilities.range",
                 "parameters": {"instance": "brightness",
                                "range": {"min": 0, "max": 100}},
                 "mqtt": "/devices/led-COM3-13/controls/bright"}
    return {"rooms": [], "devices": [{
        "id": "sock", "name": "Розетка", "type": "devices.types.socket",
        "capabilities": [cap, range_cap], "properties": []}]}


def _caps(params, profile):
    reg = DeviceRegistry(_doc(params), profile=profile)
    return reg.discovery_devices(profile)[0]["capabilities"]


class TestYandexOnOffParameters(unittest.TestCase):
    def test_instance_is_never_sent_to_yandex(self):
        on_off = _caps({"instance": "on"}, C.PROFILE_YANDEX)[0]
        self.assertEqual(on_off["type"], ON_OFF)
        self.assertNotIn("parameters", on_off)

    def test_split_survives_and_instance_is_dropped(self):
        on_off = _caps({"split": True, "instance": "on"}, C.PROFILE_YANDEX)[0]
        self.assertEqual(on_off["parameters"], {"split": True})

    def test_non_bool_split_is_not_sent(self):
        on_off = _caps({"split": "yes"}, C.PROFILE_YANDEX)[0]
        self.assertNotIn("parameters", on_off)

    def test_other_capabilities_keep_their_parameters(self):
        rng = _caps({"instance": "on"}, C.PROFILE_YANDEX)[1]
        self.assertEqual(rng["parameters"]["instance"], "brightness")

    def test_cloud_profile_bytes_unchanged(self):
        on_off = _caps({"split": True, "instance": "on"}, C.PROFILE_CLOUD)[0]
        self.assertEqual(on_off["parameters"], {"split": True, "instance": "on"})


if __name__ == "__main__":
    unittest.main()
