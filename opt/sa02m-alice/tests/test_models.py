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
                "mqtt": "/devices/sa02m-SA-02/controls/do",
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


if __name__ == "__main__":
    unittest.main()
