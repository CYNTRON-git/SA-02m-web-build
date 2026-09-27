#!/usr/bin/env python3
"""ModbusTcpClient against a real socket (the in-process fake MBAP server).

Honesty label: this proves OUR client — MBAP framing, the lifecycle rules
(timeout, stale-session resend, reconnect backoff, self refusal) and the
one-request-in-flight/write-priority discipline — over a real TCP stack on
127.0.0.1. It does not prove any device's TCP stack; the MP-02 and Carel
hardware evidence is recorded in docs/contracts/bridge-modbus-tcp.md.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if "serial" not in sys.modules:
    try:
        import serial  # noqa: F401
    except ImportError:
        sys.modules["serial"] = types.ModuleType("serial")

import bridge_tcp  # noqa: E402
from mbap_fake import Bank, FakeMbapServer, free_port  # noqa: E402


def _client(server, timeout=1.0, **kw):
    kw.setdefault("refuse_self", False)
    return bridge_tcp.ModbusTcpClient("127.0.0.1", server.port, timeout=timeout, **kw)


class _ServerCase(unittest.TestCase):
    def setUp(self):
        self.bank = Bank(regs={("h", 100): 225, ("h", 101): 7, ("i", 4000): 0xFF9C,
                               ("i", 4001): 314},
                         coils={2: 1, 16: 0, 17: 1},
                         discretes={0: 1, 9: 1},
                         fc17=b"\x01\xffCRSTDrAHAQ")
        self.srv = FakeMbapServer(self.bank)
        self.addCleanup(self.srv.stop)
        self.c = _client(self.srv)
        self.addCleanup(self.c.close)


class TestRoundTrip(_ServerCase):
    def test_fc01_fc02_bits(self):
        self.assertEqual(self.c.read_coils(1, 15, 3), [0, 0, 1])
        self.assertEqual(self.c.read_coils(1, 0, 18)[16:], [0, 1])
        self.assertEqual(self.c.read_discrete_inputs(1, 0, 10),
                         [1, 0, 0, 0, 0, 0, 0, 0, 0, 1])

    def test_fc03_fc04_words(self):
        self.assertEqual(self.c.read_holding_registers(1, 100, 2), [225, 7])
        self.assertEqual(self.c.read_input_registers(1, 4000, 2), [0xFF9C, 314])

    def test_fc05_fc06_fc16_land_in_the_device(self):
        self.c.write_coil(1, 16, True)
        self.c.write_register(1, 190, 225)
        self.c.write_registers(1, 30, [0x41BC, 0x0000])
        self.assertEqual(self.bank.coils[16], 1)
        self.assertEqual(self.bank.regs[("h", 190)], 225)
        self.assertEqual((self.bank.regs[("h", 30)], self.bank.regs[("h", 31)]),
                         (0x41BC, 0))
        # FC16 is ONE transaction at the device, never two FC06.
        self.assertEqual(self.srv.fcs(), [5, 6, 16])

    def test_fc17(self):
        self.assertEqual(self.c.report_slave_id(1), b"\x01\xffCRSTDrAHAQ")

    def test_one_socket_for_many_calls_and_units(self):
        for unit in (1, 2, 3, 1):
            self.c.read_holding_registers(unit, 100, 1)
        self.assertEqual(self.srv.accepts, 1)
        self.assertEqual([r[1] for r in self.srv.requests], [1, 2, 3, 1])

    def test_mbap_header_on_the_wire(self):
        self.c.read_holding_registers(7, 100, 1)
        self.c.read_holding_registers(7, 100, 1)
        tids = [r[0] for r in self.srv.requests]
        self.assertEqual(tids[1], (tids[0] + 1) & 0xFFFF)
        self.assertEqual(self.srv.requests[0][3], bytes([3, 0, 100, 0, 1]))

    def test_tid_wraps(self):
        self.c._tid = 0xFFFF
        self.c.read_holding_registers(1, 100, 1)
        self.assertEqual(self.srv.requests[0][0], 0)


class TestErrors(_ServerCase):
    def test_exception_reply_is_an_ioerror_like_rtu(self):
        self.srv.script = [("exception", 2)]
        with self.assertRaisesRegex(IOError, r"Modbus exception 2 on FC03"):
            self.c.read_holding_registers(1, 100, 1)
        # An exception is an ANSWER: the session stays up.
        self.c.read_holding_registers(1, 100, 1)
        self.assertEqual(self.srv.accepts, 1)

    def test_timeout_is_bounded_closes_and_is_never_resent(self):
        c = _client(self.srv, timeout=0.3)
        self.srv.script = ["silent"]
        t0 = time.monotonic()
        with self.assertRaisesRegex(IOError, "timeout"):
            c.read_holding_registers(1, 100, 1)
        self.assertLess(time.monotonic() - t0, 0.3 + 0.3)
        self.assertIsNone(c._sock)
        time.sleep(0.05)
        # A write may already have been applied: exactly ONE request seen.
        self.assertEqual(len(self.srv.requests), 1)
        self.assertEqual(c.stats.timeouts, 1)
        # The next call reconnects.
        self.assertEqual(c.read_holding_registers(1, 100, 1), [225])
        self.assertEqual(self.srv.accepts, 2)
        c.close()

    def test_a_write_that_times_out_is_not_resent(self):
        c = _client(self.srv, timeout=0.3)
        self.srv.script = ["silent"]
        with self.assertRaises(IOError):
            c.write_register(1, 190, 225)
        time.sleep(0.4)
        self.assertEqual(self.srv.fcs(), [6])
        c.close()

    def test_foreign_replies_raise_and_reconnect(self):
        self.c.read_holding_registers(1, 100, 1)
        for action in ("wrong_tid", "wrong_pid", "bad_len", "wrong_unit", "wrong_fc"):
            with self.subTest(action=action):
                before = self.srv.accepts
                self.srv.script = [action]
                with self.assertRaises(IOError) as cm:
                    self.c.read_holding_registers(1, 100, 1)
                self.assertIsNone(self.c._sock, action)
                self.assertEqual(self.c.read_holding_registers(1, 100, 1), [225])
                self.assertEqual(self.srv.accepts, before + 1, action)
                if action == "wrong_tid":
                    self.assertIn("tid=", str(cm.exception))

    def test_reply_cut_mid_frame_is_an_error_not_a_resend(self):
        self.c.read_holding_registers(1, 100, 1)       # a reused session next
        self.srv.script = ["truncated"]
        n = len(self.srv.requests)
        with self.assertRaises(IOError):
            self.c.read_holding_registers(1, 100, 1)
        time.sleep(0.05)
        self.assertEqual(len(self.srv.requests), n + 1)

    def test_segmented_reply_with_a_pause_is_read_whole(self):
        self.srv.script = [("segmented", 0.15)]
        self.assertEqual(self.c.read_input_registers(1, 4000, 2), [0xFF9C, 314])


class TestStaleSession(_ServerCase):
    def test_idle_close_gets_exactly_one_transparent_resend(self):
        self.assertEqual(self.c.read_holding_registers(1, 100, 1), [225])
        self.srv.drop_sessions()          # MP-02 idle close / reboot
        time.sleep(0.1)
        self.assertEqual(self.c.read_holding_registers(1, 100, 1), [225])
        self.assertEqual(self.srv.accepts, 2)
        self.assertEqual(self.c.backoff_s, 0.0)

    def test_a_fresh_connection_is_never_resent(self):
        self.srv.on_accept = "reset"
        with self.assertRaises(IOError):
            self.c.read_holding_registers(1, 100, 1)
        time.sleep(0.1)
        self.assertEqual(self.srv.accepts, 1)


class TestBackoff(unittest.TestCase):
    def test_refused_then_no_connect_attempt_inside_the_window(self):
        c = bridge_tcp.ModbusTcpClient("127.0.0.1", free_port(), timeout=0.5,
                                       refuse_self=False)
        real = socket.create_connection
        calls = []

        def counting(*a, **kw):
            calls.append(a)
            return real(*a, **kw)

        with mock.patch.object(bridge_tcp.socket, "create_connection", counting):
            with self.assertRaisesRegex(IOError, "connect failed"):
                c.read_holding_registers(1, 0, 1)
            t0 = time.monotonic()
            for _ in range(23):   # a template's whole channel pass
                with self.assertRaisesRegex(IOError, "reconnect in"):
                    c.read_holding_registers(1, 0, 1)
            self.assertLess(time.monotonic() - t0, 0.2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(c.backoff_s, 1.0)

    def test_accept_then_reset_enters_the_window(self):
        srv = FakeMbapServer()
        self.addCleanup(srv.stop)
        srv.on_accept = "reset"
        c = _client(srv, timeout=0.5)
        t_end = time.monotonic() + 2.8
        calls = 0
        while time.monotonic() < t_end:
            calls += 1
            with self.assertRaises(IOError):
                c.read_holding_registers(1, 0, 1)
            time.sleep(0.02)
        self.assertGreater(calls, 20)
        self.assertLessEqual(srv.accepts, 2)
        self.assertGreaterEqual(srv.accepts, 1)

    def test_window_grows_and_resets_after_a_success(self):
        port = free_port()
        c = bridge_tcp.ModbusTcpClient("127.0.0.1", port, timeout=0.5, refuse_self=False)
        steps = []
        for _ in range(6):
            c._retry_at = 0.0     # skip the wait, keep the streak
            with self.assertRaises(IOError):
                c.read_holding_registers(1, 0, 1)
            steps.append(c.backoff_s)
        self.assertEqual(steps, [1.0, 2.0, 4.0, 8.0, 10.0, 10.0])
        srv = FakeMbapServer.__new__(FakeMbapServer)
        FakeMbapServer.__init__(srv)
        self.addCleanup(srv.stop)
        c2 = bridge_tcp.ModbusTcpClient("127.0.0.1", srv.port, timeout=0.5,
                                        refuse_self=False)
        c2._fail_n, c2.backoff_s = 4, 10.0     # a long streak…
        c2.read_holding_registers(1, 0, 1)     # …then one success
        self.assertEqual((c2._fail_n, c2.backoff_s), (0, 0.0))
        srv.stop()
        c2.close()
        with self.assertRaises(IOError):
            c2.read_holding_registers(1, 0, 1)
        self.assertEqual(c2.backoff_s, 1.0)


class TestSelfRefusal(unittest.TestCase):
    def test_a_connection_to_this_board_is_refused(self):
        srv = FakeMbapServer()
        self.addCleanup(srv.stop)
        c = bridge_tcp.ModbusTcpClient("127.0.0.1", srv.port, timeout=0.5,
                                       refuse_self=True)
        with self.assertLogs(c._log.name, level="ERROR") as logs:
            with self.assertRaisesRegex(IOError, "target is this board"):
                c.read_holding_registers(1, 0, 1)
        self.assertEqual(len([ln for ln in logs.output if "ERROR" in ln]), 1)
        time.sleep(0.05)
        self.assertEqual(srv.requests, [])
        self.assertIsNone(c._sock)
        # Once per endpoint: the next refusal is not another ERROR.
        c._retry_at = 0.0
        with self.assertLogs(c._log.name, level="DEBUG") as logs2:
            with self.assertRaises(IOError):
                c.read_holding_registers(1, 0, 1)
        self.assertFalse([ln for ln in logs2.output if ln.startswith("ERROR")])


class TestReportSlaveId(unittest.TestCase):
    def test_empty_on_exception_and_on_a_dead_endpoint(self):
        srv = FakeMbapServer(Bank())          # no fc17 → exception 01
        self.addCleanup(srv.stop)
        c = _client(srv)
        self.assertEqual(c.report_slave_id(1), b"")
        dead = bridge_tcp.ModbusTcpClient("127.0.0.1", free_port(), timeout=0.3,
                                          refuse_self=False)
        self.assertEqual(dead.report_slave_id(1), b"")
        c.close()

    def test_per_call_timeout_is_honoured(self):
        srv = FakeMbapServer(Bank(fc17=b"ID"))
        self.addCleanup(srv.stop)
        c = _client(srv, timeout=0.2)
        srv.script = [("delay", 0.5)]
        self.assertEqual(c.report_slave_id(1, timeout=0.7), b"ID")
        srv.script = [("delay", 0.5)]
        with self.assertRaises(IOError):
            c.read_holding_registers(1, 0, 1)   # the client timeout (0.2) applies
        c.close()


class TestConcurrency(unittest.TestCase):
    def test_two_threads_never_have_two_requests_in_flight(self):
        srv = FakeMbapServer(Bank(regs={("h", 0): 1}), reply_delay_s=0.01)
        self.addCleanup(srv.stop)
        c = _client(srv)
        errors = []

        def worker():
            try:
                for _ in range(15):
                    c.read_holding_registers(1, 0, 1)
                    c.write_register(1, 5, 1)
            except Exception as e:   # pragma: no cover - reported below
                errors.append(e)

        ts = [threading.Thread(target=worker) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(len(srv.requests), 60)
        self.assertEqual(srv.pipelined, 0)
        self.assertEqual(srv.max_outstanding, 1)
        self.assertEqual(srv.accepts, 1)
        c.close()

    def test_a_waiting_write_overtakes_the_poll(self):
        """A poll read does not even queue for the lock while a write waits.

        Deterministic on purpose: letting two threads race for a released
        lock proves nothing on a host whose scheduler happens to hand the
        lock to the waiter (measured: the racing form stayed GREEN on this
        Windows box with the priority gate disabled). Here the lock is held
        by the test, the write queues, then a poll read starts — with the
        gate it waits OUTSIDE the lock; without it, it queues beside the
        write and the order is left to the scheduler.
        """
        srv = FakeMbapServer(Bank(regs={("h", 0): 1}))
        self.addCleanup(srv.stop)
        c = _client(srv)
        real = c._lock
        attempts = []

        class Recording:
            def __enter__(self):
                attempts.append(threading.current_thread().name)
                return real.__enter__()

            def __exit__(self, *exc):
                return real.__exit__(*exc)

        c._lock = Recording()
        real.acquire()                     # a poll read is "in flight"
        try:
            w = threading.Thread(target=c.write_register, args=(1, 190, 225),
                                 name="W", daemon=True)
            w.start()
            deadline = time.monotonic() + 2
            while c._prio.waiting == 0 and time.monotonic() < deadline:
                time.sleep(0.001)
            r = threading.Thread(target=c.read_holding_registers, args=(1, 0, 1),
                                 name="R", daemon=True)
            r.start()
            time.sleep(0.1)
            seen = list(attempts)
        finally:
            real.release()
        self.assertEqual(seen, ["W"])      # the poll yielded, it did not queue
        w.join(5)
        r.join(5)
        self.assertEqual(srv.fcs(), [6, 3])
        c.close()


class TestSilentUnit(unittest.TestCase):
    """A peer that accepts the connection but never answers one unit id.

    Review 1.0.6.56 A1: every timeout drops the socket, the reconnect
    succeeds (so no connect backoff applied), and each reconnect logged INFO
    «connected» — a 23-channel template pass wrote 23 INFO lines to the
    persistent journal and cost 23 timeouts. Bounded now: «connected» is
    INFO only on the first connect or after a connect failure, and a unit
    that times out MUTE_AFTER_TIMEOUTS times in a row fast-fails inside its
    own 1-2-4-8-10 s window — its OWN, so other units behind the same
    gateway keep being polled.
    """

    PASSES = 3
    CHANNELS = 23

    def _server(self):
        srv = FakeMbapServer(Bank(regs={("h", 0): 7}))
        self.addCleanup(srv.stop)
        srv.mute_units = {1}
        return srv

    def _pass(self, c, unit=1):
        for _ in range(self.CHANNELS):
            try:
                c.read_holding_registers(unit, 0, 1)
            except IOError:
                pass

    def test_journal_stays_bounded_over_many_passes(self):
        srv = self._server()
        c = _client(srv, timeout=0.2)
        self.addCleanup(c.close)
        with self.assertLogs(c._log.name, level="DEBUG") as logs:
            t0 = time.monotonic()
            for _ in range(self.PASSES):
                self._pass(c)
            spent = time.monotonic() - t0
        info = [ln for ln in logs.output if ln.startswith(("INFO", "WARNING", "ERROR"))]
        # One «connected», one WARN for the silent unit — not one per channel.
        self.assertLessEqual(len(info), 2, info)
        self.assertEqual(len([ln for ln in info if "connected" in ln]), 1, info)
        # Three timeouts, then the unit's window: far fewer requests (and far
        # less time) than PASSES x CHANNELS timeouts.
        self.assertLessEqual(len(srv.requests), 3 + 2, len(srv.requests))
        self.assertLess(spent, 0.2 * 6 + 1.0)

    def test_another_unit_on_the_same_endpoint_is_not_starved(self):
        srv = self._server()
        c = _client(srv, timeout=0.2)
        self.addCleanup(c.close)
        self._pass(c, unit=1)                        # unit 1 enters its window
        self.assertEqual(c.read_holding_registers(2, 0, 1), [7])
        self.assertEqual(c.backoff_s, 0.0)           # no endpoint-wide window

    def test_the_unit_recovers_after_its_window(self):
        srv = self._server()
        c = _client(srv, timeout=0.2)
        self.addCleanup(c.close)
        self._pass(c, unit=1)
        srv.mute_units = set()
        with self.assertRaisesRegex(IOError, "not answering"):
            c.read_holding_registers(1, 0, 1)        # inside the window: no I/O
        left = c._unit_retry_left(1)
        self.assertGreater(left, 0)
        self.assertLessEqual(left, bridge_tcp.BACKOFF_STEPS_S[-1])
        time.sleep(left + 0.05)
        with self.assertLogs(c._log.name, level="INFO") as logs:
            self.assertEqual(c.read_holding_registers(1, 0, 1), [7])
        self.assertTrue(any("unit 1 answering again" in ln for ln in logs.output),
                        logs.output)
        self.assertEqual(c._unit_retry_left(1), 0.0)

    def test_an_answer_clears_the_units_silence_state(self):
        """Review round 2, N1: the recovery must FORGET the silent spell.

        Mutation M4 (`self._units.pop(unit, None)` -> `.get(unit)`) survived
        every case above: the recovery INFO still fired once. What it breaks
        is the aftermath — a remembered streak re-announces every answer and
        puts the unit straight back into its window on the next single
        timeout, as if it had never answered.
        """
        srv = self._server()
        c = _client(srv, timeout=0.2)
        self.addCleanup(c.close)
        self._pass(c, unit=1)                        # 3 timeouts -> window
        srv.mute_units = set()
        time.sleep(c._unit_retry_left(1) + 0.05)
        self.assertEqual(c.read_holding_registers(1, 0, 1), [7])   # recovery
        with self.assertNoLogs(c._log.name, level="INFO"):
            self.assertEqual(c.read_holding_registers(1, 0, 1), [7])
        srv.mute_units = {1}
        with self.assertRaisesRegex(IOError, "timeout"):
            c.read_holding_registers(1, 0, 1)        # ONE fresh timeout
        self.assertEqual(c._unit_retry_left(1), 0.0)
        srv.mute_units = set()
        self.assertEqual(c.read_holding_registers(1, 0, 1), [7])  # not windowed

    def test_a_timeout_is_still_never_resent(self):
        srv = self._server()
        c = _client(srv, timeout=0.2)
        self.addCleanup(c.close)
        with self.assertRaises(IOError):
            c.write_register(1, 190, 225)
        time.sleep(0.3)
        self.assertEqual(srv.fcs(), [6])


class TestPool(unittest.TestCase):
    def test_one_client_per_endpoint(self):
        with mock.patch.dict(bridge_tcp._tcp_pool, clear=True):
            a = bridge_tcp.get_tcp_client("192.0.2.10", 502, 1.0)
            b = bridge_tcp.get_tcp_client("192.0.2.10", 502, 2.0)
            c = bridge_tcp.get_tcp_client("192.0.2.10", 503, 1.0)
        self.assertIs(a, b)
        self.assertIsNot(a, c)
        self.assertTrue(a._refuse_self)


class TestStats(unittest.TestCase):
    def test_suffix_reports_deltas(self):
        s = bridge_tcp.TcpLineStats()
        s.reconnects, s.timeouts = 2, 3
        self.assertEqual(s.suffix(), " tcp reconnects=+2 timeouts=+3")
        s.timeouts = 4
        self.assertEqual(s.suffix(), " tcp reconnects=+0 timeouts=+1")


if __name__ == "__main__":
    unittest.main()
