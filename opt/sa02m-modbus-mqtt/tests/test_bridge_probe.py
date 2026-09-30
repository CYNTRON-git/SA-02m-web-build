#!/usr/bin/env python3
"""bridge_probe — the panel's one-shot Modbus TCP reachability probe.

Honesty label: this proves OUR probe against the in-process fake MBAP server
on 127.0.0.1 — the verdict classification, the bounds (one request, read-only,
explicit close, never a retry), validate-before-anything, the single-flight
lock, the stdlib-only import, and byte parity of the request with
bridge_tcp.ModbusTcpClient. It proves nothing about any device's TCP stack.
Contract: docs/contracts/bridge-modbus-tcp.md §10.
"""
from __future__ import annotations

import errno
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge_probe  # noqa: E402
from mbap_fake import Bank, FakeMbapServer, free_port  # noqa: E402


def _run(srv, unit=1, **kw):
    kw.setdefault("refuse_self", False)
    return bridge_probe.run("127.0.0.1", srv.port, unit, **kw)


def _entry(**kw):
    base = {"type": "template", "template": "mp02-ahu", "transport": "tcp",
            "host": "192.0.2.10", "tcp_port": 502, "address": 1}
    base.update(kw)
    return base


class _ServerCase(unittest.TestCase):
    def setUp(self):
        self.bank = Bank(regs={("h", 0): 7})
        self.srv = FakeMbapServer(self.bank)
        self.addCleanup(self.srv.stop)


class TestVerdicts(_ServerCase):
    def test_device_ok_echoes_the_target(self):
        r = _run(self.srv)
        self.assertTrue(r["ok"])
        self.assertEqual(r["verdict"], "device_ok")
        self.assertIsNone(r["exception"])
        self.assertEqual((r["host"], r["tcp_port"], r["address"]),
                         ("127.0.0.1", self.srv.port, 1))
        self.assertIsInstance(r["elapsed_ms"], int)
        self.assertGreaterEqual(r["elapsed_ms"], 0)

    def test_an_exception_pdu_means_the_device_is_alive(self):
        self.bank.reject = {(3, 0, 1): 2}
        r = _run(self.srv)
        self.assertEqual((r["ok"], r["verdict"], r["exception"]), (True, "device_exception", 2))

    def test_unit_silent_is_bounded_by_the_timeout(self):
        self.srv.mute_units = {1}
        t0 = time.monotonic()
        r = _run(self.srv, timeout_s=1.0)
        spent = time.monotonic() - t0
        self.assertEqual(r["verdict"], "unit_silent")
        self.assertLess(spent, 1.5)

    def test_foreign_replies_are_not_modbus(self):
        for action in ("wrong_tid", "wrong_unit", "wrong_pid", "bad_len", "wrong_fc",
                       "truncated"):
            with self.subTest(action=action):
                self.srv.script = [action]
                self.assertEqual(_run(self.srv)["verdict"], "not_modbus", action)

    def test_an_http_server_is_not_modbus(self):
        """A web server on the typed port answers our 12 bytes with a text
        status line — pid/length garbage, never «отвечает»."""
        ls = socket.socket()
        ls.bind(("127.0.0.1", 0))
        ls.listen(1)
        self.addCleanup(ls.close)

        def serve():
            conn, _ = ls.accept()
            try:
                conn.recv(64)
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                time.sleep(0.2)
            finally:
                conn.close()

        threading.Thread(target=serve, daemon=True).start()
        r = bridge_probe.run("127.0.0.1", ls.getsockname()[1], 1, refuse_self=False)
        self.assertEqual(r["verdict"], "not_modbus")

    def test_accepted_then_reset_is_tcp_refused(self):
        self.srv.on_accept = "reset"
        self.assertEqual(_run(self.srv)["verdict"], "tcp_refused")

    def test_a_closed_port_is_tcp_refused(self):
        # 3 s, not the 1 s default: Windows retries the SYN after a loopback
        # RST and reports ECONNREFUSED only after ~2 s (measured 2.04 s), so
        # the default budget reads it as tcp_timeout there. Linux — the board
        # — refuses at once; the module is the same on both.
        r = bridge_probe.run("127.0.0.1", free_port(), 1, timeout_s=3.0, refuse_self=False)
        self.assertEqual((r["ok"], r["verdict"]), (True, "tcp_refused"))

    def test_connect_timeout_and_no_route(self):
        cases = ((socket.timeout("timed out"), "tcp_timeout"),
                 (OSError(errno.EHOSTUNREACH, "no route to host"), "tcp_unreachable"),
                 (OSError(errno.ENETUNREACH, "network unreachable"), "tcp_unreachable"))
        for exc, verdict in cases:
            with self.subTest(verdict=verdict):
                with mock.patch.object(bridge_probe.socket, "create_connection",
                                       side_effect=exc):
                    r = bridge_probe.run("192.0.2.10", 502, 1)
                self.assertEqual((r["ok"], r["verdict"]), (True, verdict))

    def test_the_board_itself_is_refused_after_connect(self):
        r = bridge_probe.run("127.0.0.1", self.srv.port, 1, refuse_self=True)
        self.assertEqual(r["verdict"], "self")
        time.sleep(0.05)
        self.assertEqual(self.srv.requests, [])      # refused BEFORE any request

    def test_a_segmented_reply_is_read_whole(self):
        self.srv.script = [("segmented", 0.15)]
        self.assertEqual(_run(self.srv)["verdict"], "device_ok")

    def test_an_unexpected_error_is_named_not_swallowed(self):
        with mock.patch.object(bridge_probe.socket, "create_connection",
                               side_effect=ValueError("weird")):
            with mock.patch.object(bridge_probe.sys, "stderr", new=mock.MagicMock()) as err:
                r = bridge_probe.run("192.0.2.10", 502, 1)
        self.assertEqual((r["ok"], r["error"], r["detail"]), (False, "probe_failed", "ValueError"))
        self.assertTrue(err.write.called)


class TestBounds(_ServerCase):
    def test_exactly_one_read_only_request_and_an_explicit_close(self):
        _run(self.srv)
        time.sleep(0.1)
        self.assertEqual(self.srv.fcs(), [3])
        self.assertEqual(self.srv.writes(), [])
        self.assertEqual(len(self.srv.requests), 1)
        self.assertEqual(self.srv.requests[0][3], bytes([3, 0, 0, 0, 1]))   # FC03 qty 1 @0
        self.assertEqual(self.srv.accepts, 1)
        # The device saw our FIN: its serve loop ended and closed its side —
        # an MP-02 holds 7 session slots and reaps idle ones only after 300 s.
        self.assertTrue(self.srv._conns)
        self.assertTrue(all(c.fileno() == -1 for c in self.srv._conns),
                        "the server side of the probe session is still open")

    def test_a_silent_unit_is_never_resent(self):
        self.srv.mute_units = {1}
        _run(self.srv, timeout_s=0.3)
        time.sleep(0.2)
        self.assertEqual(len(self.srv.requests), 1)
        self.assertEqual(self.srv.accepts, 1)

    def test_request_bytes_match_the_bridge_client(self):
        """Parity: the probe re-frames one MBAP request instead of importing
        bridge_tcp (which drags in pyserial); the bytes must be the bridge's."""
        if "serial" not in sys.modules:
            try:
                import serial  # noqa: F401
            except ImportError:
                sys.modules["serial"] = types.ModuleType("serial")
        import bridge_tcp
        _run(self.srv)
        c = bridge_tcp.ModbusTcpClient("127.0.0.1", self.srv.port, timeout=1.0,
                                       refuse_self=False)
        self.addCleanup(c.close)
        c.read_holding_registers(1, 0, 1)
        time.sleep(0.05)
        self.assertEqual(len(self.srv.requests), 2)
        self.assertEqual(self.srv.requests[0], self.srv.requests[1])   # (tid, unit, fc, pdu)
        # And the whole ADU, pid + length included (the fake does not record those).
        self.assertEqual(bridge_probe.build_adu(1, 1),
                         bytes.fromhex("0001" "0000" "0006" "01" "03" "0000" "0001"))


class TestProbeEntry(unittest.TestCase):
    """probe(entry, lock): validate → single-flight lock → run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock = os.path.join(self.tmp, "probe.lock")

    def test_refusals_use_the_bridge_codes_and_never_reach_the_network(self):
        cases = (({"host": "127.0.0.1"}, "host_forbidden"),
                 ({"host": "0177.0.0.1"}, "host_not_ipv4_literal"),
                 ({"address": 0}, "unit_invalid"),
                 ({"tcp_port": 70000}, "tcp_port_invalid"),
                 ({"type": "mr02m"}, "type_not_tcp_capable"),
                 ({"type": "carel"}, "carel_family_required"),
                 ({"transport": "rtu"}, "transport_unknown"),
                 ({"transport": "udp"}, "transport_unknown"))
        with mock.patch.object(bridge_probe, "run",
                               side_effect=AssertionError("network touched")):
            for kw, reason in cases:
                r = bridge_probe.probe(_entry(**kw), self.lock)
                self.assertEqual((r["ok"], r["error"], r["reason"]),
                                 (False, "invalid_device", reason), kw)
            r = bridge_probe.probe("not a dict", self.lock)
            self.assertEqual((r["ok"], r["error"]), (False, "invalid_device"))
        self.assertFalse(os.path.exists(self.lock))   # validation precedes the lock

    def test_a_valid_entry_runs_with_the_canonical_fields(self):
        with mock.patch.object(bridge_probe, "run",
                               return_value={"ok": True, "verdict": "device_ok"}) as run:
            r = bridge_probe.probe(_entry(tcp_port="502", address="7"), self.lock)
        run.assert_called_once_with("192.0.2.10", 502, 7)
        self.assertEqual(r["verdict"], "device_ok")
        self.assertFalse(os.path.exists(self.lock))   # released

    def test_a_second_probe_is_busy_while_the_lock_is_held(self):
        Path(self.lock).write_text("held\n")
        with mock.patch.object(bridge_probe, "run") as run:
            r = bridge_probe.probe(_entry(), self.lock)
        self.assertEqual((r["ok"], r["error"]), (False, "probe_busy"))
        run.assert_not_called()
        self.assertTrue(os.path.exists(self.lock))    # not ours to remove

    def test_a_stale_lock_is_broken(self):
        Path(self.lock).write_text("crashed\n")
        old = time.time() - 60
        os.utime(self.lock, (old, old))
        with mock.patch.object(bridge_probe, "run",
                               return_value={"ok": True, "verdict": "device_ok"}) as run:
            r = bridge_probe.probe(_entry(), self.lock)
        self.assertEqual(r["verdict"], "device_ok")
        run.assert_called_once()
        self.assertFalse(os.path.exists(self.lock))

    def test_the_lock_is_released_when_run_raises(self):
        with mock.patch.object(bridge_probe, "run", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                bridge_probe.probe(_entry(), self.lock)
        self.assertFalse(os.path.exists(self.lock))

    def test_no_lock_path_means_no_lock(self):
        with mock.patch.object(bridge_probe, "run",
                               return_value={"ok": True, "verdict": "device_ok"}):
            self.assertEqual(bridge_probe.probe(_entry(), None)["verdict"], "device_ok")


class TestStdlibOnly(unittest.TestCase):
    def test_imports_with_serial_paho_yaml_blocked(self):
        code = (
            "import sys\n"
            "class Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in ('serial', 'paho', 'yaml'):\n"
            "            raise ImportError('blocked ' + name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, Block())\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import bridge_probe\n"
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
            "('serial', 'paho', 'yaml', 'bridge_serial', 'bridge_tcp', 'bridge_device'))\n"
            "assert not bad, bad\n"
            "print('ok', bridge_probe.PROBE_FC, bridge_probe.PROBE_TIMEOUT_S)\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-c", code, str(BRIDGE_DIR)],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok 3 1.0")


if __name__ == "__main__":
    unittest.main()
