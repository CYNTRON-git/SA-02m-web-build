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


class TestFloatScale(unittest.TestCase):
    """The item-level `scale` is the ONE home for reading arithmetic."""

    def test_float_scale_default_one(self):
        block = converters.mqtt_to_float_property(
            "21.5", {"instance": "temperature", "unit": "unit.temperature.celsius"}
        )
        self.assertEqual(block["state"]["value"], 21.5)

    def test_float_scale_applied(self):
        # DTV publishes kPa; Yandex takes mmHg — ×7.50062, rounded to 3.
        block = converters.mqtt_to_float_property(
            "101.32", {"instance": "pressure", "unit": "unit.pressure.mmhg"}, 7.50062
        )
        self.assertEqual(block["state"]["value"], 759.963)

    def test_tvoc_mg_to_mcg(self):
        block = converters.mqtt_to_float_property(
            "0.35", {"instance": "tvoc", "unit": "unit.density.mcg_m3"}, 1000
        )
        self.assertEqual(block["state"]["value"], 350.0)

    def test_scale_threaded_through_property_dispatch(self):
        block = converters.property_mqtt_to_yandex(
            "devices.properties.float",
            "0.35",
            {"instance": "tvoc", "unit": "unit.density.mcg_m3"},
            1000,
        )
        self.assertEqual(block["state"]["value"], 350.0)

    def test_scale_preserves_sign_and_never_leaks(self):
        block = converters.mqtt_to_float_property(
            "-1.5", {"instance": "power", "unit": "unit.watt"}, 1000
        )
        self.assertEqual(block["state"]["value"], -1500.0)
        self.assertNotIn("scale", block)
        self.assertNotIn("scale", block["parameters"])


MOTION_PARAMS = {
    "instance": "motion",
    "events": [{"value": "detected"}, {"value": "not_detected"}],
}


class TestEventProperty(unittest.TestCase):
    def test_event_numeric_payload_maps_to_motion(self):
        """The bridge publishes DTV presence as "1.0"/"0.0" (a scaled
        register), which Yandex refuses verbatim — it must be mapped."""
        for raw, expect in (("1.0", "detected"), ("0.0", "not_detected"),
                            ("1", "detected"), ("0", "not_detected")):
            block = converters.mqtt_to_event_property(raw, MOTION_PARAMS)
            self.assertIsNotNone(block, "payload %r must map" % raw)
            self.assertEqual(block["type"], "devices.properties.event")
            self.assertEqual(block["state"]["instance"], "motion")
            self.assertEqual(block["state"]["value"], expect)

    def test_event_literal_payload_passthrough(self):
        params = {"instance": "open", "events": [{"value": "opened"}, {"value": "closed"}]}
        block = converters.mqtt_to_event_property("opened", params)
        self.assertEqual(block["state"]["value"], "opened")

    def test_event_word_payload_maps(self):
        self.assertEqual(
            converters.mqtt_to_event_property("true", MOTION_PARAMS)["state"]["value"],
            "detected",
        )
        self.assertEqual(
            converters.mqtt_to_event_property("off", MOTION_PARAMS)["state"]["value"],
            "not_detected",
        )

    def test_event_unparseable_returns_none(self):
        for raw in ("", None, "garbage", "   "):
            self.assertIsNone(converters.mqtt_to_event_property(raw, MOTION_PARAMS))

    def test_event_unknown_instance_returns_none(self):
        self.assertIsNone(
            converters.mqtt_to_event_property("1.0", {"instance": "presence", "events": []})
        )

    def test_event_none_slot_returns_none(self):
        # vibration reports the event, never its absence.
        params = {"instance": "vibration", "events": [{"value": "vibration"}]}
        self.assertEqual(
            converters.mqtt_to_event_property("1", params)["state"]["value"], "vibration"
        )
        self.assertIsNone(converters.mqtt_to_event_property("0", params))

    def test_event_dispatch_through_property_mqtt_to_yandex(self):
        block = converters.property_mqtt_to_yandex(
            "devices.properties.event", "1.0", MOTION_PARAMS
        )
        self.assertEqual(block["state"]["value"], "detected")


class TestTruthyMqtt(unittest.TestCase):
    def test_truthy_mqtt_accepts_float_string(self):
        """A ×1.0-scaled coil arrives as "1.0" — a word-set test read false."""
        self.assertTrue(converters._truthy_mqtt("1.0"))
        self.assertFalse(converters._truthy_mqtt("0.0"))
        self.assertTrue(converters.mqtt_to_on_off("1.0")["state"]["value"])
        self.assertFalse(converters.mqtt_to_on_off("0.0")["state"]["value"])

    def test_truthy_mqtt_keeps_word_set(self):
        for raw in ("1", "true", "on", "yes", "TRUE"):
            self.assertTrue(converters._truthy_mqtt(raw), raw)
        for raw in ("0", "false", "off", "no", "", "garbage"):
            self.assertFalse(converters._truthy_mqtt(raw), raw)


if __name__ == "__main__":
    unittest.main()
