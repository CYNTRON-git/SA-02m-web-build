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


class TestTabRequestPathEndToEnd(unittest.TestCase):
    """B-2 (review R4): the tab sends skip_on_error=false. Driven through JobManager and the REAL
    run_flash_batch_job / run_flash_job (only the serial session is a stub): the job's error text names
    the target (address + serial) with the reason, on EVERY path, and carries no Python class name."""

    MISMATCH = "Серийный номер устройства не совпал с выбранной целью."

    @staticmethod
    def _entry():
        return FirmwareEntry(file="MR-02m_1.0.0.0.fw", version="1.0.0.0", signatures=["MR-02m-DI16"],
                             device="MR-02m", size=1, sha256="", released="", notes="")

    def _drive(self, kind, targets, results, **params_extra):
        import contextlib
        from types import SimpleNamespace

        @contextlib.contextmanager
        def lease(*_a, **_k):
            yield []

        queue = list(results)
        calls = []

        def session(*_a, **_k):
            calls.append(1)
            return queue.pop(0)

        params = {"port": "COM3", "targets": targets, "target": targets[0], **params_extra}
        cfg = SimpleNamespace(mplc_stop_services=[])
        entry = self._entry()
        patches = [
            mock.patch.object(runner, "resolve_device_path", lambda *a, **k: "/dev/COM3"),
            mock.patch.object(runner, "_load_firmware_for_flash", lambda *a, **k: (b"img", "MR-02m-DI16", "1.0", entry)),
            mock.patch.object(runner, "port_lease", lease),
            mock.patch.object(runner, "_port_flock", lease),
            mock.patch.object(runner, "_resolve_flash_route_for_device", lambda *a, **k: (False, None)),
            mock.patch.object(runner, "device_flash_route", lambda *a, **k: "mp_mr"),
            mock.patch.object(runner, "_duplicate_modbus_address_on_line", lambda *a, **k: False),
            mock.patch.object(runner, "_run_bootloader_flash_session", session),
            mock.patch.object(runner, "_settle_between_batch_devices", lambda *a, **k: None),
        ]
        fn = runner.run_flash_batch_job if kind == JobKind.FLASH_BATCH else runner.run_flash_job
        mgr = JobManager()
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            job = mgr.submit(kind, "COM3", params, lambda j, ctx: fn(j, ctx, cfg, None))
            for _ in range(200):
                if mgr.get(job.id).state in (JobState.DONE, JobState.ERROR, JobState.CANCELLED):
                    break
                time.sleep(0.02)
        return mgr.get(job.id), len(calls)

    @staticmethod
    def _targets(*addrs):
        return [{"address": a, "serial": SERIAL + i, "signature": "MR-02m-DI16"} for i, a in enumerate(addrs)]

    def test_single_target_stop_on_error_names_the_target_without_a_class_name(self) -> None:
        job, n = self._drive(JobKind.FLASH_BATCH, self._targets(5), [self.MISMATCH], skip_on_error=False)
        self.assertEqual(job.state, JobState.ERROR)
        self.assertEqual(job.error, f"Не прошито: адр.5 SN 0x0E0A7530: {self.MISMATCH}")
        self.assertNotIn("RuntimeError", job.error)
        self.assertEqual(job.params["errors"], [{"address": 5, "serial": SERIAL, "error": self.MISMATCH}])

    def test_batch_stops_at_the_first_failed_target_and_names_only_it(self) -> None:
        job, n = self._drive(JobKind.FLASH_BATCH, self._targets(5, 6), [self.MISMATCH, None], skip_on_error=False)
        self.assertEqual(n, 1, "the second target was flashed after the first failed")
        self.assertEqual(job.state, JobState.ERROR)
        self.assertTrue(job.error.startswith("Не прошито: адр.5 SN 0x0E0A7530:"), job.error)
        self.assertNotIn("адр.6", job.error)

    def test_failure_of_the_second_target_names_the_second(self) -> None:
        job, n = self._drive(JobKind.FLASH_BATCH, self._targets(5, 6), [None, "boom"], skip_on_error=False)
        self.assertEqual(n, 2)
        self.assertEqual(job.state, JobState.ERROR)
        self.assertEqual(job.error, "Не прошито: адр.6 SN 0x0E0A7531: boom")

    def test_explicit_continue_names_every_failed_target(self) -> None:
        job, n = self._drive(JobKind.FLASH_BATCH, self._targets(5, 6, 7), ["a", None, "c"], skip_on_error=True)
        self.assertEqual(n, 3)
        self.assertEqual(job.state, JobState.ERROR)
        self.assertEqual(job.error, "Не прошито: адр.5 SN 0x0E0A7530: a; адр.7 SN 0x0E0A7532: c")

    def test_single_flash_kind_names_the_target_too(self) -> None:
        job, _ = self._drive(JobKind.FLASH, self._targets(5), [self.MISMATCH])
        self.assertEqual(job.state, JobState.ERROR)
        self.assertEqual(job.error, f"Не прошито: адр.5 SN 0x0E0A7530: {self.MISMATCH}")

    def test_a_clean_batch_stays_done(self) -> None:
        job, _ = self._drive(JobKind.FLASH_BATCH, self._targets(5, 6), [None, None], skip_on_error=False)
        self.assertEqual(job.state, JobState.DONE)
        self.assertIsNone(job.error)

    def test_a_non_flash_exception_keeps_its_class_name(self) -> None:
        mgr = JobManager()

        def run_fn(job, ctx):
            raise OSError("port vanished")

        job = mgr.submit(JobKind.FLASH_BATCH, "COM3", {}, run_fn)
        for _ in range(100):
            if mgr.get(job.id).state in (JobState.ERROR, JobState.DONE):
                break
            time.sleep(0.02)
        self.assertEqual(mgr.get(job.id).error, "OSError: port vanished")


if __name__ == "__main__":
    unittest.main()
