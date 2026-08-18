#!/usr/bin/env python3
"""Direct characterization of the pure WB /meta blob assembly (bridge_meta.py).

The blob assembly was extracted verbatim from bridge_mqtt.py by the
decompose (feature/decompose-bridge-meta). test_meta_blob.py exercises it
through the publisher; this net pins the pure functions DIRECTLY so the module
carries its own behaviour contract (num/obj/str typing, control/device blob
shape) independent of the MQTT publisher.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

from bridge_meta import (  # noqa: E402
    control_meta_blob,
    device_meta_blob,
    num_or_str,
    obj_or_str,
)


class TestNumOrStr(unittest.TestCase):
    def test_integer_string_becomes_int(self):
        self.assertEqual(num_or_str("42"), 42)
        self.assertIsInstance(num_or_str("42"), int)

    def test_float_string_becomes_float(self):
        self.assertEqual(num_or_str("3.5"), 3.5)
        self.assertIsInstance(num_or_str("3.5"), float)

    def test_non_numeric_string_unchanged(self):
        self.assertEqual(num_or_str("V"), "V")

    def test_none_returns_input(self):
        self.assertIsNone(num_or_str(None))


class TestObjOrStr(unittest.TestCase):
    def test_json_object_string_becomes_dict(self):
        self.assertEqual(obj_or_str('{"ru": "AO1", "en": "AO1"}'),
                         {"ru": "AO1", "en": "AO1"})

    def test_plain_string_unchanged(self):
        self.assertEqual(obj_or_str("range"), "range")

    def test_json_non_object_returns_original_string(self):
        # A JSON array/number parses but is not a dict — keep the raw string.
        self.assertEqual(obj_or_str("[1, 2]"), "[1, 2]")
        self.assertEqual(obj_or_str("5"), "5")


class TestControlMetaBlob(unittest.TestCase):
    def test_typed_mirror_of_subtopic_values(self):
        meta = {
            "type": "range",       # string as-is
            "units": "V",          # string as-is
            "min": "0",            # numeric
            "max": "1000",         # numeric
            "order": "3",          # numeric
            "precision": "3",      # numeric
            "readonly": "1",       # boolean
            "title": '{"ru": "AO1", "en": "AO1"}',  # structured
        }
        blob = control_meta_blob(meta)
        self.assertEqual(blob["type"], "range")
        self.assertEqual(blob["units"], "V")
        self.assertEqual(blob["min"], 0)
        self.assertEqual(blob["max"], 1000)
        self.assertEqual(blob["order"], 3)
        self.assertEqual(blob["precision"], 3)
        self.assertIs(blob["readonly"], True)
        self.assertEqual(blob["title"], {"ru": "AO1", "en": "AO1"})

    def test_readonly_false_when_not_one(self):
        self.assertIs(control_meta_blob({"readonly": "0"})["readonly"], False)

    def test_empty_meta_gives_empty_blob(self):
        self.assertEqual(control_meta_blob({}), {})


class TestDeviceMetaBlob(unittest.TestCase):
    def test_driver_and_title_shape(self):
        blob = device_meta_blob({"name": "MR-02m 6DI/6DO", "driver": "modbus-rtu"})
        self.assertEqual(blob["driver"], "modbus-rtu")
        self.assertEqual(blob["title"], {"ru": "MR-02m 6DI/6DO",
                                         "en": "MR-02m 6DI/6DO"})

    def test_empty_meta_gives_empty_blob(self):
        self.assertEqual(device_meta_blob({}), {})

    def test_driver_only(self):
        self.assertEqual(device_meta_blob({"driver": "modbus-rtu"}),
                         {"driver": "modbus-rtu"})


if __name__ == "__main__":
    unittest.main()
