#!/usr/bin/env python3
"""A board without `/opt/sa02m-carel` keeps its Alice client.

The cloud fan vocabulary lives in the shared Carel package (one home with the
register map it is derived from), which is installed by `04-flasher.sh`,
`05-mqtt.sh`, `06-alice.sh` and `update-www-only.sh`. A board where none of
them has run since the package existed has no copy — and raising out of a
catalogue build would take down EVERY device on the account for one missing
optional control.

So the import is fail-soft, and these cases are the proof: the failure is
logged once at WARNING (never swallowed), the fan control is withheld, the
family-true float rows — which need no package at all — still narrow, and
nothing raises. It self-heals on the next catalogue build after the package
lands.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_alice.client import converters  # noqa: E402
from sa02m_alice.common import carel_import  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import ahu_status, inventory  # noqa: E402

UARIA = "carel-COM3-2"
_UARIA_CONTROLS = {
    "unit_on": "1", "supply_temp": "19.5", "return_water_temp": "44.1",
    "heat_valve": "37", "fan_step": "7", "plant_state": "run",
}


def _no_package():
    """`sa02m_carel` unimportable, whatever is on sys.path in this run.

    A `None` entry in sys.modules makes the import raise ImportError — the
    same failure an uninstalled package produces, and the only way to stage
    it once another test has already put the repo copy on sys.path.
    """
    return mock.patch.dict(sys.modules, {"sa02m_carel": None,
                                         "sa02m_carel.carel_fan": None})


def _carel_device(did: str, mid: str) -> dict:
    return {
        "id": did,
        "name": "Карел",
        "type": "devices.types.ventilation",
        "icon": "fan",
        "capabilities": [{
            "type": "devices.capabilities.on_off",
            "mqtt": "/devices/%s/controls/unit_on" % mid,
            "retrievable": True, "reportable": True,
            "parameters": {"instance": "on"},
        }],
        "properties": [{
            "type": "devices.properties.float",
            "mqtt": "/devices/%s/controls/supply_temp" % mid,
            "retrievable": True, "reportable": True,
            "parameters": {"instance": "temperature",
                           "unit": "unit.temperature.celsius"},
        }],
    }


class TestTheShimDegradesLoudlyAndOnce(unittest.TestCase):
    def setUp(self):
        carel_import._reset_for_tests()
        self.addCleanup(carel_import._reset_for_tests)

    def test_a_missing_package_answers_none_instead_of_raising(self):
        with _no_package():
            with self.assertLogs(carel_import.log.name, level="WARNING"):
                self.assertIsNone(carel_import.carel_fan())

    def test_the_warning_is_logged_once_per_process_not_per_build(self):
        """A catalogue build runs on every document reload; a per-build log
        line would bury the journal on a board that simply lacks the package.
        """
        with _no_package():
            with self.assertLogs(carel_import.log.name, level="WARNING") as first:
                carel_import.carel_fan()
            self.assertEqual(len(first.output), 1, first.output)
            with mock.patch.object(carel_import.log, "warning") as again:
                for _ in range(5):
                    self.assertIsNone(carel_import.carel_fan())
                self.assertEqual(again.call_args_list, [])

    def test_the_package_is_found_when_it_is_there(self):
        """Non-vacuity: if this could not import the real package, every case
        above would pass for the wrong reason."""
        module = carel_import.carel_fan()
        self.assertIsNotNone(module, "the repo copy of sa02m_carel is not importable")
        self.assertEqual(module.mqtt_control("uaria"), "fan_step")


class TestTheCatalogueSurvivesWithoutThePackage(unittest.TestCase):
    def setUp(self):
        carel_import._reset_for_tests()
        self.addCleanup(carel_import._reset_for_tests)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ,
                              {inventory.LIVE_CACHE_DIR_ENV: self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        with open(os.path.join(self._tmp.name, "%s.json" % UARIA), "w",
                  encoding="utf-8") as fh:
            json.dump({"ok": True, "device": UARIA, "source": "cache",
                       "controls": _UARIA_CONTROLS, "units": {}, "errors": {},
                       "sensor_types": {}, "ts": 1.0}, fh)

    def test_rows_still_narrow_and_only_the_control_is_withheld(self):
        doc = {"rooms": [], "devices": [_carel_device("ua", UARIA)]}
        with _no_package():
            out = ahu_status.prepare_catalogue_doc(doc)
        dev = out["devices"][0]
        insts = [(p.get("parameters") or {}).get("instance")
                 for p in dev["properties"]]
        # The narrowing needs no package: it is the control key set, not the map.
        self.assertIn("fan_step", insts)
        self.assertNotIn("fan_speed", insts)
        # …and the one thing that DOES need the map is withheld, not faked.
        self.assertEqual([c for c in dev["capabilities"]
                          if c["type"] == "devices.capabilities.mode"], [])


class TestTheConvertersDeclineWithoutThePackage(unittest.TestCase):
    def setUp(self):
        carel_import._reset_for_tests()
        self.addCleanup(carel_import._reset_for_tests)

    def test_reading_yields_no_block_rather_than_a_fabricated_word(self):
        with _no_package():
            self.assertIsNone(converters.capability_mqtt_to_yandex(
                "devices.capabilities.mode", "7",
                {"instance": "fan_speed"}, family="uaria"))

    def test_writing_is_refused_rather_than_guessed(self):
        with _no_package():
            payload, err = converters.capability_yandex_to_mqtt(
                "devices.capabilities.mode",
                {"instance": "fan_speed", "value": "high"},
                parameters={"instance": "fan_speed"}, family="uaria")
        self.assertIsNone(payload)
        self.assertEqual(err, C.ERR_INVALID_ACTION)


if __name__ == "__main__":
    unittest.main()
