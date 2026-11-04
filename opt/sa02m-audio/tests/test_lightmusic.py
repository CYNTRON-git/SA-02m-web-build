"""Light-music analysis (sa02m_audio.lightmusic; contract §Светомузыка,
led-mb2ws.md §Поток светомузыки), driven by synthetic signals only: the
three-band split, the automatic level (silence stays dark, a sound just above
the gate stays dim, one loud tone does not max every band), the 16 kHz input
decimated by 2, the register mapping, and the rate limiter (≤ the configured
1..10 updates/s, a resend at least every 2 s)."""

from __future__ import annotations

import math
import random
import sys
import unittest
from array import array

from sa02m_audio import constants as C
from sa02m_audio import lightmusic as L

FPS = 20


def tone(freq_hz, amp, rate, start, n):
    """`n` S16 samples of a sine from sample index `start` (continuous across
    blocks). Identical expression for every rate, so the even samples of a
    16 kHz tone are exactly the 8 kHz tone."""
    return array("h", [int(round(amp * math.sin(2.0 * math.pi * freq_hz * (start + i) / rate)))
                       for i in range(n)])


def mix(*blocks):
    return array("h", [max(-32768, min(32767, sum(v))) for v in zip(*blocks)])


def feed_tone(analyzer, freq_hz, amp, frames, start_frame=0):
    """Feed `frames` frames of one tone; returns every frame's Bands."""
    n = analyzer.block_samples
    out = []
    for f in range(start_frame, start_frame + frames):
        out.append(analyzer.feed(tone(freq_hz, amp, analyzer.rate, f * n, n)))
    return out


def room_noise(rng, n, peak=12):
    """A quiet room through a USB mic: uniform noise, RMS ≈ peak/√3 (≈ 7 for
    12) — below the absolute gate."""
    return array("h", [rng.randint(-peak, peak) for _ in range(n)])


def dominant(bands):
    values = {"low": bands.low, "mid": bands.mid, "high": bands.high}
    return max(values, key=values.get)


class BandSplitTest(unittest.TestCase):
    """low < 250 Hz → R, 250..2000 Hz → G, > 2000 Hz → B."""

    def steady(self, freq_hz, amp=8000, rate=8000):
        return feed_tone(L.Analyzer(rate=rate), freq_hz, amp, 40)[-1]

    def test_bass_mid_treble_land_in_their_own_band(self):
        for freq_hz, band in ((100, "low"), (1000, "mid"), (3000, "high")):
            with self.subTest(freq_hz=freq_hz):
                b = self.steady(freq_hz)
                self.assertEqual(dominant(b), band, b)
                self.assertAlmostEqual(getattr(b, band), 1.0, places=6)
                self.assertAlmostEqual(b.total, 1.0, places=6)

    def test_the_far_band_stays_dark(self):
        bass = self.steady(100)
        treble = self.steady(3000)
        self.assertLess(bass.high, 0.2, bass)
        self.assertLess(treble.low, 0.2, treble)

    def test_color_maps_low_mid_high_to_red_green_blue(self):
        for freq_hz, channel in ((100, 0), (1000, 1), (3000, 2)):
            with self.subTest(freq_hz=freq_hz):
                rgb = L.color_of(self.steady(freq_hz))
                self.assertEqual(rgb[channel], 255, rgb)
                self.assertEqual(max(range(3), key=lambda i: rgb[i]), channel, rgb)

    def test_two_tones_light_two_bands(self):
        a = L.Analyzer()
        n = a.block_samples
        for f in range(40):
            b = a.feed(mix(tone(100, 6000, 8000, f * n, n), tone(3000, 6000, 8000, f * n, n)))
        self.assertGreater(b.low, 0.9, b)
        self.assertGreater(b.high, 0.9, b)


class AutoLevelTest(unittest.TestCase):
    def test_room_noise_never_becomes_light(self):
        """10 s of a quiet room: every band reads exactly 0, however far the
        gain could have climbed — the gate is absolute, before the gain."""
        a = L.Analyzer()
        rng = random.Random(7)
        for _ in range(10 * FPS):
            b = a.feed(room_noise(rng, a.block_samples))
            self.assertEqual(b, L.Bands(0.0, 0.0, 0.0, 0.0))
        self.assertEqual(L.color_of(b), (0, 0, 0))
        self.assertEqual(L.level_of(b), 0)

    def test_digital_silence_is_dark(self):
        a = L.Analyzer()
        for _ in range(FPS):
            b = a.feed(array("h", [0] * a.block_samples))
        self.assertEqual(b, L.Bands(0.0, 0.0, 0.0, 0.0))

    def test_noise_after_music_falls_back_to_dark(self):
        a = L.Analyzer()
        feed_tone(a, 1000, 12000, 2 * FPS)
        rng = random.Random(3)
        for _ in range(3 * FPS):
            b = a.feed(room_noise(rng, a.block_samples))
        self.assertLess(max(b), 1e-3, b)
        self.assertEqual(L.color_of(b), (0, 0, 0))
        self.assertEqual(L.level_of(b), 0)

    def test_a_sound_just_above_the_gate_stays_dim(self):
        """RMS ≈ 14 (gate 10): the gain ceiling (full scale /
        AGC_MIN_REF_RMS) keeps it dim instead of lifting it to full."""
        a = L.Analyzer()
        frames = feed_tone(a, 1000, 20, 10 * FPS)
        self.assertLess(max(max(b) for b in frames), 0.25, frames[-1])
        self.assertLess(L.level_of(frames[-1]), 25)

    def test_quiet_and_loud_music_both_reach_full_level(self):
        """The automatic level: a quiet source is lifted to the same picture
        as a loud one (per-band peak follower)."""
        quiet = feed_tone(L.Analyzer(), 1000, 800, 2 * FPS)[-1]
        loud = feed_tone(L.Analyzer(), 1000, 16000, 2 * FPS)[-1]
        self.assertGreater(quiet.mid, 0.99, quiet)
        self.assertEqual(L.level_of(quiet), 100)
        # Same picture; the absolute gate shaves a little more off a quiet
        # source's leakage bands.
        for got, want in zip(quiet, loud):
            self.assertAlmostEqual(got, want, delta=0.05)

    def test_one_loud_tone_does_not_max_all_three_bands(self):
        """Each band's gain is floored against the loudest band
        (BAND_COUPLING): a tone's leakage into the other bands is not
        lifted to full brightness."""
        for freq_hz, amp in ((100, 20000), (1000, 20000), (3000, 20000), (100, 1500)):
            with self.subTest(freq_hz=freq_hz, amp=amp):
                b = feed_tone(L.Analyzer(), freq_hz, amp, 5 * FPS)[-1]
                maxed = [name for name in ("low", "mid", "high") if getattr(b, name) > 0.95]
                self.assertEqual(len(maxed), 1, b)
                weakest = min(b.low, b.mid, b.high)
                self.assertLess(weakest, 0.5, b)
                self.assertNotEqual(L.color_of(b).count(255), 3)

    def test_release_is_slow_not_instant(self):
        """After a loud passage a quieter one is shown dimmer for a while
        (peak follower release over AGC_RELEASE_S), not re-normalised at
        once."""
        a = L.Analyzer()
        feed_tone(a, 1000, 16000, 2 * FPS)
        after = feed_tone(a, 1000, 1600, FPS // 2, start_frame=2 * FPS)[-1]
        self.assertLess(after.mid, 0.3, after)


class DecimationTest(unittest.TestCase):
    """The recorder's shared stream is 16 kHz; the analysis runs at 8 kHz."""

    def test_block_size_per_input_rate(self):
        self.assertEqual(L.Analyzer(rate=8000).block_samples, 400)
        self.assertEqual(L.Analyzer(rate=16000).block_samples, 800)
        self.assertEqual(L.Analyzer(rate=C.SAMPLE_RATE).block_samples, C.STREAM_BLOCK // 2)

    def test_other_rates_are_refused(self):
        for rate in (44100, 48000, 32000, 4000):
            with self.subTest(rate=rate):
                with self.assertRaises(ValueError):
                    L.Analyzer(rate=rate)
        with self.assertRaises(ValueError):
            L.Analyzer(rate=16000, fps=0)

    def test_16k_input_equals_its_every_second_sample_at_8k(self):
        """Decimation by 2: a 16 kHz signal gives exactly the Bands of its
        even samples fed at 8 kHz, frame after frame."""
        a16, a8 = L.Analyzer(rate=16000), L.Analyzer(rate=8000)
        rng = random.Random(11)
        for f in range(30):
            x16 = mix(tone(130, 5000, 16000, f * 800, 800), tone(900, 3000, 16000, f * 800, 800),
                      array("h", [rng.randint(-300, 300) for _ in range(800)]))
            b16 = a16.feed(x16)
            b8 = a8.feed(x16[::2])
            self.assertEqual(b16, b8, "frame %d" % f)

    def test_16k_tones_land_in_the_same_bands(self):
        for freq_hz, band in ((100, "low"), (1000, "mid"), (3000, "high")):
            with self.subTest(freq_hz=freq_hz):
                b = feed_tone(L.Analyzer(rate=16000), freq_hz, 8000, 40)[-1]
                self.assertEqual(dominant(b), band, b)

    def test_s16le_bytes_equal_the_sample_array(self):
        x = tone(440, 9000, 16000, 0, 800)
        raw = x.tobytes()
        if sys.byteorder == "big":
            swapped = array("h", x)
            swapped.byteswap()
            raw = swapped.tobytes()
        self.assertEqual(L.Analyzer(rate=16000).feed(raw), L.Analyzer(rate=16000).feed(x))
        # A torn last byte is dropped, never shifts the samples.
        self.assertEqual(L.Analyzer(rate=16000).feed(raw + b"\x7f"), L.Analyzer(rate=16000).feed(x))

    def test_wrong_block_types_are_refused(self):
        with self.assertRaises(TypeError):
            L.Analyzer().feed(array("i", [0] * 400))
        with self.assertRaises(TypeError):
            L.Analyzer().feed([0] * 400)


class MappingTest(unittest.TestCase):
    def test_color_gamma_and_bounds(self):
        self.assertEqual(L.color_of(L.Bands(1.0, 0.0, 0.0, 1.0)), (255, 0, 0))
        self.assertEqual(L.color_of(L.Bands(0.0, 1.0, 0.0, 1.0)), (0, 255, 0))
        self.assertEqual(L.color_of(L.Bands(0.0, 0.0, 1.0, 1.0)), (0, 0, 255))
        self.assertEqual(L.color_of(L.Bands(0.5, 0.0, 0.0, 0.5)), (64, 0, 0))  # 255 × 0.5²
        self.assertEqual(L.color_of(L.Bands(2.0, -1.0, float("nan"), 1.0)), (255, 0, 0))

    def test_level_is_total_loudness_0_to_100(self):
        self.assertEqual(L.level_of(L.Bands(0, 0, 0, 0.0)), 0)
        self.assertEqual(L.level_of(L.Bands(0, 0, 0, 0.5)), 50)
        self.assertEqual(L.level_of(L.Bands(0, 0, 0, 1.0)), 100)
        self.assertEqual(L.level_of(L.Bands(0, 0, 0, 7.0)), 100)
        self.assertEqual(L.level_of(L.Bands(0, 0, 0, float("nan"))), 0)


def run_limiter(limiter, values, dt=1.0 / FPS, t0=0.0):
    """Offer values[i] at t0 + i*dt; returns [(t, value)] actually sent."""
    sent = []
    for i, value in enumerate(values):
        t = t0 + i * dt
        out = limiter.offer(t, value)
        if out is not None:
            sent.append((t, out))
    return sent


def gaps(sent):
    return [b[0] - a[0] for a, b in zip(sent, sent[1:])]


class RateBoundsTest(unittest.TestCase):
    def test_configured_rate_is_clamped_to_1_10(self):
        self.assertEqual((L.RATE_HZ_MIN, L.RATE_HZ_MAX, L.RATE_HZ_DEFAULT), (1.0, 10.0, 8.0))
        cases = {0: 1.0, 0.5: 1.0, -3: 1.0, 1: 1.0, 7: 7.0, 10: 10.0, 11: 10.0, 1e9: 10.0,
                 "x": 8.0, None: 8.0, float("nan"): 8.0, float("inf"): 8.0, "4": 4.0}
        for given, want in cases.items():
            with self.subTest(given=given):
                self.assertEqual(L.clamp_rate_hz(given), want)
        self.assertEqual(L.RateLimiter(50).max_hz, 10.0)
        self.assertEqual(L.RateLimiter(0).max_hz, 1.0)

    def test_changing_values_never_exceed_the_configured_rate(self):
        """A value changing every 50 ms frame for 10 s: at most `hz` writes
        per second, never two closer than 1/hz."""
        for hz in range(1, 11):
            with self.subTest(hz=hz):
                sent = run_limiter(L.RateLimiter(hz), list(range(10 * FPS)))
                self.assertLessEqual(len(sent), 10 * hz + 1)
                self.assertGreaterEqual(min(gaps(sent)), 1.0 / hz - 1e-6)
                # …and it is a ceiling that is actually reached (within a frame).
                self.assertLessEqual(max(gaps(sent)), 1.0 / hz + 1.0 / FPS + 1e-6)

    def test_rate_above_ten_is_still_ten(self):
        sent = run_limiter(L.RateLimiter(25), list(range(10 * FPS)))
        self.assertLessEqual(len(sent), 10 * 10 + 1)
        self.assertGreaterEqual(min(gaps(sent)), 0.1 - 1e-6)

    def test_unchanged_value_is_resent_every_2_s_and_not_sooner(self):
        """The strip saves 455 a few seconds after the last write: a steady
        picture is rewritten every KEEPALIVE_S (2 s), never in between."""
        self.assertEqual(L.KEEPALIVE_S, 2.0)
        sent = run_limiter(L.RateLimiter(10), ["42"] * (10 * FPS))
        self.assertEqual(len(sent), 5)  # t = 0, 2, 4, 6, 8
        for gap in gaps(sent):
            self.assertAlmostEqual(gap, 2.0, delta=1e-6)

    def test_first_offer_is_sent_at_once(self):
        self.assertEqual(L.RateLimiter(1).offer(123.0, "7"), "7")
        self.assertEqual(L.RateLimiter(1).offer(0.0, "7"), "7")  # whatever the clock reads

    def test_a_change_inside_the_interval_waits_for_it(self):
        lim = L.RateLimiter(2)
        self.assertEqual(lim.offer(0.0, "a"), "a")
        self.assertIsNone(lim.offer(0.2, "b"))
        self.assertIsNone(lim.offer(0.45, "c"))
        self.assertEqual(lim.offer(0.5, "d"), "d")

    def test_keepalive_never_below_the_rate_interval(self):
        self.assertEqual(L.RateLimiter(1, keepalive_s=0.1).keepalive_s, 1.0)

    def test_clock_step_back_reanchors_the_keepalive(self):
        """A monotonic source that steps back must not stall the keep-alive
        until the old time is reached again."""
        lim = L.RateLimiter(10)
        self.assertEqual(lim.offer(100.0, "5"), "5")
        self.assertIsNone(lim.offer(50.0, "5"))
        self.assertEqual(lim.offer(52.0, "5"), "5")


if __name__ == "__main__":
    unittest.main()
