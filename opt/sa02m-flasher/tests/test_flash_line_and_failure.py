# -*- coding: utf-8 -*-
"""Bench FL (1.0.7.36, 2026-11-11): the tab flashed nothing on a 19200 line and still said «done».

  FL-01  an MR on a non-115200 line was unreachable: the application line was a constant.
         The line the SCAN found the device on is tried too (and the 115200 profile stays a fallback).
  FL-02  a flash with any failed target ends `error`, naming the target — never `done`.
  FL-03  a read timeout is a timeout, not «Серийный номер не совпал».
  FL-04  the log's «sig=» is the FILE's signature, never the target's.
"""
from __future__ import annotations

import sys
import threading
import time
import types
import unittest
from unittest import mock

if "fcntl" not in sys.modules:  # runner.py imports fcntl (Linux-only)
    sys.modules["fcntl"] = types.ModuleType("fcntl")
    sys.modules["fcntl"].LOCK_EX = 2
    sys.modules["fcntl"].LOCK_NB = 4
    sys.modules["fcntl"].LOCK_UN = 8
    sys.modules["fcntl"].flock = lambda *a, **k: None

from sa02m_flasher import module_profiles as mp
from sa02m_flasher import runner
from sa02m_flasher.firmware_repo import FirmwareEntry
from sa02m_flasher.jobs import JobKind, JobManager, JobState, failed_targets_text

SERIAL = 0x0E0A7530
SIGNATURE = "14DI"


class _Ser:
    def close(self) -> None:
        pass


class _FakeLine:
    """A module that answers on exactly one line; records every call by line."""

    def __init__(self, answers_on, calls):
        self.answers_on = answers_on
        self.calls = calls
        self.line = None

    def read_holding_registers(self, addr, reg, count):
        self.calls.append(("read", self.line, reg))
        if self.line != self.answers_on:
            return None, "Таймаут ответа"
        if reg == 270:
            return (SERIAL & 0xFFFF).to_bytes(2, "big") + (SERIAL >> 16).to_bytes(2, "big"), None
        return b"".join(bytes((0, c)) for c in SIGNATURE.encode().ljust(12, b"\0")), None

    def enter_bootloader_wb(self, addr):
        self.calls.append(("write129", self.line, addr))
        return None

    def enter_bootloader(self, addr):
        self.calls.append(("write06", self.line, addr))
        return None


def _enter(device, answers_on):
    calls = []

    def make_flasher(path, baud, parity, stop, cancel_evt, log_cb):
        f = _FakeLine(answers_on, calls)
        f.line = (baud, parity, stop)
        return f, _Ser()

    logs = []
    family = mp.manifest_device_for_signature(SIGNATURE)
    with mock.patch.object(runner, "_make_flasher", make_flasher):
        res = runner._enter_bootloader_from_application_line(
            "/dev/COM3", device, threading.Event(), lambda m, lvl: logs.append((lvl, m)),
            is_wb_firmware=False, use_fast_modbus=False, image_family=family,
        )
    return res, calls, logs


def _bench_device(**extra):
    d = {"address": 5, "serial": SERIAL, "signature": SIGNATURE, "baudrate": 19200, "parity": "N", "stopbits": 1}
    d.update(extra)
    return d


class TestLineFromScan(unittest.TestCase):
    """FL-01 — the 14DI @5 on COM3 (19200 N1): the old code talked 115200, read reg 270, timed out."""

    def test_candidates_scan_line_follows_the_profile_line(self) -> None:
        cands = runner._application_line_candidates(_bench_device(), is_wb_firmware=False)
        self.assertEqual(cands, [(115200, "N", 1), (19200, "N", 1)])

    def test_explicit_scan_line_goes_first_and_profile_stays_a_fallback(self) -> None:
        dev = _bench_device(app_line_baud=19200, app_line_parity="N", app_line_stopbits=1)
        self.assertEqual(
            runner._application_line_candidates(dev, is_wb_firmware=False),
            [(19200, "N", 1), (115200, "N", 1)],
        )

    def test_no_scan_baud_means_the_profile_only(self) -> None:
        self.assertEqual(
            runner._application_line_candidates({"signature": SIGNATURE}, is_wb_firmware=False),
            [(115200, "N", 1)],
        )

    def test_wb_firmware_keeps_one_line(self) -> None:
        self.assertEqual(len(runner._application_line_candidates(_bench_device(), is_wb_firmware=True)), 1)

    def test_the_module_on_19200_is_reached_and_only_then_written(self) -> None:
        (identity, err), calls, _ = _enter(_bench_device(), answers_on=(19200, "N", 1))
        self.assertIsNone(err, err)
        self.assertEqual(identity[0], SERIAL)
        writes = [c for c in calls if c[0].startswith("write")]
        self.assertTrue(writes, "reg 129 was never written")
        self.assertTrue(all(c[1] == (19200, "N", 1) for c in writes), writes)
        self.assertTrue(all(c[0] == "read" for c in calls if c[1] == (115200, "N", 1)),
                        "a write went out on the line the module does not answer on")

    def test_the_module_on_115200_with_a_false_scan_line_is_still_reached(self) -> None:
        dev = _bench_device(baudrate=19200, parity="N", stopbits=2)   # a scan that guessed wrong
        (identity, err), calls, _ = _enter(dev, answers_on=(115200, "N", 1))
        self.assertIsNone(err, err)
        self.assertEqual(identity[0], SERIAL)


class TestTimeoutIsATimeout(unittest.TestCase):
    """FL-03 — no answer on any line: say so, name the lines, write nothing."""

    def test_silent_module_reports_no_answer_on_every_line_and_nothing_is_written(self) -> None:
        (identity, err), calls, logs = _enter(_bench_device(), answers_on=None)
        self.assertIsNone(identity)
        self.assertTrue(err.startswith(runner.IDENTITY_NO_ANSWER), err)
        self.assertNotIn("не совпал", err)
        self.assertIn("115200 N1", err)
        self.assertIn("19200 N1", err)
        self.assertEqual([c for c in calls if c[0].startswith("write")], [], "a write was sent to a silent line")
        self.assertTrue(any("пробую линию" in m for _, m in logs), logs)

    def test_confirm_identity_timeout_is_not_a_mismatch(self) -> None:
        flasher = mock.Mock()
        flasher.read_holding_registers.return_value = (None, "Таймаут ответа")
        _, err = runner._confirm_flash_identity(
            flasher, {"signature": SIGNATURE, "serial": SERIAL}, 5, False,
            image_family=mp.manifest_device_for_signature(SIGNATURE))
        self.assertTrue(runner._is_no_answer(err), err)
        self.assertNotIn("не совпал", err)

    def test_confirm_identity_damaged_answer_is_a_read_failure_not_a_mismatch(self) -> None:
        flasher = mock.Mock()
        flasher.read_holding_registers.return_value = (None, "CRC error")
        _, err = runner._confirm_flash_identity(
            flasher, {"signature": SIGNATURE, "serial": SERIAL}, 5, False,
            image_family=mp.manifest_device_for_signature(SIGNATURE))
        self.assertIn("Не удалось прочитать серийный номер", err)
        self.assertNotIn("не совпал", err)
        self.assertFalse(runner._is_no_answer(err))

    def test_a_real_mismatch_still_says_so(self) -> None:
        flasher = mock.Mock()
        other = SERIAL + 1
        flasher.read_holding_registers.return_value = (
            (other & 0xFFFF).to_bytes(2, "big") + (other >> 16).to_bytes(2, "big"), None)
        _, err = runner._confirm_flash_identity(
            flasher, {"signature": SIGNATURE, "serial": SERIAL}, 5, False,
            image_family=mp.manifest_device_for_signature(SIGNATURE))
        self.assertIn("Серийный номер устройства не совпал", err)


class TestFailedTargetEndsError(unittest.TestCase):
    """FL-02 — a failed flash is never `done`."""

    def _run(self, kind, errors, cancel=False):
        mgr = JobManager()

        def run_fn(job, ctx):
            if errors:
                job.params["errors"] = errors
            if cancel:
                ctx["cancel_evt"].set()

        job = mgr.submit(kind, "COM3", {}, run_fn)
        for _ in range(100):
            if mgr.get(job.id).state in (JobState.DONE, JobState.ERROR, JobState.CANCELLED):
                break
            time.sleep(0.02)
        return mgr.get(job.id)

    def test_single_failed_target_ends_error_naming_the_target(self) -> None:
        job = self._run(JobKind.FLASH_BATCH, [{"address": 5, "serial": SERIAL, "error": "Нет ответа устройства (x)."}])
        self.assertEqual(job.state, JobState.ERROR)
        self.assertIn("адр.5", job.error)
        self.assertIn("SN 0x0E0A7530", job.error)
        self.assertIn("Нет ответа устройства", job.error)

    def test_partial_batch_is_an_error_too_and_names_only_the_failed_target(self) -> None:
        job = self._run(JobKind.FLASH_BATCH, [{"address": 10, "serial": 0, "error": "boom"}])
        self.assertEqual(job.state, JobState.ERROR)
        self.assertIn("адр.10", job.error)

    def test_clean_batch_is_done(self) -> None:
        self.assertEqual(self._run(JobKind.FLASH_BATCH, None).state, JobState.DONE)

    def test_a_scan_with_params_errors_is_not_a_failed_flash(self) -> None:
        self.assertEqual(self._run(JobKind.SCAN, [{"address": 1, "error": "x"}]).state, JobState.DONE)

    def test_cancel_stays_cancelled(self) -> None:
        job = self._run(JobKind.FLASH_BATCH, [{"address": 5, "error": "x"}], cancel=True)
        self.assertEqual(job.state, JobState.CANCELLED)

    def test_text_names_every_failed_target(self) -> None:
        txt = failed_targets_text([{"address": 5, "serial": SERIAL, "error": "a"}, {"address": 6, "serial": 0, "error": "b"}])
        self.assertEqual(txt, "Не прошито: адр.5 SN 0x0E0A7530: a; адр.6: b")


class TestLogSignatureIsTheFiles(unittest.TestCase):
    """FL-04 — «файл ce-02m-3… sig=14DI» read as «the file is a 14DI»."""

    def _entry(self, sigs):
        return FirmwareEntry(file="ce-02m-3_1.0.7.23.fw", version="1.0.7.23", signatures=sigs,
                             device="CE-02m-3", size=1, sha256="", released="", notes="")

    def test_target_signature_is_not_presented_as_the_files(self) -> None:
        label = runner._firmware_signature_label("NONE", self._entry([]), {"signature": "14DI"})
        self.assertTrue(label.startswith("нет в файле"), label)
        self.assertIn("устройство: 14DI", label)

    def test_file_signature_wins(self) -> None:
        self.assertEqual(runner._firmware_signature_label("CE02M3", self._entry([]), {"signature": "14DI"}), "CE02M3")

    def test_manifest_signature_is_the_files_too(self) -> None:
        self.assertEqual(runner._firmware_signature_label("NONE", self._entry(["CE02M3"]), {"signature": "14DI"}), "CE02M3")


if __name__ == "__main__":
    unittest.main()
