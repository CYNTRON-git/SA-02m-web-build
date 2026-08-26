"""Unit tests for MQTT ↔ Yandex converters."""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client import converters  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402


class TestOnOff(unittest.TestCase):
    def test_mqtt_to_yandex_true(self):
        block = converters.mqtt_to_on_off("1")
        self.assertTrue(block["state"]["value"])
        self.assertEqual(block["type"], "devices.capabilities.on_off")

    def test_mqtt_to_yandex_false(self):
        self.assertFalse(converters.mqtt_to_on_off("0")["state"]["value"])

    def test_yandex_to_mqtt(self):
        payload, err = converters.yandex_to_on_off({"instance": "on", "value": True})
        self.assertIsNone(err)
        self.assertEqual(payload, "1")
        payload, err = converters.yandex_to_on_off({"instance": "on", "value": False})
        self.assertEqual(payload, "0")

    def test_yandex_invalid(self):
        payload, err = converters.yandex_to_on_off({"instance": "on", "value": "yes"})
        self.assertIsNone(payload)
        self.assertEqual(err, C.ERR_INVALID_VALUE)


class TestRange(unittest.TestCase):
    def test_relative(self):
        payload, err = converters.yandex_to_range(
            {"instance": "brightness", "value": 10, "relative": True},
            current_raw="40",
            parameters={"instance": "brightness", "min": 0, "max": 100},
        )
        self.assertIsNone(err)
        self.assertEqual(payload, "50")

    def test_clamp(self):
        payload, err = converters.yandex_to_range(
            {"instance": "brightness", "value": 999},
            parameters={"instance": "brightness", "min": 0, "max": 100},
        )
        self.assertEqual(payload, "100")


class TestFloatAndColor(unittest.TestCase):
    def test_float(self):
        block = converters.mqtt_to_float_property(
            "21.5", {"instance": "temperature", "unit": "unit.temperature.celsius"}
        )
        self.assertEqual(block["state"]["value"], 21.5)
        self.assertEqual(
            block["parameters"],
            {"instance": "temperature", "unit": "unit.temperature.celsius"},
        )

    def test_float_negative(self):
        # CE-02m-3 power export publishes negative values — must parse as-is.
        block = converters.mqtt_to_float_property(
            "-1500.5", {"instance": "power", "unit": "unit.watt"}
        )
        self.assertEqual(block["state"]["value"], -1500.5)

    def test_float_unparseable_returns_none(self):
        # A garbage payload must NOT fabricate a 0.0 reading (Alice would show
        # 0 °C as real) — the property block is omitted instead.
        self.assertIsNone(
            converters.mqtt_to_float_property(
                "garbage", {"instance": "temperature", "unit": "unit.temperature.celsius"}
            )
        )
        self.assertIsNone(converters.mqtt_to_float_property(None))
        self.assertIsNone(
            converters.property_mqtt_to_yandex(
                "devices.properties.float",
                "",
                {"instance": "humidity", "unit": "unit.percent"},
            )
        )

    def test_color_hex(self):
        block = converters.mqtt_to_color_setting("#112233", {"instance": "rgb"})
        self.assertEqual(block["state"]["value"], 0x112233)


if __name__ == "__main__":
    unittest.main()
