"""Absolute client budgets over real Unix framing and descriptor transfer."""
import array
import contextlib
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sa02m_ports import client


def read_line(sock):
    payload = b""
    while b"\n" not in payload:
        chunk = sock.recv(2048)
        if not chunk:
            return payload
        payload += chunk
    return payload


def send_fd(sock, fd, payload=b'{"ok":true}\n'):
    sock.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                             array.array("i", [fd]))])


class Peer:
    def __init__(self, script):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "control.sock")
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.path)
        self.listener.listen(1)
        self.listener.settimeout(0.5)
        self.errors = []

        def run():
            try:
                conn, _ = self.listener.accept()
                with conn:
                    conn.settimeout(0.7)
                    script(conn)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass
            except BaseException as exc:
                self.errors.append(exc)

        self.thread = threading.Thread(target=run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.thread.join(1.5)
        self.listener.close()
        self.tmp.cleanup()
        if self.thread.is_alive():
            raise AssertionError("scripted coordinator did not terminate")
        if self.errors:
            raise self.errors[0]


class ObservedSocket:
    """Record received fd numbers while retaining the real socket transport."""
    def __init__(self, sock):
        self.sock = sock
        self.received = []

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def recvmsg(self, *args):
        result = self.sock.recvmsg(*args)
        for level, kind, data in result[1]:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                numbers = array.array("i")
                numbers.frombytes(data)
                self.received.extend(numbers)
        return result


@unittest.skipUnless(hasattr(socket, "SCM_RIGHTS"), "Unix descriptor passing required")
class ClientDeadlineTests(unittest.TestCase):
    def assert_fds_closed(self, numbers):
        self.assertTrue(numbers, "the peer must actually transfer a descriptor")
        for fd in numbers:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_partial_frame_uses_one_budget_and_closes_received_fd(self):
        with tempfile.TemporaryFile() as lock:
            def drip(conn):
                read_line(conn)
                send_fd(conn, lock.fileno(), b'{')
                for byte in b'"ok":true}\n':
                    time.sleep(0.03)
                    conn.sendall(bytes([byte]))

            with Peer(drip) as peer, contextlib.closing(client.connect(peer.path)) as sock:
                observed = ObservedSocket(sock)
                started = time.monotonic()
                with self.assertRaises(client.CoordinatorUnavailable):
                    client.request(observed, {"op": "acquire"}, deadline=started + 0.10)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 0.25)
                self.assertGreater(elapsed, 0.06)
                self.assert_fds_closed(observed.received)
                self.assertEqual(sock.gettimeout(), client.TIMEOUT)

    def test_default_preserves_per_socket_operation_timeout(self):
        def drip(conn):
            read_line(conn)
            for byte in b'{"ok":true}\n':
                conn.sendall(bytes([byte]))
                time.sleep(0.025)

        with mock.patch.object(client, "TIMEOUT", 0.12), Peer(drip) as peer:
            with contextlib.closing(client.connect(peer.path)) as sock:
                started = time.monotonic()
                result, fds = client.request(sock, {"op": "poll"})
                self.assertTrue(result["ok"])
                self.assertEqual(fds, [])
                self.assertGreater(time.monotonic() - started, 0.20)
                self.assertEqual(sock.gettimeout(), 0.12)

    def test_silent_reply_and_blocked_send_share_the_absolute_budget(self):
        for blocked_send in (False, True):
            with self.subTest(blocked_send=blocked_send):
                def silent(conn):
                    if not blocked_send:
                        read_line(conn)
                    time.sleep(0.3)

                with Peer(silent) as peer, contextlib.closing(client.connect(peer.path)) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                    started = time.monotonic()
                    with self.assertRaises(client.CoordinatorUnavailable):
                        client.request(sock, {"op": "poll", "padding": "x" * (500000 if blocked_send else 0)},
                                       deadline=started + 0.08)
                    self.assertLess(time.monotonic() - started, 0.22)

    def test_expired_connect_budget_closes_socket_without_connecting(self):
        for deadline, error in ((time.monotonic() - 1, client.CoordinatorUnavailable),
                                (float("nan"), ValueError), (float("inf"), ValueError)):
            with self.subTest(deadline=deadline):
                created = socket.socket(socket.AF_UNIX)
                with mock.patch.object(client.socket, "socket", return_value=created):
                    with self.assertRaises(error):
                        client.connect("/not-used", deadline=deadline)
                self.assertEqual(created.fileno(), -1)

    def test_expired_request_sends_nothing_and_preserves_socket_timeout(self):
        observed = []
        with Peer(lambda conn: observed.append(conn.recv(2048))) as peer:
            with contextlib.closing(client.connect(peer.path)) as sock:
                sock.settimeout(0.33)
                with self.assertRaises(client.CoordinatorUnavailable):
                    client.request(sock, {"op": "poll"}, deadline=time.monotonic() - 1)
                self.assertEqual(sock.gettimeout(), 0.33)
        self.assertEqual(observed, [b""])

    def test_invalid_release_budget_is_rejected_before_acquisition(self):
        for timeout in (-1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), mock.patch.object(client, "connect") as connect:
                with self.assertRaises(ValueError):
                    client.Lease("/dev/COM4", release_timeout=timeout)
                connect.assert_not_called()

    def test_connect_budget_is_applied_and_default_timeout_restored(self):
        sock = mock.Mock()
        sock.connect.side_effect = lambda path: self.assertLessEqual(sock.settimeout.call_args.args[0], 0.2)
        with mock.patch.object(client.socket, "socket", return_value=sock):
            self.assertIs(client.connect("test", deadline=time.monotonic() + 0.2), sock)
        self.assertEqual(sock.settimeout.call_args.args, (client.TIMEOUT,))

    def test_acquire_timeout_closes_fd_and_connection(self):
        disconnected = threading.Event()
        with tempfile.TemporaryFile() as lock:
            def incomplete(conn):
                read_line(conn)
                send_fd(conn, lock.fileno(), b'{"ok":')
                if conn.recv(1) == b"":
                    disconnected.set()

            with Peer(incomplete) as peer:
                observed = ObservedSocket(client.connect(peer.path))
                lease = client.Lease("/dev/COM4", socket_path=peer.path,
                                     acquire_deadline=time.monotonic() + 0.08)
                with mock.patch.object(client, "connect", return_value=observed):
                    with self.assertRaises(client.CoordinatorUnavailable):
                        lease.__enter__()
                self.assertIsNone(lease.sock)
                self.assertIsNone(lease.fd)
                self.assertEqual(observed.fileno(), -1)
                self.assert_fds_closed(observed.received)
                self.assertTrue(disconnected.wait(0.3))

    def test_release_has_an_independent_budget_after_acquire_deadline(self):
        with tempfile.TemporaryFile() as lock:
            def release(conn):
                read_line(conn)
                send_fd(conn, lock.fileno())
                self.assertIn(b'"release"', read_line(conn))
                time.sleep(0.04)
                conn.sendall(b'{"ok":true,"pending_restore":false}\n')

            with Peer(release) as peer:
                lease = client.Lease("/dev/COM4", socket_path=peer.path,
                                     acquire_deadline=time.monotonic() + 0.08,
                                     release_timeout=0.15)
                with lease:
                    transferred = lease.fd
                    time.sleep(0.11)
                self.assertIsNone(lease.sock)
                self.assertIsNone(lease.fd)
                self.assert_fds_closed([transferred])
                self.assertFalse(lease.status["pending_restore"])

    def test_release_deadline_closes_everything_and_reports_recovery_pending(self):
        disconnected = threading.Event()
        with tempfile.TemporaryFile() as lock:
            def release(conn):
                read_line(conn)
                send_fd(conn, lock.fileno())
                read_line(conn)
                conn.sendall(b'{"ok":')
                if conn.recv(1) == b"":
                    disconnected.set()

            with Peer(release) as peer:
                lease = client.Lease("/dev/COM4", socket_path=peer.path, release_timeout=0.08)
                lease.__enter__()
                transferred = lease.fd
                started = time.monotonic()
                with self.assertRaisesRegex(client.PortError, "recovery pending"):
                    lease.close()
                self.assertLess(time.monotonic() - started, 0.22)
                self.assertIsNone(lease.sock)
                self.assertIsNone(lease.fd)
                self.assert_fds_closed([transferred])
                self.assertTrue(disconnected.wait(0.3))
                lease.close()

    def test_expired_explicit_release_deadline_still_closes_lock_and_socket(self):
        observed = []
        with tempfile.TemporaryFile() as lock:
            def release(conn):
                read_line(conn)
                send_fd(conn, lock.fileno())
                observed.append(read_line(conn))

            with Peer(release) as peer:
                lease = client.Lease("/dev/COM4", socket_path=peer.path, release_timeout=1)
                lease.__enter__()
                transferred = lease.fd
                with self.assertRaisesRegex(client.PortError, "recovery pending"):
                    lease.close(deadline=time.monotonic() - 1)
                self.assertIsNone(lease.sock)
                self.assertIsNone(lease.fd)
                self.assert_fds_closed([transferred])
            self.assertEqual(observed, [b""])


if __name__ == "__main__":
    unittest.main()
