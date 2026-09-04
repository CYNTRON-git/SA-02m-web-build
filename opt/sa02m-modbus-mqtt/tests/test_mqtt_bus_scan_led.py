#!/usr/bin/env python3
"""mqtt_bus_scan: type 120 / signature LED is type led, not unknown or MR-02m."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(BRIDGE_DIR.parent / "sa02m-led"))

import mqtt_bus_scan as scan  # noqa: E402


class TestSignatureIsLed(unittest.TestCase):
    def test_four_aliases(self):
        for sig in ("LED", "RGBW", "RGBWWS2812", "RGBW_WS2812"):
            self.assertTrue(scan.signature_is_led(sig), sig)

    def test_ledge_is_not_ours(self):
        self.assertFalse(scan.signature_is_led("ledGe"))
        self.assertFalse(scan.signature_is_led("LED-64"))


class TestScanShortName(unittest.TestCase):
    def test_led_name(self):
        self.assertEqual(
            scan.scan_short_name("led", 120, "LED", 13, "LED"),
            "LED",
        )


if __name__ == "__main__":
    unittest.main()
