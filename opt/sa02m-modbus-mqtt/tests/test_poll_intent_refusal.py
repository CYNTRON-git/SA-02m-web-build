#!/usr/bin/env python3
"""A bridge restart cannot break a held lease: the poll intent is refused by the broker.

Why here and not only in the coordinator suite: `mqtt_config.cgi` may restart
`sa02m-modbus-mqtt.service` directly (sudoers.d/sa02m-mqtt), outside the
service-mutation guard. The UART guarantee therefore lives in the bridge's own
open path — `PollLock.acquire` asks the broker for `poll` intent and the broker
refuses while a lease on that UART exists — and this pins it where it lives,
against the real broker process, a real PTY and the shipped bridge pool.

Needs Linux root (the broker's lock inodes must be root-owned) and a PTY;
elsewhere the cases are reported as skipped, never as passed.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import socket
import sys
import time
import unittest

BRIDGE_DIR = Path(__file__).resolve().parents[1]
COORDINATOR_DIR = BRIDGE_DIR.parent / "sa02m-port-coordinator"
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(COORDINATOR_DIR))
sys.path.insert(0, str(COORDINATOR_DIR / "tests"))

LINUX_ROOT = sys.platform == "linux" and hasattr(os, "geteuid") and os.geteuid() == 0


@unittest.skipUnless(LINUX_ROOT, "real broker needs Linux root (root-owned lock inodes) and a PTY")
class PollIntentRefusal(unittest.TestCase):
    def setUp(self):
        import pty
        import tempfile
        import test_process_ownership as ownership  # the broker fixture (PTY ports, modelled systemd)
        from sa02m_ports.broker import atomic_json
        self.ownership = ownership
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        atomic_json(Path(self.root, "system.json"), {"active": True, "starts": 0, "stops": 0})
        master, slave = pty.openpty()
        self.master = master
        self.port = os.ttyname(slave)
        os.close(slave)
        self.path = self.root + "/run/control.sock"
        self.broker = ownership.CTX.Process(target=ownership.run_broker, args=(self.root, [self.port]))
        self.broker.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.can_connect():
            time.sleep(0.05)
        self.assertTrue(self.can_connect(), "broker did not come up")

    def can_connect(self):
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.connect(self.path)
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def tearDown(self):
        if self.broker.is_alive():
            self.broker.kill()
        self.broker.join(3)
        os.close(self.master)
        self.tmp.cleanup()

    def test_restarted_bridge_cannot_open_a_leased_uart_until_the_lease_ends(self):
        import bridge_serial
        from sa02m_ports import Lease, PollLock, PortError
        from sa02m_ports.broker import occupants
        bridge_serial._COM_HOLD_RE = re.compile(r"/dev/pts/[0-9]+\Z")
        bridge_serial._port_client = lambda: (
            lambda port: PollLock(port, self.path, self.root + "/run/locks"), Lease)
        handle = bridge_serial.get_port(self.port, 9600)
        try:
            # The flasher job (or any lease) is in progress...
            with Lease(self.port, "flasher", self.path):
                # ...and the bridge comes back (a restart outside the guard):
                # its first open of the leased line must be refused by intent,
                # before any UART descriptor exists.
                with self.assertRaises(PortError) as caught:
                    handle._ensure_open()
                self.assertIn("paused", str(caught.exception))
                self.assertNotIn(os.getpid(), occupants(self.port))
                self.assertIsNone(handle._ser)
            # Lease over: the same handle opens normally.
            handle._ensure_open()
            self.assertIn(os.getpid(), occupants(self.port))
            # And a lease taken while the bridge holds the line waits for it:
            # the bridge's pause ack is modelled absent here, so the owner sweep
            # refuses within its bound instead of killing anything.
            with self.assertRaises(PortError) as caught:
                with Lease(self.port, "flasher", self.path):
                    pass
            self.assertIn("owner", str(caught.exception))
            self.assertIn(os.getpid(), occupants(self.port))
        finally:
            handle.close()


if __name__ == "__main__":
    unittest.main()
