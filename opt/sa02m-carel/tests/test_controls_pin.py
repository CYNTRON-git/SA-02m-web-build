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


class TestControlsPin(unittest.TestCase):
    def test_the_alice_copy_lists_exactly_these_controls(self):
        self.assertTrue(TOPICS_PY.is_file(), "topics.py not found at %s" % TOPICS_PY)
        text = TOPICS_PY.read_text(encoding="utf-8")
        m = re.search(r"CAREL_CONTROLS = \(([^)]*)\)", text, re.S)
        self.assertIsNotNone(m, "topics.py no longer defines CAREL_CONTROLS")
        copied = tuple(re.findall(r'"([a-z0-9_]+)"', m.group(1)))
        self.assertEqual(copied, cc.control_names(),
                         "the alice picker copy has drifted from sa02m_carel.controls")


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
