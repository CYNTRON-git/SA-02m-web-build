#!/usr/bin/env python3
"""`rgbw_fx_aux_word_ok` — the gate on a raw FxAux 455 restore (1.0.7.10).

The light-music daemon snapshots the strip's 455 word at session start and
writes it back exactly through the bridge's `fx_aux/on` at stop
(docs/contracts/led-mb2ws.md §4 «Поток светомузыки»). The rule is the register
width and nothing per effect: a word the 16-bit frame cannot carry would be
silently truncated, so it is refused before the bus; anything that fits is
written whatever FX runs, because a restore must not drop the bytes the
running effect's layout happens not to read.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_led import led_mb2ws as lm  # noqa: E402


class TestFxAuxWordOk(unittest.TestCase):
    def test_every_sixteen_bit_word_is_accepted(self):
        for word in (0, 1, 0x7F, 0x80, 0xFF, 0x0100, 0x8000, 0xABCD, 0xFFFF):
            self.assertTrue(lm.rgbw_fx_aux_word_ok(word), hex(word))

    def test_the_edges_of_the_register_width_are_refused(self):
        """One past either end: a negative or a 17-bit word would be truncated."""
        for word in (-1, 0x10000, 0x1FFFF, -0x10000):
            self.assertFalse(lm.rgbw_fx_aux_word_ok(word), word)

    def test_the_rule_is_width_only_not_per_effect(self):
        """No `fx_id` parameter: a word another effect's layout would clamp
        (FX 0 ignores the high byte, FX 43 caps the percent at 100) is still a
        legal raw restore."""
        word = (0xAB << 8) | 0xCD
        self.assertTrue(lm.rgbw_fx_aux_word_ok(word))
        self.assertNotEqual(lm.rgbw_fx_aux_compose(0, low=0xCD, high=0xAB), word)
        self.assertNotEqual(
            lm.rgbw_fx_aux_compose(43, low=0xCD, high=0xAB), word)

    def test_only_a_real_int_is_accepted(self):
        """`type(word) is int`: a bool, a float, a numeric string or None is not
        a register word, even when its value would fit."""
        for word in (True, False, 1.0, 0.0, "1", "0", None, b"\x01", [1]):
            self.assertFalse(lm.rgbw_fx_aux_word_ok(word), repr(word))


if __name__ == "__main__":
    unittest.main()
