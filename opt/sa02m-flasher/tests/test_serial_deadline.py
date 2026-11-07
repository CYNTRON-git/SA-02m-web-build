"""Budgeted configuration UART: one write, bounded drain and ownership guard."""
import contextlib
import os
import select
import threading
import time
import unittest
from unittest import mock

import serial
from sa02m_flasher import modbus_io, modbus_rtu as rtu, serial_port

REQUEST = rtu.build_read_holding_registers(2, 10, 1)
_BODY = bytes.fromhex("020302007b")
RESPONSE = _BODY + rtu.crc16_modbus(_BODY).to_bytes(2, "little")


class Clock:
    def __init__(self):
        self.value = 10.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Port:
    bytesize, stopbits, baudrate, parity = 8, 2, 19200, "N"

    def __init__(self, clock):
        self.clock = clock
        self.timeout, self.write_timeout = .05, 2.0
        self.is_open = True
        self.writes, self.cleared, self.closed = [], [], []
        self.reads, self.flushes = 0, 0
        self.chunks = [RESPONSE[:2], RESPONSE[2:]]
        self.write_result = len(REQUEST)
        self.write_blocks = False
        self.read_blocks = False
        self.empty_reads = 0
        self.queue_stuck = False
        self.unsupported_queue = False
        self.initial_queue = 0
        self.seen_write_timeout = None
        self.seen_read_timeout = None

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        self.cleared.append(self.clock.now())
        self.queue_stuck = False

    @property
    def out_waiting(self):
        if self.unsupported_queue:
            raise NotImplementedError("no output queue probe")
        if not self.writes:
            return self.initial_queue
        return len(REQUEST) if self.queue_stuck else 0

    @property
    def in_waiting(self):
        return 1 if self.read_blocks else (len(self.chunks[0]) if self.chunks else 0)

    def write(self, data):
        self.writes.append(bytes(data))
        self.seen_write_timeout = self.write_timeout
        if self.write_blocks:
            self.clock.sleep(self.write_timeout)
            raise serial.SerialTimeoutException("write timeout")
        return self.write_result

    def flush(self):
        self.flushes += 1

    def read(self, count):
        self.reads += 1
        self.seen_read_timeout = self.timeout
        if self.read_blocks:
            self.clock.sleep(self.timeout)
            return b""
        if self.empty_reads:
            self.empty_reads -= 1
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > count:
            self.chunks.insert(0, chunk[count:])
        return chunk[:count]

    def close(self):
        self.is_open = False
        self.closed.append(self.clock.now())


def guard_time(port, request=REQUEST):
    char = (1 + port.bytesize + port.stopbits + (port.parity != "N")) / port.baudrate
    return len(request) * char + max(.001, min(.02, char * 3.5 + .002))


class SerialDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.port = Port(self.clock)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(serial_port.time, "monotonic", self.clock.now))
        self.stack.enter_context(mock.patch.object(serial_port.time, "perf_counter", self.clock.now))
        self.stack.enter_context(mock.patch.object(serial_port.time, "sleep", self.clock.sleep))

    def exchange(self, **kwargs):
        return serial_port.send_receive(self.port, REQUEST, response_timeout_ms=500, **kwargs)

    def assert_restored(self):
        self.assertEqual((self.port.timeout, self.port.write_timeout), (.05, 2.0))

    def test_success_preserves_exact_frame_and_waits_for_actual_uart_framing(self):
        for baud, parity, stop in ((115200, "N", 1), (19200, "E", 2), (9600, "N", 1.5)):
            with self.subTest(baud=baud, parity=parity, stop=stop):
                self.port = Port(self.clock)
                self.port.baudrate, self.port.parity, self.port.stopbits = baud, parity, stop
                started = self.clock.now()
                self.assertEqual(self.exchange(deadline=started + 1), RESPONSE)
                self.assertAlmostEqual(self.clock.now() - started, guard_time(self.port), places=6)
                self.assertEqual(self.port.writes, [REQUEST])
                self.assertEqual(self.port.flushes, 0)
                self.assertEqual(self.port.cleared, [])
                self.assert_restored()

    def test_default_call_still_flushes_once_and_uses_original_timeouts(self):
        self.assertEqual(self.exchange(), RESPONSE)
        self.assertEqual(self.port.flushes, 1)
        self.assertEqual(self.port.writes, [REQUEST])
        self.assertEqual(self.port.seen_write_timeout, 2.0)
        self.assert_restored()

    def test_nonfinite_deadline_refuses_before_transmission(self):
        for deadline in (float("nan"), float("inf")):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                self.exchange(deadline=deadline)
        self.assertEqual(self.port.writes, [])

    def test_blocking_and_nonblocking_timeout_settings_are_restored(self):
        for original in (None, 0):
            with self.subTest(original=original):
                self.port = Port(self.clock)
                self.port.timeout = self.port.write_timeout = original
                self.assertEqual(self.exchange(deadline=self.clock.now() + .2), RESPONSE)
                self.assertLessEqual(self.port.seen_write_timeout, .2)
                self.assertLessEqual(self.port.seen_read_timeout, .2)
                self.assertEqual((self.port.timeout, self.port.write_timeout), (original, original))

    def test_empty_read_yields_before_retrying_within_same_receive_window(self):
        self.port.empty_reads = 1
        started = self.clock.now()
        self.assertEqual(self.exchange(deadline=started + .2), RESPONSE)
        self.assertAlmostEqual(self.clock.now() - started, guard_time(self.port) + .001)
        self.assertEqual(self.port.writes, [REQUEST])
        self.assert_restored()

    def test_response_window_can_end_before_the_operation_deadline(self):
        self.port.chunks = []
        started = self.clock.now()
        self.assertIsNone(serial_port.send_receive(self.port, REQUEST, response_timeout_ms=10,
                                                   deadline=started + .2))
        self.assertAlmostEqual(self.clock.now() - started, guard_time(self.port) + .01)
        self.assertEqual(self.port.cleared, [])
        self.assert_restored()

    def test_budgeted_receive_retains_response_buffer_bound(self):
        self.port.chunks = [bytes(1100)]
        with self.assertRaises(ValueError):
            self.exchange(deadline=self.clock.now() + .2)
        self.assertEqual(self.port.reads, 1)
        self.assertEqual(len(self.port.chunks[0]), 1100 - rtu.MAX_RESPONSE_BUFFER - 1)
        self.assert_restored()

    def test_no_time_for_wire_guard_means_no_transmission(self):
        for remaining in (-1, 0, guard_time(self.port) / 2):
            with self.subTest(remaining=remaining), self.assertRaises(TimeoutError):
                self.exchange(deadline=self.clock.now() + remaining)
        self.assertEqual(self.port.writes, [])
        self.assertEqual(self.port.cleared, [])
        self.assert_restored()

    def test_unknown_or_preexisting_output_queue_fails_before_write(self):
        for unsupported in (True, False):
            self.port.unsupported_queue = unsupported
            self.port.initial_queue = 1
            with self.subTest(unsupported=unsupported), self.assertRaises((RuntimeError, NotImplementedError)):
                self.exchange(deadline=self.clock.now() + 1)
        self.assertEqual(self.port.writes, [])
        self.assertEqual(self.port.cleared, [], "do not clear a queue this exchange did not write")
        self.assert_restored()

    def test_partial_writes_abort_once_then_finish_guard_without_retry(self):
        for result in (0, len(REQUEST) - 1, None):
            with self.subTest(result=result):
                self.port = Port(self.clock)
                self.port.write_result = result
                with self.assertRaises((TimeoutError, serial.SerialException)):
                    self.exchange(deadline=self.clock.now() + .1)
                self.assertEqual(self.port.writes, [REQUEST])
                self.assertEqual(len(self.port.cleared), 1)
                self.assertAlmostEqual(self.clock.now() - self.port.cleared[0], guard_time(self.port))
                self.assertEqual(self.port.reads, 0)
                self.assert_restored()

    def test_blocked_write_reserves_abort_guard_inside_total_budget(self):
        self.port.write_blocks = True
        started, deadline = self.clock.now(), self.clock.now() + .08
        with self.assertRaises(serial.SerialTimeoutException):
            self.exchange(deadline=deadline)
        self.assertAlmostEqual(self.port.seen_write_timeout, deadline - started - guard_time(self.port))
        self.assertLessEqual(self.clock.now(), deadline + 1e-9)
        self.assertAlmostEqual(self.clock.now() - self.port.cleared[0], guard_time(self.port))
        self.assertEqual(self.port.writes, [REQUEST])
        self.assert_restored()

    def test_never_drained_queue_is_aborted_before_reserved_guard(self):
        self.port.queue_stuck = True
        deadline = self.clock.now() + .08
        with self.assertRaises(TimeoutError):
            self.exchange(deadline=deadline)
        self.assertAlmostEqual(self.clock.now(), deadline)
        self.assertAlmostEqual(self.clock.now() - self.port.cleared[0], guard_time(self.port))
        self.assertEqual(self.port.reads, 0)
        self.assertEqual(self.port.flushes, 0)
        self.assert_restored()

    def test_blocked_read_cannot_consume_more_than_remaining_budget(self):
        self.port.read_blocks = True
        started, deadline = self.clock.now(), self.clock.now() + .03
        with self.assertRaises(TimeoutError):
            self.exchange(deadline=deadline)
        self.assertLessEqual(self.port.seen_read_timeout, deadline - started - guard_time(self.port) + 1e-9)
        self.assertAlmostEqual(self.clock.now(), deadline)
        self.assertEqual(self.port.cleared, [])
        self.assert_restored()

    def test_cancel_before_write_and_after_write_do_not_report_a_reply(self):
        self.assertIsNone(self.exchange(deadline=self.clock.now() + 1, cancel_check=lambda: True))
        self.assertEqual(self.port.writes, [])
        self.assertIsNone(self.exchange(deadline=self.clock.now() + 1,
                                        cancel_check=lambda: bool(self.port.writes)))
        self.assertEqual(self.port.writes, [REQUEST])
        self.assertEqual(len(self.port.cleared), 1)
        self.assertAlmostEqual(self.clock.now() - self.port.cleared[0], guard_time(self.port))
        self.assert_restored()

    def test_cancel_during_receive_does_not_replay_or_abort_completed_transmission(self):
        self.assertIsNone(self.exchange(deadline=self.clock.now() + 1,
                                        cancel_check=lambda: self.port.reads > 0))
        self.assertEqual(self.port.writes, [REQUEST])
        self.assertEqual(self.port.reads, 1)
        self.assertEqual(self.port.cleared, [])
        self.assert_restored()

    def test_persistent_transport_closes_only_after_abort_guard(self):
        self.port.queue_stuck = True
        deadline = self.clock.now() + .08
        with mock.patch.object(modbus_io, "open_port", return_value=self.port) as opened:
            send, close = modbus_io.make_serial_send_rtu_persistent(
                "test", 19200, "N", 2, pre_open_delay_s=0, deadline=deadline)
            self.assertIsNone(send(REQUEST))
            self.assertEqual(self.port.writes, [REQUEST])
            self.assertAlmostEqual(self.port.closed[0] - self.port.cleared[0], guard_time(self.port))
            self.assertIsNone(send(REQUEST))
            self.assertEqual(self.port.writes, [REQUEST])
            self.assertEqual(opened.call_count, 1)
            close()

    def test_persistent_preopen_delay_is_bounded_and_transmits_nothing(self):
        deadline = self.clock.now() + .01
        with mock.patch.object(modbus_io, "open_port", return_value=self.port):
            send, close = modbus_io.make_serial_send_rtu_persistent(
                "test", 19200, "N", 2, pre_open_delay_s=.05, deadline=deadline)
            self.assertIsNone(send(REQUEST))
            self.assertLessEqual(self.clock.now(), deadline + 1e-9)
            self.assertEqual(self.port.writes, [])
            close()

    def test_persistent_default_does_not_add_deadline_keyword(self):
        with mock.patch.object(modbus_io, "open_port", return_value=self.port), \
                mock.patch.object(modbus_io, "send_receive", return_value=RESPONSE) as receive:
            send, close = modbus_io.make_serial_send_rtu_persistent("test", 19200, "N", 2)
            self.assertEqual(send(REQUEST), RESPONSE)
            receive.assert_called_once_with(self.port, REQUEST, response_timeout_ms=400)
            close()

    def test_identified_exchange_can_add_or_shorten_the_deadline_on_the_open_port(self):
        for default in (None, self.clock.now() + 3):
            with mock.patch.object(modbus_io, "open_port", return_value=self.port) as opened, \
                    mock.patch.object(modbus_io, "send_receive", return_value=RESPONSE) as receive:
                send, close = modbus_io.make_serial_send_rtu_persistent(
                    "test", 19200, "N", 2, pre_open_delay_s=0, deadline=default)
                send(REQUEST)
                deadline = self.clock.now() + 2
                send(REQUEST, exchange_deadline=deadline)
                self.assertEqual(receive.call_args.kwargs["deadline"], deadline)
                send(REQUEST, exchange_deadline=deadline + 2)
                self.assertEqual(receive.call_args.kwargs["deadline"], default or deadline + 2)
                opened.assert_called_once()
                close()
                self.port.is_open = True

    def test_persistent_lock_timeout_never_opens_a_port(self):
        lock = mock.MagicMock()
        lock.acquire.return_value = False
        with mock.patch.object(modbus_io.threading, "Lock", return_value=lock), \
                mock.patch.object(modbus_io, "open_port") as opened:
            send, _ = modbus_io.make_serial_send_rtu_persistent(
                "test", 19200, "N", 2, deadline=self.clock.now() + .1)
            self.assertIsNone(send(REQUEST))
            self.assertAlmostEqual(lock.acquire.call_args.kwargs["timeout"], .1)
            opened.assert_not_called()
            lock.release.assert_not_called()

    def test_persistent_transport_reuses_one_handle_until_close(self):
        with mock.patch.object(modbus_io, "open_port", return_value=self.port) as opened:
            send, close = modbus_io.make_serial_send_rtu_persistent(
                "test", 19200, "N", 2, pre_open_delay_s=0, deadline=self.clock.now() + .2)
            self.assertEqual(send(REQUEST), RESPONSE)
            self.port.chunks = [RESPONSE]
            self.assertEqual(send(REQUEST), RESPONSE)
            self.assertEqual(self.port.writes, [REQUEST, REQUEST])
            self.assertEqual(opened.call_count, 1)
            close()
            close()
            self.assertEqual(len(self.port.closed), 1)


@unittest.skipUnless(os.name == "posix", "real UART PTY requires POSIX")
class RealSerialDeadlineTests(unittest.TestCase):
    def test_bounded_exchange_on_real_pty_sends_one_exact_request(self):
        master, slave = os.openpty()
        port = serial.Serial(os.ttyname(slave), 19200, stopbits=2,
                             timeout=.05, write_timeout=2, exclusive=True)
        os.close(slave)
        captured, errors = [], []
        def peer():
            try:
                data = b""
                while len(data) < len(REQUEST):
                    if not select.select([master], [], [], .5)[0]:
                        raise AssertionError("request did not reach PTY")
                    data += os.read(master, len(REQUEST) - len(data))
                captured.append(data)
                os.write(master, RESPONSE[:2])
                time.sleep(.002)
                os.write(master, RESPONSE[2:])
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        try:
            started = time.monotonic()
            result = serial_port.send_receive(port, REQUEST, response_timeout_ms=500,
                                              deadline=started + .5)
            elapsed = time.monotonic() - started
            self.assertEqual(result, RESPONSE)
            self.assertGreaterEqual(elapsed, guard_time(port) * .95)
            self.assertLess(elapsed, .5)
            self.assertEqual((port.timeout, port.write_timeout), (.05, 2))
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(captured, [REQUEST])
            self.assertFalse(select.select([master], [], [], 0)[0], "unexpected duplicate request")
        finally:
            port.close()
            thread.join(1)
            os.close(master)


if __name__ == "__main__":
    unittest.main()
