# -*- coding: utf-8 -*-
"""FC17 identity in the RS-485 scan: framing, timeout floor, and scan_address.

The defect these pin is one the bench showed directly (192.168.1.135, COM3,
19200 8N1): both Carel PLCs answered holding register 0 but have no serial at
270-271, so the scanner listed them as MR-02m modules sitting in a bootloader —
no signature, no version, and «Обновить все» offering them firmware.

The FC17 replies are the real captures shared with the Carel package
(opt/sa02m-carel/tests/fixtures): slave 1 = c.pCOmini CRSTDrAHAQ (206 bytes off
the wire), slave 2 = uAria CRSTDm_AHU. The Carel map itself is never restated
here — it is read through the flasher's one import seam,
module_profiles.carel_ahu().
"""
from __future__ import annotations

import struct
import time
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

from sa02m_flasher import modbus_rtu, module_profiles, scanner, serial_port

ca = module_profiles.carel_ahu()

_FIXTURES = Path(__file__).resolve().parents[2] / "sa02m-carel" / "tests" / "fixtures"


def _fixture(name: str) -> bytes:
    return bytes.fromhex((_FIXTURES / name).read_text(encoding="ascii").strip())


# c.pCOmini: the whole RTU frame off the wire ([01][11][c9][payload][crc]).
CRST_FRAME = _fixture("fc17_probe2.hex")
# uAria: saved as the bare FC17 payload.
UARIA_PAYLOAD = _fixture("fc17_crstdm.hex")


def _crc(body: bytes) -> bytes:
    return struct.pack("<H", modbus_rtu.crc16_modbus(body))


def _read_reply(slave: int, func: int, data: bytes) -> bytes:
    body = bytes([slave, func, len(data)]) + data
    return body + _crc(body)


def _exception_reply(slave: int, func: int, code: int) -> bytes:
    body = bytes([slave, func | 0x80, code])
    return body + _crc(body)


def _regs(values) -> bytes:
    return b"".join(struct.pack(">H", v & 0xFFFF) for v in values)


def _ascii_regs(text: str, count: int) -> bytes:
    """12 holding registers carrying one ASCII char each in the low byte (рег. 290)."""
    out = b""
    for i in range(count):
        ch = ord(text[i]) if i < len(text) else 0
        out += struct.pack(">H", ch)
    return out


class _BenchDevice:
    """One Modbus slave answering like the controller measured on the bench."""

    def __init__(
        self,
        slave: int,
        holding: Optional[Dict[Tuple[int, int], bytes]] = None,
        input_regs: Optional[Dict[Tuple[int, int], bytes]] = None,
        fc17: Optional[bytes] = None,
        fc17_drop_first: bool = False,
    ) -> None:
        self.slave = slave
        self.holding = holding or {}
        self.input_regs = input_regs or {}
        self.fc17 = fc17
        self.fc17_drop_first = fc17_drop_first
        self.fc17_seen = 0

    def __call__(self, req: bytes) -> bytes:
        if len(req) < 4 or req[0] != self.slave:
            return b""
        func = req[1]
        if func == 0x11:
            self.fc17_seen += 1
            if self.fc17 is None:
                return _exception_reply(self.slave, func, 1)
            if self.fc17_drop_first and self.fc17_seen == 1:
                return b""
            return self.fc17
        if func in (0x03, 0x04):
            start = (req[2] << 8) | req[3]
            count = (req[4] << 8) | req[5]
            table = self.holding if func == 0x03 else self.input_regs
            data = table.get((start, count))
            if data is None:
                # The PLCs answer «illegal data address» outside their BMS map.
                return _exception_reply(self.slave, func, 2)
            return _read_reply(self.slave, func, data)
        return _exception_reply(self.slave, func, 1)


class _FakeSerial:
    """Serial port that delivers a reply at real wire speed.

    Byte N of the reply becomes readable only once N character times have really
    elapsed, so a response_timeout_ms shorter than the frame truncates it exactly
    as the RS-485 line does — that is what makes the FC17 timeout floor testable
    rather than asserted.
    """

    def __init__(
        self,
        responder: Callable[[bytes], bytes],
        baudrate: int = 19200,
        parity: str = "N",
        stopbits: int = 1,
        think_s: float = 0.0,
        wire: bool = True,
    ) -> None:
        self.baudrate = int(baudrate)
        self.parity = parity
        self.stopbits = int(stopbits)
        self.bytesize = 8
        self.responder = responder
        self.think_s = float(think_s)
        self.wire = bool(wire)
        self.requests: List[bytes] = []
        self.closed = False
        self._pending = b""
        self._sent = 0
        self._t0 = 0.0

    # -- pyserial surface used by send_receive -----------------------------
    def reset_input_buffer(self) -> None:
        self._pending = b""
        self._sent = 0

    def write(self, data: bytes) -> int:
        self.requests.append(bytes(data))
        self._pending = self.responder(bytes(data)) or b""
        self._sent = 0
        self._t0 = time.perf_counter()
        return len(data)

    def flush(self) -> None:
        return None

    @property
    def in_waiting(self) -> int:
        return max(0, self._arrived() - self._sent)

    def read(self, n: int) -> bytes:
        n = min(int(n), self.in_waiting)
        out = self._pending[self._sent : self._sent + n]
        self._sent += n
        return out

    def close(self) -> None:
        self.closed = True

    # -- wire model --------------------------------------------------------
    def _arrived(self) -> int:
        if not self._pending:
            return 0
        if not self.wire:
            return len(self._pending)
        elapsed = time.perf_counter() - self._t0 - self.think_s
        if elapsed <= 0:
            return 0
        bits = 1 + 8 + (2 if self.stopbits == 2 else 1)
        if str(self.parity).upper() in ("E", "O"):
            bits += 1
        return min(int(elapsed * self.baudrate / bits), len(self._pending))

    # -- request bookkeeping ----------------------------------------------
    def functions(self) -> List[int]:
        return [r[1] for r in self.requests if len(r) >= 2]


def _fake_port(fake: _FakeSerial):
    """Patch target for scanner.open_port — always hands back the same fake."""

    def _open(port, baudrate=9600, parity="N", stopbits=2):  # noqa: ANN001
        fake.baudrate = int(baudrate)
        fake.parity = parity
        fake.stopbits = int(stopbits)
        return fake

    return _open


def _crst_device(slave: int = 1) -> _BenchDevice:
    """c.pCOmini: HR0 = [0,0]; nothing at 270/290/320/330; FC17 answers."""
    return _BenchDevice(slave, holding={(0, 2): _regs([0, 0])}, fc17=CRST_FRAME)


def _uaria_device(slave: int = 2) -> _BenchDevice:
    """uAria: same shape, and IR100 = 1061 carries the firmware version."""
    return _BenchDevice(
        slave,
        holding={(0, 2): _regs([0, 0])},
        input_regs={(ca.IR_UARIA_PROG_VER, 1): _regs([1061])},
        fc17=_read_reply(slave, 0x11, UARIA_PAYLOAD),
    )


def _mr02m_device(slave: int = 6, serial_no: int = 235566384) -> _BenchDevice:
    """MR-02m 6AI6AO in application mode — the row that must never see FC17."""
    return _BenchDevice(
        slave,
        holding={
            (0, 2): _regs([0, 0]),
            (scanner.REG_SERIAL_LO, 2): _regs(
                [serial_no & 0xFFFF, (serial_no >> 16) & 0xFFFF]
            ),
            (scanner.REG_VERSION_MAJOR, 4): _regs([1, 0, 10, 0]),
            (scanner.REG_SIGNATURE, scanner.REG_SIGNATURE_COUNT): _ascii_regs(
                "6AI6AO", scanner.REG_SIGNATURE_COUNT
            ),
            (scanner.REG_BOOTLOADER_VER, scanner.REG_BOOTLOADER_VER_COUNT): _regs(
                [0] * scanner.REG_BOOTLOADER_VER_COUNT
            ),
        },
        fc17=CRST_FRAME,  # would answer if asked — the gate is what stops the frame
    )


def _scan_one(fake: _FakeSerial, address: int, baud: int = 19200):
    with patch("sa02m_flasher.scanner.open_port", _fake_port(fake)):
        return scanner.scan_address("COM_TEST", address, [(baud, "N", 1)])


class TestSharedMapSeam(unittest.TestCase):
    def test_seam_resolves_the_shared_package(self) -> None:
        # Non-vacuity for every test below: without the map nothing is Carel and
        # the FC17 tests would pass by never probing at all.
        self.assertIsNotNone(ca, "sa02m_carel is not importable through the seam")
        self.assertTrue(ca.signature_looks_like_carel("CRSTDrAHAQ"))


class TestFc17Framing(unittest.TestCase):
    def test_request_frame_bytes(self) -> None:
        # PDU is the function code alone: [addr][0x11][crc_lo][crc_hi].
        # 01 11 C0 2C is the documented Modbus «Report Slave ID» request frame.
        self.assertEqual(modbus_rtu.build_report_slave_id(1).hex(), "0111c02c")
        self.assertEqual(modbus_rtu.build_report_slave_id(2).hex(), "0211c0dc")

    def test_real_crst_capture_parses_to_the_fingerprint(self) -> None:
        self.assertEqual(len(CRST_FRAME), 206)
        addr, payload, err = modbus_rtu.parse_response(CRST_FRAME, expected_slave=1)
        self.assertIsNone(err)
        self.assertEqual(addr, 1)
        self.assertEqual(len(payload), CRST_FRAME[2])
        fp = ca.parse_report_slave_id(payload)
        self.assertEqual(fp.app_id, "CRSTDrAHAQ")
        self.assertEqual(fp.std_mark, "STD_E")

    def test_exception_reply_is_reported_not_parsed(self) -> None:
        addr, payload, err = modbus_rtu.parse_response(
            _exception_reply(13, 0x11, 1), expected_slave=13
        )
        self.assertEqual(addr, 13)
        self.assertIsNone(payload)
        self.assertIn("Исключение Modbus", err or "")

    def test_wiren_board_event_frame_is_not_read_as_fc17(self) -> None:
        """[SLAVE][0x46][0x11][FLAG]… must never be attributed to «slave 0x46».

        Accepting function 0x11 in the frame search opened this: the WB events
        subcommand byte is also 0x11, so a stream whose 0x46 lands where a slave
        address is expected parses as an FC17 reply from address 70. The frame
        below is built so the FC17 reading passes the CRC check too — that is the
        one case the length checks do not already reject, and the only thing left
        standing between it and a phantom device row is the explicit guard.
        """
        inner = bytes([modbus_rtu.BL_FAST_MODBUS_FUNC, 0x11, 0x04, 0x01, 0x02, 0x03, 0x04])
        frame = bytes([0x0A]) + inner + _crc(inner)
        # Pre-condition: the bytes really do form a CRC-valid «FC17 from 0x46».
        sub_addr, sub_payload, sub_err = modbus_rtu._parse_response_from(frame, 1)
        self.assertIsNone(sub_err)
        self.assertEqual(sub_addr, modbus_rtu.BL_FAST_MODBUS_FUNC)
        self.assertEqual(sub_payload, b"\x01\x02\x03\x04")
        # And parse_response still refuses to report it.
        addr, payload, err = modbus_rtu.parse_response(frame)
        self.assertIsNone(payload)
        self.assertIsNotNone(err)

    def test_byte_count_bounds(self) -> None:
        empty = bytes([1, 0x11, 0])
        self.assertIsNotNone(modbus_rtu.parse_response(empty + _crc(empty) + b"\x00\x00")[2])
        big = bytes([1, 0x11, 247]) + b"\x00" * 247
        self.assertIsNotNone(modbus_rtu.parse_response(big + _crc(big))[2])

    def test_frame_length_predictor_knows_fc17(self) -> None:
        # Without this the 206-byte reply has no early exit and is cut by the
        # read loop's deadline instead of being recognised as complete.
        self.assertEqual(
            serial_port._modbus_read_var_header_frame_len(CRST_FRAME[:3]), 206
        )
        self.assertTrue(serial_port._rtu_response_complete(CRST_FRAME))
        self.assertFalse(serial_port._rtu_response_complete(CRST_FRAME[:-1]))


class TestFc17TimeoutFloor(unittest.TestCase):
    def test_floor_applies_to_fc17_only(self) -> None:
        seen: List[int] = []

        def _fake_send_receive(ser, req, response_timeout_ms=2000, cancel_check=None):
            seen.append(int(response_timeout_ms))
            return None

        fake = _FakeSerial(lambda _req: b"", baudrate=19200)
        with patch("sa02m_flasher.scanner.send_receive", _fake_send_receive):
            scanner._transact_raw(
                modbus_rtu.build_report_slave_id(1), "COM_TEST", 1,
                19200, "N", 1, scanner.SCAN_TIMEOUT_MS, ser=fake,
            )
            scanner._transact_raw(
                modbus_rtu.build_read_holding_registers(1, 0, 2), "COM_TEST", 1,
                19200, "N", 1, scanner.SCAN_TIMEOUT_MS, ser=fake,
            )
        self.assertEqual(len(seen), 2)
        self.assertGreaterEqual(seen[0], 2 * int(scanner.SCAN_TIMING_STANDARD["scan_timeout_ms"]))
        self.assertGreaterEqual(seen[0], 400)
        # FC03 keeps its own scan timeout — the floor must not slow the scan down.
        self.assertEqual(seen[1], scanner.SCAN_TIMEOUT_MS)

    def test_capture_sized_reply_survives_at_the_slowest_scan_speed(self) -> None:
        """The real 206-byte c.pCOmini reply at 9600 8N1.

        215 ms of wire time plus the PLC's own reaction is longer than the scan
        timeout (280 ms) the caller asks for; with the floor removed the frame is
        cut mid-payload, the CRC fails and the row stays unidentified.
        """
        fake = _FakeSerial(_crst_device(1), baudrate=9600, think_s=0.10)
        dev = scanner.DeviceInfo(
            address=1, baudrate=9600, parity="N", stopbits=1,
            signature="", app_version="—", bootloader_version="—",
            serial=0, in_bootloader=True,
        )
        out = scanner._apply_carel_fc17(
            dev, "COM_TEST", 1, 9600, "N", 1, scanner.SCAN_TIMEOUT_MS, ser=fake
        )
        self.assertEqual(out.signature, "CRSTDrAHAQ")
        self.assertEqual(out.app_version, "2.03.00.46")
        self.assertFalse(out.in_bootloader)


class TestScanAddressCarel(unittest.TestCase):
    def test_carel_is_not_listed_as_a_module_in_a_bootloader(self) -> None:
        # The load-bearing return: reg 0 answers, 270-271 does not.
        device = _crst_device(1)
        fake = _FakeSerial(device, baudrate=19200)
        dev = _scan_one(fake, 1)
        self.assertIsNotNone(dev)
        self.assertEqual(dev.signature, "CRSTDrAHAQ")
        self.assertEqual(dev.app_version, "2.03.00.46")
        self.assertEqual(dev.carel_variant, ca.VARIANT_ENHANCED)
        self.assertFalse(dev.in_bootloader)
        self.assertEqual(device.fc17_seen, 1)
        # And the row is usable without a serial, yet is not our product line.
        self.assertTrue(scanner.device_identity_complete_for_module_config(dev))
        self.assertFalse(scanner.device_is_mp02_product_line_for_config(dev))

    def test_uaria_version_falls_back_to_ir100(self) -> None:
        # CRSTDm_AHU does not duplicate the version bytes in its FC17 reply.
        self.assertEqual(ca.parse_report_slave_id(UARIA_PAYLOAD).version_str(), "—")
        device = _uaria_device(2)
        fake = _FakeSerial(device, baudrate=19200)
        dev = _scan_one(fake, 2)
        self.assertIsNotNone(dev)
        self.assertEqual(dev.signature, "CRSTDm_AHU")
        self.assertEqual(dev.app_version, "1.0.61")
        self.assertIn(0x04, fake.functions())

    def test_fc17_is_retried_once_after_a_lost_frame(self) -> None:
        device = _crst_device(1)
        device.fc17_drop_first = True
        fake = _FakeSerial(device, baudrate=19200)
        dev = _scan_one(fake, 1)
        self.assertIsNotNone(dev)
        self.assertEqual(device.fc17_seen, 2)
        self.assertEqual(dev.signature, "CRSTDrAHAQ")

    def test_known_module_row_receives_zero_fc17(self) -> None:
        device = _mr02m_device(6)
        fake = _FakeSerial(device, baudrate=115200)
        dev = _scan_one(fake, 6, baud=115200)
        self.assertIsNotNone(dev)
        self.assertEqual(dev.signature, "6AI6AO")
        self.assertEqual(dev.carel_variant, "")
        self.assertEqual(device.fc17_seen, 0)
        self.assertNotIn(0x11, fake.functions())

    def test_known_module_without_a_serial_still_receives_zero_fc17(self) -> None:
        # Serial 0 is the only case where the "one of ours" predicate is what
        # holds the gate shut — with a serial the row is skipped anyway.
        device = _mr02m_device(6, serial_no=0)
        fake = _FakeSerial(device, baudrate=115200)
        dev = _scan_one(fake, 6, baud=115200)
        self.assertIsNotNone(dev)
        self.assertEqual(dev.signature, "6AI6AO")
        self.assertEqual(device.fc17_seen, 0)


class TestCarelModuleProfile(unittest.TestCase):
    def test_signature_maps_to_the_carel_type(self) -> None:
        self.assertEqual(module_profiles.code_from_signature("CRSTDrAHAQ"),
                         module_profiles.CAREL_AHU)
        self.assertEqual(module_profiles.code_from_signature("CRSTDm_AHU"),
                         module_profiles.CAREL_AHU)
        self.assertEqual(
            module_profiles.MP02_TYPE_NAMES[module_profiles.CAREL_AHU], "Carel AHU"
        )

    def test_carel_is_never_a_batch_flash_target(self) -> None:
        self.assertFalse(
            module_profiles.is_mp_module_signature_for_batch_flash("CRSTDrAHAQ")
        )
        err = module_profiles.validate_batch_flash_targets(
            [{"signature": "6AI6AO"}, {"signature": "CRSTDrAHAQ"}]
        )
        self.assertIsNotNone(err)
        self.assertIn("Carel", err)
        self.assertIn("CRSTDrAHAQ", err)
        # Our own modules keep passing.
        self.assertIsNone(
            module_profiles.validate_batch_flash_targets([{"signature": "6AI6AO"}])
        )


if __name__ == "__main__":
    unittest.main()
