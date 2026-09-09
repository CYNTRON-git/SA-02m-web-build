#!/usr/bin/env python3
"""mqtt_bus_scan: type 120 / signature LED is type led, not unknown or MR-02m.

Two audit findings pinned here (2026-09-08): E9 — `signature_is_led` has a
shared home (sa02m_led) and a fallback copy for a board without the package;
which branch runs is forced explicitly in each test, and a real bug inside the
shared helper PROPAGATES instead of being swallowed into the copy. E17 — the
scan's `detect_type` orders SIGNATURE before Input reg 0, the flasher's rule
(`module_profiles.scan_type_code`): a crosstalk 120 on a shared line cannot
type an MR-02m as a strip.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(BRIDGE_DIR.parent / "sa02m-led"))

import mqtt_bus_scan as scan  # noqa: E402
from sa02m_led import led_mb2ws as lm  # noqa: E402


class TestSignatureIsLed(unittest.TestCase):
    def test_four_aliases(self):
        for sig in ("LED", "RGBW", "RGBWWS2812", "RGBW_WS2812"):
            self.assertTrue(scan.signature_is_led(sig), sig)

    def test_ledge_is_not_ours(self):
        self.assertFalse(scan.signature_is_led("ledGe"))
        self.assertFalse(scan.signature_is_led("LED-64"))

    def test_the_shared_home_answers_when_the_package_is_present(self):
        """Branch 1 (E9): the sa02m_led helper is what decides."""
        with mock.patch.object(lm, "signature_looks_like_led", return_value=True) as sh:
            self.assertTrue(scan.signature_is_led("ledGe"))  # copy would say False
        sh.assert_called_once_with("ledGe")

    def test_the_fallback_copy_answers_only_without_the_package(self):
        """Branch 2 (E9): ImportError → the scan's own copy, same verdicts."""
        with mock.patch.dict(sys.modules, {"sa02m_led.led_mb2ws": None}):
            for sig in ("LED", "RGBW", "RGBWWS2812", "RGBW_WS2812"):
                self.assertTrue(scan.signature_is_led(sig), sig)
            self.assertFalse(scan.signature_is_led("ledGe"))
            self.assertFalse(scan.signature_is_led(""))

    def test_a_bug_in_the_shared_home_is_not_swallowed(self):
        """E9: only ImportError selects the fallback; anything else is a real
        defect in the one home and must surface, not be masked by the copy."""
        with mock.patch.object(lm, "signature_looks_like_led",
                               side_effect=RuntimeError("shared helper broke")):
            with self.assertRaises(RuntimeError):
                scan.signature_is_led("LED")


class TestScanShortName(unittest.TestCase):
    def test_led_name(self):
        self.assertEqual(
            scan.scan_short_name("led", 120, "LED", 13, "LED"),
            "LED",
        )


def _fake_regs(*, input0, sig_text=None, holding1=None):
    """Patch read_holding/read_input for one detect_type() call.

    input0: value of Input reg 0 (None = no answer); sig_text: signature at
    holding 290 (None = no answer)."""
    sig_regs = None
    if sig_text is not None:
        # One ASCII char per register, low byte (decode_signature's shape).
        sig_regs = [ord(c) for c in sig_text[:12]] + [0] * (12 - len(sig_text[:12]))

    def read_holding(ser, addr, reg, count=1, timeout=0.08):
        if reg == 1:
            return holding1
        if reg == 290:
            return sig_regs
        return None

    def read_input(ser, addr, reg, count=1, timeout=0.08):
        if reg == 0 and input0 is not None:
            return [input0]
        return None

    return mock.patch.multiple(scan, read_holding=read_holding, read_input=read_input)


class TestDetectTypePrecedence(unittest.TestCase):
    def test_led_signature_wins_over_a_module_type_code(self):
        with _fake_regs(input0=1, sig_text="RGBW_WS2812"):
            kind, code, _name, sig = scan.detect_type(None, 13, read_signature=True)
        self.assertEqual((kind, code), ("led", scan.LED_TYPE_CODE))
        self.assertEqual(sig, "RGBW_WS2812")

    def test_a_module_signature_wins_over_a_crosstalk_120(self):
        """E17: Input reg 0 said 120 (a strip) but the EEPROM signature names
        an MR-02m 6DO8DI — the signature is the device's own, reg 0 can be a
        neighbour's answer on a shared line. Typed as the module."""
        with _fake_regs(input0=scan.LED_TYPE_CODE, sig_text="6DO8DI"):
            kind, code, name, sig = scan.detect_type(None, 5)
        self.assertEqual((kind, code, name), ("mr02m", 1, "6DO8DI"))
        self.assertEqual(sig, "6DO8DI")

    def test_120_with_a_led_signature_is_led(self):
        with _fake_regs(input0=scan.LED_TYPE_CODE, sig_text="LED"):
            kind, code, _name, _sig = scan.detect_type(None, 13)
        self.assertEqual((kind, code), ("led", scan.LED_TYPE_CODE))

    def test_120_without_a_signature_is_still_led(self):
        """No signature at all: reg 0 is the only evidence and it says strip."""
        with _fake_regs(input0=scan.LED_TYPE_CODE, sig_text=None):
            kind, code, _name, sig = scan.detect_type(None, 13)
        self.assertEqual((kind, code, sig), ("led", scan.LED_TYPE_CODE, ""))

    def test_120_with_an_unrelated_signature_is_led(self):
        """A signature that names neither a strip nor a known module family
        (a vendor string) does not override reg 0."""
        with _fake_regs(input0=scan.LED_TYPE_CODE, sig_text="ACME-1"):
            kind, _code, _name, sig = scan.detect_type(None, 13)
        self.assertEqual(kind, "led")
        self.assertEqual(sig, "ACME-1")


if __name__ == "__main__":
    unittest.main()
