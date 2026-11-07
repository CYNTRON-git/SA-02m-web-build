"""Fragmented echoes and replies on both real receive loops, with a fake clock."""
import socket
import unittest
from unittest.mock import patch

from sa02m_flasher import modbus_rtu, serial_port
from .test_response_correlation import acknowledgement, exception, request


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Chunks:
    bytesize, stopbits, baudrate, parity = 8, 1, 115200, 'N'

    def __init__(self, chunks, clock, eof=False):
        self.chunks = list(filter(None, chunks))
        self.clock, self.eof = clock, eof
        self.written = []
        self.timeout = .05

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.written.append(data)
        return len(data)

    sendall = write

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, size):
        self.clock.sleep(.0001)
        data = self.chunks.pop(0)
        if len(data) > size:
            self.chunks.insert(0, data[size:])
        return data[:size]

    def settimeout(self, value):
        self.timeout = value

    def recv(self, size):
        if self.chunks:
            return self.read(size)
        if self.eof:
            return b''
        self.clock.sleep(self.timeout)
        raise socket.timeout()


class ResponseFraming(unittest.TestCase):
    def exchange(self, req, chunks, tcp=False, eof=False, cancel=None):
        clock = Clock()
        peer = Chunks(chunks, clock, eof)
        receiver = serial_port.send_receive_tcp if tcp else serial_port.send_receive
        with patch.object(serial_port.time, 'perf_counter', clock.now), patch.object(serial_port.time, 'sleep', clock.sleep):
            result = receiver(peer, req, response_timeout_ms=5000,
                              cancel_check=(lambda: cancel(clock)) if cancel else None)
        self.assertEqual(peer.written, [req], 'transport must transmit exactly one request')
        return result, clock.value, peer

    def test_every_request_echo_split(self):
        for tcp in (False, True):
            for fast in (False, True):
                req = request(fast)
                ack = acknowledgement(req)
                for split in range(1, len(req) + 1):
                    with self.subTest(tcp=tcp, fast=fast, split=split):
                        actual, elapsed, peer = self.exchange(req, [req[:split], req[split:], ack], tcp)
                        self.assertEqual(actual, ack)
                        self.assertFalse(peer.chunks)
                        self.assertLess(elapsed, .05)

    def test_every_ack_split_bytewise_and_coalesced_echo(self):
        for tcp in (False, True):
            for fast in (False, True):
                req = request(fast)
                ack = acknowledgement(req)
                forms = [[ack], [req + ack], [bytes([b]) for b in ack]]
                forms += [[ack[:i], ack[i:]] for i in range(1, len(ack))]
                for chunks in forms:
                    with self.subTest(tcp=tcp, fast=fast, chunks=len(chunks)):
                        self.assertEqual(self.exchange(req, chunks, tcp)[0], ack)

    def test_complete_fast_exception_does_not_consume_timeout(self):
        req = request(True)
        reply = exception(req)
        self.assertEqual(len(reply), 11)
        self.assertTrue(serial_port._rtu_response_complete(reply))
        for tcp in (False, True):
            actual, elapsed, _ = self.exchange(req, [reply], tcp)
            self.assertEqual(actual, reply)
            self.assertLessEqual(elapsed, .05)

    def test_absence_partial_echo_and_stale_ack_wait_only_original_deadline(self):
        for tcp in (False, True):
            for fast in (False, True):
                req = request(fast)
                # A valid but unrelated request's response is never an ACK here.
                stale = acknowledgement(request(not fast))
                for chunks in ([], [req[:16]], [acknowledgement(req)[:-1]], [stale] * 10):
                    with self.subTest(tcp=tcp, fast=fast, chunks=len(chunks)):
                        actual, elapsed, _ = self.exchange(req, chunks, tcp)
                        self.assertIsNone(actual)
                        self.assertGreaterEqual(elapsed, 5)
                        self.assertLess(elapsed, 5.05)

    def test_cancellation_and_tcp_eof_are_not_acknowledgements(self):
        req = request()
        for tcp in (False, True):
            actual, elapsed, _ = self.exchange(req, [req[:16]], tcp, cancel=lambda clock: clock.value > .02)
            self.assertIsNone(actual)
            self.assertLess(elapsed, .1)
        actual, elapsed, _ = self.exchange(req, [req[:16]], tcp=True, eof=True)
        self.assertIsNone(actual)
        self.assertLess(elapsed, .05)

    def test_cancellation_during_read_cannot_publish_a_matching_ack(self):
        req = request()
        for tcp in (False, True):
            with self.subTest(tcp=tcp):
                threshold = .00205 if tcp else .00235
                actual, _, _ = self.exchange(req, [acknowledgement(req)], tcp,
                                             cancel=lambda clock: clock.value > threshold)
                self.assertIsNone(actual)

    def test_noise_limit_is_explicit_and_does_not_grow_forever(self):
        req = request()
        ack = acknowledgement(req)
        for tcp in (False, True):
            self.assertEqual(self.exchange(req, [b'\xff' * (1024 - len(ack)) + ack], tcp)[0], ack)
            with self.assertRaisesRegex(ValueError, '1024'):
                self.exchange(req, [b'\xff' * 1025], tcp)

    def test_ambiguous_prefix_is_resolved_only_at_deadline_or_after_full_echo(self):
        req = modbus_rtu.build_write_multiple_registers(1, 6171, [0x8D00] * 123)
        ack = acknowledgement(req)
        for tcp in (False, True):
            actual, elapsed, _ = self.exchange(req, [ack], tcp)
            self.assertEqual(actual, ack)
            self.assertGreaterEqual(elapsed, 5)
            self.assertIsNone(self.exchange(req, [req[:16]], tcp)[0])
            for split in range(1, len(req) + 1):
                with self.subTest(tcp=tcp, split=split):
                    actual, elapsed, peer = self.exchange(req, [req[:split], req[split:], ack], tcp)
                    self.assertEqual(actual, ack)
                    self.assertFalse(peer.chunks)
                    self.assertLess(elapsed, .05)


if __name__ == '__main__':
    unittest.main()
