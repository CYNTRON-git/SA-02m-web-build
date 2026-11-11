"""MR configure ACKs: firmware-valid replies, rejection and classic fallback.

Only serial transport and the clock are fake; the manager and frame builders
are real. Legacy flags=OK comes from shared/modbus before 42ae732; current
modbus_rtu_fc46_events.c returns the WB post-apply register mask.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge_fmb as fmb  # noqa: E402


def with_crc(body):
    crc = 0xFFFF
    for byte in body:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return bytes(body) + crc.to_bytes(2, "little")


def reply(mask, addr=2):
    return with_crc(bytes([addr, 0x46, 0x18, len(mask)]) + mask)


class SerialReplies:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = []

    def fmb_send_recv(self, frame, min_len, max_len, timeout):
        self.sent.append((bytes(frame), min_len, max_len, timeout))
        response = next(self.replies, b"")
        if isinstance(response, Exception):
            raise response
        return response


class ReadyPoller:
    device_id = "mr-ack-test"

    def __init__(self):
        self.covered = []
        self.reads = 0

    def classic_ready_for_fmb(self):
        return True

    def set_fmb_io_covered(self, covered):
        self.covered.append(covered)

    def poll_io(self):
        self.reads += 1


class TestMrReplyTruth(unittest.TestCase):
    def make_manager(self, count=16, wire_mode="auto", poller=None, ranges=None,
                     dev_type="mr02m", addr=2):
        manager = fmb.FastModbusEventPortManager("/dev/COMT", 115200)
        manager.register_device(
            addr, "mr-ack-test", ranges or [(fmb.FMB_EVT_COIL, 1, count)],
            lambda *args: None, poller=poller, dev_type=dev_type,
            wire_mode=wire_mode)
        return manager, manager._devices[addr]

    def assert_rejected(self, response, *, wire_mode="auto", cached=None):
        manager, dev = self.make_manager(wire_mode=wire_mode)
        if cached is not None:
            manager._wb_frame_slaves[2] = cached
        serial = SerialReplies([response, response])
        self.assertFalse(manager._configure_device(serial, 2, dev))
        self.assertFalse(dev["configured"])
        self.assertEqual(dev["pending"], dev["ranges"])
        self.assertEqual(manager._wb_frame_slaves,
                         {} if cached is None else {2: cached})
        self.assertEqual(len(serial.sent), 1 if wire_mode == "legacy" else 2)
        self.assertTrue(all(call[1:] == (5, 48, 0.4) for call in serial.sent))

    def test_old_zero_crc_success_fixture_is_rejected_on_both_wires(self):
        for addr in (2, 5, 10):
            with self.subTest(addr=addr):
                manager, dev = self.make_manager(addr=addr)
                response = bytes([addr, 0x46, 0x18, 1, 0, 0, 0])
                self.assertFalse(manager._configure_device(
                    SerialReplies([response, response]), addr, dev))
                self.assertFalse(dev["configured"])
                self.assertEqual(dev["pending"], dev["ranges"])
                self.assertNotIn(addr, manager._wb_frame_slaves)

    def test_incomplete_uncorrelated_and_corrupt_replies_stay_pending(self):
        valid = reply(b"\xff\xff")
        cases = {
            "empty": b"",
            "address only": b"\x02",
            "prefix only": b"\x02\x46\x18",
            "no payload": b"\x02\x46\x18\x02",
            "short payload": b"\x02\x46\x18\x02\xff",
            "missing CRC": valid[:-2],
            "short CRC": valid[:-1],
            "bad CRC": valid[:-1] + bytes([valid[-1] ^ 1]),
            "wrong address": reply(b"\xff\xff", addr=3),
            "wrong function": with_crc(b"\x02\x45\x18\x02\xff\xff"),
            "wrong subfunction": with_crc(b"\x02\x46\x19\x02\xff\xff"),
            "declared length too long": with_crc(b"\x02\x46\x18\x03\xff\xff"),
            "empty payload": reply(b""),
        }
        for name, response in cases.items():
            with self.subTest(name=name):
                self.assert_rejected(response)

    def test_wb_requires_every_requested_bit_and_exact_mask_length(self):
        # The all-zero mask of the right length is not a rejection since
        # 1.0.7.36 -- test_zero_mask_of_the_right_length_is_unsupported.
        for mask in (b"\xff\x7f", b"\xfe\xff",
                     b"\xff", b"\xff\xff\xff"):
            with self.subTest(mask=mask.hex()):
                self.assert_rejected(reply(mask))

    def test_zero_mask_of_the_right_length_is_unsupported(self):
        """Audit 2026-11-11 A2-01: a CRC-valid all-zero mask answering an
        enable request is the firmware's final "no producer" -- the range
        leaves `pending` for `unsupported` (not configured, no generation
        learnt), after the legacy form also had its chance."""
        for dev_type, wire_mode, count, mask in (
                ("mr02m", "auto", 16, b"\x00\x00"),
                ("dtv", "wb", 2, b"\x00")):
            with self.subTest(dev_type=dev_type):
                manager, dev = self.make_manager(
                    count=count, wire_mode=wire_mode, dev_type=dev_type)
                serial = SerialReplies([reply(mask), b""])
                self.assertTrue(manager._configure_device(serial, 2, dev))
                self.assertFalse(dev["configured"])
                self.assertEqual(dev["pending"], [])
                self.assertEqual(dev["unsupported"], dev["ranges"])
                self.assertEqual(manager._wb_frame_slaves, {})
                self.assertEqual(len(serial.sent), 2)

    def test_valid_wb_masks_and_arbitration_noise_confirm_without_fallback(self):
        for count, mask in ((2, b"\x03"), (6, b"\x3f"), (6, b"\xff"),
                            (8, b"\xff"), (16, b"\xff\xff"),
                            (36, b"\xff\xff\xff\xff\x0f")):
            with self.subTest(count=count, mask=mask.hex()):
                manager, dev = self.make_manager(count=count)
                serial = SerialReplies([b"\xff\xff" + reply(mask)])
                self.assertTrue(manager._configure_device(serial, 2, dev))
                self.assertTrue(dev["configured"])
                self.assertEqual(dev["pending"], [])
                self.assertEqual(manager._wb_frame_slaves, {2: True})
                self.assertEqual(len(serial.sent), 1)

    def test_valid_legacy_flags_after_unanswered_wb_preserve_fallback(self):
        for no_wb_reply in (b"", TimeoutError("no WB support")):
            with self.subTest(no_wb_reply=no_wb_reply):
                manager, dev = self.make_manager()
                serial = SerialReplies([no_wb_reply, reply(b"\x00")])
                self.assertTrue(manager._configure_device(serial, 2, dev))
                self.assertTrue(dev["configured"])
                self.assertEqual(dev["pending"], [])
                self.assertEqual(manager._wb_frame_slaves, {2: False})
                self.assertEqual([call[0][3] for call in serial.sent], [20, 5])

    def test_explicit_legacy_accepts_only_complete_flags_ok(self):
        manager, dev = self.make_manager(wire_mode="legacy")
        serial = SerialReplies([b"\xff" + reply(b"\x00")])
        self.assertTrue(manager._configure_device(serial, 2, dev))
        self.assertEqual(manager._wb_frame_slaves, {2: False})
        self.assertEqual(len(serial.sent), 1)
        for response in (bytes.fromhex("02461801000000"), reply(b"\x01"),
                         reply(b""), reply(b"\x00\x00")):
            with self.subTest(response=response.hex()):
                self.assert_rejected(response, wire_mode="legacy")

    def test_malformed_fallback_does_not_reclassify_confirmed_wb_slave(self):
        self.assert_rejected(bytes.fromhex("02461801000000"), cached=True)

    def test_explicit_dtv_wb_requires_requested_mask(self):
        # b"\x00" (all zero): test_zero_mask_of_the_right_length_is_unsupported.
        for mask, accepted in ((b"\x03", True), (b"\x01", False),
                               (b"\x03\x00", False)):
            with self.subTest(mask=mask.hex()):
                manager, dev = self.make_manager(
                    count=2, wire_mode="wb", dev_type="dtv")
                serial = SerialReplies([reply(mask), b""])
                self.assertEqual(manager._configure_device(serial, 2, dev), accepted)
                self.assertEqual(dev["configured"], accepted)
                self.assertEqual(manager._wb_frame_slaves,
                                 {2: True} if accepted else {})
                self.assertEqual(len(serial.sent), 1 if accepted else 2)

    def test_default_dtv_keeps_legacy_only_and_requires_flags_ok(self):
        for response, accepted in ((reply(b"\x00"), True),
                                   (reply(b"\x03"), False),
                                   (bytes.fromhex("02461801000000"), False)):
            with self.subTest(response=response.hex()):
                manager, dev = self.make_manager(count=2, dev_type="dtv")
                serial = SerialReplies([response])
                self.assertEqual(manager._configure_device(serial, 2, dev), accepted)
                self.assertEqual(manager._wb_frame_slaves,
                                 {2: False} if accepted else {})
                self.assertEqual(len(serial.sent), 1)
                self.assertEqual(serial.sent[0][0][3:5], b"\x05\x00")

    def test_reboot_discards_confirmed_wire_and_requires_a_fresh_ack(self):
        poller = ReadyPoller()
        manager, dev = self.make_manager(poller=poller)
        self.assertTrue(manager._configure_device(
            SerialReplies([reply(b"\xff\xff")]), 2, dev))
        manager._dispatch(2, fmb.FMB_EVT_REBOOT, 0, -1)
        self.assertNotIn(2, manager._wb_frame_slaves)
        self.assertFalse(dev["configured"])
        self.assertEqual(dev["pending"], dev["ranges"])
        self.assertEqual(poller.covered, [False])
        self.assertFalse(manager._configure_device(
            SerialReplies([bytes.fromhex("02461801000000")] * 2), 2, dev))
        self.assertNotIn(2, manager._wb_frame_slaves)

    def test_failed_handshake_preserves_classic_polling_and_retry_budget(self):
        poller = ReadyPoller()
        manager, dev = self.make_manager(poller=poller)
        serial = SerialReplies([bytes.fromhex("02461801000000")] * 12)
        clock = [1000.0]
        with mock.patch.object(fmb, "_get_port", return_value=serial), \
                mock.patch.object(fmb.time, "sleep") as sleep, \
                mock.patch.object(fmb.time, "monotonic", side_effect=lambda: clock[0]):
            manager.configure_all(only_ready=True)
            self.assertFalse(manager.has_configured())
            self.assertEqual(poller.covered, [False])
            self.assertEqual(poller.reads, 0)
            self.assertEqual(len(serial.sent), 6)
            self.assertEqual(sleep.call_args_list, [mock.call(0.5)] * 3)
            self.assertEqual(dev["retry_at"], 1015.0)
            manager.configure_all(only_ready=True)
            self.assertEqual(len(serial.sent), 6)
            clock[0] = 1015.0
            manager.configure_all(only_ready=True)
            self.assertEqual(len(serial.sent), 12)
            self.assertEqual(dev["retry_at"], 1045.0)
        manager.set_insurance(True)
        self.assertTrue(all(covered is False for covered in poller.covered))

    def test_partial_success_keeps_only_real_ack_and_rejected_range_pending(self):
        ranges = [(fmb.FMB_EVT_COIL, 1, 16), (fmb.FMB_EVT_INPUT, 18, 8)]
        manager, dev = self.make_manager(ranges=ranges)
        serial = SerialReplies([reply(b"\xff\xff"),
                                bytes.fromhex("02461801000000"), b""])
        self.assertFalse(manager._configure_device(serial, 2, dev))
        self.assertTrue(dev["configured"])
        self.assertEqual(dev["pending"], [ranges[1]])
        self.assertEqual(manager._wb_frame_slaves, {2: True})
        self.assertEqual(len(serial.sent), 3)


if __name__ == "__main__":
    unittest.main()
