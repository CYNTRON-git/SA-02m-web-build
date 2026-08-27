"""Unit tests for device-document validation (models.py).

Validating tests for the device-document property model in
docs/contracts/alice-mqtt-mapping.md (float-property parameters allowlist).
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.config import models  # noqa: E402


def _switch_device():
    return {
        "id": "d1",
        "name": "Pump",
        "type": "devices.types.switch",
        "capabilities": [
            {
                "type": "devices.capabilities.on_off",
                "mqtt": "/devices/test-ctl/controls/do",
                "retrievable": True,
                "reportable": True,
                "parameters": {"instance": "on"},
            }
        ],
        "properties": [],
    }


def _sensor_device():
    return {
        "id": "s1",
        "name": "DTV temp",
        "type": "devices.types.sensor.climate",
        "capabilities": [],
        "properties": [
            {
                "type": "devices.properties.float",
                "mqtt": "/devices/dtv-COM3-1/controls/temp_bme680",
                "retrievable": True,
                "reportable": True,
                "parameters": {
                    "instance": "temperature",
                    "unit": "unit.temperature.celsius",
                },
            }
        ],
    }


def _float_prop(instance, unit, topic="/devices/dtv-COM3-1/controls/x"):
    return {
        "type": "devices.properties.float",
        "mqtt": topic,
        "retrievable": True,
        "reportable": True,
        "parameters": {"instance": instance, "unit": unit},
    }


def _motion_prop(topic="/devices/dtv-COM3-1/controls/presence"):
    return {
        "type": "devices.properties.event",
        "mqtt": topic,
        "retrievable": True,
        "reportable": True,
        "parameters": {
            "instance": "motion",
            "events": [{"value": "detected"}, {"value": "not_detected"}],
        },
    }


class TestValidateDevice(unittest.TestCase):
    def test_legacy_switch_document_unchanged(self):
        dev = _switch_device()
        out, err = models.validate_device(dev)
        self.assertIsNone(err)
        self.assertEqual(out, dev)

    def test_sensor_document_round_trips(self):
        dev = _sensor_device()
        out, err = models.validate_device(dev)
        self.assertIsNone(err)
        self.assertEqual(out, dev)

    def test_float_property_without_parameters_rejected(self):
        dev = _sensor_device()
        del dev["properties"][0]["parameters"]
        out, err = models.validate_device(dev)
        self.assertIsNone(out)
        self.assertEqual(err, "float property requires parameters")

    def test_float_property_bad_instance_rejected(self):
        dev = _sensor_device()
        dev["properties"][0]["parameters"]["instance"] = "hostile$(rm)"
        out, err = models.validate_device(dev)
        self.assertIsNone(out)
        self.assertEqual(err, "invalid float property instance")

    def test_float_property_bad_unit_rejected(self):
        for bad in ("celsius", "unit.CAPS", "unit.$(rm)", "", None, 42):
            dev = _sensor_device()
            dev["properties"][0]["parameters"]["unit"] = bad
            out, err = models.validate_device(dev)
            self.assertIsNone(out, "unit %r must be rejected" % (bad,))
            self.assertEqual(err, "invalid float property unit")

    def test_hand_edit_instances_allowed(self):
        # The allowlist is wider than the UI's six kinds on purpose.
        for inst, unit in (
            ("pressure", "unit.pressure.mmhg"),
            ("co2_level", "unit.ppm"),
            ("battery_level", "unit.percent"),
        ):
            dev = _sensor_device()
            dev["properties"][0]["parameters"] = {"instance": inst, "unit": unit}
            out, err = models.validate_device(dev)
            self.assertIsNone(err, "instance %r must validate" % inst)
            self.assertIsNotNone(out)

    def test_capability_typed_as_property_rejected(self):
        dev = _switch_device()
        dev["capabilities"][0]["type"] = "devices.properties.float"
        out, err = models.validate_device(dev)
        self.assertIsNone(out)
        self.assertEqual(err, "invalid capability type")

    def test_property_typed_as_capability_rejected(self):
        dev = _sensor_device()
        dev["properties"][0]["type"] = "devices.capabilities.on_off"
        out, err = models.validate_device(dev)
        self.assertIsNone(out)
        self.assertEqual(err, "invalid property type")


class TestFloatInstances(unittest.TestCase):
    def test_new_float_instances_accepted(self):
        for inst, unit in (
            ("pressure", "unit.pressure.mmhg"),
            ("co2_level", "unit.ppm"),
            ("tvoc", "unit.density.mcg_m3"),
            ("illumination", "unit.illumination.lux"),
            ("water_level", "unit.percent"),
            ("electricity_meter", "unit.kilowatt_hour"),
        ):
            dev = _sensor_device()
            dev["properties"][0]["parameters"] = {"instance": inst, "unit": unit}
            out, err = models.validate_device(dev)
            self.assertIsNone(err, "instance %r must validate" % inst)
            self.assertIsNotNone(out)

    def test_mcg_m3_unit_accepted(self):
        """The unit pattern had no digit class, so TVOC's unit was rejected."""
        dev = _sensor_device()
        dev["properties"][0]["parameters"] = {
            "instance": "tvoc",
            "unit": "unit.density.mcg_m3",
        }
        out, err = models.validate_device(dev)
        self.assertIsNone(err)
        self.assertIsNotNone(out)

    def test_unit_stays_lowercase_and_anchored(self):
        # Widening by a digit class must not widen anything else.
        for bad in ("unit.MCG_M3", "unit.density.mcg_m3 ", "xunit.ppm", "unit.a;b"):
            dev = _sensor_device()
            dev["properties"][0]["parameters"] = {"instance": "tvoc", "unit": bad}
            out, err = models.validate_device(dev)
            self.assertIsNone(out, "unit %r must be rejected" % (bad,))
            self.assertEqual(err, "invalid float property unit")


class TestEventProperties(unittest.TestCase):
    def test_event_property_valid_motion(self):
        dev = _sensor_device()
        dev["type"] = "devices.types.sensor.motion"
        dev["properties"] = [_motion_prop()]
        out, err = models.validate_device(dev)
        self.assertIsNone(err)
        self.assertEqual(out["properties"][0], _motion_prop())

    def test_event_property_bad_value_rejected(self):
        dev = _sensor_device()
        prop = _motion_prop()
        prop["parameters"]["events"] = [{"value": "present"}]
        dev["properties"] = [prop]
        out, err = models.validate_device(dev)
        self.assertIsNone(out)
        self.assertEqual(err, "invalid event property events")

    def test_event_property_missing_events_rejected(self):
        for events in (None, [], "detected", [{"no_value": 1}], [{"value": 1}]):
            dev = _sensor_device()
            prop = _motion_prop()
            if events is None:
                del prop["parameters"]["events"]
            else:
                prop["parameters"]["events"] = events
            dev["properties"] = [prop]
            out, err = models.validate_device(dev)
            self.assertIsNone(out, "events %r must be rejected" % (events,))
            self.assertEqual(err, "invalid event property events")

    def test_event_property_duplicate_value_rejected(self):
        dev = _sensor_device()
        prop = _motion_prop()
        prop["parameters"]["events"] = [{"value": "detected"}, {"value": "detected"}]
        dev["properties"] = [prop]
        out, err = models.validate_device(dev)
        self.assertIsNone(out)
        self.assertEqual(err, "invalid event property events")

    def test_event_property_bad_instance_rejected(self):
        dev = _sensor_device()
        prop = _motion_prop()
        prop["parameters"]["instance"] = "presence"
        dev["properties"] = [prop]
        out, err = models.validate_device(dev)
        self.assertIsNone(out)
        self.assertEqual(err, "invalid event property instance")

    def test_event_property_without_parameters_rejected(self):
        dev = _sensor_device()
        prop = _motion_prop()
        del prop["parameters"]
        dev["properties"] = [prop]
        out, err = models.validate_device(dev)
        self.assertIsNone(out)
        self.assertEqual(err, "event property requires parameters")


class TestScale(unittest.TestCase):
    def test_scale_absent_leaves_item_unchanged(self):
        dev = _sensor_device()
        out, err = models.validate_device(dev)
        self.assertIsNone(err)
        self.assertNotIn("scale", out["properties"][0])

    def test_scale_accepted_and_normalised_to_float(self):
        for good in (1000, 7.50062, 0.001, -1.5):
            dev = _sensor_device()
            dev["properties"][0]["scale"] = good
            out, err = models.validate_device(dev)
            self.assertIsNone(err, "scale %r must validate" % (good,))
            self.assertIsInstance(out["properties"][0]["scale"], float)
            self.assertEqual(out["properties"][0]["scale"], float(good))

    def test_scale_rejected(self):
        for bad in (0, "x", 1e9, True, None, float("inf"), float("nan")):
            dev = _sensor_device()
            dev["properties"][0]["scale"] = bad
            out, err = models.validate_device(dev)
            self.assertIsNone(out, "scale %r must be rejected" % (bad,))
            self.assertEqual(err, "invalid property scale")

    def test_scale_normalisation_does_not_mutate_input(self):
        dev = _sensor_device()
        dev["properties"][0]["scale"] = 1000
        models.validate_device(dev)
        self.assertIsInstance(dev["properties"][0]["scale"], int)


class TestDuplicateInstances(unittest.TestCase):
    """A Yandex property is addressed by (type, instance) and nothing else —
    a repeat means the second reading is invisible in the app."""

    def test_duplicate_float_instance_rejected(self):
        dev = _sensor_device()
        dev["properties"] = [
            _float_prop("voltage", "unit.volt", "/devices/ce02m3-COM2-14/controls/voltage_a"),
            _float_prop("voltage", "unit.volt", "/devices/ce02m3-COM2-14/controls/voltage_b"),
        ]
        out, err = models.validate_device(dev)
        self.assertIsNone(out)
        self.assertEqual(err, "duplicate property instance: voltage")

    def test_duplicate_capability_instance_rejected(self):
        dev = _switch_device()
        dev["capabilities"] = [dev["capabilities"][0], dict(dev["capabilities"][0])]
        out, err = models.validate_device(dev)
        self.assertIsNone(out)
        self.assertEqual(err, "duplicate capability instance: on")

    def test_distinct_instances_accepted(self):
        dev = _sensor_device()
        dev["properties"] = [
            _float_prop("temperature", "unit.temperature.celsius", "/devices/dtv-COM4-3/controls/temp_bme680"),
            _float_prop("humidity", "unit.percent", "/devices/dtv-COM4-3/controls/humidity_bme680"),
            _float_prop("pressure", "unit.pressure.mmhg", "/devices/dtv-COM4-3/controls/pressure_bme680_kpa"),
            _float_prop("co2_level", "unit.ppm", "/devices/dtv-COM4-3/controls/eco2_bme680"),
            _float_prop("tvoc", "unit.density.mcg_m3", "/devices/dtv-COM4-3/controls/tvoc_zmod"),
            _motion_prop("/devices/dtv-COM4-3/controls/presence"),
        ]
        dev["properties"][2]["scale"] = 7.50062
        dev["properties"][4]["scale"] = 1000
        out, err = models.validate_device(dev)
        self.assertIsNone(err)
        self.assertEqual(len(out["properties"]), 6)


if __name__ == "__main__":
    unittest.main()
