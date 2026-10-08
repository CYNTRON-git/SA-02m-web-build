# -*- coding: utf-8 -*-
"""The frozen copy of the control names in the alice package matches this home.

`sa02m_alice.config.topics.CAREL_CONTROLS` is a deliberate duplicate: the alice
client is deployed as its own tree and cannot import this package at runtime, so
the binding picker carries its own list. Duplication without a pin is drift —
rename a control here and the picker silently stops offering it, which reads to
the operator as "the unit has no such reading". The copy is read as TEXT, not
imported, so this test needs nothing from the alice package.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_carel import carel_ahu as ca  # noqa: E402
from sa02m_carel import controls as cc  # noqa: E402

TOPICS_PY = (Path(__file__).resolve().parents[3]
             / "opt/sa02m-alice/sa02m_alice/config/topics.py")
MQTT_JS = (Path(__file__).resolve().parents[3]
           / "www/network_config/static/js/mqtt.js")


class TestControlsPin(unittest.TestCase):
    def test_the_alice_copy_lists_exactly_these_controls(self):
        self.assertTrue(TOPICS_PY.is_file(), "topics.py not found at %s" % TOPICS_PY)
        text = TOPICS_PY.read_text(encoding="utf-8")
        m = re.search(r"CAREL_CONTROLS = \(([^)]*)\)", text, re.S)
        self.assertIsNotNone(m, "topics.py no longer defines CAREL_CONTROLS")
        copied = tuple(re.findall(r'"([a-z0-9_]+)"', m.group(1)))
        self.assertEqual(copied, cc.control_names(),
                         "the alice picker copy has drifted from sa02m_carel.controls")

    def test_the_mqtt_page_lists_exactly_these_controls(self):
        """The MQTT accordion cannot import this package. A missing name is
        the empty Carel channel list: the page draws only what this table names."""
        self.assertTrue(MQTT_JS.is_file(), "mqtt.js not found at %s" % MQTT_JS)
        text = MQTT_JS.read_text(encoding="utf-8")
        m = re.search(r"const CAREL_CONTROLS = \[([\s\S]*?)\n\];", text)
        self.assertIsNotNone(m, "mqtt.js no longer defines CAREL_CONTROLS")
        copied = tuple(re.findall(r"name:\s*'([a-z0-9_]+)'", m.group(1)))
        self.assertEqual(copied, cc.control_names(),
                         "the MQTT channel list has drifted from sa02m_carel.controls")
        # mqtt_set.cgi refuses these three (docs/contracts/mqtt-set-endpoint.md).
        # A write box here would toast bad_control and look like a dead channel.
        for name in ("net_enable", "sys_mode", "fan_exhaust"):
            row = re.search(r"\{name:'%s'[^}]*\}" % name, m.group(1))
            self.assertIsNotNone(row, "mqtt.js dropped %s" % name)
            self.assertRegex(row.group(0), r"write:\s*''")


class TestAlarmResetButton(unittest.TestCase):
    """`alarm_reset` is the one pushbutton: writable on both families, no
    units, and its coil is never the uAria local-terminal coil."""

    def test_a_writable_pushbutton_on_both_families(self):
        rows = {row[0]: row for row in cc.CONTROLS}
        self.assertIn("alarm_reset", rows)
        _name, wb_type, units, readonly, _fams = rows["alarm_reset"]
        self.assertEqual(wb_type, "pushbutton")
        self.assertEqual(units, "")
        self.assertFalse(readonly)
        for family in cc.BOTH:
            self.assertIn("alarm_reset", cc.writable_names(family))

    def test_the_coil_it_pulses_is_never_the_local_terminal(self):
        for family in cc.BOTH:
            self.assertNotEqual(ca.alarm_reset_coil(family), ca.COIL_UARIA_LOCAL)


class TestPlantStateWords(unittest.TestCase):
    """The words the bridge publishes are the words a binding may declare.

    `plant_state` is an event property: the Alice/cloud converter forwards a
    payload ONLY when it is in the declared value set, and drops it silently
    otherwise. A binding declaring `running`/`stopped` against a bridge
    publishing `run`/`stop` therefore shows an empty tile with no error
    anywhere — found on bench 1.135, 2026-09-03.
    """

    def test_the_alice_validator_allows_exactly_these_words(self):
        models_py = (Path(__file__).resolve().parents[3]
                     / "opt/sa02m-alice/sa02m_alice/config/models.py")
        text = models_py.read_text(encoding="utf-8")
        m = re.search(r'"plant_state": frozenset\(\(([^)]*)\)\)', text)
        self.assertIsNotNone(m, "models.py no longer declares plant_state")
        declared = set(re.findall(r'"([a-z_]+)"', m.group(1)))
        self.assertEqual(declared, {ca.PLANT_RUN, ca.PLANT_STOP, ca.PLANT_ALARM})


if __name__ == "__main__":
    unittest.main()
