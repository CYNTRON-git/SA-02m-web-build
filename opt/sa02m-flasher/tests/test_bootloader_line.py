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


# ---------------------------------------------------------------------------------------------------------------
# The REAL search + probe + identity over a fake wire that records every frame (G-1, G-2, G-4).
# ---------------------------------------------------------------------------------------------------------------
FOREIGN_SERIAL = 0x0BADF00D


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


def _crc(frame: bytes) -> bytes:
    return runner.modbus_rtu.crc16_modbus(frame).to_bytes(2, "little")


class _Wire:
    """What answers at 247 on which line: {line: (serial, signature)}; records every frame that leaves."""

    def __init__(self, devices):
        self.devices = devices
        self.frames = []     # (line, frame bytes)

    @staticmethod
    def _words(serial, signature):
        words = {270: serial & 0xFFFF, 271: serial >> 16}
        for i, c in enumerate(signature.encode().ljust(12, b"\0")):
            words[290 + i] = c
        return words

    def _read_pdu(self, start, count, serial, signature):
        words = self._words(serial, signature)
        return bytes([0x03, 2 * count]) + b"".join(words.get(start + i, 0).to_bytes(2, "big") for i in range(count))

    def send_receive(self, ser, request, response_timeout_ms=0, cancel_check=None, **kw):
        self.frames.append((ser.line, bytes(request)))
        dev = self.devices.get(ser.line)
        if dev is None:
            return None
        serial, signature = dev
        if request[0] == 0xFD and request[1] == 0x46 and request[2] == 0x08:      # by serial
            if int.from_bytes(request[3:7], "big") != serial:
                return None
            inner = request[7:-2]
            if inner[0] != 0x03:
                return None
            start, count = int.from_bytes(inner[1:3], "big"), int.from_bytes(inner[3:5], "big")
            body = bytes([0xFD, 0x46, 0x09]) + request[3:7] + self._read_pdu(start, count, serial, signature)
            return body + _crc(body)
        if request[0] == 247 and request[1] == 0x03:                               # by address
            start, count = int.from_bytes(request[2:4], "big"), int.from_bytes(request[4:6], "big")
            body = bytes([247]) + self._read_pdu(start, count, serial, signature)
            return body + _crc(body)
        return None

    def only_reads(self):
        """Every frame that left is a read: FC03 to 247, the 0xFD 0x46 0x08 read by serial, the 0x04 scan-end prime."""
        bad = []
        for line, f in self.frames:
            if f[0] == 0xFD and f[1] == 0x46:
                ok = f[2] == 0x04 or (f[2] == 0x08 and f[7] == 0x03)
            else:
                ok = f[0] == 247 and f[1] == 0x03
            if not ok:
                bad.append((line, f.hex()))
        return bad


class _WirePort:
    def __init__(self, line, tracker):
        self.line = line
        self.closed = False
        tracker.append(self)

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _run_wire(devices, *, use_fast=False, duplicate=False, app_line=LINE_19200, family="MR-02m",
              enter_results=None):
    """The real session with the real search/probe/identity; only the serial port and the clock are fake."""
    wire = _Wire(devices)
    ports, flashed = [], []
    clock = _Clock()
    logs = []

    def open_serial(path, *, is_wb_firmware, line=None):
        return _WirePort(line or LINE_115200, ports)

    calls = {"n": 0}

    def enter(device_path, device, cancel_evt, log_cb, **kw):
        calls["n"] += 1
        if enter_results is not None and calls["n"] > len(enter_results):
            return None, f"{runner.IDENTITY_NO_ANSWER} адр.5: таймаут чтения серийного номера на линиях 19200 N1, 115200 N1. Прошивка не начата, запись не отправлялась."
        entered = kw.get("entered")
        if entered is not None:
            entered["line"] = app_line
        return (SERIAL, mp.manifest_device_for_signature(SIGNATURE)), None

    def flash_one(flasher, device, image, file_signature, **kw):
        flashed.append(kw["ser"].line)
        return None

    with mock.patch.object(runner, "_open_bootloader_serial", open_serial), \
            mock.patch.object(runner, "_enter_bootloader_from_application_line", enter), \
            mock.patch.object(runner, "send_receive", wire.send_receive), \
            mock.patch.object(runner, "_flash_one_device", flash_one), \
            mock.patch.object(runner.time, "perf_counter", clock.perf_counter), \
            mock.patch.object(runner.time, "sleep", clock.sleep):
        err = runner._run_bootloader_flash_session(
            "/dev/COM3", _device(), threading.Event(), lambda m, lvl: logs.append((lvl, m)),
            lambda pct, msg: None,
            use_fast_modbus=use_fast, is_wb_firmware=False, is_bootloader_firmware=False,
            duplicate_on_line=duplicate, recovery=False, image=b"\0" * 16, file_signature=SIGNATURE,
            image_family=family,
        )
    return err, wire, ports, flashed, logs


class TestProbeIsReadOnly(unittest.TestCase):
    """G-1 — the claim «read-only before the bootloader is found and identified», measured on the wire."""

    def test_silent_bus_only_reads_leave_on_both_lines(self) -> None:
        err, wire, ports, flashed, _ = _run_wire({})
        self.assertEqual(wire.only_reads(), [])
        lines = {line for line, _f in wire.frames}
        self.assertEqual(lines, {LINE_19200, LINE_115200}, "both candidate lines must have been probed")
        self.assertTrue(wire.frames, "vacuous: nothing was sent")
        self.assertEqual(flashed, [])
        self.assertIn("мог остаться в загрузчике", err)

    def test_bootloader_on_115200_found_with_reads_only(self) -> None:
        err, wire, ports, flashed, _ = _run_wire({LINE_115200: (SERIAL, SIGNATURE)})
        self.assertIsNone(err, err)
        self.assertEqual(wire.only_reads(), [])
        self.assertEqual(flashed, [LINE_115200])
        self.assertEqual(wire.frames[0][0], LINE_19200, "the application's line is looked at first")

    def test_by_serial_under_duplicate_and_fast_sends_the_prime_and_reads_only(self) -> None:
        err, wire, ports, flashed, _ = _run_wire({LINE_19200: (SERIAL, SIGNATURE)}, use_fast=True, duplicate=True)
        self.assertIsNone(err, err)
        self.assertEqual(wire.only_reads(), [])
        primes = [f for _l, f in wire.frames if f[:3] == b"\xfd\x46\x04"]
        self.assertEqual(len(primes), 2, "one prime before the probe and one before the identity read")
        # the second prime really precedes the first read of the serial register (270 = 0x010E)
        idx_prime = max(i for i, (_l, f) in enumerate(wire.frames) if f[:3] == b"\xfd\x46\x04")
        idx_serial = next(i for i, (_l, f) in enumerate(wire.frames)
                          if f[:3] == b"\xfd\x46\x08" and f[8:10] == b"\x01\x0e")
        self.assertLess(idx_prime, idx_serial)
        self.assertTrue(any(f[:3] == b"\xfd\x46\x08" and f[7] == 0x03 for _l, f in wire.frames))
        self.assertEqual(flashed, [LINE_19200])


class TestForeignDeviceAt247(unittest.TestCase):
    """G-2 — another module (or an application at 247) answers on the first line; the target sits elsewhere."""

    def test_foreign_on_the_application_line_target_on_115200_is_still_found(self) -> None:
        err, wire, ports, flashed, logs = _run_wire(
            {LINE_19200: (FOREIGN_SERIAL, SIGNATURE), LINE_115200: (SERIAL, SIGNATURE)})
        self.assertIsNone(err, err)
        self.assertEqual(flashed, [LINE_115200], "the target in a 115200 bootloader was never looked for")
        self.assertEqual(wire.only_reads(), [])
        self.assertTrue(any("не целевой" in m for _lvl, m in logs), "the refusal of the foreign module is not logged")

    def test_foreign_on_both_lines_fails_with_the_left_in_bootloader_note_and_flashes_nothing(self) -> None:
        err, wire, ports, flashed, _ = _run_wire(
            {LINE_19200: (FOREIGN_SERIAL, SIGNATURE), LINE_115200: (FOREIGN_SERIAL, SIGNATURE)})
        self.assertEqual(flashed, [])
        self.assertEqual(wire.only_reads(), [])
        self.assertIn("мог остаться в загрузчике", err)
        self.assertNotIn("не совпал", err, "the foreign module's mismatch text must not replace the D36b-02 note")

    def test_by_serial_path_is_unaffected_by_a_foreign_module_at_247(self) -> None:
        err, wire, ports, flashed, _ = _run_wire(
            {LINE_19200: (SERIAL, SIGNATURE), LINE_115200: (FOREIGN_SERIAL, SIGNATURE)}, use_fast=True)
        self.assertIsNone(err, err)
        self.assertEqual(flashed, [LINE_19200])
        self.assertEqual(wire.only_reads(), [])


class TestPortHygiene(unittest.TestCase):
    """G-4 — a probe or an identity read that raises must not leak the port opened for it."""

    def _search(self, **patches):
        ports = []

        def open_serial(path, *, is_wb_firmware, line=None):
            return _WirePort(line, ports)

        confirm = patches.pop("confirm", None)
        with mock.patch.object(runner, "_open_bootloader_serial", open_serial), \
                mock.patch.object(runner, "_wait_bootloader_ready_on_serial", patches["wait"]):
            try:
                runner._open_bootloader_on_candidates(
                    "/dev/COM3", [LINE_19200, LINE_115200], threading.Event(), lambda m, l: None,
                    prime_fast=False, probe_serial=None, probe_addr=247, confirm=confirm,
                )
            except OSError:
                pass
            else:
                self.fail("the search swallowed the I/O error")
        return ports

    def test_a_raising_probe_closes_its_port(self) -> None:
        def wait(ser, cancel_evt, log_cb, **kw):
            raise OSError("pyserial: device disconnected")

        ports = self._search(wait=wait)
        self.assertEqual(len(ports), 1)
        self.assertTrue(all(p.closed for p in ports), "the port opened for the probe leaked")

    def test_a_raising_identity_read_closes_its_port(self) -> None:
        def wait(ser, cancel_evt, log_cb, **kw):
            return True, None

        def confirm(port):
            raise OSError("pyserial: write failed")

        ports = self._search(wait=wait, confirm=confirm)
        self.assertTrue(all(p.closed for p in ports), "the port opened for the identity read leaked")

    def test_every_port_but_the_returned_one_is_closed_after_a_silent_search(self) -> None:
        ports = []

        def open_serial(path, *, is_wb_firmware, line=None):
            return _WirePort(line, ports)

        with mock.patch.object(runner, "_open_bootloader_serial", open_serial), \
                mock.patch.object(runner, "_wait_bootloader_ready_on_serial", lambda *a, **k: (False, "Таймаут ответа")):
            ser, line, ready, err, ident = runner._open_bootloader_on_candidates(
                "/dev/COM3", [LINE_19200, LINE_115200], threading.Event(), lambda m, l: None,
                prime_fast=False, probe_serial=None, probe_addr=247,
            )
        self.assertFalse(ready)
        self.assertIs(ser, ports[-1])
        self.assertTrue(all(p.closed for p in ports[:-1]), "a probe port leaked")
        self.assertFalse(ser.closed)


class TestReentryNoAnswer(unittest.TestCase):
    """G-6 — the «код 1» re-entry finds no application answer, but reg 129 was already written in this job."""

    def test_text_does_not_claim_that_nothing_was_written(self) -> None:
        bus = _Bus(("never", "N", 1))
        enters = {"n": 0}

        def enter(device_path, device, cancel_evt, log_cb, **kw):
            enters["n"] += 1
            if enters["n"] == 1:
                kw["entered"]["line"] = LINE_19200
                return (SERIAL, "MR-02m"), None
            return None, (f"{runner.IDENTITY_NO_ANSWER} адр.5: таймаут чтения серийного номера на линиях 19200 N1. "
                          "Прошивка не начата, запись не отправлялась. Проверьте скорость линии модуля и адрес.")

        def wait(ser, cancel_evt, log_cb, **kw):
            return False, "Исключение Modbus: код 1"

        with mock.patch.object(runner, "_open_bootloader_serial", bus.open_serial), \
                mock.patch.object(runner, "_enter_bootloader_from_application_line", enter), \
                mock.patch.object(runner, "_wait_bootloader_ready_on_serial", wait), \
                mock.patch.object(runner.time, "sleep", lambda s: None):
            err = runner._run_bootloader_flash_session(
                "/dev/COM3", _device(), threading.Event(), lambda m, l: None, lambda p, m: None,
                use_fast_modbus=False, is_wb_firmware=False, is_bootloader_firmware=False,
                duplicate_on_line=False, recovery=False, image=b"\0", file_signature=SIGNATURE,
                image_family="MR-02m",
            )
        self.assertEqual(enters["n"], 2, "the re-entry path did not run")
        self.assertIn("мог остаться в загрузчике", err)
        self.assertNotIn("запись не отправлялась", err)



if __name__ == "__main__":
    unittest.main()
