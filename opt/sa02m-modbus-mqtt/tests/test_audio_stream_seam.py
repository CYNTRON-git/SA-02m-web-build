#!/usr/bin/env python3
"""Light-music seam: the audio daemon and the LED bridge name the same topics.

The daemon (opt/sa02m-audio/sa02m_audio/constants.py) writes
`<strip>/controls/<name>/on` for the stream sink of its style, the style's FX
at start, and the remembered controls at stop; the bridge
(opt/sa02m-modbus-mqtt/bridge_led.py) subscribes to its writable controls plus
STREAM_CONTROLS and FX_AUX_CONTROL. Both sides hold their own copy of the
names and nothing else ties them: renaming one side leaves each package's own
suite green while every stream frame lands on a topic nobody serves
(docs/contracts/led-mb2ws.md §4 «Поток светомузыки», docs/contracts/
audio-recorder.md «Светомузыка»).

The style → FX numbers are checked against the map the bridge packs FxAux 455
with (sa02m_led.led_mb2ws): a `music_level` style must run an effect whose 455
low byte is the 0..100 level/percent field the bridge writes, and the
`music_color` style must run Static, the effect that paints holding 434.

Every collection is asserted non-empty first: an emptied table on either side
fails here instead of comparing nothing to nothing.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# The bridge's sys.path and its runtime-dependency stubs (yaml, serial, paho)
# have one home: importing it is what lets bridge_led load without them.
import test_led_poller  # noqa: F401,E402

import bridge_led  # noqa: E402
from sa02m_led import controls as lc  # noqa: E402
from sa02m_led import led_mb2ws as lm  # noqa: E402

AUDIO_DIR = Path(__file__).resolve().parents[2] / "sa02m-audio"
sys.path.insert(0, str(AUDIO_DIR))
from sa02m_audio import constants as C  # noqa: E402
from sa02m_audio import lightshow  # noqa: E402

# The 455 low-byte layouts that carry a 0..100 field `music_level` can fill.
LEVEL_LAYOUTS = (lm.RGBW_FX_AUX_LOW_LEVEL, lm.RGBW_FX_AUX_LOW_PERCENT)


def _bridge_subscribed():
    """Every `<name>/on` the bridge acts on (LedPoller's subscribe set)."""
    return (set(lc.writable_names()) | set(bridge_led.STREAM_CONTROLS)
            | {bridge_led.FX_AUX_CONTROL})


class TestStreamControlNames(unittest.TestCase):

    def test_the_daemon_streams_to_exactly_the_bridge_sinks(self):
        daemon = set(C.LIGHT_MUSIC_STREAM_CONTROL.values())
        bridge = set(bridge_led.STREAM_CONTROLS)
        self.assertTrue(daemon, "audio LIGHT_MUSIC_STREAM_CONTROL is empty")
        self.assertTrue(bridge, "bridge STREAM_CONTROLS is empty")
        self.assertEqual(daemon, bridge)

    def test_the_stream_sinks_are_music_level_and_music_color(self):
        # Pinned by name as well: both sides renamed together would keep the
        # sets equal while breaking the contract and any deployed counterpart.
        self.assertEqual(set(bridge_led.STREAM_CONTROLS), {"music_level", "music_color"})

    def test_fx_aux_restore_control_is_the_same_name(self):
        self.assertEqual(C.FX_AUX_CONTROL, "fx_aux")
        self.assertEqual(bridge_led.FX_AUX_CONTROL, C.FX_AUX_CONTROL)

    def test_every_style_has_a_sink_and_an_fx(self):
        styles = set(C.LIGHT_MUSIC_STYLES)
        self.assertTrue(styles, "audio LIGHT_MUSIC_STYLES is empty")
        self.assertEqual(set(C.LIGHT_MUSIC_STREAM_CONTROL), styles)
        self.assertEqual(set(C.LIGHT_MUSIC_FX), styles)


class TestStyleEffects(unittest.TestCase):
    """The FX the daemon starts is one the bridge's stream write fits."""

    def _styles_streaming(self, control):
        out = [s for s, c in C.LIGHT_MUSIC_STREAM_CONTROL.items() if c == control]
        self.assertTrue(out, "no style streams %r" % control)
        return out

    def test_level_styles_run_an_effect_with_a_level_field(self):
        for style in self._styles_streaming("music_level"):
            fx = C.LIGHT_MUSIC_FX[style]
            spec = lm.rgbw_fx_aux_spec(fx)
            with self.subTest(style=style, fx=fx):
                self.assertIn(spec.low_kind, LEVEL_LAYOUTS)
                self.assertEqual((spec.low_min, spec.low_max),
                                 (0, bridge_led.MUSIC_LEVEL_MAX))

    def test_color_style_runs_static(self):
        for style in self._styles_streaming("music_color"):
            with self.subTest(style=style):
                self.assertEqual(C.LIGHT_MUSIC_FX[style], lm.RGBW_FX_MODE_STATIC)
        self.assertEqual(lc.control_row("color")[4], lm.MB2WS_USER_COLOR)

    def test_the_numbers_both_sides_name(self):
        # level 75 and equalizer 43 are the map's two level layouts; colour 0 is
        # Static. A renumbering on either side is a contract change, not a refactor.
        self.assertEqual(C.LIGHT_MUSIC_FX,
                         {C.LM_STYLE_LEVEL: 75, C.LM_STYLE_EQUALIZER: 43, C.LM_STYLE_COLOR: 0})
        self.assertEqual(lm.rgbw_fx_aux_spec(75).low_kind, lm.RGBW_FX_AUX_LOW_LEVEL)
        self.assertEqual(lm.rgbw_fx_aux_spec(43).low_kind, lm.RGBW_FX_AUX_LOW_PERCENT)
        self.assertEqual(lm.RGBW_FX_MODE_STATIC, 0)


class TestSessionWritesLandOnBridgeTopics(unittest.TestCase):
    """Start and restore writes go to controls the bridge subscribes to."""

    def test_start_writes(self):
        subscribed = _bridge_subscribed()
        for style in C.LIGHT_MUSIC_STYLES:
            names = [name for name, _ in lightshow.start_writes(style)]
            self.assertTrue(names)
            with self.subTest(style=style):
                self.assertLessEqual(set(names), subscribed)

    def test_snapshot_controls_are_published_and_restorable(self):
        snapshot = set(C.LED_SNAPSHOT_CONTROLS)
        self.assertTrue(snapshot, "audio LED_SNAPSHOT_CONTROLS is empty")
        published = set(lc.control_names()) | {bridge_led.FX_AUX_CONTROL}
        self.assertLessEqual(snapshot, published)
        full = {name: "1" for name in snapshot}
        restored = {name for name, _ in lightshow.restore_writes(C.LM_STYLE_LEVEL, full)}
        self.assertEqual(restored, snapshot)
        self.assertLessEqual(restored, _bridge_subscribed())


if __name__ == "__main__":
    unittest.main()
