#!/usr/bin/env python3
"""Fast Modbus 0x11 event-record grammars: WB vs legacy, and their collisions.

Pure-unit: no serial port, no MQTT broker. Pins:
  * both record grammars byte-for-byte (WB value is LE, legacy value is BE);
  * whole-frame validation (truncated / trailing / wrong n_events -> None);
  * the reboot collision rule — a WB reboot record must never be read as a
    legacy COIL event, even for a slave cached as legacy;
  * the latent invariant the auto-detection rests on, stated at the WB gate so
    it covers a frame of any length: no (type, register high byte) pair a
    genuine legacy frame can START with — the configured ranges plus a reboot
    at any register — satisfies that gate, so such a frame fails WB on its
    first record and therefore never satisfies the WB grammar;
  * bus-garbage robustness (no exception, never more events than announced).

Contract home: docs/contracts/fmb-event-wire.md.

Run dev-side:  python -m unittest discover opt/sa02m-modbus-mqtt/tests
"""
from __future__ import annotations

import random
import sys
import types
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))


def _stub_missing(name: str, module: types.ModuleType) -> None:
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module


# The bridge sys.exit()s when a third-party import is missing; these tests
# never touch a broker/serial/yaml file, so stub whichever the dev box lacks.
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

import modbus_mqtt_bridge as bridge  # noqa: E402


class FakePub:
    """Minimal publisher stub — the poller ctors need one, nothing publishes."""

    def pub_control(self, *a, **k): pass
    def pub_error(self, *a, **k): pass
    def pub_meta(self, *a, **k): pass
    def pub_control_meta(self, *a, **k): pass
    def pub_control_units(self, *a, **k): pass
    def subscribe_writeback(self, *a, **k): pass


# The WB grammar's wire → payload-length table, restated here so the pins are
# an independent statement of the contract rather than an echo of the parser.
WIRE_PLEN = {1: 1, 2: 1, 3: 2, 4: 2, 0x0F: 0}


def first_record_collision_limit(evt_type: int) -> int | None:
    """Lowest register value at which a legacy record of this type also
    satisfies the WB gate (its type byte read as LEN, its register high byte
    read as the wire code). None when no wire code can ever match — the WB
    parser then rejects such a record outright, at any register."""
    wires = [wire for wire, plen in WIRE_PLEN.items() if plen == evt_type]
    return (min(wires) << 8) if wires else None


def legacy_record(evt_type: int, reg: int, val: int) -> bytes:
    """[TYPE int][REG_H][REG_L][payload sized by type] (value BE)."""
    head = bytes([evt_type, (reg >> 8) & 0xFF, reg & 0xFF])
    if evt_type in (bridge.FMB_EVT_COIL, bridge.FMB_EVT_DISCRETE):
        return head + bytes([val & 0xFF])
    if evt_type in (bridge.FMB_EVT_HOLDING, bridge.FMB_EVT_INPUT):
        return head + bytes([(val >> 8) & 0xFF, val & 0xFF])
    return head          # REBOOT: no payload


def wb_record(wire: int, reg: int, val: int) -> bytes:
    """[LEN][TYPE wire][ID_H][ID_L][payload x LEN] (value LE)."""
    if wire == 0x0F:
        payload = b""
    elif wire in (1, 2):
        payload = bytes([val & 0xFF])
    else:
        payload = bytes([val & 0xFF, (val >> 8) & 0xFF])
    return (bytes([len(payload), wire, (reg >> 8) & 0xFF, reg & 0xFF])
            + payload)


def frame(records: bytes, n_events: int) -> bytes:
    """A 0x11 payload as _poll_once hands it to the parsers (offset 6)."""
    return bytes([9, 0x46, 0x11, 0, n_events, len(records)]) + records


def parse_wb(buf: bytes, n_events: int):
    return bridge.FastModbusEventPortManager._parse_events_wb(
        9, buf, 6, len(buf), n_events)


def parse_legacy(buf: bytes, n_events: int):
    return bridge.FastModbusEventPortManager._parse_events_legacy(
        9, buf, 6, len(buf), n_events)


def all_event_ranges() -> list[tuple[int, int, int]]:
    """Every range map the bridge really configures today (all poller types)."""
    pub = FakePub()
    ranges: list[tuple[int, int, int]] = []
    for mt in bridge.MR02M_MODULE_TYPES:
        p = bridge.MR02mPoller(
            {"id": f"mr-{mt}", "type": "mr02m", "address": 5,
             "module_type": mt}, pub)
        ranges.extend(p.fmb_event_ranges())
    ranges.extend(bridge.DTVPoller(
        {"id": "dtv", "type": "dtv", "address": 15}, pub).fmb_event_ranges())
    ranges.extend(bridge.CE02M3Poller(
        {"id": "ce", "type": "ce02m3", "address": 14}, pub).fmb_event_ranges())
    return ranges


# ── 3. WB parser ─────────────────────────────────────────────────────────────
class TestWbParser(unittest.TestCase):
    def test_one_record_per_wire(self):
        cases = [
            (1, 1, 1, bridge.FMB_EVT_COIL, 1),
            (2, 18, 0, bridge.FMB_EVT_DISCRETE, 0),
            (3, 33, 0x1234, bridge.FMB_EVT_HOLDING, 0x1234),
            (4, 403, 0x1234, bridge.FMB_EVT_INPUT, 0x1234),
        ]
        for wire, reg, val, want_type, want_val in cases:
            with self.subTest(wire=wire):
                buf = frame(wb_record(wire, reg, val), 1)
                self.assertEqual(parse_wb(buf, 1),
                                 [(9, want_type, reg, want_val)])

    def test_value_is_little_endian(self):
        # 34 12 on the wire == 0x1234 (the legacy grammar reads the same bytes
        # BE = 0x3412 — a swap here would move an AI value by orders).
        buf = frame(bytes([2, 4, 0x01, 0x93, 0x34, 0x12]), 1)
        self.assertEqual(parse_wb(buf, 1), [(9, bridge.FMB_EVT_INPUT, 403,
                                             0x1234)])

    def test_reboot_zero_payload_gives_minus_one(self):
        buf = frame(wb_record(0x0F, 0x0105, 0), 1)
        self.assertEqual(parse_wb(buf, 1),
                         [(9, bridge.FMB_EVT_REBOOT, 0x0105, -1)])

    def test_unknown_wire_and_wrong_len_rejected(self):
        self.assertIsNone(parse_wb(frame(bytes([1, 5, 0, 1, 7]), 1), 1))
        self.assertIsNone(parse_wb(frame(bytes([2, 1, 0, 1, 7, 7]), 1), 1))

    def test_two_records(self):
        rec = wb_record(1, 3, 1) + wb_record(4, 431, 373)
        self.assertEqual(parse_wb(frame(rec, 2), 2), [
            (9, bridge.FMB_EVT_COIL, 3, 1),
            (9, bridge.FMB_EVT_INPUT, 431, 373),
        ])


# ── 4. Legacy parser ─────────────────────────────────────────────────────────
class TestLegacyParser(unittest.TestCase):
    def test_value_is_big_endian(self):
        buf = frame(legacy_record(bridge.FMB_EVT_INPUT, 403, 0x1234), 1)
        self.assertEqual(parse_legacy(buf, 1),
                         [(9, bridge.FMB_EVT_INPUT, 403, 0x1234)])

    def test_coil_and_reboot_payload_sizes(self):
        buf = frame(legacy_record(bridge.FMB_EVT_COIL, 2, 1)
                    + legacy_record(bridge.FMB_EVT_REBOOT, 0, 0), 2)
        self.assertEqual(parse_legacy(buf, 2), [
            (9, bridge.FMB_EVT_COIL, 2, 1),
            (9, bridge.FMB_EVT_REBOOT, 0, -1),
        ])

    def test_truncated_and_trailing_rejected(self):
        rec = legacy_record(bridge.FMB_EVT_INPUT, 403, 5)
        self.assertIsNone(parse_legacy(frame(rec[:-1], 1), 1))
        self.assertIsNone(parse_legacy(frame(rec + b"\x00", 1), 1))

    def test_unknown_type_rejected(self):
        self.assertIsNone(parse_legacy(frame(bytes([7, 0, 1, 0]), 1), 1))


# ── 5. Whole-frame validation ────────────────────────────────────────────────
class TestFrameValidation(unittest.TestCase):
    def test_n_events_mismatch_rejected_by_both(self):
        rec = wb_record(4, 403, 5)
        self.assertIsNone(parse_wb(frame(rec, 2), 2))       # promises 2, has 1
        self.assertIsNone(parse_wb(frame(rec + rec, 1), 1))  # 2 present, 1 read
        leg = legacy_record(bridge.FMB_EVT_INPUT, 403, 5)
        self.assertIsNone(parse_legacy(frame(leg, 2), 2))
        self.assertIsNone(parse_legacy(frame(leg + leg, 1), 1))

    def test_empty_payload_with_events_promised(self):
        self.assertIsNone(parse_wb(frame(b"", 1), 1))
        self.assertIsNone(parse_legacy(frame(b"", 1), 1))


# ── 6. Collision pins (the core of the auto-detection safety argument) ───────
class TestCollisionPins(unittest.TestCase):
    def _mgr(self):
        return bridge.FastModbusEventPortManager("/dev/COMT", 115200)

    def test_wb_reboot_wins_over_legacy_cache(self):
        # 00 0F hi lo is a valid legacy COIL record too (reg 0x0Fxx) — but no
        # range map produces one, so it is always the WB reboot. Without this
        # rule a reflashed module stays event-dead until a service restart.
        mgr = self._mgr()
        mgr._wb_frame_slaves[9] = False          # cached as legacy
        buf = frame(bytes([0x00, 0x0F, 0x01, 0x05]), 1)
        self.assertEqual(mgr._parse_event_records(9, buf, 6, len(buf), 1),
                         [(9, bridge.FMB_EVT_REBOOT, 0x0105, -1)])
        # and the raw legacy parser really does accept those same bytes:
        self.assertEqual(parse_legacy(buf, 1),
                         [(9, bridge.FMB_EVT_COIL, 0x0F01, 5)])

    def test_wb_reboot_wins_when_it_is_not_the_first_record(self):
        # The exact frame that defeated a position-only rule: an AI value whose
        # high byte (0x02) doubles as the next legacy type byte lets the legacy
        # grammar consume the whole frame exactly, reboot and all.
        mgr = self._mgr()
        mgr._wb_frame_slaves[9] = False          # cached as legacy
        records = bytes([0x02, 0x04, 0x01, 0x93, 0x34, 0x02]) \
            + bytes([0x00, 0x0F, 0x00, 0x05])
        buf = frame(records, 2)
        # Both grammars accept it — the length check cannot decide:
        self.assertIsNotNone(parse_legacy(buf, 2))
        self.assertEqual(mgr._parse_event_records(9, buf, 6, len(buf), 2), [
            (9, bridge.FMB_EVT_INPUT, 403, 564),
            (9, bridge.FMB_EVT_REBOOT, 5, -1),
        ])

    def test_wb_reboot_wins_from_the_middle_of_a_frame(self):
        mgr = self._mgr()
        mgr._wb_frame_slaves[9] = False
        records = (wb_record(1, 3, 1) + wb_record(0x0F, 5, 0)
                   + wb_record(4, 403, 564))
        buf = frame(records, 3)
        out = mgr._parse_event_records(9, buf, 6, len(buf), 3)
        self.assertEqual([e[1] for e in out], [
            bridge.FMB_EVT_COIL, bridge.FMB_EVT_REBOOT, bridge.FMB_EVT_INPUT])

    def test_rule_does_not_hijack_a_genuine_legacy_reboot_frame(self):
        # A real legacy frame carrying a legacy reboot must still read as
        # legacy — the rule fires on the WB grammar succeeding, not on 0x0F.
        mgr = self._mgr()
        mgr._wb_frame_slaves[9] = False
        records = (legacy_record(bridge.FMB_EVT_COIL, 1, 1)
                   + legacy_record(bridge.FMB_EVT_REBOOT, 0, 0))
        buf = frame(records, 2)
        self.assertIsNone(parse_wb(buf, 2))
        self.assertEqual(mgr._parse_event_records(9, buf, 6, len(buf), 2), [
            (9, bridge.FMB_EVT_COIL, 1, 1),
            (9, bridge.FMB_EVT_REBOOT, 0, -1),
        ])
        self.assertIs(mgr._wb_frame_slaves[9], False)

    def test_handshake_contract_still_governs_a_reboot_free_frame(self):
        # The rule overrides the cache ONLY for a reboot. Everything else still
        # follows the handshake-confirmed contract, and a frame only one
        # grammar accepts still falls through to that grammar.
        mgr = self._mgr()
        wb_only = frame(wb_record(4, 403, 564), 1)
        legacy_only = frame(legacy_record(bridge.FMB_EVT_INPUT, 403, 564), 1)
        self.assertIsNone(parse_legacy(wb_only, 1))
        self.assertIsNone(parse_wb(legacy_only, 1))
        mgr._wb_frame_slaves[9] = False                   # cached legacy
        self.assertEqual(mgr._parse_event_records(9, wb_only, 6,
                                                  len(wb_only), 1),
                         [(9, bridge.FMB_EVT_INPUT, 403, 564)])
        self.assertIs(mgr._wb_frame_slaves[9], False)     # cache not rewritten
        mgr._wb_frame_slaves[9] = True                    # cached WB
        self.assertEqual(mgr._parse_event_records(9, legacy_only, 6,
                                                  len(legacy_only), 1),
                         [(9, bridge.FMB_EVT_INPUT, 403, 564)])

    def test_single_record_legacy_frames_never_satisfy_wb_grammar(self):
        # Scope, stated narrowly on purpose: this corpus builds ONE-record
        # frames only, so on its own it says nothing about multi-record frames
        # (a two-record frame can be accepted where each record alone is not).
        # The general statement — for every n — is
        # test_no_first_record_a_legacy_frame_can_carry_satisfies_the_wb_gate
        # below.
        vals = (0, 1, 0x7F, 0x80, 0xFF, 0x0100, 0x0F0F, 0xFFFF)
        checked = 0
        for (evt_type, start, count) in all_event_ranges():
            for reg in range(start, start + count):
                for val in vals:
                    buf = frame(legacy_record(evt_type, reg, val), 1)
                    self.assertIsNotNone(parse_legacy(buf, 1))
                    self.assertIsNone(
                        parse_wb(buf, 1),
                        f"legacy type={evt_type} reg={reg} val={val} "
                        f"parsed as WB")
                    checked += 1
        self.assertGreater(checked, 500, "corpus went vacuous")

    def test_no_first_record_a_legacy_frame_can_carry_satisfies_the_wb_gate(self):
        # THE invariant the auto-detection rests on, stated at the gate so it
        # holds for a frame of ANY length: the WB parser accepts a record only
        # when buf[pos+1] is a wire code and buf[pos] equals that wire's payload
        # length. In a legacy record those two bytes are the type and the
        # register HIGH byte. The corpus below is every (type, reg_high) pair a
        # genuine legacy frame of this device set can START with — the
        # configured ranges (today [(0,0), (2,0), (3,0), (3,1), (3,2)]) PLUS a
        # reboot, which is not a configured range yet can stand first and whose
        # register is unconstrained, hence all 256 high bytes. None passes the
        # gate, so such a frame fails WB on its FIRST record and therefore as a
        # whole, for every n. A failure here means auto-detection is no longer
        # safe — see docs/contracts/fmb-event-wire.md — NOT that the test needs
        # relaxing.
        configured = {(evt_type, (reg >> 8) & 0xFF)
                      for (evt_type, start, count) in all_event_ranges()
                      for reg in range(start, start + count)}
        self.assertGreaterEqual(len(configured), 5, "pair corpus went vacuous")
        reboot = {(bridge.FMB_EVT_REBOOT, rh) for rh in range(256)}
        for (evt_type, reg_high) in sorted(configured | reboot):
            self.assertFalse(
                reg_high in WIRE_PLEN and WIRE_PLEN[reg_high] == evt_type,
                f"a legacy record type={evt_type} reg_high={reg_high:#04x} "
                f"now satisfies the WB gate — the two grammars became "
                f"ambiguous on a record a legacy frame can start with")

    def test_legacy_reboot_standing_first_is_not_hijacked(self):
        # The frame-level companion to the gate pin: a genuine legacy frame
        # whose FIRST record is a reboot must still read as legacy, at every
        # register high byte, even for a slave cached as legacy. (The other
        # no-hijack pin puts the reboot second.)
        mgr = self._mgr()
        mgr._wb_frame_slaves[9] = False
        for reg_high in range(256):
            reg = (reg_high << 8) | 0x05
            records = (legacy_record(bridge.FMB_EVT_REBOOT, reg, 0)
                       + legacy_record(bridge.FMB_EVT_COIL, 1, 1))
            buf = frame(records, 2)
            self.assertIsNone(parse_wb(buf, 2),
                              f"legacy reboot at reg_high={reg_high:#04x} "
                              f"parsed as WB")
            self.assertEqual(
                mgr._parse_event_records(9, buf, 6, len(buf), 2),
                [(9, bridge.FMB_EVT_REBOOT, reg, -1),
                 (9, bridge.FMB_EVT_COIL, 1, 1)])

    def test_configured_ranges_stay_below_their_collision_limit(self):
        # The readable corollary of the gate property: how far each type's
        # registers may grow before its FIRST-record bytes start colliding.
        # Derived from WIRE_PLEN, not hand-copied: COIL 0x0F00, DISCRETE
        # 0x0100 (not 0x0F00 — wires 1 and 2 both carry a 1-byte payload),
        # HOLDING 0x0300, INPUT unbounded (no wire code carries a 3-byte
        # payload, so an INPUT record never satisfies the gate).
        ranges = all_event_ranges()
        self.assertGreater(len(ranges), 10)
        checked = 0
        for (evt_type, start, count) in ranges:
            limit = first_record_collision_limit(evt_type)
            if limit is None:
                continue
            last = start + count - 1
            self.assertLess(last, limit,
                            f"range type={evt_type} {start}..{last} crosses "
                            f"the {limit:#06x} collision limit")
            checked += 1
        self.assertGreater(checked, 0, "no bounded type in the corpus")

    def test_configured_ranges_never_yield_count_one(self):
        # The second latent invariant: at COUNT == 1 the WB and legacy
        # configure_events frames have the same length and differ only in the
        # type byte, so an old firmware could ACCEPT the WB frame and configure
        # the WRONG event type. Safe only while no range map emits COUNT == 1 —
        # see docs/contracts/fmb-event-wire.md.
        ranges = all_event_ranges()
        self.assertGreater(len(ranges), 10)
        for (evt_type, start, count) in ranges:
            self.assertGreaterEqual(
                count, 2,
                f"range type={evt_type} start={start} has count=1 — the WB "
                f"and legacy configure_events frames become ambiguous")


# ── 10. Bus-garbage robustness ───────────────────────────────────────────────
class TestGarbageFrames(unittest.TestCase):
    def test_random_frames_never_raise_and_never_over_report(self):
        rnd = random.Random(20260728)
        mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
        for _ in range(2000):
            n_events = rnd.randrange(0, 256)
            data = bytes(rnd.randrange(0, 256)
                         for _ in range(rnd.randrange(0, 40)))
            buf = frame(data, n_events)
            out = mgr._parse_event_records(9, buf, 6, len(buf), n_events)
            if out is not None:
                self.assertLessEqual(len(out), n_events)


if __name__ == "__main__":
    unittest.main()
