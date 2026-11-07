"""MR writes are sent once even when a reused transparent session fails."""
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge_serial import _append_crc
from bridge_rtu_tcp import RtuTcpClient
from bridge_mr02m import MR02mPoller
from bridge_mqtt import DeviceLiveCache
from mbap_fake import FakeMbapServer, _recv_exact
from test_mr_writeback_readback import OutputPub


class _ActuatingPeer(FakeMbapServer):
    def __init__(self, fail_fc, failure):
        self.fail_fc, self.failure = fail_fc, failure
        self.failed = False
        self.coil, self.register = 0, 0
        self.applied = threading.Event()
        super().__init__()

    def _serve(self, conn):
        try:
            while True:
                request = _recv_exact(conn, 8)
                if request is None: return
                with self._lock:
                    self.requests.append(request)
                    fc = request[1]
                    if fc == 5: self.coil = int(request[4:6] == b"\xff\x00")
                    if fc == 6: self.register = int.from_bytes(request[4:6], "big")
                    fail = fc == self.fail_fc and not self.failed
                    if fail: self.failed = True
                if fc in (5, 6): self.applied.set()
                if fail:
                    if self.failure == "partial": conn.sendall(request[:3])
                    conn.shutdown(socket.SHUT_WR)
                    return
                if fc == 1:
                    response = _append_crc(bytes([request[0], fc, 1, self.coil]))
                elif fc in (3, 4):
                    response = _append_crc(bytes([request[0], fc, 2]) + self.register.to_bytes(2, "big"))
                else:
                    response = request
                conn.sendall(response)
        except OSError:
            pass
        finally:
            conn.close()


class _FailedSend:
    """The socket boundary lost the send outcome after bytes reached the peer."""
    def __init__(self, sock, applied):
        self.sock, self.applied = sock, applied

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def sendall(self, data):
        self.sock.sendall(data)
        if not self.applied.wait(2): raise AssertionError("peer never observed write")
        raise OSError("uncertain send")


class TestMrRtuTcpWriteOnce(unittest.TestCase):
    def test_reused_session_lost_ack_partial_and_send_failure(self):
        for fc in (5, 6):
            for failure in ("ack_lost", "partial", "send_failure"):
                with self.subTest(fc=fc, failure=failure):
                    peer = _ActuatingPeer(fc, failure)
                    client = RtuTcpClient("127.0.0.1", peer.port, 0.3, refuse_self=False)
                    try:
                        client.read_holding_registers(2, 33, 1)
                        if failure == "send_failure":
                            client._sock = _FailedSend(client._sock, peer.applied)
                        pub = OutputPub()
                        p = MR02mPoller({"id": "mr-wire", "type": "mr02m", "module_type": 11,
                            "transport": "rtu_tcp", "host": "192.0.2.10", "tcp_port": peer.port,
                            "address": 2}, pub)
                        p.get_port = lambda: client
                        with mock.patch.object(DeviceLiveCache, "flush_file"):
                            (p._writeback_do(1, True) if fc == 5 else p._writeback_ao(1, 201))
                        requests = list(peer.requests)
                        self.assertEqual(sum(r[1] == fc for r in requests), 1, "one actuator wire request")
                        self.assertEqual(requests[-1][1], 1 if fc == 5 else 3)
                        self.assertEqual(pub.controls[-1][2], "1" if fc == 5 else "201")
                        self.assertEqual(pub.errors[-1][2], "")
                    finally:
                        client.close(); peer.stop()

    def test_default_transport_retry_remains_for_other_callers_and_reads(self):
        for fc in (1, 5, 6):
            with self.subTest(fc=fc):
                peer = _ActuatingPeer(fc, "ack_lost")
                client = RtuTcpClient("127.0.0.1", peer.port, 0.3, refuse_self=False)
                try:
                    client.read_holding_registers(2, 33, 1)
                    if fc == 1: client.read_coils(2, 1, 1)
                    elif fc == 5: client.write_coil(2, 1, True)
                    else: client.write_register(2, 33, 201)
                    self.assertEqual(sum(r[1] == fc for r in peer.requests), 2)
                finally:
                    client.close(); peer.stop()


if __name__ == "__main__":
    unittest.main()
