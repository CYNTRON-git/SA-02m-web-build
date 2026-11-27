"""Observed fallback reasons must not turn silence into unsupported-register proof."""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sa02m_flasher import flash_protocol as protocol


class ReadinessDiagnosticTests(unittest.TestCase):
    def exercise(self, state_error=None, reject_error=None, *, max_wait=4.0):
        now = [100.0]
        calls, sleeps, logs = [], [], []

        def read(register, count):
            calls.append((register, count, round(now[0] - 100.0, 2)))
            error = state_error if register == protocol.REG_DIAG_STATE else reject_error
            return (None, error) if error else (b'\x00\x00', None)

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        with patch.object(protocol.time, 'perf_counter', side_effect=lambda: now[0]), \
             patch.object(protocol.time, 'sleep', side_effect=sleep):
            error = protocol._wait_bootloader_ready_for_data_impl(
                SimpleNamespace(log_cb=logs.append), read, 'fixture', max_wait_s=max_wait)
        return error, logs, calls, sleeps, now[0] - 100.0

    def test_exception02_reports_only_the_observed_unsupported_registers(self):
        error, logs, *_ = self.exercise('Исключение Modbus: код 2')
        self.assertIsNone(error)
        fallback = next(line for line in logs if 'legacy-паузой' in line)
        self.assertIn('0xF3: Modbus exception 02', fallback)
        self.assertNotIn('0xF2: Modbus exception 02', fallback)
        self.assertNotIn('таймаут', fallback)

    def test_timeout_does_not_claim_register_support_or_busy_cause(self):
        error, logs, *_ = self.exercise('Таймаут ответа', 'timeout')
        self.assertIsNone(error)
        fallback = next(line for line in logs if 'legacy-паузой' in line)
        self.assertIn('0xF3: нет ответа (таймаут)', fallback)
        self.assertIn('0xF2: нет ответа (таймаут)', fallback)
        self.assertNotIn('не поддерживает', fallback)
        self.assertNotIn('не поддерживается', fallback)
        self.assertNotIn('занят', fallback)

    def test_mixed_reasons_remain_attributed_to_each_register(self):
        for state, reject in [('timeout', 'Исключение Modbus: код 2'),
                              ('Исключение Modbus: код 2', 'timeout')]:
            with self.subTest(state=state):
                error, logs, *_ = self.exercise(state, reject)
                self.assertIsNone(error)
                fallback = next(line for line in logs if 'legacy-паузой' in line)
                unsupported = '0xF2' if state == 'timeout' else '0xF3'
                silent = '0xF3' if state == 'timeout' else '0xF2'
                self.assertIn(unsupported + ': Modbus exception 02', fallback)
                self.assertIn(silent + ': нет ответа (таймаут)', fallback)

    def test_diagnostic_change_preserves_legacy_reads_and_pause(self):
        expected = [(register, 1, round(index * .18, 2)) for index in range(17)
                    for register in (protocol.REG_DIAG_STATE, protocol.REG_LAST_DATA_REJECT)]
        for state, reject in [('timeout', 'timeout'), ('Исключение Modbus: код 2', None),
                              ('timeout', 'Исключение Modbus: код 2')]:
            with self.subTest(state=state, reject=reject):
                error, _, calls, sleeps, elapsed = self.exercise(state, reject)
                self.assertIsNone(error)
                self.assertEqual(calls, expected)
                self.assertEqual(sleeps, [.18] * 16)
                self.assertAlmostEqual(elapsed, 2.88)

    def test_supported_and_unrelated_error_keep_their_existing_policy(self):
        for reject in (None, 'timeout'):
            error, logs, calls, sleeps, elapsed = self.exercise(reject_error=reject)
            self.assertIsNone(error)
            self.assertEqual(calls, [(protocol.REG_DIAG_STATE, 1, 0), (protocol.REG_LAST_DATA_REJECT, 1, 0)])
            self.assertEqual(sleeps, [])
            self.assertEqual(elapsed, 0)
            self.assertFalse(any('legacy-паузой' in line for line in logs))
        error, logs, calls, sleeps, elapsed = self.exercise('CRC mismatch', max_wait=.5)
        self.assertIn('CRC mismatch', error)
        self.assertEqual(len(calls), 6)
        self.assertEqual(sleeps, [.18] * 3)
        self.assertAlmostEqual(elapsed, .54)
        self.assertFalse(any('legacy-паузой' in line for line in logs))


if __name__ == '__main__':
    unittest.main()
