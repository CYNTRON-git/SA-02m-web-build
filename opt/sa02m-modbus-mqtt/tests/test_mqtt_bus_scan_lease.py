#!/usr/bin/env python3
"""The scan pause follows the UART the caller named.

A hardcoded `/dev/COM3` fails these cases: COM1 and COM5 are the arguments,
and the lease is released from the scan's finally (success, empty line,
serial error, port already gone). A gateway scan names host:tcp_port and
does not pause a local COM.
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

import bridge_scan_lease as lease  # noqa: E402
import mqtt_bus_scan as scan  # noqa: E402


class _LeaseSock:
    def __init__(self):
        self.sent = b""
        self.closed = False
        self._reply = b'{"ok":true,"held":true}\n'

    def settimeout(self, _timeout):
        return None

    def connect(self, _path):
        return None

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        chunk = self._reply[:n]
        self._reply = self._reply[n:]
        return chunk

    def close(self):
        self.closed = True


class _RecordingLease:
    """Records the spec main() actually releases. GC of the generator does not."""

    def __init__(self, spec, inner, exits):
        self.spec = spec
        self.inner = inner
        self.exits = exits

    def __enter__(self):
        return self.inner.__enter__()

    def __exit__(self, exc_type, exc, tb):
        self.exits.append(dict(self.spec))
        return self.inner.__exit__(exc_type, exc, tb)


class _Flag:
    def __init__(self):
        self.on = False

    def set(self):
        self.on = True

    def clear(self):
        self.on = False


class _Poller:
    def __init__(self, bus):
        self.bus = bus
        self.released = 0

    def release_line(self):
        self.released += 1


class _Sched:
    def __init__(self, bus):
        self._pollers = [_Poller(bus)]
        self._scan_hold = _Flag()


class _Bus:
    def __init__(self, port=None, transport="rtu", host=None, tcp_port=None):
        self.port = port
        self.transport = transport
        self.host = host
        self.tcp_port = tcp_port


class _Conn:
    def __init__(self, line: bytes):
        self._reads = [line, b""]
        self.sent = b""
        self.closed = False

    def settimeout(self, _timeout):
        return None

    def recv(self, _n):
        if not self._reads:
            return b""
        return self._reads.pop(0)

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True


class TestScanLeaseFollowsPort(unittest.TestCase):
    def setUp(self):
        self._n = 0
        os.makedirs("/tmp", exist_ok=True)
        # CPython on Windows has no AF_UNIX. The board is Linux; the test
        # only needs the name so poll_lease can build its socket call.
        if not hasattr(scan.socket, "AF_UNIX"):
            scan.socket.AF_UNIX = 1
            self.addCleanup(lambda: delattr(scan.socket, "AF_UNIX"))

    def _token(self) -> str:
        self._n += 1
        return "L%05d" % self._n

    def _params(self, payload: dict) -> str:
        path = "/tmp/sa02m-mqttscan." + self._token()
        Path(path).write_text(json.dumps(payload), encoding="utf-8")
        self.addCleanup(lambda p=path: os.path.isfile(p) and os.remove(p))
        return path

    def _run(self, payload, *, serial=None, port_exists=True, uart_free=True,
             gateway_result=None):
        path = self._params(payload)
        socks = []
        exits = []
        real_lease = scan.poll_lease

        def factory(*_args, **_kwargs):
            sock = _LeaseSock()
            socks.append(sock)
            return sock

        def wrapped(spec):
            return _RecordingLease(spec, real_lease(spec), exits)

        out = io.StringIO()
        serial_mock = mock.Mock(side_effect=serial) if serial else mock.Mock(
            return_value=mock.MagicMock())

        def _exists(self_path):
            if port_exists:
                return True
            return not str(self_path).replace("\\", "/").startswith("/dev/")
        patches = [
            mock.patch.object(scan.socket, "socket", factory),
            mock.patch.object(scan, "poll_lease", wrapped),
            mock.patch.object(scan, "_lease_trace"),
            mock.patch.object(scan, "_wait_uart_free", return_value=uart_free),
            mock.patch.object(scan.time, "sleep"),
            mock.patch.object(scan.Path, "exists", _exists),
            mock.patch.object(scan.serial, "Serial", serial_mock),
            mock.patch.object(
                scan, "_fast_then_standard", return_value=([], "fast", set())),
            mock.patch.object(
                scan, "_mtd_stop2_pass", side_effect=AssertionError("8n2")),
            mock.patch.object(scan.sys, "argv", ["mqtt_bus_scan.py", path]),
            mock.patch("sys.stdout", out),
        ]
        if gateway_result is not None:
            patches.append(mock.patch.object(
                scan, "_scan_gateway", return_value=gateway_result))
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            scan.main()
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        return socks, exits, lines, serial_mock

    def test_com1_and_com5_are_the_lease_argument_and_release_is_in_finally(self):
        for port in ("/dev/COM1", "/dev/COM5"):
            for outcome in ("ok", "serial", "missing", "busy"):
                with self.subTest(port=port, outcome=outcome):
                    kwargs = {}
                    if outcome == "serial":
                        kwargs["serial"] = scan.serial.SerialException("busy")
                    elif outcome == "missing":
                        kwargs["port_exists"] = False
                    elif outcome == "busy":
                        kwargs["uart_free"] = False
                    socks, exits, lines, serial_mock = self._run(
                        {"via": "com", "port": port, "baudrate": 9600,
                         "max_addr": 1, "phase": "fast"},
                        **kwargs)
                    self.assertEqual(len(socks), 1, lines)
                    sent = json.loads(socks[0].sent.decode())
                    self.assertEqual(sent, {"via": "com", "port": port})
                    self.assertTrue(socks[0].closed)
                    self.assertEqual(exits, [sent])
                    self.assertTrue(lines)
                    if outcome == "ok":
                        self.assertTrue(lines[-1]["ok"], lines[-1])
                        self.assertEqual(lines[-1]["port"], port)
                        serial_mock.assert_called_once()
                        self.assertEqual(serial_mock.call_args.args[0], port)
                    elif outcome == "serial":
                        self.assertFalse(lines[-1]["ok"], lines[-1])
                        serial_mock.assert_called_once()
                        self.assertEqual(serial_mock.call_args.args[0], port)
                    else:
                        self.assertFalse(lines[-1]["ok"], lines[-1])
                        serial_mock.assert_not_called()

    def test_port_outside_the_allow_list_opens_no_lease(self):
        for port in ("/dev/COM6", "/dev/COM0", "/dev/ttyS4", "COM3",
                     "/dev/COM3/../COM1"):
            with self.subTest(port=port):
                path = self._params(
                    {"via": "com", "port": port, "baudrate": 9600, "max_addr": 1})
                exits = []

                def wrapped(spec, _exits=exits):
                    _exits.append(spec)
                    raise AssertionError("lease opened for " + port)

                out = io.StringIO()
                with mock.patch.object(scan, "poll_lease", wrapped), \
                        mock.patch.object(scan.sys, "argv", ["mqtt_bus_scan.py", path]), \
                        mock.patch("sys.stdout", out):
                    with self.assertRaises(SystemExit) as raised:
                        scan.main()
                self.assertEqual(raised.exception.code, 0)
                self.assertEqual(exits, [])
                body = json.loads(out.getvalue())
                self.assertFalse(body["ok"])
                self.assertEqual(body["devices"], [])

    def test_gateway_lease_names_the_endpoint_and_does_not_open_a_com(self):
        socks, exits, lines, serial_mock = self._run(
            {"via": "gateway", "host": "192.168.1.10", "tcp_port": 4001,
             "max_addr": 1, "phase": "fast"},
            gateway_result={
                "ok": True, "devices": [], "scan_method": "fast", "phase": "fast",
            })
        self.assertEqual(len(socks), 1, lines)
        sent = json.loads(socks[0].sent.decode())
        self.assertEqual(sent, {
            "via": "gateway", "host": "192.168.1.10", "tcp_port": 4001})
        self.assertNotIn("COM", socks[0].sent.decode())
        self.assertTrue(socks[0].closed)
        self.assertEqual(exits, [sent])
        serial_mock.assert_not_called()
        self.assertTrue(lines[-1]["ok"], lines[-1])
        self.assertEqual(lines[-1].get("port"), None)


class TestLeaseServerPausesOneLine(unittest.TestCase):
    def setUp(self):
        lease._holds.clear()

    def tearDown(self):
        lease._holds.clear()

    def _schedulers(self):
        return {
            "/dev/COM1": _Sched(_Bus(port="/dev/COM1")),
            "/dev/COM4": _Sched(_Bus(port="/dev/COM4")),
            "/dev/COM5": _Sched(_Bus(port="/dev/COM5")),
        }

    def test_stop_and_start_use_com1_and_com5_not_the_other_uarts(self):
        for port in ("/dev/COM1", "/dev/COM5"):
            with self.subTest(port=port):
                lease._holds.clear()
                scheds = self._schedulers()
                order = []
                traces = []
                during = {}

                def hold(asked):
                    order.append(("hold", asked))
                    return True

                def release(asked):
                    order.append(("release", asked))

                def on_trace(msg):
                    traces.append(msg)
                    if msg.startswith("stop"):
                        during["asked"] = scheds[port]._scan_hold.on
                        during["com4"] = scheds["/dev/COM4"]._scan_hold.on
                        during["other"] = scheds[
                            "/dev/COM5" if port == "/dev/COM1" else "/dev/COM1"
                        ]._scan_hold.on

                conn = _Conn((json.dumps({"via": "com", "port": port}) + "\n").encode())
                with mock.patch.object(lease.bridge_serial, "hold_com", side_effect=hold), \
                        mock.patch.object(lease.bridge_serial, "release_com", side_effect=release), \
                        mock.patch.object(lease, "_live_schedulers",
                                          return_value=list(scheds.values())), \
                        mock.patch.object(lease, "trace", side_effect=on_trace):
                    lease.handle(conn)
                self.assertEqual(order, [("hold", port), ("release", port)])
                self.assertEqual(traces, [
                    "stop %s held=1" % port,
                    "start %s" % port,
                ])
                self.assertTrue(during["asked"])
                self.assertFalse(during["com4"])
                self.assertFalse(during["other"])
                self.assertFalse(scheds[port]._scan_hold.on)
                self.assertEqual(scheds[port]._pollers[0].released, 1)
                self.assertEqual(scheds["/dev/COM4"]._pollers[0].released, 0)
                self.assertEqual(lease._holds, {})
                reply = json.loads(conn.sent.decode())
                self.assertTrue(reply["ok"])
                self.assertEqual(reply["via"], "com")
                self.assertTrue(conn.closed)

    def test_allow_list_is_com1_through_com5(self):
        for n in range(1, 6):
            port = "/dev/COM%d" % n
            self.assertEqual(
                lease.allow({"via": "com", "port": port}),
                {"via": "com", "port": port})
        for port in ("/dev/COM0", "/dev/COM6", "/dev/ttyS4", "COM3",
                     "/dev/COM1 ", "/dev/COM4/../COM3"):
            self.assertIsNone(lease.allow({"via": "com", "port": port}), port)

    def test_gateway_pauses_that_endpoint_and_not_a_local_com(self):
        com4 = _Sched(_Bus(port="/dev/COM4"))
        gw = _Sched(_Bus(transport="rtu_tcp", host="192.168.1.10", tcp_port=4001))
        during = {}

        def on_trace(msg):
            if msg.startswith("stop"):
                during["com4"] = com4._scan_hold.on
                during["gw"] = gw._scan_hold.on

        body = {"via": "gateway", "host": "192.168.1.10", "tcp_port": 4001,
                "port": "/dev/COM4"}
        conn = _Conn((json.dumps(body) + "\n").encode())
        with mock.patch.object(lease.bridge_serial, "hold_com") as hold_com, \
                mock.patch.object(lease.bridge_serial, "release_com") as release_com, \
                mock.patch.object(lease.bridge_rtu_tcp, "hold_endpoint",
                                  return_value=True) as hold_ep, \
                mock.patch.object(lease.bridge_rtu_tcp, "release_endpoint") as release_ep, \
                mock.patch("bridge_spodes.hold_gateway", return_value=False) as hold_gw, \
                mock.patch.object(lease, "_live_schedulers", return_value=[com4, gw]), \
                mock.patch.object(lease, "trace", side_effect=on_trace):
            lease.handle(conn)
        hold_com.assert_not_called()
        release_com.assert_not_called()
        hold_ep.assert_called_once_with("192.168.1.10", 4001)
        hold_gw.assert_called_once_with("192.168.1.10", 4001)
        release_ep.assert_called_once_with("192.168.1.10", 4001)
        self.assertFalse(during["com4"])
        self.assertTrue(during["gw"])
        self.assertFalse(gw._scan_hold.on)
        self.assertEqual(com4._pollers[0].released, 0)
        self.assertEqual(gw._pollers[0].released, 1)
        self.assertEqual(lease._holds, {})

    def test_rejected_request_pauses_nothing(self):
        conn = _Conn(b'{"via":"com","port":"/dev/COM6"}\n')
        with mock.patch.object(lease.bridge_serial, "hold_com") as hold_com, \
                mock.patch.object(lease, "trace") as trace:
            lease.handle(conn)
        hold_com.assert_not_called()
        trace.assert_not_called()
        reply = json.loads(conn.sent.decode())
        self.assertFalse(reply["ok"])
        self.assertEqual(lease._holds, {})


if __name__ == "__main__":
    unittest.main()
