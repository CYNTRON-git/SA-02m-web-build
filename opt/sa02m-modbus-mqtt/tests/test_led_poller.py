#!/usr/bin/env python3
"""LedPoller (type: led) — publish set, the lock bracket, and the colour form.

HONESTY LABEL — READ BEFORE TRUSTING A GREEN RUN. These tests drive the RUNTIME
(register bank → decode → MQTT publish, and MQTT /on → write sequence) against a
FakeSerial. Unlike the Carel suite beside them, the register bank here is
INVENTED: no type-120 device has answered a scan on any of the five ports at
either baud, so nothing below is evidence that the strip's firmware answers these
addresses, in this order, with these scales. What they DO prove is that the
poller obeys the map (`sa02m_led`) and the control declaration
(`sa02m_led.controls`) — that a lock-gated register is never written plain, that
the marquee window is never written at a computed base, that a malformed payload
reaches no write, and that the `color` form the poller publishes is the one the
Alice bridge beside it actually reads and writes back.

Two reads are the MR-02m FAMILY convention rather than a product-low-map fact —
the live DI block (Input 18..21) and the NTC/VLED scales. They are pinned here as
the poller's behaviour, not as hardware truth; the bench check is named in
docs/MQTT_TOPICS.md.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
REPO_OPT = BRIDGE_DIR.parent
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(REPO_OPT / "sa02m-led"))
sys.path.insert(0, str(REPO_OPT / "sa02m-alice"))


def _stub_missing(name: str, module: types.ModuleType) -> None:
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module


_stub_missing("yaml", types.ModuleType("yaml"))
_stub_missing("serial", types.ModuleType("serial"))
try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    _paho = types.ModuleType("paho")
    _paho_mqtt = types.ModuleType("paho.mqtt")
    _paho_client = types.ModuleType("paho.mqtt.client")
    _paho_client.Client = object
    _paho_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    _paho.mqtt = _paho_mqtt
    _paho_mqtt.client = _paho_client
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _paho_mqtt
    sys.modules["paho.mqtt.client"] = _paho_client

import bridge_led  # noqa: E402
import modbus_mqtt_bridge as bridge  # noqa: E402
from sa02m_led import controls as lc  # noqa: E402
from sa02m_led import led_mb2ws as lm  # noqa: E402

# The one consumer this control inventory has today. Imported, not described:
# the `color` payload form is a contract between two packages and a prose
# comment cannot fail when one of them moves.
from sa02m_alice.client import converters as alice_cv  # noqa: E402


class FakeSerial:
    """ModbusSerial stand-in over crafted banks; records every write in order."""

    def __init__(self, regs=None, blind=()):
        self.regs = dict(regs or {})
        # (kind, start) pairs that raise, to model a block this firmware does
        # not map. `blind` holds START addresses of reads to refuse.
        self.blind = set(blind)
        self.writes = []

    def _read(self, kind, start, count):
        if (kind, start) in self.blind:
            raise IOError("illegal data address %d" % start)
        return [self.regs.get((kind, start + i), 0) for i in range(count)]

    def read_input_registers(self, addr, start, count):
        return self._read("i", start, count)

    def read_holding_registers(self, addr, start, count):
        return self._read("h", start, count)

    def read_coils(self, addr, start, count):
        return [0] * count

    def read_discrete_inputs(self, addr, start, count):
        return [0] * count

    def write_register(self, addr, reg, value):
        self.writes.append(("reg", reg, value))

    def write_registers(self, addr, start, values):
        self.writes.append(("regs", start, list(values)))

    def write_coil(self, addr, coil, value):
        self.writes.append(("coil", coil, bool(value)))


# ── the invented bank (see the honesty label above) ──────────────────────────

BANK_COLOR_HEX = "#FF8000"
BANK_TEXT = "ЦИНТРОН"


def led_bank():
    regs = {}
    # Settings block 400..419 — every address answered, as one FC03 of 20.
    for a in range(lm.MB2WS_LOCK_GATED_FIRST, lm.MB2WS_LOCK_GATED_LAST + 1):
        regs[("h", a)] = 0
    regs[("h", lm.MB2WS_LED_COUNT0)] = 60
    regs[("h", lm.MB2WS_LED_TYPE)] = 0
    regs[("h", lm.MB2WS_RENDER_SOURCE)] = lm.MB2WS_RENDER_FX
    regs[("h", lm.MB2WS_FX_ID)] = lm.RGBW_FX_MODE_MATRIX_TEXT      # 62
    regs[("h", lm.MB2WS_FX_SPEED)] = 128
    regs[("h", lm.MB2WS_FX_PARAM)] = 200
    regs[("h", lm.MB2WS_LINE_MODE)] = lm.MB2WS_LINE_SINGLE_WS
    regs[("h", lm.MB2WS_PLAY_CTRL)] = lm.MB2WS_PLAY_PLAY
    # PWM triple + W (permille), and the strip mode on its own read.
    r, g, b = lm.rgbw_hex_to_pwm_permille(BANK_COLOR_HEX)
    for i, v in enumerate((r, g, b, 250)):
        regs[("h", lm.RGBW_PWM_HOLDING_BASE + i)] = v
    regs[("h", lm.RGBW_PWM_STRIP_MODE_HOLDING)] = 0
    # Live analog inputs and the family DI block.
    regs[("i", lm.RGBW_IREG_NTC)] = 315
    regs[("i", lm.RGBW_IREG_VLED)] = 1198
    for i, v in enumerate((1, 0, 1, 0)):
        regs[("i", lm.RGBW_DI_INPUT_BASE + i)] = v
    # Marquee window.
    for i, word in enumerate(lm.rgbw_pack_text_cp1251(BANK_TEXT)):
        regs[("h", lm.MB2WS_TEXT_BASE + i)] = word
    return regs


def _poller(regs=None, blind=(), **cfg):
    ser = FakeSerial(led_bank() if regs is None else regs, blind=blind)
    base = {"id": "led-COM3-13", "type": "led", "port": "/dev/COM3",
            "baudrate": 115200, "address": 13}
    base.update(cfg)
    pub = mock.Mock()
    p = bridge_led.LedPoller(base, pub)
    p.get_port = lambda: ser
    return p, pub, ser


def _published(pub):
    return {c.args[1]: c.args[2] for c in pub.pub_control.call_args_list}


def _errors(pub):
    return {c.args[1]: c.args[2] for c in pub.pub_error.call_args_list}


class TestPoll(unittest.TestCase):
    def setUp(self):
        self.p, self.pub, self.ser = _poller()
        self.p.poll_io()
        self.values = _published(self.pub)

    def test_the_settings_block_reaches_mqtt(self):
        self.assertEqual(self.values["power"], "1")
        self.assertEqual(self.values["play_state"], str(lm.MB2WS_PLAY_PLAY))
        self.assertEqual(self.values["brightness"], "200")
        self.assertEqual(self.values["speed"], "128")
        self.assertEqual(self.values["effect"], "62")
        self.assertEqual(self.values["scene_source"], str(lm.MB2WS_RENDER_FX))
        self.assertEqual(self.values["led_count"], "60")
        self.assertEqual(self.values["line_mode"], "0")

    def test_the_whole_settings_block_is_one_transaction(self):
        """400..419 in ONE FC03 — 20 single reads per poll on a shared line."""
        reads = []
        self.ser.read_holding_registers = lambda a, s, c: (
            reads.append((s, c)) or [0] * c)
        p, _pub, _s = _poller()
        p.get_port = lambda: self.ser
        p.poll_io()
        self.assertIn((lm.MB2WS_LOCK_GATED_FIRST,
                       lm.MB2WS_LOCK_GATED_LAST - lm.MB2WS_LOCK_GATED_FIRST + 1),
                      reads)

    def test_effect_group_comes_from_the_catalog(self):
        self.assertEqual(self.values["effect_group"],
                         lm.rgbw_fx_group_of(lm.RGBW_FX_MODE_MATRIX_TEXT))

    def test_a_gyver_alias_publishes_the_canonical_id(self):
        """405 may answer 128..209 for the same effect.

        Publishing the alias would make the echo of a written `effect` disagree
        with the next poll of the untouched register, and would push the group
        lookup off the end of the 82-entry catalog.
        """
        regs = led_bank()
        regs[("h", lm.MB2WS_FX_ID)] = lm.RGBW_FX_MODE_GYVER_BASE + 62
        p, pub, _s = _poller(regs)
        p.poll_io()
        self.assertEqual(_published(pub)["effect"], "62")

    def test_colour_is_published_as_hex_from_the_pwm_triple(self):
        self.assertEqual(self.values["color"], BANK_COLOR_HEX)
        self.assertEqual(self.values["white"], "250")

    def test_the_analog_inputs_carry_their_scale(self):
        self.assertEqual(self.values["temperature"], "31.5")
        self.assertEqual(self.values["vled"], "11.98")

    def test_a_sub_zero_ntc_reading_is_signed(self):
        """An unsigned read would publish −5.0 °C as +6549.1 °C."""
        regs = led_bank()
        regs[("i", lm.RGBW_IREG_NTC)] = 0x10000 - 50
        p, pub, _s = _poller(regs)
        p.poll_io()
        self.assertEqual(_published(pub)["temperature"], "-5.0")

    def test_the_four_dry_contacts_are_published(self):
        self.assertEqual(
            [self.values["di_%d" % i] for i in (1, 2, 3, 4)],
            ["1", "0", "1", "0"])

    def test_every_declared_control_has_a_producer(self):
        """`controls.py` promises the bridge's own test catches an orphan name.

        This is that test. A name added to the inventory with no code publishing
        it would otherwise reach MQTT as meta with no value — a control that
        exists in the topic tree and never says anything.
        """
        p, pub, _s = _poller()
        p.poll_io()
        p.poll_slow_if_due(1e6)
        produced = set(_published(pub))
        missing = [n for n in lc.control_names() if n not in produced]
        self.assertEqual([], missing,
                         "declared controls nothing publishes: %s" % missing)


class TestOptionalBlocks(unittest.TestCase):
    """The family-convention reads must not be able to kill a healthy poll."""

    def test_a_blind_di_block_flags_only_its_own_controls(self):
        p, pub, _s = _poller(blind=[("i", lm.RGBW_DI_INPUT_BASE)])
        p.poll_io()
        values, errs = _published(pub), _errors(pub)
        self.assertEqual(values["brightness"], "200")   # the poll survived
        for i in (1, 2, 3, 4):
            self.assertNotIn("di_%d" % i, values)
            self.assertEqual(errs["di_%d" % i], "r")

    def test_a_blind_optional_block_never_takes_the_device_offline(self):
        """Input 18 and the NTC pair are UNVERIFIED addresses on this product.

        Routing them through the availability wrappers would let a device that
        answers everything the poller actually needs be marked offline and
        backed off, on the strength of a read the map itself hedges.
        """
        p, pub, _s = _poller(blind=[("i", lm.RGBW_DI_INPUT_BASE),
                                    ("i", lm.RGBW_IREG_NTC),
                                    ("h", lm.RGBW_PWM_HOLDING_BASE),
                                    ("h", lm.RGBW_PWM_STRIP_MODE_HOLDING)])
        p.poll_io()
        self.assertTrue(p._online)
        self.assertEqual(p._fail_count, 0)
        self.assertEqual(pub.device_online.call_args_list, [])
        for name in ("color", "white", "temperature", "vled", "pwm_mode"):
            self.assertEqual(_errors(pub)[name], "r")

    def test_a_failed_settings_read_publishes_nothing(self):
        p, pub, _s = _poller(blind=[("h", lm.MB2WS_LOCK_GATED_FIRST)])
        p.poll_io()
        self.assertEqual(pub.pub_control.call_args_list, [])
        self.assertEqual(p._fail_count, 1)


class TestMarqueeText(unittest.TestCase):
    def test_the_window_is_read_on_the_slow_cadence_only(self):
        p, pub, ser = _poller()
        p.poll_io()
        self.assertNotIn("text", _published(pub))
        p.poll_slow_if_due(1e6)
        self.assertEqual(_published(pub)["text"], BANK_TEXT)

    def test_an_empty_window_reads_back_as_empty_text(self):
        """516..579 is pixel-data class: a power cycle clears it.

        Publishing an error for the cleared window would make a normal state
        look like a broken device.
        """
        regs = led_bank()
        for i in range(lm.MB2WS_TEXT_REG_COUNT):
            regs[("h", lm.MB2WS_TEXT_BASE + i)] = 0
        p, pub, _s = _poller(regs)
        p.poll_slow_if_due(1e6)
        self.assertEqual(_published(pub)["text"], "")
        self.assertEqual(_errors(pub).get("text"), "")

    def test_a_text_write_is_one_full_window_at_the_map_base(self):
        p, _pub, ser = _poller()
        p._writeback("text", "ПРИВЕТ")
        self.assertEqual(len(ser.writes), 1)
        kind, start, values = ser.writes[0]
        self.assertEqual(kind, "regs")
        self.assertEqual(start, lm.MB2WS_TEXT_BASE)
        self.assertEqual(len(values), lm.MB2WS_TEXT_REG_COUNT)
        self.assertEqual(lm.rgbw_unpack_text_cp1251(values), "ПРИВЕТ")

    def test_text_longer_than_the_window_is_truncated_and_echoed_truncated(self):
        p, pub, ser = _poller()
        long_text = "A" * (lc.TEXT_MAX_CHARS + 20)
        p._writeback("text", long_text)
        self.assertEqual(_published(pub)["text"], "A" * lc.TEXT_MAX_CHARS)


class TestBlockWriteGuard(unittest.TestCase):
    """An executor guard independent of the control table."""

    def test_the_legacy_marquee_base_is_refused(self):
        """495 drove the safe-state PWM block 503..506 to 100 %.

        The map refuses to name it as a base; this refuses to write it even if a
        future caller computes its way down there.
        """
        p, _pub, ser = _poller()
        with self.assertRaises(ValueError):
            p._write_block(lm.MB2WS_TEXT_LEGACY_BASE,
                           [0] * lm.MB2WS_TEXT_REG_COUNT)
        self.assertEqual(ser.writes, [])

    def test_a_short_window_write_is_refused(self):
        p, _pub, ser = _poller()
        with self.assertRaises(ValueError):
            p._write_block(lm.MB2WS_TEXT_BASE, [0] * 8)
        self.assertEqual(ser.writes, [])

    def test_only_the_two_sanctioned_blocks_are_writable(self):
        self.assertEqual(
            set(bridge_led._LEGAL_BLOCK_WRITES),
            {lm.MB2WS_TEXT_BASE, lm.RGBW_PWM_HOLDING_BASE})


class TestLockBracket(unittest.TestCase):
    def test_power_off_is_bracketed_by_the_unlock(self):
        """A plain write to 416 on a locked device is refused with a normal ack.

        The strip then keeps playing while MQTT reports it off — a fail-open on
        a stop command, and the reason `power` is not a template row.
        """
        p, _pub, ser = _poller()
        p._writeback("power", "0")
        self.assertEqual(ser.writes, [
            ("reg", lm.MB2WS_LOCK, lm.MB2WS_UNLOCK_KEY),
            ("reg", lm.MB2WS_PLAY_CTRL, lm.MB2WS_PLAY_STOP),
            ("reg", lm.MB2WS_LOCK, 0),
        ])

    def test_every_settings_control_is_bracketed(self):
        for name, payload, reg in (
                ("power", "1", lm.MB2WS_PLAY_CTRL),
                ("brightness", "77", lm.MB2WS_FX_PARAM),
                ("speed", "33", lm.MB2WS_FX_SPEED),
                ("effect", "12", lm.MB2WS_FX_ID),
                ("scene_source", "2", lm.MB2WS_RENDER_SOURCE)):
            p, _pub, ser = _poller()
            p._writeback(name, payload)
            self.assertEqual(ser.writes[0],
                             ("reg", lm.MB2WS_LOCK, lm.MB2WS_UNLOCK_KEY), name)
            self.assertEqual(ser.writes[-1], ("reg", lm.MB2WS_LOCK, 0), name)
            self.assertEqual([w[1] for w in ser.writes[1:-1]], [reg], name)

    def test_the_pwm_block_is_written_without_an_unlock(self):
        """33..36 sit in the product low map, outside the 400..419 gate.

        An unnecessary unlock costs two frames on a shared line and leaves the
        device unlocked if the batch aborts.
        """
        p, _pub, ser = _poller()
        p._writeback("white", "600")
        self.assertEqual(ser.writes,
                         [("reg", lm.RGBW_PWM_HOLDING_BASE + 3, 600)])

    def test_a_failed_settings_write_re_locks_the_device(self):
        """The map's bracket helper is pure — it cannot know a write failed.

        Leaving 410 unlocked lets anything else on the line rewrite the strip's
        settings block.
        """
        p, _pub, ser = _poller()
        real = ser.write_register

        def boom(addr, reg, value):
            if reg == lm.MB2WS_FX_PARAM:
                raise IOError("no answer")
            real(addr, reg, value)

        ser.write_register = boom
        p._writeback("brightness", "77")
        self.assertEqual(ser.writes, [
            ("reg", lm.MB2WS_LOCK, lm.MB2WS_UNLOCK_KEY),
            ("reg", lm.MB2WS_LOCK, 0),
        ])

    def test_the_call_site_never_orders_the_writes_itself(self):
        """Order is the map's (413, 403, 401, 400, 414, 419 first), never sorted().

        Asserted through the poller's own primitive so a future handler that
        batches several settings inherits the ordering instead of restating it.
        """
        p, _pub, ser = _poller()
        batch = {lm.MB2WS_LED_COUNT0: 60, lm.MB2WS_LINE_MODE: 1,
                 lm.MB2WS_FX_PARAM: 10}
        p._write_settings(batch)
        got = [w[1] for w in ser.writes[1:-1]]
        self.assertEqual(got, lm.rgbw_settings_write_order(batch.keys()))
        self.assertNotEqual(got, sorted(batch.keys()))


class TestColourForm(unittest.TestCase):
    """The published form and the read form are ONE decision, checked both ways."""

    def test_the_published_form_is_what_the_alice_bridge_reads(self):
        p, pub, _s = _poller()
        p.poll_io()
        published = _published(pub)["color"]
        block = alice_cv.mqtt_to_color_setting(published, {"instance": "rgb"})
        self.assertEqual(block["state"]["value"], 0xFF8000)

    def test_what_the_alice_bridge_writes_back_is_accepted(self):
        """`yandex_to_color_setting` emits a DECIMAL int, not the hex it reads.

        Accepting only `#RRGGBB` would break the round trip in the one consumer
        this control has, and the failure would be silent: the writeback would
        raise inside the worker and the strip would simply not change colour.
        """
        payload, err = alice_cv.yandex_to_color_setting(
            {"instance": "rgb", "value": 0xFF8000})
        self.assertIsNone(err)
        p, pub, ser = _poller()
        p._writeback("color", payload)
        self.assertEqual(ser.writes,
                         [("regs", lm.RGBW_PWM_HOLDING_BASE,
                           list(lm.rgbw_hex_to_pwm_permille(BANK_COLOR_HEX)))])
        self.assertEqual(_published(pub)["color"], BANK_COLOR_HEX)

    def test_the_wiren_board_triple_form_is_accepted(self):
        p, _pub, ser = _poller()
        p._writeback("color", "255;128;0")
        self.assertEqual(ser.writes,
                         [("regs", lm.RGBW_PWM_HOLDING_BASE,
                           list(lm.rgbw_hex_to_pwm_permille(BANK_COLOR_HEX)))])

    def test_a_bare_six_digit_payload_is_decimal_not_hex(self):
        """`255000` is valid in both forms; the `#` is what disambiguates.

        The Alice reader draws the same line (`s.startswith('#') and len == 7`),
        so guessing hex here would make the two homes disagree on one payload.
        """
        p, _pub, ser = _poller()
        p._writeback("color", "255000")
        expected = lm.rgbw_hex_to_pwm_permille("#%06X" % 255000)
        self.assertEqual(ser.writes,
                         [("regs", lm.RGBW_PWM_HOLDING_BASE, list(expected))])

    def test_the_colour_is_one_transaction(self):
        """Three FC06 would put a wrong colour on the LEDs between frames."""
        p, _pub, ser = _poller()
        p._writeback("color", "#102030")
        self.assertEqual(len(ser.writes), 1)
        self.assertEqual(ser.writes[0][0], "regs")

    def test_an_eight_bit_colour_survives_the_round_trip(self):
        """Written hex → permille → published hex must be the SAME hex.

        Otherwise every poll after a write would contradict the echo and the
        writeback-protection window would just be hiding a drift.
        """
        for hexs in ("#000000", "#FFFFFF", "#FF8000", "#123456", "#010203"):
            p, pub, _s = _poller()
            p._writeback("color", hexs)
            self.assertEqual(_published(pub)["color"], hexs)

    def test_a_malformed_colour_reaches_no_write(self):
        for bad in ("", "nope", "#12345", "300;0;0", "1;2", "#GGGGGG",
                    str(0x1000000)):
            p, pub, ser = _poller()
            p._writeback("color", bad)
            self.assertEqual(ser.writes, [], bad)
            self.assertEqual(_errors(pub).get("color"), "w", bad)


class TestPayloadValidation(unittest.TestCase):
    def test_an_unrecognised_switch_payload_reaches_no_write(self):
        """Treating every unknown string as "on" is how a typo lights a strip."""
        for bad in ("maybe", "", "2on", "нет"):
            p, pub, ser = _poller()
            p._writeback("power", bad)
            self.assertEqual(ser.writes, [], bad)
            self.assertEqual(_errors(pub).get("power"), "w", bad)

    def test_the_known_switch_words_are_accepted(self):
        for payload, want in (("1", lm.MB2WS_PLAY_PLAY), ("on", lm.MB2WS_PLAY_PLAY),
                              ("TRUE", lm.MB2WS_PLAY_PLAY), ("0", lm.MB2WS_PLAY_STOP),
                              ("off", lm.MB2WS_PLAY_STOP), ("False", lm.MB2WS_PLAY_STOP)):
            p, _pub, ser = _poller()
            p._writeback("power", payload)
            self.assertEqual(ser.writes[1], ("reg", lm.MB2WS_PLAY_CTRL, want),
                             payload)

    def test_a_non_numeric_range_payload_reaches_no_write(self):
        for name in ("brightness", "speed", "effect", "white", "scene_source"):
            p, pub, ser = _poller()
            p._writeback(name, "хочу поярче")
            self.assertEqual(ser.writes, [], name)
            self.assertEqual(_errors(pub).get(name), "w", name)

    def test_ranges_are_clamped_to_the_declared_limits(self):
        for name, reg in (("brightness", lm.MB2WS_FX_PARAM),
                          ("speed", lm.MB2WS_FX_SPEED),
                          ("scene_source", lm.MB2WS_RENDER_SOURCE)):
            lo, hi = lc.RANGE_LIMITS[name]
            for payload, want in (("-40", lo), ("100000", hi)):
                p, _pub, ser = _poller()
                p._writeback(name, payload)
                self.assertEqual([w for w in ser.writes if w[1] == reg],
                                 [("reg", reg, want)], (name, payload))

    def test_white_is_clamped_in_permille(self):
        p, _pub, ser = _poller()
        p._writeback("white", "5000")
        self.assertEqual(ser.writes,
                         [("reg", lm.RGBW_PWM_HOLDING_BASE + 3,
                           lm.RGBW_PWM_PERMILLE_MAX)])

    def test_the_effect_clamp_is_the_maps_and_the_declaration_agrees(self):
        """Two homes for one bound; the firmware grows effects, so pin them together."""
        self.assertEqual(lc.RANGE_LIMITS["effect"],
                         (0, lm.RGBW_FX_MODE_COUNT - 1))
        p, _pub, ser = _poller()
        p._writeback("effect", "999")
        self.assertEqual(ser.writes[1],
                         ("reg", lm.MB2WS_FX_ID, lm.RGBW_FX_MODE_COUNT - 1))

    def test_a_retained_on_message_submits_no_job(self):
        """A retained /on replayed at broker restart must not re-drive the strip.

        Asserting "no register was written" proves nothing — the callback never
        writes inline, it hands the job to the port's writeback worker.
        """
        p, _pub, _ser = _poller()
        cb = p._make_writeback_cb("power")
        with mock.patch.object(p, "_wb_submit") as submit:
            cb(None, None, types.SimpleNamespace(retain=True, payload=b"0"))
            self.assertEqual(submit.call_args_list, [])
            cb(None, None, types.SimpleNamespace(retain=False, payload=b"0"))
            self.assertEqual(len(submit.call_args_list), 1)

    def test_an_offline_device_is_not_hammered_with_writes(self):
        p, pub, ser = _poller()
        p._online = False
        p._writeback("power", "1")
        self.assertEqual(ser.writes, [])
        self.assertEqual(_errors(pub).get("power"), "w")


class TestSetup(unittest.TestCase):
    def setUp(self):
        self.p, self.pub, _s = _poller()
        self.p.setup()

    def _meta(self):
        out = {}
        for c in self.pub.pub_control_meta.call_args_list:
            out.setdefault(c.args[1], {})[c.args[2]] = c.args[3]
        return out

    def test_every_declared_control_gets_its_type_and_readonly_flag(self):
        meta = self._meta()
        for name, wb_type, _units, readonly, _reg in lc.CONTROLS:
            self.assertEqual(meta[name]["type"], wb_type)
            self.assertEqual(meta[name]["readonly"], "1" if readonly else "0")

    def test_every_writable_control_is_subscribed(self):
        subscribed = {c.args[1] for c in self.pub.subscribe_writeback.call_args_list}
        self.assertEqual(subscribed, set(lc.writable_names()))

    def test_no_readonly_control_is_subscribed(self):
        subscribed = {c.args[1] for c in self.pub.subscribe_writeback.call_args_list}
        readonly = {row[0] for row in lc.CONTROLS if row[3]}
        self.assertEqual(subscribed & readonly, set())

    def test_range_bounds_reach_the_broker(self):
        meta = self._meta()
        for name, (lo, hi) in lc.RANGE_LIMITS.items():
            self.assertEqual(meta[name]["min"], str(lo))
            self.assertEqual(meta[name]["max"], str(hi))

    def test_no_fast_modbus_is_claimed(self):
        self.assertEqual(self.p.fmb_event_ranges(), [])


class TestMixedBaudOnOneLine(unittest.TestCase):
    """The strip ships at 115200; the Carel units on the bench line are at 19200."""

    def test_two_bauds_on_one_port_are_reported(self):
        conflicts = bridge.mixed_baud_port_conflicts(
            ["/dev/COM3:19200", "/dev/COM3:115200", "/dev/COM4:9600"])
        self.assertEqual(conflicts, {"/dev/COM3": [19200, 115200]})

    def test_one_baud_per_port_is_clean(self):
        self.assertEqual(
            bridge.mixed_baud_port_conflicts(
                ["/dev/COM3:19200", "/dev/COM4:115200", "/dev/COM4:115200"]),
            {})

    def test_the_composer_reports_it_rather_than_refusing_to_start(self):
        """A typo in one entry must not take down the ports that ARE consistent."""
        self.assertIsInstance(
            bridge.mixed_baud_port_conflicts(["/dev/COM3:19200",
                                              "/dev/COM3:115200"]), dict)


class TestSharedMapIsNotRestated(unittest.TestCase):
    def test_the_poller_reads_every_address_from_the_package(self):
        """A pasted address is the defect led-shared-home exists to catch.

        That gate proves the MAP has one home; this proves this consumer reaches
        it through the package rather than carrying its own copy of the numbers.
        """
        src = (BRIDGE_DIR / "bridge_led.py").read_text(encoding="utf-8")
        self.assertIn("from sa02m_led import", src)
        for banned in ("= 0x10C8", "MB2WS_TEXT_BASE = ", "RGBW_PWM_HOLDING_BASE = "):
            self.assertNotIn(banned, src,
                             "bridge_led.py restates the map: %s" % banned)


if __name__ == "__main__":
    unittest.main()
