#!/usr/bin/env python3
"""C3 (1.0.6.51): the Carel setpoint range Alice sees is the family's real one.

The «Умный дом» window writes the setpoint binding as `range` / `temperature`
0..99 for every Carel (smarthome.js SH_VENT kinds). A uAria accepts 0..50
(`sa02m_carel.controls.SETPOINT_RANGE`, the clamps carel_ahu applies), so
Alice offered 51..99 on a unit that silently clamps them to 50 —
docs/contracts/carel-ahu.md §6 promises «crst 0..99, uaria 0..50, шаг 0,5».

The catalogue build narrows the stored range to the family's bounds, in
memory only: an unknown family, a narrower stored range (never widen), and a
board without the shared Carel package all leave the stored range as is.
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_alice.common import carel_import  # noqa: E402
from sa02m_alice.config.ahu_status import ensure_ahu_cloud_status  # noqa: E402

MID = "carel-COM3-2"
RANGE = "devices.capabilities.range"


def _doc(rng):
    return {"rooms": [], "devices": [{
        "id": "carel-uaria", "name": "uAria",
        "type": "devices.types.ventilation",
        "capabilities": [
            {"type": "devices.capabilities.on_off",
             "mqtt": "/devices/%s/controls/unit_on" % MID},
            {"type": RANGE,
             "parameters": {"instance": "temperature",
                            "unit": "unit.temperature.celsius",
                            "range": dict(rng)},
             "mqtt": "/devices/%s/controls/setpoint" % MID},
        ],
        "properties": [],
    }]}


def _setpoint_range(doc):
    for cap in doc["devices"][0]["capabilities"]:
        if cap["type"] == RANGE:
            return cap["parameters"]["range"]
    raise AssertionError("no setpoint binding")


def _build(rng, family):
    doc = _doc(rng)
    ensure_ahu_cloud_status(doc, {}, {MID: family})
    return _setpoint_range(doc)


WINDOW = {"min": 0, "max": 99, "precision": 0.5}


class TestCarelSetpointRange(unittest.TestCase):
    def setUp(self):
        carel_import._reset_for_tests()
        self.addCleanup(carel_import._reset_for_tests)

    def test_uaria_is_narrowed_to_0_50(self):
        self.assertEqual(_build(WINDOW, "uaria"),
                         {"min": 0.0, "max": 50.0, "precision": 0.5})

    def test_crst_keeps_0_99(self):
        self.assertEqual(_build(WINDOW, "crst"),
                         {"min": 0.0, "max": 99.0, "precision": 0.5})

    def test_unknown_family_leaves_the_stored_range(self):
        self.assertEqual(_build(WINDOW, None), WINDOW)

    def test_a_narrower_stored_range_is_never_widened(self):
        narrow = {"min": 16, "max": 30, "precision": 1}
        self.assertEqual(_build(narrow, "uaria"), narrow)

    def test_the_stored_document_is_not_the_one_narrowed(self):
        # prepare_catalogue_doc hands ensure_ahu_cloud_status a deep copy;
        # the dict passed in here IS that copy, the input fixture is not.
        src = _doc(WINDOW)
        before = copy.deepcopy(src)
        work = copy.deepcopy(src)
        ensure_ahu_cloud_status(work, {}, {MID: "uaria"})
        self.assertEqual(src, before)

    def test_without_the_carel_package_the_range_is_untouched(self):
        with mock.patch.dict(sys.modules, {"sa02m_carel": None,
                                           "sa02m_carel.carel_fan": None,
                                           "sa02m_carel.controls": None}):
            carel_import._reset_for_tests()
            with self.assertLogs("sa02m_alice.common.carel_import",
                                 level="WARNING"):
                self.assertEqual(_build(WINDOW, "uaria"), WINDOW)

    def test_a_second_build_is_a_no_op(self):
        doc = _doc(WINDOW)
        ensure_ahu_cloud_status(doc, {}, {MID: "uaria"})
        again = copy.deepcopy(doc)
        ensure_ahu_cloud_status(again, {}, {MID: "uaria"})
        self.assertEqual(_setpoint_range(again), _setpoint_range(doc))


if __name__ == "__main__":
    unittest.main()
