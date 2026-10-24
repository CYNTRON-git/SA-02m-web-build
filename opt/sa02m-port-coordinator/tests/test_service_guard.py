"""Outage vs refusal on the service-guard path, MQTT pause readiness, READY=1 plumbing.

Real Unix sockets with scripted peers; no broker, no systemd. The broker-backed
cases live in test_process_ownership.py.
"""

import contextlib
import io
import json
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
from sa02m_ports import CoordinatorUnavailable, PortError, ServiceMutation, client  # noqa: E402
from sa02m_ports import broker, service_guard  # noqa: E402


def scripted_peer(path, script):
    """Bind `path`, serve one connection with `script(conn)` on a thread."""
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(path)
    listener.listen(1)

    def run():
        conn, _ = listener.accept()
        try:
            script(conn)
        finally:
            conn.close()
            listener.close()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class GuardErrorCodes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "control.sock")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self):
        argv = ["guard", "--run", "/bin/sh", "start", "mplc4", "mplc4.service"]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(client, "SOCKET", self.path), \
                mock.patch.object(service_guard.os, "geteuid", return_value=0), \
                contextlib.redirect_stdout(out):
            rc = service_guard.main()
        return rc, json.loads(out.getvalue())

    def test_absent_socket_is_an_outage_not_a_busy_port(self):
        with self.assertRaises(CoordinatorUnavailable):
            ServiceMutation("mplc4.service", self.path).__enter__()
        rc, reply = self.run_main()
        self.assertEqual((rc, reply["ok"], reply["error"]), (1, False, "coordinator_unavailable"))
        self.assertIn("unavailable", reply["detail"])

    def test_silent_broker_is_an_outage(self):
        scripted_peer(self.path, lambda conn: time.sleep(2))
        with mock.patch.object(client, "TIMEOUT", 0.3):
            started = time.monotonic()
            with self.assertRaises(CoordinatorUnavailable):
                ServiceMutation("mplc4.service", self.path).__enter__()
            self.assertLess(time.monotonic() - started, 1.5)
            rc, reply = self.run_main()
        self.assertEqual(reply["error"], "coordinator_unavailable")

    def test_broker_refusal_stays_port_busy(self):
        def refuse(conn):
            conn.recv(2048)
            conn.sendall(b'{"ok": false, "error": "Service is reserved by a UART operation or restoration"}\n')
        scripted_peer(self.path, refuse)
        rc, reply = self.run_main()
        self.assertEqual((rc, reply["error"]), (1, "port_busy"))
        self.assertIn("reserved", reply["detail"])

    def test_error_code_mapping(self):
        self.assertEqual(service_guard.error_code(CoordinatorUnavailable("x")), "coordinator_unavailable")
        self.assertEqual(service_guard.error_code(PortError("x")), "port_busy")
        self.assertEqual(service_guard.error_code(PermissionError("x")), "port_busy")


class MqttPauseReadiness(unittest.TestCase):
    """The bridge binds its pause socket after its MQTT connect: a lease at boot waits, bounded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "scan-lease.sock")
        self.system = broker.System()
        self.system.MQTT_PAUSE_RETRY_DELAY = 0.05
        self.system.MQTT_PAUSE_RETRIES = 10

    def tearDown(self):
        self.tmp.cleanup()

    def ack(self, conn):
        conn.recv(4096)
        conn.sendall(b'{"ok": true}\n')
        time.sleep(0.5)

    def test_socket_appearing_late_is_awaited(self):
        with mock.patch.object(self.system, "_state", return_value="active"):
            timer = threading.Timer(0.2, lambda: scripted_peer(self.path, self.ack))
            timer.start()
            hold = self.system.mqtt_pause("/dev/COM1", self.path)
        self.assertIsNotNone(hold)
        hold.close()

    def test_bridge_still_activating_is_awaited(self):
        states = iter(["changing", "changing", "active"])
        scripted_peer(self.path, self.ack)
        with mock.patch.object(self.system, "_state", side_effect=lambda unit: next(states)):
            hold = self.system.mqtt_pause("/dev/COM1", self.path)
        self.assertIsNotNone(hold)
        hold.close()

    def test_wait_is_bounded_and_inactive_bridge_needs_no_pause(self):
        with mock.patch.object(self.system, "_state", return_value="active"):
            started = time.monotonic()
            with self.assertRaises(PortError) as caught:
                self.system.mqtt_pause("/dev/COM1", self.path)
        self.assertIn("not ready", str(caught.exception))
        self.assertLess(time.monotonic() - started, 3)
        with mock.patch.object(self.system, "_state", return_value="inactive"):
            self.assertIsNone(self.system.mqtt_pause("/dev/COM1", self.path))


class ReadyNotification(unittest.TestCase):
    def test_ready_reaches_the_notify_socket_and_absence_is_harmless(self):
        with tempfile.TemporaryDirectory() as name:
            path = os.path.join(name, "notify")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(path)
            sock.settimeout(2)
            try:
                with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
                    self.assertTrue(broker.sd_notify("READY=1"))
                self.assertEqual(sock.recv(64), b"READY=1")
                with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path + ".gone"}):
                    self.assertFalse(broker.sd_notify("READY=1"))
            finally:
                sock.close()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(broker.sd_notify("READY=1"))


if __name__ == "__main__":
    unittest.main()
