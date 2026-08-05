# -*- coding: utf-8 -*-
"""Host tests for BACnet MS/TP verify + recover pure logic (sa02m_flasher.bacnet_mstp).

Ported from MR-02m-flasher/tests/test_bacnet_mstp.py (pytest → stdlib unittest)
plus the WriteProperty-recovery vectors derived from MR-02m/scripts/hw/
bacnet_recover.py (plan §5.2/§5.4/§11). Pure encoders/parsers/FSM — no serial
hardware. Covers CRC-8 header fixtures, CRC-16 data + frame round-trip, RP/WP
APDU encoding, ring-join FSM (RP and WP), the sniff summary, identity, baud
resolution, and the serial-transport glue over a fake serial.
"""
from __future__ import annotations

import struct
import unittest

from sa02m_flasher import bacnet_mstp as bm


# ── CRC-8 header fixtures (the mstp_sniff.selftest vectors) ──────────────────
class TestHeaderCrc(unittest.TestCase):
    FIXTURES = [
        (bytes([0x55, 0xFF, 0, 1, 2, 0, 0, 0x40]), True),
        (bytes([0x55, 0xFF, 1, 255, 15, 0, 0, 0x72]), True),
        (bytes([0x55, 0xFF, 6, 1, 15, 0, 3, 0x62]), True),
        (bytes([0x55, 0xFF, 0, 1, 2, 0, 0, 0x41]), False),  # tampered CRC
        (bytes([0x55, 0xFF, 0, 1, 3, 0, 0, 0x40]), False),  # tampered src
    ]

    def test_header_selftest_ok(self) -> None:
        results = bm.header_selftest()
        self.assertEqual(len(results), 5)
        for frame, expect, got in results:
            self.assertIs(got, expect, frame.hex(" "))
        self.assertIs(bm.header_selftest_ok(), True)

    def test_header_crc_valid(self) -> None:
        for frame, valid in self.FIXTURES:
            with self.subTest(frame=frame.hex(" ")):
                self.assertIs(bm.header_crc_valid(frame), valid)

    def test_short_frame_is_false(self) -> None:
        self.assertIs(bm.header_crc_valid(bytes([0x55, 0xFF, 0, 1])), False)


# ── CRC-16 data + frame round-trip ──────────────────────────────────────────
class TestFrameCodec(unittest.TestCase):
    def test_header_only_roundtrip(self) -> None:
        frame = bm.mstp_frame(bm.FRAME_TYPE_TOKEN, 1, 15)
        self.assertEqual(frame[:2], bm.PREAMBLE)
        self.assertEqual(len(frame), bm.HEADER_LEN)
        self.assertIs(bm.header_crc_valid(frame), True)

    def test_data_crc_matches_and_validates(self) -> None:
        data = bytes([0x01, 0x00, 0x30, 0x01, 0x0C])
        frame = bm.mstp_frame(bm.FRAME_TYPE_DATA_NOT_EXPECTING_REPLY, 1, 15, data)
        self.assertIs(bm.header_crc_valid(frame[:8]), True)
        body = frame[8 : 8 + len(data)]
        self.assertEqual(body, data)
        crc_lo, crc_hi = frame[8 + len(data)], frame[9 + len(data)]
        crc = 0xFFFF
        for b in data:
            crc = bm.crc16_data_update(b, crc)
        crc = (~crc) & 0xFFFF
        self.assertEqual((crc_lo, crc_hi), (crc & 0xFF, crc >> 8))
        self.assertIs(bm.data_crc_valid(body + bytes([crc_lo, crc_hi])), True)

    def test_data_crc_rejects_tampered(self) -> None:
        data = bytes([0x01, 0x00, 0x30])
        frame = bm.mstp_frame(6, 1, 15, data)
        bad = bytearray(frame[8:])
        bad[0] ^= 0x01
        self.assertIs(bm.data_crc_valid(bytes(bad)), False)


# ── ReadProperty / WriteProperty APDU encoding ──────────────────────────────
class TestApduEncoding(unittest.TestCase):
    def test_rp_request_golden(self) -> None:
        apdu = bm.rp_request(1, 8, 720361, 77)
        self.assertEqual(
            apdu,
            bytes([
                0x01, 0x04,
                0x00, 0x01, 0x01, 0x0C,
                0x0C, 0x02, 0x0A, 0xFD, 0xE9,
                0x19, 0x4D,
            ]),
        )

    def test_rp_request_object_packing(self) -> None:
        apdu = bm.rp_request(0, 8, 0, 0)
        self.assertEqual(int.from_bytes(apdu[7:11], "big"), (8 << 22))

    def test_wp_request_golden_mr_msv(self) -> None:
        # MR MSV:1 present-value = 2 (unsigned application value 0x21 0x02).
        apdu = bm.wp_request(7, 19, 1, 85, bm.app_value_unsigned8(2))
        self.assertEqual(
            apdu,
            bytes([
                0x01, 0x04,              # NPDU
                0x00, 0x01, 0x07, 0x0F,  # ConfirmedReq, max-APDU 128, invoke 7, WP
                0x0C, 0x04, 0xC0, 0x00, 0x01,  # ctx0 object-id MSV:1
                0x19, 0x55,              # ctx1 property-id 85 (present-value)
                0x3E, 0x21, 0x02, 0x3F,  # ctx3 value: unsigned 2
            ]),
        )

    def test_app_value_unsigned8(self) -> None:
        self.assertEqual(bm.app_value_unsigned8(2), bytes([0x21, 0x02]))
        self.assertEqual(bm.app_value_unsigned8(0), bytes([0x21, 0x00]))

    def test_app_value_real_ieee754_be(self) -> None:
        for v in (0.0, 1.0, 2.0):
            with self.subTest(v=v):
                self.assertEqual(bm.app_value_real(v), bytes([0x44]) + struct.pack(">f", v))
        # canonical bytes for the DTV recovery value 0.0
        self.assertEqual(bm.app_value_real(0), bytes([0x44, 0x00, 0x00, 0x00, 0x00]))

    def test_mr_recovery_wp_payload_structure(self) -> None:
        payload = bm.mr_recovery_wp_payload(7, 2)
        # object-id packs Multistate Value (19) instance 1
        self.assertEqual(int.from_bytes(payload[7:11], "big"), (19 << 22) | 1)
        self.assertEqual(payload[11:13], bytes([0x19, 0x55]))  # present-value
        self.assertEqual(payload[13:], bytes([0x3E, 0x21, 0x02, 0x3F]))

    def test_dtv_recovery_wp_payload_structure(self) -> None:
        payload = bm.dtv_recovery_wp_payload(7, 0)
        # object-id packs Analog Value (2) instance 122
        self.assertEqual(int.from_bytes(payload[7:11], "big"), (2 << 22) | 122)
        self.assertEqual(payload[11:13], bytes([0x19, 0x55]))  # present-value
        # value = REAL 0.0
        self.assertEqual(payload[13:], bytes([0x3E, 0x44, 0x00, 0x00, 0x00, 0x00, 0x3F]))


# ── parse_frames ─────────────────────────────────────────────────────────────
class TestParseFrames(unittest.TestCase):
    def test_multiple_and_consume(self) -> None:
        f1 = bm.mstp_frame(bm.FRAME_TYPE_POLL_FOR_MASTER, 1, 15)
        f2 = bm.mstp_frame(bm.FRAME_TYPE_DATA_NOT_EXPECTING_REPLY, 1, 15, b"\x01\x00\x30")
        buf = bytearray(f1 + f2)
        frames = bm.parse_frames(buf)
        self.assertEqual(
            [f.ftype for f in frames],
            [bm.FRAME_TYPE_POLL_FOR_MASTER, bm.FRAME_TYPE_DATA_NOT_EXPECTING_REPLY],
        )
        self.assertEqual(frames[1].data, b"\x01\x00\x30")
        self.assertEqual(len(buf), 0)

    def test_skips_garbage_before_preamble(self) -> None:
        f1 = bm.mstp_frame(bm.FRAME_TYPE_TOKEN, 2, 9)
        buf = bytearray(b"\x00\xAA\x13" + f1)
        frames = bm.parse_frames(buf)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].src, 9)

    def test_keeps_incomplete_tail(self) -> None:
        f = bm.mstp_frame(6, 1, 15, b"\x01\x00\x30\x04")
        buf = bytearray(f[:-1])
        self.assertEqual(bm.parse_frames(buf), [])
        self.assertEqual(len(buf), len(f) - 1)

    def test_drops_bad_header_crc(self) -> None:
        f = bytearray(bm.mstp_frame(bm.FRAME_TYPE_TOKEN, 1, 15))
        f[7] ^= 0xFF
        self.assertEqual(bm.parse_frames(bytearray(f)), [])


# ── device identity ──────────────────────────────────────────────────────────
class TestIdentity(unittest.TestCase):
    def test_device_instance_formula(self) -> None:
        self.assertEqual(bm.device_instance(720361, 15), 720361 & 0x3FFFFF)
        self.assertEqual(bm.device_instance(0, 15), 100015)
        self.assertEqual(bm.device_instance(0xFFFFFFFF, 7), 100007)
        self.assertEqual(bm.device_instance(0x0E0A000F, 1), (0x0E0A000F & 0x3FFFFF))

    def test_family_identity(self) -> None:
        self.assertEqual(
            bm.family_identity(bm.FAMILY_MR),
            {"vendor_id": 260, "vendor_name": "CYNTRON", "model": "MP-02m"},
        )
        self.assertEqual(bm.family_identity(bm.FAMILY_DTV)["vendor_id"], 381)

    def test_resolve_mstp_baud(self) -> None:
        self.assertEqual(bm.resolve_mstp_baud(bm.FAMILY_MR), 38400)
        self.assertEqual(bm.resolve_mstp_baud(bm.FAMILY_MR, 9600), 38400)  # MR fixed
        self.assertEqual(bm.resolve_mstp_baud(bm.FAMILY_DTV), 38400)  # default
        self.assertEqual(bm.resolve_mstp_baud(bm.FAMILY_DTV, 57600), 57600)  # persisted
        self.assertEqual(bm.resolve_mstp_baud(bm.FAMILY_DTV, 0), 38400)  # bad → default


# ── passive sniff ────────────────────────────────────────────────────────────
class _Clock:
    def __init__(self, step: float = 0.01) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        v = self.t
        self.t += self.step
        return v


def _one_shot_reader(blob: bytes):
    state = {"sent": False}

    def _read() -> bytes:
        if state["sent"]:
            return b""
        state["sent"] = True
        return blob

    return _read


class TestSniff(unittest.TestCase):
    def test_counts_frames_and_alive(self) -> None:
        blob = (
            bm.mstp_frame(bm.FRAME_TYPE_POLL_FOR_MASTER, 255, 15)
            + bm.mstp_frame(bm.FRAME_TYPE_TOKEN, 1, 15)
            + bm.mstp_frame(bm.FRAME_TYPE_POLL_FOR_MASTER, 255, 15)
        )
        res = bm.sniff_stream(_one_shot_reader(blob), 0.05, monotonic=_Clock())
        self.assertEqual(res.frames_seen, 3)
        self.assertIs(res.alive, True)
        self.assertEqual(res.histogram[bm.FRAME_TYPE_POLL_FOR_MASTER], 2)
        self.assertEqual(dict(res.type_counts_named())["Poll For Master"], 2)
        # to_dict is the job/HTTP serialization.
        d = res.to_dict()
        self.assertEqual(d["frames_seen"], 3)
        self.assertIs(d["alive"], True)

    def test_silence_is_not_alive_reported(self) -> None:
        res = bm.sniff_stream(_one_shot_reader(b"\x00\x01\x02\x03"), 0.05, monotonic=_Clock())
        self.assertEqual(res.frames_seen, 0)
        self.assertIs(res.alive, False)

    def test_counts_bad_header_crc(self) -> None:
        bad = bytearray(bm.mstp_frame(bm.FRAME_TYPE_TOKEN, 1, 15))
        bad[7] ^= 0xFF
        res = bm.sniff_stream(_one_shot_reader(bytes(bad)), 0.05, monotonic=_Clock())
        self.assertEqual(res.frames_seen, 0)
        self.assertGreaterEqual(res.bad_header_crc, 1)


# ── ring-join FSM (RP default and WP recovery payload) ──────────────────────
class TestRingJoin(unittest.TestCase):
    def test_rp_happy_path_transitions(self) -> None:
        fsm = bm.RingJoinFSM(my_mac=1, dut_mac=15, obj_inst=720361)
        self.assertEqual(fsm.state, bm.RING_WAIT_PFM)

        tx = fsm.step(bm.Frame(bm.FRAME_TYPE_POLL_FOR_MASTER, 1, 15, b""))
        self.assertEqual(fsm.state, bm.RING_WAIT_TOKEN)
        self.assertEqual(tx, bm.mstp_frame(bm.FRAME_TYPE_REPLY_TO_PFM, 15, 1))

        tx = fsm.step(bm.Frame(bm.FRAME_TYPE_TOKEN, 1, 15, b""))
        self.assertEqual(fsm.state, bm.RING_WAIT_REPLY)
        self.assertEqual(tx, fsm._der())

        reply = bm.Frame(bm.FRAME_TYPE_DATA_NOT_EXPECTING_REPLY, 1, 15, b"\x01\x00\x30")
        tx = fsm.step(reply)
        self.assertEqual(fsm.state, bm.RING_DONE)
        self.assertIs(fsm.done, True)
        self.assertEqual(tx, bm.mstp_frame(bm.FRAME_TYPE_TOKEN, 15, 1))

    def test_ignores_irrelevant_frames(self) -> None:
        fsm = bm.RingJoinFSM(my_mac=1, dut_mac=15)
        self.assertIsNone(fsm.step(bm.Frame(bm.FRAME_TYPE_POLL_FOR_MASTER, 2, 15, b"")))
        self.assertEqual(fsm.state, bm.RING_WAIT_PFM)
        self.assertIsNone(fsm.step(bm.Frame(bm.FRAME_TYPE_TOKEN, 1, 15, b"")))
        self.assertEqual(fsm.state, bm.RING_WAIT_PFM)

    def test_wp_payload_der_carries_write_property(self) -> None:
        # A recovery FSM sends the injected WP payload in its DER frame.
        payload = bm.mr_recovery_wp_payload(7, 2)
        fsm = bm.RingJoinFSM(my_mac=5, dut_mac=4, der_payload=payload)
        fsm.step(bm.Frame(bm.FRAME_TYPE_POLL_FOR_MASTER, 5, 4, b""))
        der = fsm.step(bm.Frame(bm.FRAME_TYPE_TOKEN, 5, 4, b""))
        expected = bm.mstp_frame(bm.FRAME_TYPE_DATA_EXPECTING_REPLY, 4, 5, payload)
        self.assertEqual(der, expected)

    def test_run_ring_join_reaches_done_and_captures_reply(self) -> None:
        fsm = bm.RingJoinFSM(my_mac=1, dut_mac=15, obj_inst=720361)
        stream = (
            bm.mstp_frame(bm.FRAME_TYPE_POLL_FOR_MASTER, 1, 15)
            + bm.mstp_frame(bm.FRAME_TYPE_TOKEN, 1, 15)
            + bm.mstp_frame(bm.FRAME_TYPE_DATA_NOT_EXPECTING_REPLY, 1, 15, b"\x01\x00\x30\x2C")
        )
        tx_frames = []
        res = bm.run_ring_join(_one_shot_reader(stream), tx_frames.append, fsm, 0.1, monotonic=_Clock())
        self.assertIs(res.answered, True)
        self.assertEqual(res.state, bm.RING_DONE)
        self.assertEqual(res.frames_tx, 3)  # Reply-PFM, DER, Token-back
        self.assertEqual(res.reply_data, b"\x01\x00\x30\x2C")

    def test_recovery_acked_on_simple_ack(self) -> None:
        # SimpleAck (pdu type 2) over local NPDU is treated as a confirmed return.
        self.assertIs(bm.recovery_acked(b"\x01\x00\x20"), True)   # 0x2_ = SimpleAck
        self.assertIs(bm.recovery_acked(b"\x01\x00\x30"), True)   # 0x3_ = ComplexAck
        self.assertIs(bm.recovery_acked(b"\x01\x00\x50"), False)  # 0x5_ = Error
        self.assertIs(bm.recovery_acked(None), False)


# ── serial transport glue (fake serial) ─────────────────────────────────────
class _FakeSerial:
    def __init__(self, data: bytes) -> None:
        self._data = bytearray(data)
        self.baudrate = 115200
        self.parity = "E"
        self.stopbits = 2
        self.bytesize = 8
        self.timeout = 0.1
        self.reset_calls = 0
        self.written = bytearray()

    def read(self, n: int) -> bytes:
        if not self._data:
            return b""
        out = bytes(self._data[:n])
        del self._data[:n]
        return out

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self.reset_calls += 1


class TestSerialTransport(unittest.TestCase):
    def test_sniff_serial_reads_and_restores_params(self) -> None:
        blob = bm.mstp_frame(bm.FRAME_TYPE_TOKEN, 1, 15) + bm.mstp_frame(
            bm.FRAME_TYPE_POLL_FOR_MASTER, 255, 15
        )
        ser = _FakeSerial(blob)
        res = bm.sniff_serial(ser, 38400, 0.5)
        self.assertGreaterEqual(res.frames_seen, 1)
        self.assertIs(res.alive, True)
        # Modbus params restored after the sniff window.
        self.assertEqual(ser.baudrate, 115200)
        self.assertEqual(ser.parity, "E")
        self.assertEqual(ser.stopbits, 2)

    def test_sniff_serial_none_reports_error(self) -> None:
        res = bm.sniff_serial(None, 38400, 0.02)
        self.assertEqual(res.open_error, "serial not open")
        self.assertIs(res.alive, False)

    def test_ring_join_serial_runs_and_restores(self) -> None:
        stream = (
            bm.mstp_frame(bm.FRAME_TYPE_POLL_FOR_MASTER, 5, 4)
            + bm.mstp_frame(bm.FRAME_TYPE_TOKEN, 5, 4)
            + bm.mstp_frame(bm.FRAME_TYPE_DATA_NOT_EXPECTING_REPLY, 5, 4, b"\x01\x00\x20")
        )
        ser = _FakeSerial(stream)
        fsm = bm.RingJoinFSM(my_mac=5, dut_mac=4, der_payload=bm.mr_recovery_wp_payload(7, 2))
        res = bm.ring_join_serial(ser, 38400, fsm, 1.0)
        self.assertIs(res.answered, True)
        self.assertIs(res.acked, True)  # SimpleAck reply
        self.assertGreater(len(ser.written), 0)
        self.assertEqual(ser.baudrate, 115200)  # restored


if __name__ == "__main__":
    unittest.main()
