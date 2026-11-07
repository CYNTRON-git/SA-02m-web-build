#!/usr/bin/env python3
"""CE-02m-3 on the WB Fast Modbus wire: subscribe form by firmware, reply mask.

Pure-unit: no serial port, no MQTT broker. A fake bus that answers the way the
CE-02m-3 core does (issue CYNTRON-git/SA-02m-web-build#193, bench 1.135
2026-09-29): a WB configure_events record is applied and answered with the
post-apply mask; a legacy record with COUNT > 1 is rejected with NO reply.
Pins:
  * the wire the bridge picks for a `ce02m3` under `fmb_event_wire: auto` from
    the firmware version the poller read (HR320..323): >= 1.0.6.13 -> WB only;
    lower (the upper-port wedge firmware) or unknown -> no 0x18 at all;
  * the explicit `legacy` hatch still wins over a WB-capable firmware;
  * a WB subscription counts only when the reply mask confirms every requested
    register (CRC checked, ceil(count/8) bytes per range, LSB first);
  * the power-event disable range INPUT 518..546 (setting 0) on >= 1.0.7.6
    only, and a refused disable never un-configures U/I;
  * the #193 / bench 0x11 fixtures decode and publish with the classic scaling.

Fixtures are the CRC-complete frames of #193 and the bench capture
/root/bench/ce_1a-1223.txt. Contract home: docs/contracts/fmb-event-wire.md.

Run dev-side:  python -m unittest discover opt/sa02m-modbus-mqtt/tests
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

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

ADDR = 14
CE_RANGES = [(bridge.FMB_EVT_INPUT, 500, 3), (bridge.FMB_EVT_INPUT, 510, 4)]

# ── #193 fixtures (CRC-complete, verified against the CE reference parser) ──
FX_WB_SUBSCRIBE_510 = bytes.fromhex("0E 46 18 08 04 01 FE 04 01 01 01 01 44 62")
FX_LEGACY_SUBSCRIBE_510 = bytes.fromhex("0E 46 18 05 03 01 FE 04 01 BD D2")
FX_TWO_RANGE_REPLY = bytes.fromhex("0E 46 18 02 0F 07 6B A8")    # 510/4 + 500/3
FX_WB_EVENTS_510_511 = bytes.fromhex(
    "0E 46 11 00 02 0C 02 04 01 FE DC 00 02 04 01 FF D0 00 7B 59")
# Bench 1.135, 2026-09-29, CE fw 1.0.7.5: one 0x11 frame of 11 WB records.
FX_BENCH_EVENTS = bytes.fromhex(
    "0E 46 11 AA 0B 42 02 04 01 F5 02 09 02 04 02 00 B4 00 02 04 02 01 B4 00"
    " 02 04 01 F4 0F 09 02 04 01 F5 10 09 02 04 02 00 C4 00 02 04 02 01 C4 00"
    " 02 04 02 00 B4 00 02 04 02 01 B4 00 02 04 02 00 C8 00 02 04 02 01 C8 00"
    " 62 BB")


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


def wb_records(frame: bytes) -> list[tuple[int, int, int, bytes]] | None:
    """(wire, reg, count, settings) per record of a WB 0x18 frame, or None
    when the payload does not walk as WB records exactly."""
    data_len = frame[3]
    body = frame[4:4 + data_len]
    out, pos = [], 0
    while pos < len(body):
        if pos + 4 > len(body):
            return None
        wire, reg, count = body[pos], (body[pos + 1] << 8) | body[pos + 2], \
            body[pos + 3]
        settings = body[pos + 4:pos + 4 + count]
        if wire not in (1, 2, 3, 4, 0x0F) or len(settings) != count:
            return None
        out.append((wire, reg, count, bytes(settings)))
        pos += 4 + count
    return out


def is_legacy(frame: bytes) -> bool:
    return frame[3] == 5 and frame[4] in (0, 1, 2, 3, 0x0F) and frame[7] != 1


class CeWireBus:
    """fmb_send_recv fake answering like the CE-02m-3 core.

    WB record -> reply [addr][46][18][MASK_LEN][mask…][CRC], one bit per
    register whose setting is non-zero (the post-apply state); a legacy record
    with COUNT > 1 -> no reply (TimeoutError), as on the bench. `reply_for`
    lets a test replace the reply for a given start register.
    """

    def __init__(self, fw_wb: bool = True):
        self.fw_wb = fw_wb
        self.sent: list[bytes] = []
        self.reply_for: dict[int, object] = {}

    @staticmethod
    def mask_reply(addr: int, records) -> bytes:
        masks = b""
        for _wire, _reg, count, settings in records:
            bits = [1 if s else 0 for s in settings]
            for i in range(0, count, 8):
                byte = 0
                for j, bit in enumerate(bits[i:i + 8]):
                    byte |= bit << j
                masks += bytes([byte])
        return with_crc(bytes([addr, 0x46, 0x18, len(masks)]) + masks)

    def fmb_send_recv(self, frame, min_len, max_len, timeout):
        frame = bytes(frame)
        self.sent.append(frame)
        addr = frame[0]
        records = wb_records(frame)
        if records is not None and not is_legacy(frame) and self.fw_wb:
            start = records[0][1]
            custom = self.reply_for.get(start)
            if custom == "timeout":
                raise TimeoutError("no configure reply")
            if custom is not None:
                return custom
            return self.mask_reply(addr, records)
        raise TimeoutError("no configure reply")

    def wb_starts(self) -> list[int]:
        return [wb_records(f)[0][1] for f in self.sent
                if wb_records(f) is not None and not is_legacy(f)]

    def legacy_frames(self) -> list[bytes]:
        return [f for f in self.sent if is_legacy(f)]


class CeFwPoller:
    """Duck-typed CE poller: classic reads done, firmware version known or not."""

    device_id = "ce02m3-ut-14"

    def __init__(self, fw):
        self.fw = fw
        self.covered: list[bool] = []

    def fmb_firmware_version(self):
        return self.fw

    def classic_ready_for_fmb(self, min_ok: int = 2) -> bool:
        return True

    def set_fmb_io_covered(self, covered: bool) -> None:
        self.covered.append(covered)

    def poll_io(self) -> None:
        pass


def make_ce(fw, wire_mode="auto"):
    mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
    poller = CeFwPoller(fw)
    mgr.register_device(ADDR, "ce02m3-ut-14", list(CE_RANGES),
                        lambda *a: None, poller=poller, dev_type="ce02m3",
                        wire_mode=wire_mode)
    return mgr, mgr._devices[ADDR]


# ── 1. Wire choice by firmware version (fmb_event_wire: auto) ─────────────────
class TestCeWireByFirmware(unittest.TestCase):
    def test_fw_1075_gets_the_wb_frame_never_the_legacy_one(self):
        # RED on 1.0.6.61: the bridge sent FX_LEGACY_SUBSCRIBE_510 here.
        mgr, dev = make_ce((1, 0, 7, 5))
        bus = CeWireBus()
        self.assertTrue(mgr._configure_device(bus, ADDR, dev))
        self.assertIn(FX_WB_SUBSCRIBE_510, bus.sent)
        self.assertNotIn(FX_LEGACY_SUBSCRIBE_510, bus.sent)
        self.assertEqual(bus.legacy_frames(), [])
        self.assertEqual(dev["pending"], [])
        self.assertIs(mgr._wb_frame_slaves[ADDR], True)

    def test_fw_10613_is_the_first_wb_version(self):
        mgr, dev = make_ce((1, 0, 6, 13))
        bus = CeWireBus()
        self.assertTrue(mgr._configure_device(bus, ADDR, dev))
        self.assertEqual(bus.legacy_frames(), [])
        self.assertEqual(sorted(bus.wb_starts()), [500, 510])

    def test_wedge_prone_firmware_gets_no_configure_events(self):
        # CE <= 1.0.6.12 answered a 0x18 from the upper port via the END
        # connector and left the upper receiver off until a power cycle
        # (1.0.6.11 accepted legacy count 3/4; 1.0.6.12 still wedges on a
        # count-1 record). 1.0.6.13 answers on the request's port.
        for fw in ((1, 0, 6, 12), (1, 0, 6, 11), (1, 0, 5, 46)):
            with self.subTest(fw=fw):
                mgr, dev = make_ce(fw)
                bus = CeWireBus(fw_wb=False)
                self.assertFalse(mgr._configure_device(bus, ADDR, dev))
                self.assertEqual(bus.sent, [])
                self.assertFalse(dev["configured"])
                self.assertEqual(dev["pending"], CE_RANGES)

    def test_wedge_prone_firmware_is_logged_once_with_the_version(self):
        mgr, dev = make_ce((1, 0, 6, 12))
        bus = CeWireBus(fw_wb=False)
        with self.assertLogs("fmb.COMT", level="DEBUG") as cm:
            mgr._configure_device(bus, ADDR, dev)
            mgr._configure_device(bus, ADDR, dev)
        lines = [r for r in cm.records
                 if "1.0.6.12" in r.getMessage() and r.levelno >= 20]
        self.assertEqual(len(lines), 1, [r.getMessage() for r in cm.records])

    def test_fw_unknown_sends_no_configure_events_at_all(self):
        # RED on 1.0.6.61: two legacy frames went out with no version known.
        mgr, dev = make_ce(None)
        bus = CeWireBus()
        self.assertFalse(mgr._configure_device(bus, ADDR, dev))
        self.assertEqual(bus.sent, [])
        self.assertFalse(dev["configured"])
        self.assertEqual(dev["pending"], CE_RANGES)

    def test_fw_unknown_is_logged_once_not_per_pass(self):
        mgr, dev = make_ce(None)
        bus = CeWireBus()
        with self.assertLogs("fmb.COMT", level="DEBUG") as cm:
            mgr._configure_device(bus, ADDR, dev)
            mgr._configure_device(bus, ADDR, dev)
        lines = [r for r in cm.records
                 if "firmware" in r.getMessage()
                 and "unknown" in r.getMessage()
                 and r.levelno >= 20]
        self.assertEqual(len(lines), 1, [r.getMessage() for r in cm.records])

    def test_fw_known_later_is_re_evaluated(self):
        mgr, dev = make_ce(None)
        bus = CeWireBus()
        mgr._configure_device(bus, ADDR, dev)
        self.assertEqual(bus.sent, [])
        dev["poller"].fw = (1, 0, 7, 5)
        self.assertTrue(mgr._configure_device(bus, ADDR, dev))
        self.assertIn(FX_WB_SUBSCRIBE_510, bus.sent)

    def test_explicit_legacy_wins_over_a_wb_firmware(self):
        mgr, dev = make_ce((1, 0, 7, 5), wire_mode="legacy")
        bus = CeWireBus()
        mgr._configure_device(bus, ADDR, dev)
        self.assertEqual(bus.wb_starts(), [])
        self.assertIn(FX_LEGACY_SUBSCRIBE_510, bus.sent)


# ── 2. Reply mask verification (WB contract) ─────────────────────────────────
class TestCeReplyMask(unittest.TestCase):
    def test_two_range_reference_reply_confirms_both_ranges(self):
        # #193: reply to the concatenated 510/4 + 500/3 subscribe.
        self.assertTrue(bridge.fmb_configure_reply_confirms(
            FX_TWO_RANGE_REPLY, ADDR, [(4, 1), (3, 1)]))

    def test_reference_reply_behind_arbitration_noise(self):
        self.assertTrue(bridge.fmb_configure_reply_confirms(
            b"\xff\xff" + FX_TWO_RANGE_REPLY, ADDR, [(4, 1), (3, 1)]))

    def test_reference_reply_does_not_confirm_a_wider_request(self):
        # 510..514 would need bit 4 of the first mask byte — 0x0F lacks it.
        self.assertFalse(bridge.fmb_configure_reply_confirms(
            FX_TWO_RANGE_REPLY, ADDR, [(5, 1), (3, 1)]))

    def test_reference_reply_with_a_bad_crc_confirms_nothing(self):
        bad = FX_TWO_RANGE_REPLY[:-1] + bytes([FX_TWO_RANGE_REPLY[-1] ^ 0x01])
        self.assertFalse(bridge.fmb_configure_reply_confirms(
            bad, ADDR, [(4, 1), (3, 1)]))

    def test_disable_reply_must_be_all_zero(self):
        zero = with_crc(bytes([ADDR, 0x46, 0x18, 4, 0, 0, 0, 0]))
        one_left = with_crc(bytes([ADDR, 0x46, 0x18, 4, 0, 0, 0x10, 0]))
        self.assertTrue(bridge.fmb_configure_reply_confirms(
            zero, ADDR, [(29, 0)]))
        self.assertFalse(bridge.fmb_configure_reply_confirms(
            one_left, ADDR, [(29, 0)]))

    def test_cleared_bit_leaves_that_range_pending(self):
        mgr, dev = make_ce((1, 0, 7, 5))
        bus = CeWireBus()
        # 510..513 answered with bit 2 (reg 512) cleared.
        bus.reply_for[510] = with_crc(bytes([ADDR, 0x46, 0x18, 1, 0x0B]))
        self.assertFalse(mgr._configure_device(bus, ADDR, dev))
        self.assertTrue(dev["configured"])                 # 500..502 confirmed
        self.assertEqual(dev["pending"], [(bridge.FMB_EVT_INPUT, 510, 4)])

    def test_crc_bad_reply_leaves_that_range_pending(self):
        mgr, dev = make_ce((1, 0, 7, 5))
        bus = CeWireBus()
        good = CeWireBus.mask_reply(ADDR, [(4, 510, 4, b"\x01" * 4)])
        bus.reply_for[510] = good[:-1] + bytes([good[-1] ^ 0xFF])
        self.assertFalse(mgr._configure_device(bus, ADDR, dev))
        self.assertIn(FX_WB_SUBSCRIBE_510, bus.sent)
        self.assertEqual(dev["pending"], [(bridge.FMB_EVT_INPUT, 510, 4)])

    def test_short_reply_leaves_that_range_pending(self):
        mgr, dev = make_ce((1, 0, 7, 5))
        bus = CeWireBus()
        bus.reply_for[510] = bytes([ADDR, 0x46, 0x18, 1])
        self.assertFalse(mgr._configure_device(bus, ADDR, dev))
        self.assertEqual(dev["pending"], [(bridge.FMB_EVT_INPUT, 510, 4)])

    def test_mismatch_is_logged_with_the_mask_seen(self):
        mgr, dev = make_ce((1, 0, 7, 5))
        bus = CeWireBus()
        bus.reply_for[510] = with_crc(bytes([ADDR, 0x46, 0x18, 1, 0x0B]))
        with self.assertLogs("fmb.COMT", level="WARNING") as cm:
            mgr._configure_device(bus, ADDR, dev)
        self.assertTrue(any("0b" in r.getMessage().lower()
                            and "510" in r.getMessage() for r in cm.records),
                        [r.getMessage() for r in cm.records])


# ── 3. Power events off on CE >= 1.0.7.6 ─────────────────────────────────────
FX_DISABLE_518_546 = with_crc(
    bytes([ADDR, 0x46, 0x18, 4 + 29, 0x04, 0x02, 0x06, 29]) + bytes(29))


class TestCePowerEventsDisable(unittest.TestCase):
    def test_disable_range_sent_on_1076(self):
        mgr, dev = make_ce((1, 0, 7, 6))
        bus = CeWireBus()
        self.assertTrue(mgr._configure_device(bus, ADDR, dev))
        self.assertIn(FX_DISABLE_518_546, bus.sent)
        # The disable is not an event range of ours: never pending, never
        # part of the registered ranges.
        self.assertEqual(dev["pending"], [])
        self.assertEqual(dev["ranges"], CE_RANGES)

    def test_disable_range_not_sent_below_1076(self):
        mgr, dev = make_ce((1, 0, 7, 5))
        bus = CeWireBus()
        mgr._configure_device(bus, ADDR, dev)
        self.assertNotIn(518, bus.wb_starts())

    def test_unanswered_disable_leaves_u_and_i_configured(self):
        mgr, dev = make_ce((1, 0, 7, 6))
        bus = CeWireBus()
        bus.reply_for[518] = "timeout"
        with self.assertLogs("fmb.COMT", level="WARNING") as cm:
            self.assertTrue(mgr._configure_device(bus, ADDR, dev))
        self.assertTrue(dev["configured"])
        self.assertEqual(dev["pending"], [])
        self.assertTrue(any("518" in r.getMessage() for r in cm.records),
                        [r.getMessage() for r in cm.records])

    def test_refused_disable_warns_once_then_debug(self):
        mgr, dev = make_ce((1, 0, 7, 6))
        bus = CeWireBus()
        bus.reply_for[518] = "timeout"
        mgr._configure_device(bus, ADDR, dev)
        dev["pending"] = list(CE_RANGES)        # e.g. after a reboot event
        with self.assertLogs("fmb.COMT", level="DEBUG") as cm:
            mgr._configure_device(bus, ADDR, dev)
        lines = [r for r in cm.records if "518" in r.getMessage()]
        self.assertTrue(lines)
        self.assertTrue(all(r.levelname == "DEBUG" for r in lines),
                        [r.levelname for r in lines])
        self.assertEqual(bus.sent.count(FX_DISABLE_518_546), 2)

    def test_disable_resent_after_a_reboot_event(self):
        mgr, dev = make_ce((1, 0, 7, 6))
        bus = CeWireBus()
        mgr._configure_device(bus, ADDR, dev)
        mgr._dispatch(ADDR, bridge.FMB_EVT_REBOOT, 0, -1)
        mgr._configure_device(bus, ADDR, dev)
        self.assertEqual(bus.sent.count(FX_DISABLE_518_546), 2)


# ── 3b. MR-02m also requires a complete, validated configure ACK ──
class TestMaskScope(unittest.TestCase):
    def test_mr02m_wb_prefix_ack_stays_pending_without_wire_cache(self):
        mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
        mgr.register_device(5, "mr02m-ut-5", [(bridge.FMB_EVT_COIL, 1, 6)],
                            lambda *a: None, dev_type="mr02m")

        class PrefixAck:
            # The former positive fixture has a zero mask and invalid CRC.
            def fmb_send_recv(self, frame, *a):
                return bytes([5, 0x46, 0x18, 1, 0x00, 0x00, 0x00])
        self.assertFalse(mgr._configure_device(PrefixAck(), 5,
                                               mgr._devices[5]))
        self.assertFalse(mgr._devices[5]["configured"])
        self.assertEqual(mgr._devices[5]["pending"], [(bridge.FMB_EVT_COIL, 1, 6)])
        self.assertNotIn(5, mgr._wb_frame_slaves)


# ── 3c. Reply-mask edges: wrong length, padding bits, the disable call site ──
def reply(mask: bytes) -> bytes:
    """CRC-valid configure_events reply carrying `mask` from addr 14."""
    return with_crc(bytes([ADDR, 0x46, 0x18, len(mask)]) + mask)


class TestReplyMaskLength(unittest.TestCase):
    """Contract §1: a reply whose mask is not ceil(count/8) bytes per range is
    «не применён» — even when its CRC is valid. A longer mask must not be
    accepted on its leading bytes; a shorter one must not raise out of the
    configure pass (configure_all runs unwrapped at port start-up)."""

    def test_wrong_length_masks_confirm_nothing_and_never_raise(self):
        cases = [(b"\x0f\x00", [(4, 1)]),           # longer, leading byte OK
                 (b"", [(4, 1)]),                   # empty
                 (b"\x00\x00\x00", [(29, 0)]),      # shorter than 4 bytes
                 (b"\x0f", [(4, 1), (3, 1)])]       # one range short
        for mask, requested in cases:
            with self.subTest(mask=mask.hex(), requested=requested):
                self.assertFalse(bridge.fmb_configure_reply_confirms(
                    reply(mask), ADDR, requested))

    def test_longer_mask_leaves_the_range_pending(self):
        mgr, dev = make_ce((1, 0, 7, 5))
        bus = CeWireBus()
        bus.reply_for[510] = reply(b"\x0f\x00")
        self.assertFalse(mgr._configure_device(bus, ADDR, dev))
        self.assertEqual(dev["pending"], [(bridge.FMB_EVT_INPUT, 510, 4)])

    def test_empty_mask_leaves_the_range_pending(self):
        mgr, dev = make_ce((1, 0, 7, 5))
        bus = CeWireBus()
        bus.reply_for[510] = reply(b"")
        self.assertFalse(mgr._configure_device(bus, ADDR, dev))
        self.assertEqual(dev["pending"], [(bridge.FMB_EVT_INPUT, 510, 4)])

    def test_short_disable_mask_is_refused_not_raised(self):
        mgr, dev = make_ce((1, 0, 7, 6))
        bus = CeWireBus()
        bus.reply_for[518] = reply(b"\x00\x00\x00")
        with self.assertLogs("fmb.COMT", level="WARNING") as cm:
            self.assertTrue(mgr._configure_device(bus, ADDR, dev))
        self.assertTrue(any("518" in r.getMessage()
                            and "not confirmed" in r.getMessage()
                            for r in cm.records),
                        [r.getMessage() for r in cm.records])

    def test_configure_all_survives_a_short_mask(self):
        import bridge_fmb
        from unittest import mock
        mgr, dev = make_ce((1, 0, 7, 5))
        dev["poller"].device_id = "ce02m3-ut-14"
        bus = CeWireBus()
        bus.reply_for[510] = reply(b"")
        with mock.patch.object(bridge_fmb.time, "sleep", lambda s: None), \
                mock.patch.object(bridge, "get_port", lambda *a, **k: bus):
            mgr.configure_all(only_ready=True)          # must not raise
        self.assertTrue(dev["configured"])              # 500..502 confirmed
        self.assertEqual(dev["pending"], [(bridge.FMB_EVT_INPUT, 510, 4)])


class TestReplyMaskPadding(unittest.TestCase):
    """Contract §1: bits past COUNT in a range's last mask byte are padding
    and are not compared."""

    def test_padding_bits_set_still_confirm(self):
        self.assertTrue(bridge.fmb_configure_reply_confirms(
            reply(b"\xff"), ADDR, [(4, 1)]))
        # 29 registers disabled: bits 5..7 of byte 4 are padding.
        self.assertTrue(bridge.fmb_configure_reply_confirms(
            reply(b"\x00\x00\x00\xe0"), ADDR, [(29, 0)]))

    def test_padding_bits_set_configure_the_range(self):
        mgr, dev = make_ce((1, 0, 7, 5))
        bus = CeWireBus()
        bus.reply_for[500] = reply(b"\xff")     # 3 used bits + 5 padding
        bus.reply_for[510] = reply(b"\xff")     # 4 used bits + 4 padding
        self.assertTrue(mgr._configure_device(bus, ADDR, dev))
        self.assertEqual(dev["pending"], [])


class TestPowerDisableConfirmation(unittest.TestCase):
    """Contract §3: the 518..546 disable waits for a ZERO mask — checked at
    the call site, not only in the pure helper."""

    def test_zero_mask_reply_is_confirmed(self):
        mgr, dev = make_ce((1, 0, 7, 6))
        bus = CeWireBus()                   # answers the disable with 0x00 x4
        with self.assertLogs("fmb.COMT", level="DEBUG") as cm:
            self.assertTrue(mgr._configure_device(bus, ADDR, dev))
        msgs = [r.getMessage() for r in cm.records]
        self.assertTrue(any("518..546 disabled" in m for m in msgs), msgs)
        self.assertFalse(any("not confirmed" in m for m in msgs), msgs)
        self.assertFalse([r for r in cm.records if r.levelno >= 30], msgs)
        self.assertFalse(dev["power_off_warned"])

    def test_non_zero_mask_reply_is_not_confirmed(self):
        mgr, dev = make_ce((1, 0, 7, 6))
        bus = CeWireBus()
        bus.reply_for[518] = reply(b"\x00\x00\x10\x00")   # reg 538 left on
        with self.assertLogs("fmb.COMT", level="DEBUG") as cm:
            self.assertTrue(mgr._configure_device(bus, ADDR, dev))
        msgs = [r.getMessage() for r in cm.records]
        self.assertFalse(any("518..546 disabled" in m for m in msgs), msgs)
        self.assertTrue(any("not confirmed" in m and "00001000" in m
                            for m in msgs), msgs)
        self.assertTrue(dev["power_off_warned"])
        self.assertEqual(dev["pending"], [])


# ── 3d. A firmware getter that raises fails closed, and says so ─────────────
class _RaisingFwPoller(CeFwPoller):
    def fmb_firmware_version(self):
        raise RuntimeError("fw getter boom")


class TestFirmwareGetterRaises(unittest.TestCase):
    def test_raise_sends_nothing_and_is_logged_once(self):
        mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
        mgr.register_device(ADDR, "ce02m3-ut-14", list(CE_RANGES),
                            lambda *a: None, poller=_RaisingFwPoller(None),
                            dev_type="ce02m3")
        dev = mgr._devices[ADDR]
        bus = CeWireBus()
        with self.assertLogs("fmb.COMT", level="DEBUG") as cm:
            self.assertFalse(mgr._configure_device(bus, ADDR, dev))
            self.assertFalse(mgr._configure_device(bus, ADDR, dev))
        self.assertEqual(bus.sent, [])
        self.assertEqual(dev["pending"], CE_RANGES)
        boom = [r for r in cm.records if "fw getter boom" in r.getMessage()
                and r.levelno >= 30]
        self.assertEqual(len(boom), 1, [r.getMessage() for r in cm.records])


# ── 4. CE version read (HR320..323) ──────────────────────────────────────────
class _NullPub:
    def __getattr__(self, name):
        return lambda *a, **k: None


class TestCePollerFirmwareRead(unittest.TestCase):
    def _poller(self):
        return bridge.CE02M3Poller(
            {"id": "ce02m3-ut-14", "type": "ce02m3", "address": ADDR},
            _NullPub())

    def test_setup_reads_hr320_count_4_as_a_tuple(self):
        p = self._poller()
        calls = []

        def fake_read(addr, start, count):
            calls.append((addr, start, count))
            return [1, 0, 7, 5]
        p.read_holding_registers = fake_read
        p.setup()
        self.assertEqual(calls, [(ADDR, 320, 4)])
        self.assertEqual(p.fmb_firmware_version(), (1, 0, 7, 5))

    def test_failed_read_leaves_it_unknown_and_is_retried(self):
        p = self._poller()
        answers = [OSError("timeout"), [1, 0, 6, 12]]

        def fake_read(addr, start, count):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        p.read_holding_registers = fake_read
        p.setup()
        self.assertIsNone(p._fw_version)
        self.assertEqual(p.fmb_firmware_version(), (1, 0, 6, 12))

    def test_short_read_is_unknown_not_a_short_version(self):
        # (1, 0, 7) would compare >= (1, 0, 6, 13) and unlock the WB wire.
        p = self._poller()
        p.read_holding_registers = lambda a, s, c: [1, 0, 7]
        p.setup()
        self.assertIsNone(p._fw_version)

    def test_reboot_event_forgets_the_cached_version(self):
        # A reboot may follow a reflash: the next 0x18 must be decided on the
        # firmware that runs now (a downgrade to <= 1.0.6.12 would wedge).
        p = self._poller()
        reads = [[1, 0, 7, 5], [1, 0, 6, 12]]
        p.read_holding_registers = lambda a, s, c: reads.pop(0)
        p.setup()
        mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
        mgr.register_device(ADDR, p.device_id, list(CE_RANGES),
                            p.fmb_dispatch, poller=p, dev_type="ce02m3")
        mgr._dispatch(ADDR, bridge.FMB_EVT_REBOOT, 0, -1)
        bus = CeWireBus()
        self.assertFalse(mgr._configure_device(bus, ADDR, mgr._devices[ADDR]))
        self.assertEqual(bus.sent, [])
        self.assertEqual(p.fmb_firmware_version(), (1, 0, 6, 12))


# ── 5. 0x11 fixtures: WB records decode and publish like the classic poll ────
class _RecPub:
    def __init__(self):
        self.controls: list[tuple[str, str, str]] = []

    def pub_control(self, device_id, name, value, force=False):
        self.controls.append((device_id, name, value))

    def __getattr__(self, name):
        return lambda *a, **k: None


class _FrameBus:
    def __init__(self, frame: bytes):
        self.frame = frame

    def fmb_send_recv(self, frame, min_len, max_len, timeout):
        return self.frame


def wb_event_frame(regs_vals: list[tuple[int, int]]) -> bytes:
    recs = b"".join(bytes([2, 4, r >> 8, r & 0xFF, v & 0xFF, v >> 8])
                    for r, v in regs_vals)
    return with_crc(bytes([ADDR, 0x46, 0x11, 0x00, len(regs_vals), len(recs)])
                    + recs)


class TestCeWidePower(unittest.TestCase):
    def connect(self, fw=(1, 0, 7, 19), refuse=False, publisher=None, **cfg):
        self.pub = publisher if publisher is not None else _RecPub()
        self.poller = bridge.CE02M3Poller(dict(id="ce", address=ADDR, **cfg), self.pub)
        self.poller._fw_version = fw
        self.mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
        self.mgr.register_device(ADDR, "ce", self.poller.fmb_event_ranges(),
                                 self.poller.fmb_dispatch, poller=self.poller, dev_type="ce02m3")
        self.bus = CeWireBus()
        if refuse:
            self.bus.reply_for[518] = with_crc(bytes([ADDR, 0x46, 0x18, 4, 255, 255, 255, 31]))
        self.mgr._configure_device(self.bus, ADDR, self.mgr._devices[ADDR])

    def feed(self, *records):
        data = bytes.fromhex(" ".join(records))
        frame = with_crc(bytes([ADDR, 0x46, 0x11, 0, len(records), len(data)]) + data)
        _, events = self.mgr._poll_once(_FrameBus(frame))
        for event in events:
            self.mgr._dispatch(*event)
        if isinstance(self.pub, _RecPub):
            return [(name, value) for _, name, value in self.pub.controls]

    def test_event_reaches_retained_mqtt_and_deduplicates_classic_sample(self):
        import bridge_mqtt
        with mock.patch.object(bridge_mqtt.mqtt, "Client") as client, \
                mock.patch.object(bridge_mqtt.DeviceLiveCache, "_controls", {}):
            self.connect(publisher=bridge_mqtt.MQTTPublisher({}))
            self.feed("04 04 02 12 f0 ff ff ff")
            regs = [0] * 48
            regs[30:32] = [0xfff0, 0xffff]
            self.poller._read_power_input_block = lambda: regs
            self.poller._poll_power()
            self.feed("04 04 02 12 f8 ff ff ff")
            calls = [c for c in client.return_value.publish.call_args_list
                     if c.args[0] == "/devices/ce/controls/reactive_c"]
            self.assertEqual(calls, [
                mock.call("/devices/ce/controls/reactive_c", "-16", qos=1, retain=True),
                mock.call("/devices/ce/controls/reactive_c", "-8", qos=1, retain=True),
            ])
            self.assertEqual(bridge_mqtt.DeviceLiveCache._controls["ce"]["reactive_c"], "-8")

    def test_all_power_disabled_keeps_single_disable_and_ignores_queued_records(self):
        self.connect(channels_enabled={key: False for key in (
            "power_active", "power_reactive", "power_apparent", "frequency", "power_factor")})
        frame, = [frame for frame in self.bus.sent if int.from_bytes(frame[5:7], "big") == 518]
        self.assertEqual(frame[8:-2], bytes(29))
        self.assertEqual(self.feed("04 04 02 12 f0 ff ff ff"), [])

    def test_meter_link_loss_preserves_all_event_values_until_classic_recovery(self):
        import bridge_mqtt
        with mock.patch.object(bridge_mqtt.mqtt, "Client") as client, \
                mock.patch.object(bridge_mqtt.DeviceLiveCache, "_controls", {}), \
                mock.patch.object(bridge_mqtt.DeviceLiveCache, "_errors", {}):
            self.connect(publisher=bridge_mqtt.MQTTPublisher({}))
            cache = bridge_mqtt.DeviceLiveCache
            channels = ("voltage_a", "current_c", "reactive_c")
            self.feed("02 04 01 f4 fc 08", "02 04 02 00 b4 00", "04 04 02 12 f0 ff ff ff")
            values = {name: cache._controls["ce"][name] for name in channels}
            self.assertEqual(values, dict(zip(channels, ("230.0", "0.18", "-16"))))
            self.poller._set_meter_link(True)
            for name in channels:
                client.return_value.publish.assert_any_call(
                    f"/devices/ce/controls/{name}/meta/error", "r", qos=1, retain=True)
            client.return_value.publish.reset_mock()
            self.feed("02 04 01 f4 00 00", "02 04 02 00 00 00", "04 04 02 12 00 00 00 00")
            client.return_value.publish.assert_not_called()
            self.assertEqual({name: cache._controls["ce"][name] for name in channels}, values)
            self.assertTrue(all(cache._errors["ce"][name] == "r" for name in channels))
            fresh = [0] * 48
            fresh[0], fresh[12], fresh[30], fresh[31] = 2310, 220, 0xfff8, 0xffff
            self.poller._read_power_input_block = lambda: fresh
            self.poller._poll_power()
            for name in channels:
                client.return_value.publish.assert_any_call(
                    f"/devices/ce/controls/{name}/meta/error", "", qos=1, retain=True)
            self.feed("02 04 01 f4 10 09", "02 04 02 00 fa 00", "04 04 02 12 fc ff ff ff")
            self.assertEqual([cache._controls["ce"][name] for name in channels], ["232.0", "0.25", "-4"])

    def test_signed_power_and_scalar_profile_match_classic_poll(self):
        self.connect(channels_enabled={"power_apparent": True})
        observed = self.feed("04 04 02 12 f0 ff ff ff", "04 04 02 0a 2e 00 00 00",
                             "04 04 02 1c 00 00 01 00", "02 04 02 1e 88 13",
                             "02 04 02 21 18 fc")
        self.assertEqual(observed, [("reactive_c", "-16"), ("power_c", "46"),
                                    ("apparent_total", "65536"), ("frequency", "50.0"), ("pf_c", "-1.0")])
        regs = [0] * 48
        for reg, value in {530:65520, 531:65535, 522:46, 541:1, 542:5000, 545:64536}.items():
            regs[reg - 500] = value
        self.pub.controls.clear()
        self.poller._read_power_input_block = lambda: regs
        self.poller._poll_power()
        self.assertLessEqual(set(observed), {(name, value) for _, name, value in self.pub.controls})

    def test_priority_mask_matches_channels_and_disabled_halves(self):
        self.connect(phases=["C"], channels_enabled={"power_reactive":False, "power_apparent":True,
                                                      "frequency":False})
        frame, = [frame for frame in self.bus.sent if int.from_bytes(frame[5:7], "big") == 518]
        self.assertEqual(frame[7], 29)
        self.assertEqual({518 + i for i, bit in enumerate(frame[8:-2]) if bit},
                         {522, 524, 534, 536, 538, 540, 543, 544, 545, 546})
        self.assertEqual(self.feed("04 04 02 06 ff ff ff 7f", "04 04 02 12 f0 ff ff ff",
                                   "04 04 02 0a 00 00 00 80"), [("power_c", "-2147483648")])

    def test_empty_wrong_width_high_word_and_forged_ack_do_not_publish(self):
        self.connect()
        self.assertEqual(self.feed("02 04 02 12 f0 ff", "04 04 02 13 ff ff ff ff",
                                   "04 04 02 1e 01 02 03 04", "02 04 02 00 b4 00"),
                         [("current_c", "0.18")])
        self.connect(refuse=True)
        self.assertEqual(self.feed("04 04 02 12 f0 ff ff ff", "02 04 02 1e 88 13"), [])

    def test_previous_firmware_reboot_and_bad_tail_fail_closed(self):
        self.connect(fw=(1, 0, 7, 18))
        self.assertEqual(self.feed("04 04 02 12 f0 ff ff ff", "02 04 02 1e 88 13"), [])
        self.connect()
        self.assertEqual(self.feed("04 04 02 12 f0 ff ff ff", "04 04 02 0a 2e"), [])
        self.assertEqual(self.feed("00 0f 00 00", "04 04 02 12 f0 ff ff ff"), [])


class TestCeEventFrames(unittest.TestCase):
    def _mgr(self):
        mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
        mgr.register_device(ADDR, "ce02m3-ut-14", list(CE_RANGES),
                            lambda *a: None, dev_type="ce02m3")
        mgr._wb_frame_slaves[ADDR] = True       # as the WB handshake leaves it
        return mgr

    def test_issue_193_fixture_decodes(self):
        had, events = self._mgr()._poll_once(_FrameBus(FX_WB_EVENTS_510_511))
        self.assertTrue(had)
        self.assertEqual(events, [(ADDR, bridge.FMB_EVT_INPUT, 510, 220),
                                  (ADDR, bridge.FMB_EVT_INPUT, 511, 208)])

    def test_bench_frame_decodes_all_eleven_records(self):
        had, events = self._mgr()._poll_once(_FrameBus(FX_BENCH_EVENTS))
        self.assertTrue(had)
        self.assertEqual([(e[2], e[3]) for e in events], [
            (501, 2306), (512, 180), (513, 180), (500, 2319), (501, 2320),
            (512, 196), (513, 196), (512, 180), (513, 180), (512, 200),
            (513, 200)])
        self.assertTrue(all(e[1] == bridge.FMB_EVT_INPUT for e in events))

    def test_wb_frame_publishes_with_the_classic_scaling(self):
        pub = _RecPub()
        poller = bridge.CE02M3Poller(
            {"id": "ce02m3-ut-14", "type": "ce02m3", "address": ADDR}, pub)
        mgr = bridge.FastModbusEventPortManager("/dev/COMT", 115200)
        mgr.register_device(ADDR, poller.device_id, list(CE_RANGES),
                            poller.fmb_dispatch, poller=poller,
                            dev_type="ce02m3")
        mgr._wb_frame_slaves[ADDR] = True
        frame = wb_event_frame([(500, 2319), (501, 2306), (502, 2298),
                                (512, 180), (513, 196)])
        _had, events = mgr._poll_once(_FrameBus(frame))
        for ev in events:
            mgr._dispatch(*ev)
        got = {name: val for _id, name, val in pub.controls}
        self.assertEqual(got, {"voltage_a": "231.9", "voltage_b": "230.6",
                               "voltage_c": "229.8", "current_c": "0.18",
                               "current_n": "0.196"})

    def test_power_registers_518_plus_are_ignored_by_dispatch(self):
        pub = _RecPub()
        poller = bridge.CE02M3Poller(
            {"id": "ce02m3-ut-14", "type": "ce02m3", "address": ADDR}, pub)
        for reg in (518, 519, 542, 546):
            poller.fmb_dispatch(bridge.FMB_EVT_INPUT, reg, 1234)
        self.assertEqual(pub.controls, [])


if __name__ == "__main__":
    unittest.main()
