"""Light music on the strip (sa02m_audio.lightshow; contract §Светомузыка,
led-mb2ws.md §Поток светомузыки): the register value per style, the writes
that switch the strip into a style (default: the level bar, LED effect 75) and
back, and the Sink — 16 kHz shared-stream blocks → 50 ms frames → at most the
configured updates/s, a keep-alive write at least every 2 s. Synthetic
signals and a fake monotonic clock only."""

from __future__ import annotations

import math
import re
import threading
import unittest
from array import array

from sa02m_audio import config as conf_mod
from sa02m_audio import constants as C
from sa02m_audio import lightmusic as L
from sa02m_audio import lightshow as S

FRAME_S = 0.05


def tone16(freq_hz, amp, start, n):
    return array("h", [int(round(amp * math.sin(2.0 * math.pi * freq_hz * (start + i) / 16000)))
                       for i in range(n)]).tobytes()


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Recorder:
    """The sink's publish and prelude, in call order."""

    def __init__(self) -> None:
        self.calls = []

    def publish(self, value):
        self.calls.append(("publish", value))

    def prelude(self):
        self.calls.append(("prelude", None))

    @property
    def values(self):
        return [v for kind, v in self.calls if kind == "publish"]


def run_sink(sink, clock, blocks):
    """Feed one 50 ms block per frame time; returns the times of the sends."""
    times = []
    for block in blocks:
        before = sink.frames
        sink.feed(block)
        if sink.frames > before:
            times.append(clock.t)
        clock.t += FRAME_S
    return times


class StyleTest(unittest.TestCase):
    def test_default_style_is_the_level_bar_on_effect_75(self):
        self.assertEqual(C.LIGHT_MUSIC_DEFAULT_STYLE, C.LM_STYLE_LEVEL)
        self.assertEqual(conf_mod.RecorderConfig().light_music_style, C.LM_STYLE_LEVEL)
        self.assertEqual(C.LIGHT_MUSIC_FX[C.LIGHT_MUSIC_DEFAULT_STYLE], 75)
        self.assertEqual(C.LIGHT_MUSIC_STREAM_CONTROL[C.LIGHT_MUSIC_DEFAULT_STYLE], "music_level")
        self.assertEqual(S.start_writes(C.LIGHT_MUSIC_DEFAULT_STYLE), [("power", "1"), ("effect", "75")])

    def test_colour_wash_and_equaliser_stay_selectable(self):
        self.assertEqual(set(C.LIGHT_MUSIC_STYLES), {"level", "color", "equalizer"})
        self.assertEqual(S.start_writes(C.LM_STYLE_EQUALIZER), [("power", "1"), ("effect", "43")])
        self.assertEqual(S.start_writes(C.LM_STYLE_COLOR), [("power", "1"), ("effect", "0")])
        self.assertEqual(C.LIGHT_MUSIC_STREAM_CONTROL[C.LM_STYLE_EQUALIZER], "music_level")
        self.assertEqual(C.LIGHT_MUSIC_STREAM_CONTROL[C.LM_STYLE_COLOR], "music_color")

    def test_power_goes_before_the_effect(self):
        """The bridge's power/on 1 resets FX to 0: the style's FX goes after."""
        for style in C.LIGHT_MUSIC_STYLES:
            with self.subTest(style=style):
                self.assertEqual([c for c, _ in S.start_writes(style)], ["power", "effect"])

    def test_encode_per_style(self):
        b = L.Bands(1.0, 0.5, 0.0, 0.42)
        self.assertEqual(S.encode(C.LM_STYLE_LEVEL, b), "42")
        self.assertEqual(S.encode(C.LM_STYLE_EQUALIZER, b), "42")
        self.assertEqual(S.encode(C.LM_STYLE_COLOR, b), "#FF4000")
        self.assertEqual(S.encode(C.LM_STYLE_COLOR, L.Bands(0, 0, 0, 0)), "#000000")
        self.assertRegex(S.encode(C.LM_STYLE_LEVEL, L.Bands(0, 0, 0, 1.0)), r"^(100|[1-9]?[0-9])\Z")


class RestoreWritesTest(unittest.TestCase):
    FULL = {"power": "0", "effect": "12", "speed": "128", "color": "#102030", "fx_aux": "513",
            "brightness": "200", "scene_source": "manual"}

    def test_order_power_effect_fx_aux_then_the_rest_scene_source_last(self):
        self.assertEqual(S.restore_writes(C.LM_STYLE_LEVEL, self.FULL), [
            ("power", "0"), ("effect", "12"), ("fx_aux", "513"), ("speed", "128"),
            ("color", "#102030"), ("brightness", "200"), ("scene_source", "manual")])

    def test_a_control_never_seen_is_left_alone(self):
        self.assertEqual(S.restore_writes(C.LM_STYLE_LEVEL, {"effect": "3", "brightness": "9"}),
                         [("effect", "3"), ("brightness", "9")])
        self.assertEqual(S.restore_writes(C.LM_STYLE_LEVEL, {}), [])

    def test_fx_aux_only_as_an_exact_16_bit_word(self):
        for raw, kept in (("0", True), ("65535", True), ("65536", False), ("070", False),
                          ("-1", False), ("1e3", False), ("12\n", False), ("", False), ("abc", False)):
            with self.subTest(raw=raw):
                writes = S.restore_writes(C.LM_STYLE_LEVEL, {"fx_aux": raw})
                self.assertEqual(writes, [("fx_aux", raw)] if kept else [])


class SinkFramingTest(unittest.TestCase):
    def test_a_frame_is_50_ms_of_the_16k_stream_and_one_stream_read(self):
        self.assertEqual(S.Sink.FRAME_BYTES, 1600)
        self.assertEqual(S.Sink.FRAME_BYTES, C.STREAM_BLOCK)
        sink = S.Sink(C.LM_STYLE_LEVEL, 8, lambda v: None)
        self.assertEqual(sink._analyzer.rate, C.SAMPLE_RATE)
        self.assertEqual(C.SAMPLE_RATE, 16000)

    def test_uneven_reads_are_reassembled_into_frames(self):
        sink = S.Sink(C.LM_STYLE_LEVEL, 8, lambda v: None, mono=Clock())
        data = tone16(440, 8000, 0, 16000)  # 1 s = 32000 bytes
        for size in (1, 999, 1600, 3201, 7, 10000):
            chunk, data = data[:size], data[size:]
            sink.feed(chunk)
        sink.feed(data)
        self.assertEqual(sink.frames, 20)

    def test_16k_treble_is_blue_after_decimation(self):
        """The Sink analyses the shared 16 kHz stream decimated by 2: a
        3 kHz tone reads as treble (blue), not as a 1.5 kHz mid."""
        rec = Recorder()
        clock = Clock()
        sink = S.Sink(C.LM_STYLE_COLOR, 10, rec.publish, mono=clock)
        run_sink(sink, clock, [tone16(3000, 8000, f * 800, 800) for f in range(40)])
        last = rec.values[-1]
        self.assertRegex(last, r"^#[0-9A-F]{6}\Z")
        r, g, b = (int(last[i:i + 2], 16) for i in (1, 3, 5))
        self.assertEqual(b, 255, last)
        self.assertLess(r, 64, last)
        self.assertGreater(b, g, last)


class SinkRateTest(unittest.TestCase):
    def changing_blocks(self, frames):
        """A picture that changes every frame: loud and silent frames alternate."""
        silence = bytes(1600)
        return [tone16(200, 12000, f * 800, 800) if f % 2 == 0 else silence for f in range(frames)]

    def sends(self, rate_hz, blocks):
        clock = Clock()
        rec = Recorder()
        sent_at = []
        sink = S.Sink(C.LM_STYLE_LEVEL, rate_hz,
                      lambda v: (rec.publish(v), sent_at.append(clock.t)), mono=clock)
        run_sink(sink, clock, blocks)
        return sink, rec, sent_at

    def test_updates_never_exceed_the_configured_rate(self):
        for rate_hz in (1, 2, 5, 8, 10):
            with self.subTest(rate_hz=rate_hz):
                sink, _rec, sent_at = self.sends(rate_hz, self.changing_blocks(20 * 10))
                gaps = [b - a for a, b in zip(sent_at, sent_at[1:])]
                self.assertGreaterEqual(min(gaps), 1.0 / rate_hz - 1e-6)
                self.assertLessEqual(len(sent_at), rate_hz * 10 + 1)
                self.assertLessEqual(sink.update_hz(), rate_hz)

    def test_rate_out_of_range_is_clamped_to_1_10(self):
        sink, _rec, sent_at = self.sends(40, self.changing_blocks(20 * 10))
        self.assertLessEqual(len(sent_at), 10 * 10 + 1)
        self.assertEqual(sink._limiter.max_hz, 10.0)
        sink, _rec, sent_at = self.sends(0, self.changing_blocks(20 * 10))
        self.assertLessEqual(len(sent_at), 1 * 10 + 1)

    def test_a_steady_picture_is_rewritten_at_least_every_2_s(self):
        """Silence = a steady `0`: rewritten at least every 2 s (the strip
        saves 455 3–4 s after the last write), never more often than that."""
        sink, rec, sent_at = self.sends(10, [bytes(1600)] * (20 * 20))
        self.assertTrue(rec.values)
        self.assertEqual(set(rec.values), {"0"})
        gaps = [b - a for a, b in zip(sent_at, sent_at[1:])]
        self.assertGreaterEqual(len(gaps), 9)
        self.assertLessEqual(max(gaps), 2.0 + 1e-6)
        self.assertGreaterEqual(min(gaps), 2.0 - FRAME_S - 1e-6)

    def test_tick_keeps_the_keepalive_going_without_frames(self):
        clock = Clock()
        rec = Recorder()
        sink = S.Sink(C.LM_STYLE_LEVEL, 10, rec.publish, mono=clock)
        sink.tick()
        self.assertEqual(rec.values, [])  # nothing to repeat yet
        sink.feed(bytes(1600))
        self.assertEqual(rec.values, ["0"])
        clock.t += 1.0
        sink.tick()
        self.assertEqual(rec.values, ["0"])
        clock.t += 1.0
        sink.tick()
        self.assertEqual(rec.values, ["0", "0"])

    def test_update_hz_counts_the_writes_actually_sent(self):
        sink, _rec, sent_at = self.sends(5, self.changing_blocks(20 * 10))
        self.assertAlmostEqual(sink.update_hz(), round(len(sent_at) / 10.0, 1), delta=0.11)
        self.assertEqual(S.Sink(C.LM_STYLE_LEVEL, 5, lambda v: None, mono=Clock()).update_hz(), 0.0)


class SinkSwitchTest(unittest.TestCase):
    def test_prelude_runs_once_right_before_the_first_write(self):
        clock = Clock()
        rec = Recorder()
        sink = S.Sink(C.LM_STYLE_LEVEL, 10, rec.publish, mono=clock, prelude=rec.prelude)
        self.assertFalse(sink.switched)
        run_sink(sink, clock, [bytes(1600)] * 100)
        self.assertTrue(sink.switched)
        self.assertEqual(rec.calls[0], ("prelude", None))
        self.assertEqual(rec.calls[1][0], "publish")
        self.assertEqual([k for k, _ in rec.calls].count("prelude"), 1)

    def test_no_audio_never_switches_the_strip(self):
        rec = Recorder()
        sink = S.Sink(C.LM_STYLE_LEVEL, 10, rec.publish, mono=Clock(), prelude=rec.prelude)
        sink.feed(bytes(1599))  # less than one frame
        sink.tick()
        sink.end("")
        self.assertEqual(rec.calls, [])
        self.assertFalse(sink.switched)

    def test_nothing_is_published_after_close(self):
        clock = Clock()
        rec = Recorder()
        sink = S.Sink(C.LM_STYLE_LEVEL, 10, rec.publish, mono=clock)
        run_sink(sink, clock, [bytes(1600)] * 3)
        n = len(rec.values)
        sink.close()
        clock.t += 5.0
        run_sink(sink, clock, [tone16(300, 9000, f * 800, 800) for f in range(40)])
        sink.tick()
        self.assertEqual(len(rec.values), n)

    def test_close_waits_for_a_publish_in_flight(self):
        """close() returns only after a publish running on the reader thread
        has finished (the stop restore must never be overwritten)."""
        entered, release = threading.Event(), threading.Event()
        order = []

        def slow_publish(value):
            entered.set()
            release.wait(5)
            order.append("publish")

        sink = S.Sink(C.LM_STYLE_LEVEL, 10, slow_publish, mono=Clock())
        reader = threading.Thread(target=sink.feed, args=(bytes(1600),))
        reader.start()
        self.assertTrue(entered.wait(5))
        closer = threading.Thread(target=lambda: (sink.close(), order.append("closed")))
        closer.start()
        closer.join(0.2)
        self.assertTrue(closer.is_alive(), "close() did not wait for the publish in flight")
        release.set()
        reader.join(5)
        closer.join(5)
        self.assertEqual(order, ["publish", "closed"])


class ValueGrammarTest(unittest.TestCase):
    def test_level_values_are_0_to_100_integers(self):
        clock = Clock()
        rec = Recorder()
        sink = S.Sink(C.LM_STYLE_LEVEL, 10, rec.publish, mono=clock)
        blocks = [tone16(150 * (1 + f % 7), 1000 * (1 + f % 13), f * 800, 800) for f in range(200)]
        run_sink(sink, clock, blocks)
        self.assertTrue(rec.values)
        for v in rec.values:
            self.assertRegex(v, r"^(100|[1-9]?[0-9])\Z")
        self.assertTrue(any(re.match(r"^[1-9]", v) for v in rec.values))


if __name__ == "__main__":
    unittest.main()
