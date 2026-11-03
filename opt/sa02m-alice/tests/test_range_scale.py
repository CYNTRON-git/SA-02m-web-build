"""Opt-in 4TO6DI logical percentages leave raw AO and independent DO intact."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sa02m_alice.client import converters
from sa02m_alice.client.device_registry import DeviceRegistry
from sa02m_alice.config.models import validate_device
from sa02m_alice.common import constants as C

P = "devices.capabilities."
DO = "/devices/triac/controls/do_1"
AO = "/devices/triac/controls/ao_1"


def fixture(instance="fan_speed", lo=40):
    return {"id": "triac", "name": "Triac", "type": "devices.types.ventilation.fan",
            "properties": [], "capabilities": [
                {"type": P + "on_off", "mqtt": DO, "parameters": {"instance": "on"}},
                {"type": P + "range", "mqtt": AO, "range_scale": .1, "cloud_only": instance == "fan_speed",
                 "parameters": {"instance": instance, "unit": "unit.percent",
                                "range": {"min": lo, "max": 100, "precision": 1}}}]}


class RangeScale(unittest.TestCase):
    def test_schema_rejects_invalid_scale_and_nonrange(self):
        for value in (False, 0, -1, "0.1", None, 1e-7, 1e7, float("nan"), float("inf"), 10**400):
            doc = fixture()
            doc["capabilities"][1]["range_scale"] = value
            self.assertIsNotNone(validate_device(doc)[1], repr(value))
        doc = fixture()
        doc["capabilities"][0]["range_scale"] = .1
        self.assertIsNotNone(validate_device(doc)[1])

    def test_logical_bounds_and_independent_power(self):
        doc, error = validate_device(fixture())
        self.assertIsNone(error)
        reg = DeviceRegistry({"devices": [doc]}, profile=C.PROFILE_CLOUD)
        for kind, instance, value, topic, expected in (
                ("range", "fan_speed", 40, AO, "400"), ("range", "fan_speed", 100, AO, "1000"),
                ("range", "fan_speed", 65, AO, "650"), ("on_off", "on", False, DO, "0"),
                ("on_off", "on", True, DO, "1")):
            _, writes = reg.apply_actions([{"id": "triac", "capabilities": [
                {"type": P + kind, "state": {"instance": instance, "value": value}}]}])
            self.assertEqual(writes, [(topic + "/on", expected)])
        for value in (0, 39, 101, True, float("nan")):
            _, writes = reg.apply_actions([{"id": "triac", "capabilities": [
                {"type": P + "range", "state": {"instance": "fan_speed", "value": value}}]}])
            self.assertEqual(writes, [])
        self.assertNotIn("range_scale", repr(reg.discovery_devices()))
        yandex = DeviceRegistry({"devices": [doc]}, profile=C.PROFILE_YANDEX)
        self.assertEqual(len(yandex.discovery_devices()[0]["capabilities"]), 1)

    def test_query_and_events_share_logical_percent(self):
        reg = DeviceRegistry({"devices": [fixture()]}, profile=C.PROFILE_CLOUD)
        reg.note_mqtt(DO, "1")
        reg.note_mqtt(AO, "650")
        caps = reg.query_devices(["triac"])[0]["capabilities"]
        self.assertEqual(next(c["state"]["value"] for c in caps if c["type"] == P + "range"), 65)
        blocks = reg.state_blocks_for_topic(AO)
        self.assertEqual(blocks[0]["capabilities"][0]["state"]["value"], 65)

    def test_brightness_zero_full_scale_and_relative(self):
        params = fixture("brightness", 0)["capabilities"][1]["parameters"]
        for logical, raw in ((0, "0"), (25, "250"), (100, "1000"), (25.5, "255")):
            self.assertEqual(converters.capability_yandex_to_mqtt(P + "range", {"instance": "brightness", "value": logical}, parameters=params, range_scale=.1, strict=True), (raw, None))
            self.assertEqual(converters.capability_mqtt_to_yandex(P + "range", raw, params, range_scale=.1)["state"]["value"], logical)
        self.assertEqual(converters.capability_yandex_to_mqtt(P + "range", {"instance": "brightness", "value": 10, "relative": True}, parameters=params, current_raw="500", range_scale=.1, yandex=True), ("600", None))
        # Existing raw range semantics remain unchanged without opt-in.
        self.assertEqual(converters.mqtt_to_range("650", {"instance": "brightness"})["state"]["value"], 650)
        for raw in ("nan", "inf", "garbled"):
            self.assertIsNone(converters.mqtt_to_range(raw, params, range_scale=.1))


if __name__ == "__main__":
    unittest.main()
