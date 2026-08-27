"""Unit tests for the MQTT topic-picker inventory (topics.py yaml expansion).

Real bridge yaml entries for DTV / CE-02m-3 carry no controls/Channels key —
without the type-based expansion the picker never lists their topics and the
sensor feature is unusable on a real board.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.config import topics  # noqa: E402


class TestTopicsFromYaml(unittest.TestCase):
    def test_dtv_with_sensors_present(self):
        doc = {
            "devices": [
                {
                    "id": "dtv-COM3-1",
                    "type": "dtv",
                    "sensors_present": ["temp_bme680", "humidity_bme680"],
                }
            ]
        }
        out = topics._topics_from_yaml(doc)
        self.assertIn("/devices/dtv-COM3-1/controls/temp_bme680", out)
        self.assertIn("/devices/dtv-COM3-1/controls/humidity_bme680", out)
        # Actuators always listed for a DTV.
        self.assertIn("/devices/dtv-COM3-1/controls/buzzer", out)
        self.assertIn("/devices/dtv-COM3-1/controls/leds", out)
        # Only the declared sensors — not the default set.
        self.assertNotIn("/devices/dtv-COM3-1/controls/temp_ds18b20", out)

    def test_dtv_without_sensors_present_uses_default_set(self):
        doc = {"devices": [{"id": "dtv-COM3-1", "type": "dtv"}]}
        out = topics._topics_from_yaml(doc)
        for name in topics.DTV_DEFAULT_CONTROLS:
            self.assertIn("/devices/dtv-COM3-1/controls/%s" % name, out)
        self.assertIn("/devices/dtv-COM3-1/controls/buzzer", out)

    def test_ce02m3_static_set(self):
        doc = {"devices": [{"id": "ce02m3-COM2-14", "type": "ce02m3"}]}
        out = topics._topics_from_yaml(doc)
        expected = {
            "/devices/ce02m3-COM2-14/controls/%s" % name
            for name in topics.CE02M3_CONTROLS
        }
        self.assertTrue(expected.issubset(set(out)))
        self.assertIn("/devices/ce02m3-COM2-14/controls/voltage_a", out)
        self.assertIn("/devices/ce02m3-COM2-14/controls/power_total", out)
        self.assertIn("/devices/ce02m3-COM2-14/controls/current_n", out)

    def test_controls_style_device_still_parsed(self):
        doc = {
            "devices": [
                {
                    "id": "mr02m-COM1-5",
                    "type": "mr02m",
                    "controls": ["do_1", "di_1"],
                }
            ]
        }
        out = topics._topics_from_yaml(doc)
        self.assertEqual(
            out,
            [
                "/devices/mr02m-COM1-5/controls/di_1",
                "/devices/mr02m-COM1-5/controls/do_1",
            ],
        )

    def test_non_dict_doc_empty(self):
        self.assertEqual(topics._topics_from_yaml(None), [])
        self.assertEqual(topics._topics_from_yaml([]), [])



class TestMr02mChannelExpansion(unittest.TestCase):
    """Pins the 1.0.6.16 gap: a `type: mr02m` entry carries `channels`, not
    `controls`, so the picker listed nothing for it — the bench 12AI module's
    live temperature probes (ai_7..ai_12) could not be bound to Alice at all."""

    def test_channels_expand_per_kind(self):
        doc = {"devices": [{
            "id": "mr02m-COM4-12", "type": "mr02m",
            "channels": {
                "ai": [{"ch": 1, "enabled": True}, {"ch": 2}, {"ch": 3, "enabled": False}],
                "sys": [{"ch": 1}],
            },
        }]}
        out = topics._topics_from_yaml(doc)
        self.assertIn("/devices/mr02m-COM4-12/controls/ai_1", out)
        self.assertIn("/devices/mr02m-COM4-12/controls/ai_2", out)
        self.assertNotIn("/devices/mr02m-COM4-12/controls/ai_3", out)  # disabled
        self.assertNotIn("/devices/mr02m-COM4-12/controls/sys_1", out)  # unknown kind
        self.assertIn("/devices/mr02m-COM4-12/controls/mcu_temp", out)

    def test_explicit_controls_are_not_second_guessed(self):
        doc = {"devices": [{
            "id": "mr02m-COM1-5", "type": "mr02m",
            "controls": ["do_1"],
            "channels": {"do": [{"ch": 9}]},
        }]}
        out = topics._topics_from_yaml(doc)
        self.assertEqual(out, ["/devices/mr02m-COM1-5/controls/do_1"])


if __name__ == "__main__":
    unittest.main()
