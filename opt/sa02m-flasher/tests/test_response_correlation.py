"""A flash acknowledgement must belong to the transmitted request."""
import struct
import io
import sys
import types
import unittest
from unittest.mock import patch

from sa02m_flasher import modbus_rtu as rtu
from sa02m_flasher.flash_protocol import FlasherProtocol


def frame(body, reverse=False):
    return body + struct.pack('>H' if reverse else '<H', rtu.crc16_modbus(body))


def request(fast=False):
    standard = rtu.build_write_multiple_registers(247, 0x2100, [0] * 123)
    return rtu.build_fast_modbus_request(0x0E0A0001, standard[1:-2]) if fast else standard


def acknowledgement(req):
    if req[:3] == b'\xfd\x46\x08':
        return frame(b'\xfd\x46\x09' + req[3:7] + req[7:12])
    return frame(req[:6])


def exception(req, code=4):
    if req[:3] == b'\xfd\x46\x08':
        return frame(b'\xfd\x46\x09' + req[3:7] + bytes([req[7] | 128, code]))
    return frame(bytes([req[0], req[1] | 128, code]))


class ProtocolCorrelation(unittest.TestCase):
    def test_wrong_write_acknowledgements_never_advance(self):
        for fast in (False, True):
            for wrong in ('destination', 'function', 'register', 'quantity'):
                with self.subTest(fast=fast, wrong=wrong):
                    def respond(req):
                        reply = bytearray(acknowledgement(req)[:-2])
                        base = 7 if fast else 1
                        if wrong == 'destination':
                            reply[3 if fast else 0] ^= 1
                        elif wrong == 'function':
                            reply[base:] = b'\x03\x02\x00\x01'
                        elif wrong == 'register':
                            reply[base + 2] ^= 1
                        else:
                            reply[base + 4] -= 1
                        return frame(bytes(reply))
                    protocol = FlasherProtocol(respond)
                    protocol.use_indexed_data_blocks = True
                    result = (protocol.send_data_block_by_serial(0x0E0A0001, 0, bytes(246))
                              if fast else protocol.send_data_block(247, 0, bytes(246)))
                    self.assertIsNotNone(result, 'foreign response acknowledged a block')

    def test_matching_callback_and_exceptions(self):
        for fast in (False, True):
            for reply, valid in ((acknowledgement, True), (exception, False)):
                with self.subTest(fast=fast, valid=valid):
                    protocol = FlasherProtocol(reply)
                    result = (protocol.write_multiple_registers_by_serial(0x0E0A0001, 0x2100, [1])
                              if fast else protocol.write_multiple_registers(247, 0x2100, [1]))
                    self.assertEqual(result is None, valid)

    def test_console_jump_callbacks_cannot_bypass_correlation(self):
        firmware = types.ModuleType('flasher_windows.firmware')
        firmware.check_app_vector_table = lambda image: (True, None)
        with patch.dict(sys.modules, {'flasher_windows.firmware': firmware}), patch('sys.stderr', new_callable=io.StringIO):
            for fast in (False, True):
                for wrong in (False, True):
                    def respond(req):
                        body = bytearray(acknowledgement(req)[:-2])
                        if wrong:
                            body[9 if fast else 3] ^= 1
                        return frame(bytes(body))
                    protocol = FlasherProtocol(respond)
                    error = (protocol.jump_to_app_with_console_diagnostics_by_serial(123, b'') if fast
                             else protocol.jump_to_app_with_console_diagnostics(12, b''))
                    self.assertEqual(error is not None, wrong)

    def test_oversized_injected_callback_is_a_bounded_protocol_error(self):
        protocol = FlasherProtocol(lambda req: b'X' * 1025)
        for error in (protocol.write_single_register(12, 1, 2),
                      protocol.write_multiple_registers_by_serial(123, 1, [2])):
            self.assertIsNotNone(error)
            self.assertLess(len(error), 200)


class ResponseSelector(unittest.TestCase):
    def select(self, req, data, **kwargs):
        selector = getattr(rtu, 'find_matching_response', None)
        self.assertTrue(callable(selector), 'request-aware response boundary is missing')
        return selector(req, data, **kwargs)

    def test_write_echo_tuples_and_crc(self):
        for fast in (False, True):
            req = request(fast)
            ack = acknowledgement(req)
            for data in (ack, req + ack, b'\x00\xff\x00' + req + ack):
                with self.subTest(fast=fast, length=len(data)):
                    self.assertEqual(self.select(req, data), ack)
            self.assertIsNone(self.select(req, req))
            self.assertIsNone(self.select(req, ack[:-1]))
            self.assertIsNone(self.select(req, ack[:-1] + bytes([ack[-1] ^ 1])))
            stale = bytearray(ack[:-2])
            stale[9 if fast else 3] ^= 1
            stale = frame(bytes(stale))
            self.assertIsNone(self.select(req, stale))
            self.assertEqual(self.select(req, stale + ack), ack)
            self.assertEqual(self.select(req, exception(req)), exception(req))
            broken = exception(req)[:-1] + bytes([exception(req)[-1] ^ 1])
            self.assertIsNone(self.select(req, broken))

    def test_reads_report_id_and_crc_compatibility(self):
        for func, count, payload in ((1, 9, b'\x01\x01'), (2, 9, b'\x01\x01'),
                                     (3, 2, b'\x00\x01\x00\x02'), (4, 2, b'\x00\x01\x00\x02')):
            req = frame(struct.pack('>BBHH', 12, func, 0x100, count))
            good = frame(bytes([12, func, len(payload)]) + payload)
            self.assertEqual(self.select(req, good), good)
            bad = frame(bytes([12, func, len(payload) + 1]) + payload + b'\0')
            self.assertIsNone(self.select(req, bad))
            self.assertIsNone(self.select(req, frame(good[:-2], reverse=True)))
            fast_req = rtu.build_fast_modbus_request(123, req[1:-2])
            fast_good = frame(b'\xfd\x46\x09' + struct.pack('>I', 123) + good[1:-2])
            self.assertEqual(self.select(fast_req, fast_good), fast_good)
        req = rtu.build_report_slave_id(12)
        for count in (1, 206, 246):
            reply = frame(bytes([12, 17, count]) + b'X' * count)
            self.assertEqual(self.select(req, reply), reply)
        for count in (0, 247):
            self.assertIsNone(self.select(req, frame(bytes([12, 17, count]) + b'X' * count)))
        for req in (rtu.build_write_coil(12, 2, True), rtu.build_write_single_register(12, 2, 3), request()):
            ack = acknowledgement(req)
            self.assertEqual(self.select(req, ack), ack)
            reverse = frame(ack[:-2], reverse=True)
            self.assertEqual(self.select(req, reverse), reverse)
            self.assertIsNone(rtu.parse_response(reverse)[2])

    def test_long_request_ack_prefix_collision_waits_for_window_end(self):
        # This fixed tuple makes the FC16 ACK CRC start with the request byte count.
        req = rtu.build_write_multiple_registers(1, 6171, [0x8D00] * 123)
        ack = acknowledgement(req)
        self.assertEqual(req[:8], ack, 'collision vector must remain a real valid ACK')
        self.assertIsNone(self.select(req, ack))
        self.assertEqual(self.select(req, ack, final=True), ack)
        self.assertIsNone(self.select(req, req[:16], final=True))
        self.assertEqual(self.select(req, req + ack), ack)

    def test_invalid_request_and_bounded_input(self):
        for req in (b'', b'\x01', b'\x01\x03', b'\x01\x03\x00\x00\x00\x01\x00\x00'):
            self.assertIsNone(self.select(req, acknowledgement(request())))
        with self.assertRaises(ValueError):
            self.select(request(), b'\xff' * 1025)


if __name__ == '__main__':
    unittest.main()
