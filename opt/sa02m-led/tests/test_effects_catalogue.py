# -*- coding: utf-8 -*-
"""docs/contracts/led-effects.json == the map's effect ids/groups == led.js names.

The JSON is a published copy (voice commands, integrations) of what two homes
own: ids and groups belong to led_mb2ws_map (RGBW_FX_MODE_COUNT,
RGBW_FX_GROUP_RANGES), the Russian names to the flasher window's LED_T in
led.js. Every field is compared; a parse that finds fewer names than the map
has effects FAILS, so a changed led.js layout cannot pass by matching nothing.
LED_EFFECTS_JSON=<file> judges another copy of the catalogue (the RED run).
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_led import led_mb2ws_map as lmap  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
CATALOGUE = Path(os.environ.get("LED_EFFECTS_JSON")
                 or REPO_ROOT / "docs" / "contracts" / "led-effects.json")
LED_JS = REPO_ROOT / "www" / "network_config" / "static" / "js" / "flasher" / "led.js"

_FX_NAME = re.compile(r"^\s*'rgbw_fx_(\d{2})':\s*'([^']*)',", re.M)
_GROUP_NAME = re.compile(r"^\s*'(rgbw_fx_grp_[a-z]+)':\s*'([^']*)',", re.M)


def _led_js_names():
    text = LED_JS.read_text(encoding="utf-8")
    fx = {int(n): name for n, name in _FX_NAME.findall(text)}
    groups = dict(_GROUP_NAME.findall(text))
    return fx, groups


class TestEffectsCatalogue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = json.loads(CATALOGUE.read_text(encoding="utf-8"))
        cls.fx_names, cls.group_names = _led_js_names()

    def test_led_js_parse_is_not_vacuous(self):
        self.assertGreaterEqual(len(self.fx_names), lmap.RGBW_FX_MODE_COUNT)
        self.assertGreaterEqual(len(self.group_names), len(lmap.RGBW_FX_GROUP_RANGES))

    def test_header(self):
        self.assertEqual(self.doc["control"], "effect")
        self.assertEqual(self.doc["device_type"], 120)
        self.assertEqual(self.doc["sound_reactive"], [])

    def test_ids_are_exactly_the_map_range(self):
        ids = [e["id"] for e in self.doc["effects"]]
        self.assertEqual(ids, list(range(lmap.RGBW_FX_MODE_COUNT)))

    def test_every_effect_matches_led_js_and_the_map(self):
        for e in self.doc["effects"]:
            with self.subTest(id=e["id"]):
                self.assertEqual(set(e), {"id", "name_ru", "group"})
                self.assertEqual(e["name_ru"], self.fx_names[e["id"]])
                self.assertEqual(e["group"], lmap.fx_group_for_id(e["id"]))

    def test_groups_match_the_map_ranges_and_led_js(self):
        got = [(g["id"], g["first"], g["last"]) for g in self.doc["groups"]]
        want = [(gid, first, last) for (gid, _key, first, last) in lmap.RGBW_FX_GROUP_RANGES]
        self.assertEqual(got, want)
        keys = {gid: key for (gid, key, _first, _last) in lmap.RGBW_FX_GROUP_RANGES}
        for g in self.doc["groups"]:
            with self.subTest(group=g["id"]):
                self.assertEqual(set(g), {"id", "name_ru", "first", "last"})
                self.assertEqual(g["name_ru"], self.group_names[keys[g["id"]]])


if __name__ == "__main__":
    unittest.main()
