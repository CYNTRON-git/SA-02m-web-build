# -*- coding: utf-8 -*-
"""Bench D36b (1.0.7.36, 2026-11-12): the FL-01 fix was half done.

MR 14DI @5 on COM3 (19200 N1): the application line came from the scan, reg 129 was accepted, then the flasher
opened the BOOTLOADER line at the constant 115200 and got no answer at @247 — the module stayed in its bootloader.

  D36b-01  after reg 129 the bootloader is looked for on the application's line (baud, parity AND stop bits),
           then on 115200 8N1 - for every family (the rule lives in the MR-02m repo: `doc/05_BOOTLOADER.md` §5
           «Параметры линии», `docs/contracts/bootloader-modbus.md` «Listen parameters»; the family image and the
           pre-final loaders listen on 115200, a final BL >= 0.0.0.35 on the application's line). The probe is
           read only and ends before any block is written.
  D36b-02  the bootloader cannot be found after a successful reg 129: the error says the module may have been
           left in its bootloader and how it returns, and names the target.
"""
from __future__ import annotations

import sys
import threading
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

SERIAL = 0x0E0A7530
SIGNATURE = "14DI"
LINE_19200 = (19200, "N", 1)
LINE_115200 = (115200, "N", 1)


class _Port:
    """An opened port: remembers the line it was opened on."""

    def __init__(self, baud, parity, stop):
        self.line = (baud, parity, stop)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Bus:
    """The bench: a bootloader that listens on exactly one line; records every open, read and flash call."""

    def __init__(self, bootloader_line, *, app_line=LINE_19200):
        self.bootloader_line = bootloader_line
        self.app_line = app_line
        self.opened = []        # lines opened as bootloader line, in order
        self.probed = []        # lines a read-only probe ran on
        self.flashed_on = []    # lines _flash_one_device ran on
        self.writes = []
        self.logs = []

    # -- patched collaborators -------------------------------------------------------------------
    def open_serial(self, path, *, is_wb_firmware, line=None):
        port = _Port(*(line or LINE_115200))
        self.opened.append(port.line)
        return port

    def enter(self, device_path, device, cancel_evt, log_cb, **kw):
        entered = kw.get("entered")
        if entered is not None:        # the application phase hands over the line reg 129 succeeded on
            entered["line"] = self.app_line
        return (SERIAL, mp.manifest_device_for_signature(SIGNATURE)), None

    def wait_ready(self, ser, cancel_evt, log_cb, **kw):
        self.probed.append(ser.line)
        return ser.line == self.bootloader_line, None if ser.line == self.bootloader_line else "Таймаут ответа"

    def make_protocol(self, ser, cancel_evt, log_cb, **kw):
        return types.SimpleNamespace(ser=ser)

    def confirm_identity(self, flasher, device, address, use_fast, **kw):
        if flasher.ser.line != self.bootloader_line:
            return None, f"{runner.IDENTITY_NO_ANSWER} (таймаут чтения серийного номера)."
        return (SERIAL, "MR-02m"), None

    def flash_one(self, flasher, device, image, file_signature, **kw):
        self.flashed_on.append(flasher.ser.line)
        return None


def _device(**extra):
    d = {"address": 5, "serial": SERIAL, "signature": SIGNATURE, "baudrate": 19200, "parity": "N", "stopbits": 1}
    d.update(extra)
    return d


def _run(bus, device=None, family="MR-02m"):
    device = device or _device()
    with mock.patch.object(runner, "_open_bootloader_serial", bus.open_serial), \
            mock.patch.object(runner, "_enter_bootloader_from_application_line", bus.enter), \
            mock.patch.object(runner, "_wait_bootloader_ready_on_serial", bus.wait_ready), \
            mock.patch.object(runner, "_make_bootloader_flasher_protocol", bus.make_protocol), \
            mock.patch.object(runner, "_confirm_flash_identity", bus.confirm_identity), \
            mock.patch.object(runner, "_flash_one_device", bus.flash_one), \
            mock.patch.object(runner.time, "sleep", lambda s: None):
        return runner._run_bootloader_flash_session(
            "/dev/COM3", device, threading.Event(), lambda m, lvl: bus.logs.append((lvl, m)),
            lambda pct, msg: None,
            use_fast_modbus=False, is_wb_firmware=False, is_bootloader_firmware=False,
            duplicate_on_line=False, recovery=False, image=b"\0" * 16, file_signature=SIGNATURE,
            image_family=family,
        )


class TestBootloaderLineOrder(unittest.TestCase):
    """The order: the application's line first, then 115200 8N1 — the same for every family."""

    def test_bootloader_on_the_line_speed_is_found_and_flashed(self) -> None:
        bus = _Bus(LINE_19200)
        err = _run(bus)
        self.assertIsNone(err, err)
        self.assertEqual(bus.flashed_on, [LINE_19200], "the image went to a port that is not on the bootloader's line")
        self.assertEqual(bus.opened[0], LINE_19200)

    def test_older_generation_on_115200_while_the_line_is_19200_is_found(self) -> None:
        bus = _Bus(LINE_115200)
        err = _run(bus)
        self.assertIsNone(err, err)
        self.assertEqual(bus.flashed_on, [LINE_115200])
        self.assertEqual(bus.probed[0], LINE_19200, "the application's line is looked at first")
        self.assertIn(LINE_115200, bus.probed)

    def test_parity_and_stop_bits_travel_with_the_baud(self) -> None:
        line = (19200, "E", 2)
        bus = _Bus(line, app_line=line)
        err = _run(bus, _device(parity="E", stopbits=2))
        self.assertIsNone(err, err)
        self.assertEqual(bus.opened[0], line)
        self.assertEqual(bus.flashed_on, [line])

    def test_a_115200_line_stays_one_candidate(self) -> None:
        bus = _Bus(LINE_115200, app_line=LINE_115200)
        err = _run(bus, _device(baudrate=115200))
        self.assertIsNone(err, err)
        self.assertEqual(bus.opened, [LINE_115200], "a 115200 line must not probe anything else")
        self.assertEqual(bus.flashed_on, [LINE_115200])

    def test_every_family_probes_the_line_then_115200(self) -> None:
        for family in ("MR-02m", "RTU-Sensor", "CE-02m-3"):
            bus = _Bus(LINE_115200)
            err = _run(bus, family=family)
            self.assertIsNone(err, f"{family}: {err}")
            self.assertEqual(bus.probed[:2], [LINE_19200, LINE_115200], family)
            self.assertEqual(bus.flashed_on, [LINE_115200], family)

    def test_the_probe_never_writes_a_block_before_the_loader_is_found(self) -> None:
        bus = _Bus(LINE_115200)
        _run(bus)
        self.assertEqual(bus.writes, [])


class TestCandidatesPure(unittest.TestCase):
    def test_application_line_then_115200_without_duplicates(self) -> None:
        self.assertEqual(runner._bootloader_line_candidates(_device(), LINE_19200), [LINE_19200, LINE_115200])
        self.assertEqual(runner._bootloader_line_candidates(_device(), (19200, "E", 2)),
                         [(19200, "E", 2), LINE_115200])
        self.assertEqual(runner._bootloader_line_candidates(_device(), LINE_115200), [LINE_115200])
        self.assertEqual(runner._bootloader_line_candidates(_device(), None), [LINE_115200])

    def test_a_module_the_scan_found_in_its_bootloader_is_looked_for_on_the_scan_line(self) -> None:
        self.assertEqual(runner._bootloader_line_candidates(_device(in_bootloader=True), None),
                         [LINE_19200, LINE_115200])
        self.assertEqual(runner._bootloader_line_candidates(_device(in_bootloader=True, baudrate=115200), None),
                         [LINE_115200])


class TestBootloaderNotFound(unittest.TestCase):
    """D36b-02 — reg 129 was accepted, the bootloader answers nowhere."""

    def setUp(self) -> None:
        self.bus = _Bus(("never", "N", 1))
        self.err = _run(self.bus)

    def test_nothing_is_written_and_both_lines_were_probed(self) -> None:
        self.assertEqual(self.bus.flashed_on, [])
        self.assertIn(LINE_19200, self.bus.probed)
        self.assertIn(LINE_115200, self.bus.probed)

    def test_error_says_the_module_may_be_left_in_its_bootloader_and_how_it_returns(self) -> None:
        self.assertIsNotNone(self.err)
        self.assertIn("мог остаться в загрузчике", self.err)
        self.assertIn("120 с", self.err)
        self.assertIn("питани", self.err)
        self.assertIn("19200 N1", self.err)
        self.assertIn("115200 N1", self.err)
        self.assertIn("Образ не записывался", self.err)

    def test_error_is_not_the_old_pre_write_text(self) -> None:
        self.assertNotIn("Прошивка не начата, запись не отправлялась", self.err,
                         "that text is true only BEFORE reg 129; here the module has been put into the bootloader")

    def test_a_silent_application_that_never_entered_keeps_the_old_text(self) -> None:
        # the enter phase failed (no answer on any line): the module was never touched
        def enter_fails(device_path, device, cancel_evt, log_cb, **kw):
            return None, f"{runner.IDENTITY_NO_ANSWER} адр.5: таймаут чтения серийного номера на линиях 19200 N1. Прошивка не начата, запись не отправлялась."

        bus = _Bus(LINE_19200)
        with mock.patch.object(runner, "_enter_bootloader_from_application_line", enter_fails), \
                mock.patch.object(runner.time, "sleep", lambda s: None):
            err = runner._run_bootloader_flash_session(
                "/dev/COM3", _device(), threading.Event(), lambda m, lvl: None, lambda p, m: None,
                use_fast_modbus=False, is_wb_firmware=False, is_bootloader_firmware=False,
                duplicate_on_line=False, recovery=False, image=b"\0", file_signature=SIGNATURE,
                image_family="MR-02m",
            )
        self.assertIn("Прошивка не начата, запись не отправлялась", err)
        self.assertNotIn("мог остаться в загрузчике", err)


class TestApplicationPhaseHandsOverTheLine(unittest.TestCase):
    """The line reg 129 succeeded on travels to the bootloader open."""

    def _enter(self, device, answers_on):
        class _F:
            def __init__(self, line):
                self.line = line

            def read_holding_registers(self, addr, reg, count):
                if self.line != answers_on:
                    return None, "Таймаут ответа"
                if reg == 270:
                    return (SERIAL & 0xFFFF).to_bytes(2, "big") + (SERIAL >> 16).to_bytes(2, "big"), None
                return b"".join(bytes((0, c)) for c in SIGNATURE.encode().ljust(12, b"\0")), None

            def enter_bootloader_wb(self, addr):
                return None

        class _S:
            def close(self):
                pass

        family = mp.manifest_device_for_signature(SIGNATURE)
        entered = {}
        with mock.patch.object(runner, "_make_flasher", lambda p, b, par, st, c, l: (_F((b, par, st)), _S())):
            res = runner._enter_bootloader_from_application_line(
                "/dev/COM3", device, threading.Event(), lambda m, l: None,
                is_wb_firmware=False, use_fast_modbus=False, image_family=family, entered=entered,
            )
        return res, entered

    def test_enter_records_the_line_it_succeeded_on(self) -> None:
        res, entered = self._enter(_device(app_line_baud=19200, app_line_parity="N", app_line_stopbits=1), LINE_19200)
        self.assertIsNone(res[1], res[1])
        self.assertEqual(entered.get("line"), LINE_19200)

    def test_enter_records_the_fallback_line_when_the_first_one_was_silent(self) -> None:
        res, entered = self._enter(_device(app_line_baud=19200, app_line_parity="N", app_line_stopbits=1), LINE_115200)
        self.assertIsNone(res[1], res[1])
        self.assertEqual(entered.get("line"), LINE_115200)


if __name__ == "__main__":
    unittest.main()
