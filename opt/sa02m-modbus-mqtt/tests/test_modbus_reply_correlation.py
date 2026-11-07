"""Request/reply correlation through real transaction and receive paths.

Only the UART boundary is scripted; TCP peers are real loopback sockets.
Protocol acknowledgement does not prove electrical state or exclude an
indistinguishable same-shape late RTU read / local single-write echo.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
import socket
import struct
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge_serial as serial_bus
import bridge_rtu_tcp
import bridge_tcp
from mbap_fake import FakeMbapServer, _recv_exact


class _Uart:
    is_open = True

    def __init__(self, response):
        self.response = response
        self.buf = b""
        self.requests = []

    def reset_input_buffer(self):
        self.buf = b""

    def write(self, request):
        self.requests.append(request)
        self.buf = self.response

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return len(self.buf)

    def read(self, n):
        data, self.buf = self.buf[:n], self.buf[n:]
        return data


class _ReplyPeer(FakeMbapServer):
    def __init__(self, request, response, raw=False, fragmented=False):
        self.request = request
        self.response = response
        self.raw = raw
        self.fragmented = fragmented
        super().__init__()

    def _send(self, conn, wire):
        if self.fragmented:
            conn.sendall(wire[:2])
            time.sleep(0.003)
            conn.sendall(wire[2:5])
            time.sleep(0.003)
            conn.sendall(wire[5:])
        else:
            conn.sendall(wire)

    def _act(self, conn, action, tid, pid, unit, pdu):
        self._send(conn, struct.pack(">HHH", tid, 0, len(self.response))
                   + self.response)
        return True

    def _serve(self, conn):
        if not self.raw:
            return super()._serve(conn)
        try:
            while True:
                request = _recv_exact(conn, len(self.request))
                if request is None:
                    return
                with self._lock:
                    self.requests.append(request)
                self._send(conn, self.response)
        except OSError:
            pass
        finally:
            conn.close()


def _request(fc):
    if fc in (1, 2):
        return serial_bus.build_request(10, fc, 9, 9)
    if fc in (3, 4):
        return serial_bus.build_request(10, fc, 9, 2)
    if fc == 5:
        return serial_bus.build_write_coil(10, 1, True)
    if fc == 6:
        return serial_bus.build_write_register(10, 1, 0x3456)
    if fc == 16:
        return serial_bus.build_write_registers(10, 1, [0x1234, 0x5678])
    return serial_bus.build_report_slave_id(10)


def _reply(request):
    fc = request[1]
    if fc in (1, 2):
        return bytes([10, fc, 2, 0x55, 0x01])
    if fc in (3, 4):
        return bytes([10, fc, 4, 0x12, 0x34, 0x56, 0x78])
    if fc in (5, 6, 16):
        return request[:6]
    return bytes([10, fc, 201]) + bytes(range(201))


class TestReplyCorrelation(unittest.TestCase):
    transports = ("serial", "rtu_tcp", "tcp")
    functions = (1, 2, 3, 4, 5, 6, 16, 17)

    @contextlib.contextmanager
    def exchange(self, transport, request, reply, *, corrupt_crc=False,
                 fragmented=False):
        wire = reply
        if transport != "tcp":
            wire = serial_bus._append_crc(reply)
            if corrupt_crc:
                wire = wire[:-1] + bytes([wire[-1] ^ 0x80])
        if transport == "serial":
            peer = _Uart(wire)
            client = serial_bus.ModbusSerial("test-uart", 19200, timeout=0.08,
                                             inter_frame_delay_s=0)
            with mock.patch.object(client, "_ensure_open", return_value=peer):
                yield client, peer
            return
        peer = _ReplyPeer(request, wire, raw=transport == "rtu_tcp",
                          fragmented=fragmented)
        cls = bridge_tcp.ModbusTcpClient if transport == "tcp" \
            else bridge_rtu_tcp.RtuTcpClient
        client = cls("127.0.0.1", peer.port, timeout=0.12, refuse_self=False)
        client._fail_n = 2
        if transport == "tcp":
            client._units[10] = [2, 0, 0, True]
        try:
            yield client, peer
        finally:
            client.close()
            peer.stop()

    def transact(self, transport, client, request):
        size = len(_reply(request)) if request[1] != 17 else 3
        if transport == "tcp":
            return client._transact(10, request, size)
        return client._transact(request, size + 2)

    def rejected(self, transport, request, reply, *, pattern=None,
                 corrupt_crc=False):
        with self.exchange(transport, request, reply,
                           corrupt_crc=corrupt_crc) as (client, peer):
            with self.assertRaises(IOError) as caught:
                self.transact(transport, client, request)
            if pattern:
                self.assertRegex(str(caught.exception), pattern)
            self.assertEqual(len(peer.requests), 1, "invalid replies must not resend")
            if transport != "serial":
                self.assertIsNone(client._sock, "semantic desync must drop the stream")
                self.assertEqual(client._fail_n, 2, "invalid reply is not success")
            if transport == "tcp":
                self.assertIn(10, client._units, "invalid reply is not a unit answer")

    def test_valid_reply_matrix_and_fragmented_tcp(self):
        for transport in self.transports:
            for fc in self.functions:
                with self.subTest(transport=transport, fc=fc):
                    request = _request(fc)
                    reply = _reply(request)
                    with self.exchange(transport, request, reply,
                                       fragmented=True) as (client, peer):
                        result = self.transact(transport, client, request)
                        self.assertEqual(result, reply if transport == "tcp" else
                                         serial_bus._append_crc(reply))
                        self.assertEqual(len(peer.requests), 1)
                        if transport != "serial":
                            self.assertEqual(client._fail_n, 0)
                        if transport == "tcp":
                            self.assertNotIn(10, client._units)

    def test_wrong_slave_and_function_matrix(self):
        for transport in self.transports:
            for fc in self.functions:
                request = _request(fc)
                reply = _reply(request)
                for label, bad in (
                    ("slave", bytes([11]) + reply[1:]),
                    ("function", reply[:1] + bytes([2 if fc == 1 else 1]) + reply[2:]),
                ):
                    with self.subTest(transport=transport, fc=fc, fault=label):
                        self.rejected(transport, request, bad)

    def test_wrong_write_tuple_matrix_preserves_root_red_bytes(self):
        for transport in self.transports:
            for fc in (5, 6, 16):
                request = _request(fc)
                reply = _reply(request)
                for label, bad in (
                    ("address", reply[:3] + bytes([2]) + reply[4:]),
                    ("value_or_count", reply[:4] + b"\x00\x00"),
                ):
                    with self.subTest(transport=transport, fc=fc, fault=label):
                        self.rejected(transport, request, bad)
            # Verbatim payload of root's wrong-FC positive, now a negative.
            with self.subTest(transport=transport, fault="root_wrong_function"):
                self.rejected(transport, _request(5), bytes.fromhex("0a060001ff00"))

    def test_read_byte_count_and_payload_length_matrix(self):
        for transport in self.transports:
            for fc in (1, 2, 3, 4, 17):
                request = _request(fc)
                reply = _reply(request)
                bad_replies = {
                    "short_payload": reply[:-1],
                    "extra_payload": reply + b"\x00",
                    "declared_too_large": reply[:2] + bytes([reply[2] + 1]) + reply[3:],
                    "declared_too_small": reply[:2] + bytes([reply[2] - 1]) + reply[3:],
                }
                if fc != 17:
                    bad_replies["self_consistent_wrong_count"] = (
                        reply[:2] + bytes([reply[2] + 1]) + reply[3:] + b"\x00")
                for label, bad in bad_replies.items():
                    with self.subTest(transport=transport, fc=fc, fault=label):
                        self.rejected(transport, request, bad)

    def test_exception_shape_and_corresponding_function_matrix(self):
        for transport in self.transports:
            for fc in self.functions:
                request = _request(fc)
                reply = bytes([10, fc | 0x80, 2])
                with self.subTest(transport=transport, fc=fc, fault="correct_exception"):
                    with self.exchange(transport, request, reply) as (client, peer):
                        with self.assertRaisesRegex(IOError,
                                "Modbus exception 2 on FC%02X" % fc):
                            self.transact(transport, client, request)
                        self.assertEqual(len(peer.requests), 1)
                        if transport != "serial":
                            self.assertIsNotNone(client._sock)
                for label, bad in (
                    ("foreign_exception", bytes([10, 0x82 if fc == 1 else 0x81, 2])),
                    ("missing_code", reply[:2]),
                    ("extra_code", reply + b"\x04"),
                ):
                    with self.subTest(transport=transport, fc=fc, fault=label):
                        self.rejected(transport, request, bad)

    def test_corrupt_crc_cannot_acknowledge(self):
        for transport in ("serial", "rtu_tcp"):
            for fc in self.functions:
                with self.subTest(transport=transport, fc=fc):
                    request = _request(fc)
                    self.rejected(transport, request, _reply(request), corrupt_crc=True)

    def test_public_read_and_write_do_not_return_false_values(self):
        for transport in self.transports:
            with self.subTest(transport=transport, operation="read"):
                request = _request(3)
                with self.exchange(transport, request,
                        b"\x0a\x03\x06\x00\x01\x00\x02\x00\x03") as (client, peer):
                    with self.assertRaises(IOError):
                        client.read_holding_registers(10, 9, 2)
                    self.assertEqual(len(peer.requests), 1)
            with self.subTest(transport=transport, operation="write"):
                request = _request(5)
                with self.exchange(transport, request,
                        bytes.fromhex("0a050002ff00")) as (client, peer):
                    with self.assertRaises(IOError):
                        client.write_coil(10, 1, True)
                    self.assertEqual(len(peer.requests), 1)


if __name__ == "__main__":
    unittest.main()
