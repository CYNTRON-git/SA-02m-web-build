"""Real PTY and local socket I/O through FlasherProtocol, without device writes."""
import os
import select
import socket
import threading
import time
import unittest

import serial

from sa02m_flasher import modbus_rtu as rtu, serial_port
from sa02m_flasher.flash_protocol import FlasherProtocol
from .test_response_correlation import acknowledgement, exception


class ObservedTransport:
    def __init__(self, stream, consumed):
        self.stream, self.consumed = stream, consumed

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, count):
        data = self.stream.read(count)
        if data:
            self.consumed.set()
        return data

    def recv(self, count):
        data = self.stream.recv(count)
        if data:
            self.consumed.set()
        return data


@unittest.skipUnless(os.name == 'posix', 'real PTY composition requires POSIX')
class FlashTransportComposition(unittest.TestCase):
    def exercise(self, *, tcp, fast, mode):
        expected = rtu.build_write_multiple_registers(247, 0x2101, [0] * 123)
        stale_request = rtu.build_write_multiple_registers(247, 0x2100, [0] * 123)
        if fast:
            expected = rtu.build_fast_modbus_request(0x0E0A0001, expected[1:-2])
            stale_request = rtu.build_fast_modbus_request(0x0E0A0001, stale_request[1:-2])
        consumed, ready, completed = threading.Event(), threading.Event(), threading.Event()
        failures, captured = [], []
        if tcp:
            client, peer = socket.socketpair()
            peer_fd = peer.fileno()
        else:
            peer_fd, slave_fd = os.openpty()
            try:
                client = serial.Serial(os.ttyname(slave_fd), 115200, timeout=.05, write_timeout=1, exclusive=True)
            finally:
                os.close(slave_fd)
            peer = None

        def write_all(data):
            while data:
                written = os.write(peer_fd, data)
                self.assertGreater(written, 0)
                data = data[written:]

        def device():
            try:
                ready.set()
                data = bytearray()
                deadline = time.monotonic() + 3
                while len(data) < len(expected):
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, 'peer request deadline')
                    self.assertTrue(select.select([peer_fd], [], [], remaining)[0])
                    part = os.read(peer_fd, len(expected) - len(data))
                    self.assertTrue(part, 'peer request EOF')
                    data.extend(part)
                captured.append(bytes(data))
                self.assertEqual(bytes(data), expected)
                if mode == 'fragmented_echo':
                    write_all(expected[:8])
                    self.assertTrue(consumed.wait(3), 'transport did not read first echo fragment')
                    write_all(expected[8:] + acknowledgement(stale_request) + acknowledgement(expected))
                elif mode == 'exception':
                    write_all(exception(expected))
                elif mode == 'stale_only':
                    write_all(acknowledgement(stale_request))
                else:
                    self.fail('unknown peer mode')
                self.assertTrue(completed.wait(3), 'transport did not finish')
                if select.select([peer_fd], [], [], 0)[0]:
                    extra = os.read(peer_fd, 1024)
                    self.assertFalse(extra, 'unexpected retransmission or readiness/scan request')
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=device, name='flasher-scripted-peer', daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(3))
            transport = ObservedTransport(client, consumed)
            receive = serial_port.send_receive_tcp if tcp else serial_port.send_receive
            timeout = 150 if mode == 'stale_only' else 2000
            protocol = FlasherProtocol(lambda req: receive(transport, req, response_timeout_ms=timeout))
            protocol.use_indexed_data_blocks = True
            error = (protocol.send_data_block_by_serial(0x0E0A0001, 1, bytes(246)) if fast
                     else protocol.send_data_block(247, 1, bytes(246)))
            completed.set()
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'scripted peer leaked')
            if failures:
                raise failures[0]
            self.assertEqual(captured, [expected])
            if mode == 'fragmented_echo':
                self.assertIsNone(error)
            elif mode == 'exception':
                self.assertIn('Исключение Modbus: код 4', error or '')
            else:
                self.assertIsNotNone(error)
        finally:
            completed.set()
            client.close()
            if peer:
                peer.close()
            else:
                os.close(peer_fd)
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'cleanup did not join the peer')

    def test_pty_and_tcp_fragmented_echo_stale_then_current_ack(self):
        for tcp in (False, True):
            for fast in (False, True):
                with self.subTest(tcp=tcp, fast=fast):
                    self.exercise(tcp=tcp, fast=fast, mode='fragmented_echo')

    def test_complete_exceptions_on_real_transports(self):
        for tcp in (False, True):
            for fast in (False, True):
                with self.subTest(tcp=tcp, fast=fast):
                    self.exercise(tcp=tcp, fast=fast, mode='exception')

    def test_stale_ack_alone_cannot_complete_a_block(self):
        for tcp in (False, True):
            for fast in (False, True):
                with self.subTest(tcp=tcp, fast=fast):
                    self.exercise(tcp=tcp, fast=fast, mode='stale_only')


if __name__ == '__main__':
    unittest.main()
