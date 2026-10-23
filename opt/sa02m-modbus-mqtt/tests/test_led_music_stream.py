#!/usr/bin/env python3
"""LedPoller light-music side channel (1.0.7.10) — docs/contracts/led-mb2ws.md
§4 «Поток светомузыки».

Three topics the light-music daemon writes and nothing else offers:

  * `music_level/on` — loudness 0..100 into the low byte of FxAux 455, packed
    by the map for the RUNNING effect's layout over the last 455 the device
    held;
  * `music_color/on` — `#RRGGBB` into holding 434, the same RGB565 encoder as
    `color`;
  * `fx_aux/on` — the exact 455 word back at session stop (the raw restore),
    plus the readonly `fx_aux` value the slow poll reads back.

A stream write is ONE FC06 and no echo, no forced publish, no live-cache flush:
up to 10 of them a second land on a line shared with Carel and MR-02m.

HONESTY LABEL: the register bank is the invented one of test_led_poller.py
(its honesty label applies); nothing here is evidence the firmware renders
these words. It proves what the poller writes, reads and publishes.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

# The fake bank, the poller factory and the publish readers have one home.
from test_led_poller import _errors, _poller, _published, led_bank  # noqa: E402

import bridge_led  # noqa: E402
from sa02m_led import controls as lc  # noqa: E402
from sa02m_led import led_mb2ws as lm  # noqa: E402

FX_LEVEL = 75        # level bar: low = level 0..100 + bit 7 orientation, high = colour
FX_EQUALIZER = 43    # equaliser: low = percent 0..100, high byte not read

# A 455 word with every part FX 75 keeps: colour override 2, orientation bit
# set, level 10.
AUX_75 = (2 << 8) | 0x80 | 10


def _bank(fx_id=FX_LEVEL, aux=AUX_75):
    regs = led_bank()
    regs[("h", lm.MB2WS_FX_ID)] = fx_id
    regs[("h", lm.MB2WS_FX_AUX)] = aux
    return regs


def _warm(fx_id=FX_LEVEL, aux=AUX_75, **cfg):
    """A poller that has run one fast and one slow poll (FX and 455 known)."""
    p, pub, ser = _poller(_bank(fx_id, aux), **cfg)
    p.poll_io()
    p.poll_slow_if_due(1e6)
    pub.reset_mock()
    return p, pub, ser


def _count_reads(ser):
    """Wrap the fake's holding reads; returns the list of (start, count) seen."""
    seen = []
    real = ser.read_holding_registers

    def spy(addr, start, count):
        seen.append((start, count))
        return real(addr, start, count)

    ser.read_holding_registers = spy
    return seen


def _fx_aux_publishes(pub):
    return [c for c in pub.pub_control.call_args_list
            if c.args[1] == bridge_led.FX_AUX_CONTROL]


class TestStreamSinksStayOffTheControlTable(unittest.TestCase):
    """The stream sinks are subscribed but are not controls a reader can use."""

    def test_the_stream_names_and_fx_aux_are_not_in_the_inventory(self):
        """`sa02m_led.controls` is what Alice offers and every write echoes."""
        names = set(lc.control_names())
        for name in bridge_led.STREAM_CONTROLS + (bridge_led.FX_AUX_CONTROL,):
            self.assertNotIn(name, names)

    def test_setup_subscribes_the_stream_and_the_raw_restore(self):
        p, pub, _s = _poller()
        p.setup()
        subscribed = {c.args[1] for c in pub.subscribe_writeback.call_args_list}
        self.assertTrue({"music_level", "music_color", "fx_aux"} <= subscribed)
        self.assertEqual(set(bridge_led.STREAM_CONTROLS),
                         {"music_level", "music_color"})

    def test_fx_aux_meta_is_readonly_and_the_stream_has_no_meta(self):
        p, pub, _s = _poller()
        p.setup()
        meta = {}
        for c in pub.pub_control_meta.call_args_list:
            meta.setdefault(c.args[1], {})[c.args[2]] = c.args[3]
        self.assertEqual(meta["fx_aux"], {"type": "value", "readonly": "1"})
        for name in bridge_led.STREAM_CONTROLS:
            self.assertNotIn(name, meta)
        units = {c.args[1] for c in pub.pub_control_units.call_args_list}
        self.assertFalse(units & set(bridge_led.STREAM_CONTROLS))

    def test_a_stream_frame_is_queued_under_its_own_name(self):
        """The port's writeback worker coalesces by (device, control): a frame
        that outpaces the line is replaced by the newer one, never queued."""
        p, _pub, _s = _poller()
        for name in bridge_led.STREAM_CONTROLS:
            cb = p._make_writeback_cb(name)
            with mock.patch.object(p, "_wb_submit") as submit:
                cb(None, None, types.SimpleNamespace(retain=False, payload=b"40"))
            self.assertEqual([c.args[0] for c in submit.call_args_list], [name])

    def test_a_retained_stream_frame_submits_no_job(self):
        p, _pub, _s = _poller()
        for name in bridge_led.STREAM_CONTROLS + (bridge_led.FX_AUX_CONTROL,):
            cb = p._make_writeback_cb(name)
            with mock.patch.object(p, "_wb_submit") as submit:
                cb(None, None, types.SimpleNamespace(retain=True, payload=b"40"))
            self.assertEqual(submit.call_args_list, [], name)


class TestMusicColor(unittest.TestCase):
    def test_one_fc06_to_434_with_the_colour_encoder(self):
        p, pub, ser = _poller()
        with mock.patch.object(bridge_led.DeviceLiveCache, "flush_file") as flush:
            p._writeback("music_color", "#FF8000")
        word = lm.rgbw_rgb8_to_user_color(0xFF, 0x80, 0x00)
        self.assertEqual(ser.writes, [("reg", lm.MB2WS_USER_COLOR, word)])
        self.assertEqual(flush.call_args_list, [])

    def test_no_echo_only_the_error_flag_is_cleared(self):
        p, pub, _s = _poller()
        p._writeback("music_color", "#102030")
        self.assertEqual(pub.pub_control.call_args_list, [])
        self.assertEqual(_errors(pub), {"music_color": ""})

    def test_black_is_not_the_white_sentinel(self):
        p, _pub, ser = _poller()
        p._writeback("music_color", "#000000")
        self.assertEqual(ser.writes, [("reg", lm.MB2WS_USER_COLOR, 1)])

    def test_only_the_hash_form_is_accepted(self):
        """`color` also takes decimal and `R;G;B`; the stream takes `#RRGGBB`."""
        for bad in ("255;128;0", "16744448", "FF8000", "#12345", "#GGGGGG", ""):
            p, pub, ser = _poller()
            p._writeback("music_color", bad)
            self.assertEqual(ser.writes, [], bad)
            self.assertEqual(_errors(pub).get("music_color"), "w", bad)


class TestMusicLevel(unittest.TestCase):
    def test_level_bar_keeps_orientation_and_colour_override(self):
        p, pub, ser = _warm(FX_LEVEL, AUX_75)
        p._writeback("music_level", "55")
        self.assertEqual(ser.writes,
                         [("reg", lm.MB2WS_FX_AUX, (2 << 8) | 0x80 | 55)])

    def test_the_word_is_packed_by_the_map_for_the_running_effect(self):
        for fx_id, aux in ((FX_LEVEL, AUX_75), (FX_EQUALIZER, (3 << 8) | 20),
                           (FX_LEVEL, 0), (FX_EQUALIZER, 0)):
            p, _pub, ser = _warm(fx_id, aux)
            p._writeback("music_level", "70")
            _low, flag, high = lm.rgbw_fx_aux_decompose(fx_id, aux)
            want = lm.rgbw_fx_aux_compose(fx_id, 70, flag, high)
            self.assertEqual(ser.writes, [("reg", lm.MB2WS_FX_AUX, want)],
                             (fx_id, aux))

    def test_equalizer_drops_the_high_byte_it_does_not_read(self):
        p, _pub, ser = _warm(FX_EQUALIZER, (3 << 8) | 20)
        p._writeback("music_level", "70")
        self.assertEqual(ser.writes, [("reg", lm.MB2WS_FX_AUX, 70)])

    def test_level_is_clamped_to_0_100(self):
        """Pins the OUTCOME, not where it happens: the bridge clamps to
        MUSIC_LEVEL_MAX and the map's compose clamps the low field to 0..100
        for FX 43 / FX 75 again — removing either clamp alone stays green."""
        for payload, level in (("150", 100), ("-5", 0), ("42.7", 42), ("0", 0),
                               ("100", 100)):
            p, _pub, ser = _warm(FX_EQUALIZER, 0)
            p._writeback("music_level", payload)
            self.assertEqual(ser.writes, [("reg", lm.MB2WS_FX_AUX, level)], payload)

    def test_a_warm_frame_is_one_write_and_no_read(self):
        p, _pub, ser = _warm()
        reads = _count_reads(ser)
        p._writeback("music_level", "30")
        p._writeback("music_level", "31")
        self.assertEqual(reads, [])
        self.assertEqual([w[1] for w in ser.writes],
                         [lm.MB2WS_FX_AUX, lm.MB2WS_FX_AUX])

    def test_a_cold_first_frame_reads_fx_and_455_once(self):
        p, _pub, ser = _poller(_bank(FX_LEVEL, AUX_75))
        reads = _count_reads(ser)
        p._writeback("music_level", "40")
        self.assertEqual(sorted(reads),
                         sorted([(lm.MB2WS_FX_ID, 1), (lm.MB2WS_FX_AUX, 1)]))
        self.assertEqual(ser.writes,
                         [("reg", lm.MB2WS_FX_AUX, (2 << 8) | 0x80 | 40)])
        del reads[:]
        p._writeback("music_level", "41")
        self.assertEqual(reads, [])
        # The next frame composes from the word the previous one wrote.
        self.assertEqual(ser.writes[-1],
                         ("reg", lm.MB2WS_FX_AUX, (2 << 8) | 0x80 | 41))

    def test_a_failed_cold_read_writes_nothing(self):
        for blind in (lm.MB2WS_FX_ID, lm.MB2WS_FX_AUX):
            p, pub, ser = _poller(_bank(), blind=[("h", blind)])
            p._writeback("music_level", "40")
            self.assertEqual(ser.writes, [], blind)
            self.assertEqual(_errors(pub).get("music_level"), "w", blind)

    def test_a_gyver_alias_uses_the_canonical_layout(self):
        p, _pub, ser = _warm(lm.RGBW_FX_MODE_GYVER_BASE + FX_LEVEL, AUX_75)
        p._writeback("music_level", "55")
        self.assertEqual(ser.writes,
                         [("reg", lm.MB2WS_FX_AUX, (2 << 8) | 0x80 | 55)])

    def test_our_own_fx_write_sets_the_layout_without_a_read(self):
        """The session's `effect` write lands before the first stream frame."""
        p, _pub, ser = _warm(FX_LEVEL, (3 << 8) | 20)
        p._writeback("effect", str(FX_EQUALIZER))
        del ser.writes[:]
        reads = _count_reads(ser)
        p._writeback("music_level", "30")
        self.assertEqual(reads, [])
        self.assertEqual(ser.writes, [("reg", lm.MB2WS_FX_AUX, 30)])

    def test_no_echo_no_flush_only_the_error_flag(self):
        p, pub, _s = _warm()
        with mock.patch.object(bridge_led.DeviceLiveCache, "flush_file") as flush:
            p._writeback("music_level", "50")
        self.assertEqual(pub.pub_control.call_args_list, [])
        self.assertEqual(flush.call_args_list, [])
        self.assertEqual(_errors(pub), {"music_level": ""})

    def test_a_non_numeric_frame_writes_nothing(self):
        p, pub, ser = _warm()
        p._writeback("music_level", "loud")
        self.assertEqual(ser.writes, [])
        self.assertEqual(_errors(pub).get("music_level"), "w")

    def test_an_offline_strip_is_not_streamed_to(self):
        p, pub, ser = _warm()
        p._online = False
        p._writeback("music_level", "50")
        p._writeback("music_color", "#FFFFFF")
        self.assertEqual(ser.writes, [])
        self.assertEqual(_errors(pub).get("music_level"), "w")
        self.assertEqual(_errors(pub).get("music_color"), "w")


class TestFxAuxRawRestore(unittest.TestCase):
    def test_the_exact_word_is_written_whatever_fx_runs(self):
        """FX 0 ignores the high byte; a restore must still put it back."""
        p, pub, ser = _warm(0, 0)
        p._writeback("fx_aux", str(0xABCD))
        self.assertEqual(ser.writes, [("reg", lm.MB2WS_FX_AUX, 0xABCD)])

    def test_one_forced_publish_of_the_written_word(self):
        p, pub, _s = _warm()
        p._writeback("fx_aux", "33489")
        published = _fx_aux_publishes(pub)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].args[2], "33489")
        self.assertEqual(published[0].kwargs.get("force"), True)
        self.assertEqual(_errors(pub).get("fx_aux"), "")

    def test_the_register_edges_are_accepted(self):
        for payload, word in (("0", 0), ("65535", 0xFFFF), (" 7 ", 7), ("00012", 12)):
            p, _pub, ser = _warm()
            p._writeback("fx_aux", payload)
            self.assertEqual(ser.writes, [("reg", lm.MB2WS_FX_AUX, word)], payload)

    def test_anything_but_a_16_bit_decimal_is_refused_before_the_bus(self):
        for bad in ("65536", "99999", "123456", "-1", "0x10", "1.5", "1e3",
                    "", "abc", "#FFFFFF"):
            p, pub, ser = _warm()
            p._writeback("fx_aux", bad)
            self.assertEqual(ser.writes, [], bad)
            self.assertEqual(_errors(pub).get("fx_aux"), "w", bad)
            self.assertEqual(_fx_aux_publishes(pub), [], bad)

    def test_the_next_stream_frame_composes_from_the_restored_word(self):
        p, _pub, ser = _warm(FX_LEVEL, 0)
        p._writeback("fx_aux", str(AUX_75))
        p._writeback("music_level", "60")
        self.assertEqual(ser.writes[-1],
                         ("reg", lm.MB2WS_FX_AUX, (2 << 8) | 0x80 | 60))


class TestFxAuxReadBack(unittest.TestCase):
    def test_the_slow_poll_publishes_455_as_a_decimal_word(self):
        p, pub, _s = _poller(_bank(FX_LEVEL, 0xABCD))
        p.poll_io()
        self.assertNotIn("fx_aux", _published(pub))
        p.poll_slow_if_due(1e6)
        self.assertEqual(_published(pub)["fx_aux"], str(0xABCD))
        self.assertEqual(_errors(pub).get("fx_aux"), "")

    def test_a_failed_455_read_flags_only_fx_aux(self):
        p, pub, _s = _poller(_bank(), blind=[("h", lm.MB2WS_FX_AUX)])
        p.poll_slow_if_due(1e6)
        self.assertEqual(_errors(pub).get("fx_aux"), "r")
        self.assertNotIn("fx_aux", _published(pub))
        self.assertIn("text", _published(pub))
        self.assertTrue(p._online)

    def test_a_read_straddling_a_restore_is_dropped(self):
        """A 455 read on the bus while the stop's raw restore landed is older
        than the restore: publishing it would hand a next session the streamed
        word as its snapshot."""
        streamed = (2 << 8) | 0x80 | 99
        p, pub, ser = _poller(_bank(FX_LEVEL, streamed))
        real = ser.read_holding_registers

        def read(addr, start, count):
            out = real(addr, start, count)
            if start == lm.MB2WS_FX_AUX:
                p._writeback("fx_aux", str(AUX_75))   # lands mid-read
            return out

        ser.read_holding_registers = read
        p.poll_slow_if_due(1e6)
        published = [c.args[2] for c in _fx_aux_publishes(pub)]
        self.assertEqual(published, [str(AUX_75)])
        # The cache holds the restore, not the stale read.
        ser.read_holding_registers = real
        p._writeback("music_level", "5")
        self.assertEqual(ser.writes[-1],
                         ("reg", lm.MB2WS_FX_AUX, (2 << 8) | 0x80 | 5))


class TestFxIdFromThePoll(unittest.TestCase):
    def test_a_snapshot_straddling_our_fx_write_is_not_adopted(self):
        """The poll read FX 75; our `effect 43` landed during that read. The
        snapshot is older than the write and must not hand `music_level` the
        level-bar layout."""
        p, _pub, ser = _poller(_bank(FX_LEVEL, AUX_75))
        p.poll_slow_if_due(1e6)                       # 455 known
        real = ser.read_holding_registers

        def read(addr, start, count):
            out = real(addr, start, count)
            if start == lm.MB2WS_LOCK_GATED_FIRST:
                p._write_settings({lm.MB2WS_FX_ID: FX_EQUALIZER})
            return out

        ser.read_holding_registers = read
        p.poll_io()
        ser.read_holding_registers = real
        del ser.writes[:]
        p._writeback("music_level", "30")
        self.assertEqual(ser.writes, [("reg", lm.MB2WS_FX_AUX, 30)])

    def test_a_clean_snapshot_is_adopted(self):
        """FX changed by someone else (the panel window) reaches the layout
        through the poll — not through a stream-time read of 405."""
        p, _pub, ser = _poller(_bank(FX_EQUALIZER, 0))
        p.poll_io()
        p.poll_slow_if_due(1e6)
        ser.regs[("h", lm.MB2WS_FX_ID)] = FX_LEVEL   # someone else changed FX
        ser.regs[("h", lm.MB2WS_FX_AUX)] = AUX_75
        p._t_poll = 0.0
        p.poll_io()
        p._t_text = 0.0
        p.poll_slow_if_due(1e6)
        del ser.writes[:]
        reads = _count_reads(ser)
        p._writeback("music_level", "30")
        self.assertEqual(reads, [])
        self.assertEqual(ser.writes,
                         [("reg", lm.MB2WS_FX_AUX, (2 << 8) | 0x80 | 30)])


if __name__ == "__main__":
    unittest.main()
