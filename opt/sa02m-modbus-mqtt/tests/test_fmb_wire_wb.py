#!/usr/bin/env python3
"""configure_events (0x18): WB frame form, its scope, fallback and cache.

Pure-unit: no serial port, no MQTT broker — a fake serial that understands
both configure_events grammars. Pins:
  * the WB frame bytes (wire code = internal type + 1, one setting byte per
    register) and the unchanged legacy frame bytes;
  * count == 1 makes the two forms the same LENGTH — they differ only in the
    type byte, so nothing may key a decision on data_len alone;
  * the generation cache: seeded by the handshake, cleared on reboot and on
    re-registration, and a legacy-confirmed slave is never probed with WB again;
  * fallback: legacy only after WB goes unanswered, never a double frame;
  * scope: no WB probe for CE-02m-3 / DTV under `auto`, and the
    `fmb_event_wire: wb` hatch overrides that.

Contract home: docs/contracts/fmb-event-wire.md.

Run dev-side:  python -m unittest discover opt/sa02m-modbus-mqtt/tests
"""
from __future__ import annotations

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

MR_RANGES = [(bridge.FMB_EVT_COIL, 1, 6), (bridge.FMB_EVT_INPUT, 18, 8)]
CE_RANGES = [(bridge.FMB_EVT_INPUT, 500, 3), (bridge.FMB_EVT_INPUT, 510, 4)]


def crc16_ref(data: bytes) -> int:
    """Modbus CRC-16/IBM, written out here so the frame pins do not simply
    mirror the implementation they check."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(body: bytes) -> bytes:
    c = crc16_ref(body)
    return body + bytes([c & 0xFF, c >> 8])


def decode_configure(frame: bytes) -> tuple[str, int, int, int]:
    """(contract, evt_type, start_reg, count) from a configure_events frame.

    count == 1 is genuinely ambiguous (both forms are 5 payload bytes) — the
    ranges used here all have count >= 2, and TestFrameShapes pins that
    ambiguity separately.
    """
    data_len = frame[3]
    body = frame[4:4 + data_len]
    reg = (body[1] << 8) | body[2]
    count = body[3]
    if data_len == 4 + count and data_len != 5:
        wire = body[0]
        evt = (bridge.FMB_EVT_REBOOT if wire == 0x0F else wire - 1)
        return "wb", evt, reg, count
    if data_len == 5:
        return "legacy", body[0], reg, count
    raise AssertionError(f"unrecognized configure_events frame: {frame!r}")


class WireFakeSerial:
    """fmb_send_recv fake that acks per (contract, evt_type) pair."""

    def __init__(self, acks):
        # acks: iterable of (contract, evt_type)
        self.acks = set(acks)
        self.sent: list[tuple[str, int, int, int]] = []

    def fmb_send_recv(self, frame, min_len, max_len, timeout):
        addr = frame[0]
        decoded = decode_configure(bytes(frame))
        self.sent.append(decoded)
        if (decoded[0], decoded[1]) in self.acks:
            return bytes([addr, 0x46, 0x18, 1, 0x00, 0x00, 0x00])
        raise TimeoutError("no configure ack")

    def contracts(self) -> list[str]:
        return [s[0] for s in self.sent]


def make_mgr(dev_type="mr02m", wire_mode="auto", addr=5, ranges=None):
    mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
    mgr.register_device(addr, f"{dev_type}-ut-{addr}",
                        ranges if ranges is not None else MR_RANGES,
                        lambda *a: None, dev_type=dev_type,
                        wire_mode=wire_mode)
    return mgr, mgr._devices[addr]


# ── 1 & 2. Frame shapes ──────────────────────────────────────────────────────
class TestFrameShapes(unittest.TestCase):
    def test_wb_frame_reference_bytes(self):
        # wire = internal type + 1; COUNT setting bytes, one per register.
        cases = [
            (bridge.FMB_EVT_COIL, 1, 6, 1),        # wire 1
            (bridge.FMB_EVT_DISCRETE, 18, 2, 1),   # wire 2
            (bridge.FMB_EVT_HOLDING, 33, 6, 1),    # wire 3
            (bridge.FMB_EVT_INPUT, 403, 36, 1),    # wire 4
        ]
        for evt_type, reg, count, prio in cases:
            with self.subTest(evt_type=evt_type):
                record = (bytes([evt_type + 1, reg >> 8, reg & 0xFF, count])
                          + bytes([prio] * count))
                want = with_crc(bytes([5, 0x46, 0x18, len(record)]) + record)
                self.assertEqual(
                    bridge.build_fmb_configure_events_wb(
                        5, evt_type, reg, count, prio), want)

    def test_wb_frame_reboot_type_passes_through(self):
        # 0x0F is already a wire code — it must NOT be shifted by +1.
        f = bridge.build_fmb_configure_events_wb(
            5, bridge.FMB_EVT_REBOOT, 0, 0, 1)
        self.assertEqual(f[4], 0x0F)
        self.assertEqual(f[3], 4)

    def test_legacy_frame_reference_bytes_unchanged(self):
        want = with_crc(bytes([5, 0x46, 0x18, 5,
                               bridge.FMB_EVT_INPUT, 0x01, 0x93, 36, 1]))
        self.assertEqual(
            bridge.build_fmb_configure_events(
                5, bridge.FMB_EVT_INPUT, 403, 36, 1), want)

    def test_count_one_differs_only_in_the_type_byte(self):
        # Old firmware may therefore ACCEPT a WB count==1 frame and configure
        # the WRONG event type. No range map produces count 1 today; the
        # invariant is recorded in docs/contracts/fmb-event-wire.md.
        wb = bridge.build_fmb_configure_events_wb(
            5, bridge.FMB_EVT_INPUT, 403, 1, 1)
        legacy = bridge.build_fmb_configure_events(
            5, bridge.FMB_EVT_INPUT, 403, 1, 1)
        self.assertEqual(len(wb), len(legacy))
        self.assertEqual(wb[3], legacy[3])              # same data_len
        self.assertEqual(wb[5:-2], legacy[5:-2])        # same reg/count/setting
        self.assertNotEqual(wb[4], legacy[4])           # only the type byte


# ── 7. Generation cache ──────────────────────────────────────────────────────
class TestGenerationCache(unittest.TestCase):
    def test_seeded_by_the_handshake(self):
        mgr, dev = make_mgr()
        ser = WireFakeSerial({("wb", bridge.FMB_EVT_COIL),
                              ("wb", bridge.FMB_EVT_INPUT)})
        self.assertTrue(mgr._configure_device(ser, 5, dev))
        self.assertIs(mgr._wb_frame_slaves[5], True)

    def test_legacy_confirmed_slave_is_not_probed_with_wb_again(self):
        mgr, dev = make_mgr()
        ser = WireFakeSerial({("legacy", bridge.FMB_EVT_COIL),
                              ("legacy", bridge.FMB_EVT_INPUT)})
        self.assertTrue(mgr._configure_device(ser, 5, dev))
        self.assertIs(mgr._wb_frame_slaves[5], False)
        # Second pass over the same ranges: the known contract goes first and
        # is acked, so no WB frame is ever put on the bus again.
        dev["pending"] = list(dev["ranges"])
        ser2 = WireFakeSerial({("legacy", bridge.FMB_EVT_COIL),
                               ("legacy", bridge.FMB_EVT_INPUT)})
        self.assertTrue(mgr._configure_device(ser2, 5, dev))
        self.assertEqual(ser2.contracts(), ["legacy", "legacy"])

    def test_reboot_clears_the_cache(self):
        mgr, dev = make_mgr()
        mgr._wb_frame_slaves[5] = False
        mgr._dispatch(5, bridge.FMB_EVT_REBOOT, 0, -1)
        self.assertNotIn(5, mgr._wb_frame_slaves)
        self.assertFalse(dev["configured"])
        self.assertEqual(dev["pending"], dev["ranges"])

    def test_register_device_clears_the_cache(self):
        mgr, _ = make_mgr()
        mgr._wb_frame_slaves[5] = True
        mgr.register_device(5, "mr02m-ut-5", MR_RANGES, lambda *a: None,
                            dev_type="mr02m")
        self.assertNotIn(5, mgr._wb_frame_slaves)

    def test_unrecognized_wire_mode_falls_back_to_auto(self):
        _, dev = make_mgr(wire_mode="WB-ish")
        self.assertEqual(dev["wire_mode"], "auto")


# ── 8. Fallback ──────────────────────────────────────────────────────────────
class TestFallback(unittest.TestCase):
    def test_legacy_sent_after_wb_goes_unanswered(self):
        mgr, dev = make_mgr()
        ser = WireFakeSerial({("legacy", bridge.FMB_EVT_COIL),
                              ("legacy", bridge.FMB_EVT_INPUT)})
        self.assertTrue(mgr._configure_device(ser, 5, dev))
        self.assertEqual(ser.contracts(), ["wb", "legacy", "wb", "legacy"])

    def test_no_second_frame_when_wb_is_acked(self):
        mgr, dev = make_mgr()
        ser = WireFakeSerial({("wb", bridge.FMB_EVT_COIL),
                              ("wb", bridge.FMB_EVT_INPUT)})
        self.assertTrue(mgr._configure_device(ser, 5, dev))
        self.assertEqual(ser.contracts(), ["wb", "wb"])

    def test_both_forms_rejected_keeps_range_pending(self):
        mgr, dev = make_mgr()
        ser = WireFakeSerial(set())
        self.assertFalse(mgr._configure_device(ser, 5, dev))
        self.assertFalse(dev["configured"])
        self.assertEqual(dev["pending"], MR_RANGES)
        self.assertNotIn(5, mgr._wb_frame_slaves)


# ── 9. Scope of the WB probe ─────────────────────────────────────────────────
class TestWbProbeScope(unittest.TestCase):
    def test_ce02m3_gets_no_wb_frame_under_auto(self):
        mgr, dev = make_mgr(dev_type="ce02m3", addr=14, ranges=CE_RANGES)
        ser = WireFakeSerial({("legacy", bridge.FMB_EVT_INPUT)})
        self.assertTrue(mgr._configure_device(ser, 14, dev))
        self.assertEqual(ser.contracts(), ["legacy", "legacy"])

    def test_dtv_gets_no_wb_frame_under_auto(self):
        mgr, dev = make_mgr(dev_type="dtv", addr=15,
                            ranges=[(bridge.FMB_EVT_COIL, 1, 2)])
        ser = WireFakeSerial(set())
        self.assertFalse(mgr._configure_device(ser, 15, dev))
        self.assertEqual(ser.contracts(), ["legacy"])

    def test_cached_wb_does_not_unlock_the_probe_for_ce02m3(self):
        # The cache can be filled by the parser's guess (_parse_event_records
        # setdefault), not only by a handshake — so a CE cached as WB must
        # still never see a WB frame. Cost of the alternative: a wedged
        # CE-02m-3 recoverable only by a power cycle (CHANGELOG 1.0.5.46).
        mgr, dev = make_mgr(dev_type="ce02m3", addr=14, ranges=CE_RANGES)
        mgr._wb_frame_slaves[14] = True
        ser = WireFakeSerial({("legacy", bridge.FMB_EVT_INPUT)})
        self.assertTrue(mgr._configure_device(ser, 14, dev))
        self.assertEqual(ser.contracts(), ["legacy", "legacy"])

    def test_cached_wb_does_not_unlock_the_probe_for_dtv(self):
        mgr, dev = make_mgr(dev_type="dtv", addr=15,
                            ranges=[(bridge.FMB_EVT_COIL, 1, 2)])
        mgr._wb_frame_slaves[15] = True
        ser = WireFakeSerial({("legacy", bridge.FMB_EVT_COIL)})
        self.assertTrue(mgr._configure_device(ser, 15, dev))
        self.assertEqual(ser.contracts(), ["legacy"])

    def test_explicit_wb_hatch_probes_ce02m3(self):
        mgr, dev = make_mgr(dev_type="ce02m3", wire_mode="wb", addr=14,
                            ranges=CE_RANGES)
        ser = WireFakeSerial({("wb", bridge.FMB_EVT_INPUT)})
        self.assertTrue(mgr._configure_device(ser, 14, dev))
        self.assertEqual(ser.contracts(), ["wb", "wb"])

    def test_explicit_legacy_hatch_silences_the_probe_on_mr02m(self):
        mgr, dev = make_mgr(wire_mode="legacy")
        ser = WireFakeSerial({("legacy", bridge.FMB_EVT_COIL),
                              ("legacy", bridge.FMB_EVT_INPUT)})
        self.assertTrue(mgr._configure_device(ser, 5, dev))
        self.assertEqual(ser.contracts(), ["legacy", "legacy"])


if __name__ == "__main__":
    unittest.main()
