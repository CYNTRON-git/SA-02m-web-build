# -*- coding: utf-8 -*-
"""MTDx62-MB: device_config dispatch and the flasher-scan recognition.

The dispatch half: an MTD scan row (signature «MTDx62-MB») is routed to
mtd_config on the EXISTING routes (/device_config/snapshot, /holding, /network)
and never through the MR identity block (holding 270/290/320/330), which the
sensor does not have; the coil and bus-mode paths refuse it. The scan half:
the bench dumps come out as an MTD row instead of an MR-02m «module in a
bootloader» — RED on main's scanner, which listed exactly that. Bus model:
test_mtd_config.FakeMtd / Bus (real register dumps, modelled line); the scan
cases decode real request PDUs. Nothing touches a serial port.
"""
from __future__ import annotations

import struct
import unittest
from pathlib import Path
from unittest import mock

from sa02m_flasher import device_config, modbus_rtu, module_profiles, mtd_config, scanner
from tests.test_mtd_config import (
    DEVICE, HR_BAUD, REAL_HR, REAL_IR, Bus, FakeMtd, TemplateEnv, _payload, fake_at,
)

ROOT = Path(__file__).resolve().parents[1]
CAREL_FIXTURES = ROOT.parent / "sa02m-carel" / "tests" / "fixtures"

BENCH_IR = [1, 3682, 141, 1, 1, 260, 0, 0]
BENCH_HR = [600, 60, 10, 30, 7, 7, 60, 20, 19200, 0, 2, 0, 600, 1]


class TestDispatch(TemplateEnv):
    """device_config routes an MTD scan row to mtd_config — never through the
    MR identity block (holding 270/290/320/330), which the sensor does not have."""

    def _patched(self, conn):
        return mock.patch.object(device_config, "_open_transport",
                                 lambda path, line: conn.open_conn(line))

    def _no_identity(self):
        return mock.patch.object(device_config, "_read_live_identity",
                                 side_effect=AssertionError("identity read"))

    def assert_inside_the_sensor_map(self, dev):
        self.assertTrue(dev.reads)
        for _is_ir, start, count in dev.reads:
            self.assertLessEqual(start + count, mtd_config.HR_COUNT, (start, count))

    def test_snapshot_for_an_mtd_skips_the_live_identity_block(self):
        dev = FakeMtd()
        with self._patched(dev), dev.patch(), self._no_identity():
            snap = device_config.snapshot_for_device("/dev/x", dict(DEVICE), active_tab="mtd")
        self.assertEqual(snap["kind"], "mtd")
        self.assertEqual(snap["active_tab"], "mtd")
        self.assert_inside_the_sensor_map(dev)

    def test_holding_write_returns_the_read_back_and_a_fresh_snapshot(self):
        dev = FakeMtd()
        with self._patched(dev), dev.patch(), self._no_identity():
            snap = device_config.write_allowed_holding("/dev/x", dict(DEVICE), 4, 5)
        self.assertEqual(snap["write"], {"reg": 4, "written": 5, "read_back": 5, "applied": True})
        fields = {f["reg"]: f for f in snap["mtd"]["settings"]}
        self.assertEqual(fields[4]["value"], "5")
        self.assert_inside_the_sensor_map(dev)

    def test_an_out_of_range_holding_write_sends_no_frame_at_all(self):
        dev = FakeMtd()
        with self._patched(dev), dev.patch(), self._no_identity():
            with self.assertRaises(ValueError):
                device_config.write_allowed_holding("/dev/x", dict(DEVICE), 4, 10)
            with self.assertRaises(ValueError):
                device_config.write_allowed_holding("/dev/x", dict(DEVICE), 10, 1)
        self.assertEqual((dev.writes, dev.reads), ([], []))

    def test_a_forged_signature_cannot_route_a_write_into_a_foreign_device(self):
        """The scan row is the browser's: the sensor is re-verified live before
        the write. A device whose HR7..9 are not an MTD link triple gets nothing."""
        dev = FakeMtd()
        dev.hr[HR_BAUD] = 1234
        with self._patched(dev), dev.patch(), self._no_identity():
            with self.assertRaisesRegex(RuntimeError, "не опознан"):
                device_config.write_allowed_holding("/dev/x", dict(DEVICE), 4, 5)
        self.assertEqual(dev.writes, [])

    def test_the_coil_and_bus_mode_paths_refuse_an_mtd(self):
        dev = FakeMtd()
        with self._patched(dev), dev.patch(), self._no_identity():
            with self.assertRaises(ValueError):
                device_config.write_allowed_coil("/dev/x", dict(DEVICE), 1, True)
            with self.assertRaises(ValueError):
                device_config.write_bus_mode("/dev/x", dict(DEVICE), 1)
        self.assertEqual((dev.writes, dev.reads), ([], []))

    def test_the_measured_silent_change_reconnects_on_the_new_line(self):
        """1 @9600 → 20 @19200 on a sensor that sends no FC06 reply: the fresh
        snapshot is read where the sensor now is (addr 20 @19200)."""
        dev = FakeMtd("live", reply=False)
        with self._patched(dev), dev.patch(), self._no_identity():
            snap = device_config.apply_network_settings(
                "/dev/x", dict(DEVICE), {"address": 20, "baudrate": 19200, "parity": "N"})
        self.assertIsNone(snap["network_apply"]["error"])
        self.assertEqual((snap["info"]["address"], snap["info"]["line"]["baudrate"]), (20, 19200))
        self.assertEqual(snap["network"]["address"], 20)
        self.assertEqual(snap["kind"], "mtd")
        self.assert_inside_the_sensor_map(dev)

    def test_a_half_applied_change_names_the_error_and_the_real_line(self):
        dev = FakeMtd("live", reply=False, ignore={HR_BAUD})
        with self._patched(dev), dev.patch(), self._no_identity():
            snap = device_config.apply_network_settings(
                "/dev/x", dict(DEVICE), {"address": 20, "baudrate": 19200, "parity": "N"})
        self.assertTrue(snap["network_apply"]["error"])
        self.assertEqual((snap["info"]["address"], snap["info"]["line"]["baudrate"]), (20, 9600))

    def test_an_occupied_target_line_is_a_refusal_with_nothing_written(self):
        """The refusal reaches the route as RuntimeError (HTTP 409 with its
        text) — no snapshot, the window stays on the line it had."""
        a, b = fake_at(1, reply=False), fake_at(20, reply=False)
        bus = Bus([a, b])
        with self._patched(bus), bus.patch(), self._no_identity():
            with self.assertRaisesRegex(RuntimeError, "уже отвечает другое устройство"):
                device_config.apply_network_settings(
                    "/dev/x", dict(DEVICE), {"address": 20, "baudrate": 19200, "parity": "N"})
        self.assertEqual((a.writes, b.writes), ([], []))

    def test_network_goes_through_the_mtd_flow(self):
        """1200 baud is the sensor's own list, not the MR codes; parity E is
        refused by the MTD rule with zero frames."""
        dev = FakeMtd("restart")
        with self._patched(dev), dev.patch(), self._no_identity():
            snap = device_config.apply_network_settings(
                "/dev/x", dict(DEVICE), {"address": 3, "baudrate": 9600, "parity": "N"})
            self.assertEqual(dev.writes, [(7, 3)])
            self.assertTrue(snap["network_apply"]["applies_after_restart"])
            self.assertEqual(snap["kind"], "mtd")
            dev.reads.clear()
            with self.assertRaisesRegex(ValueError, "только N"):
                device_config.apply_network_settings(
                    "/dev/x", dict(DEVICE), {"address": 3, "baudrate": 9600, "parity": "E"})
        self.assertEqual(dev.reads, [])


class _ScanBus:
    """scanner._transact_raw stand-in: decodes the request PDU and answers from
    register banks. A request outside a bank is a Modbus exception — the
    scanner sees (None, None), as it does for a real exception reply."""

    def __init__(self, slave, hr=None, ir=None, fc17=None):
        self.slave, self.hr, self.ir, self.fc17 = slave, hr, ir, fc17
        self.requests = []

    def __call__(self, req, port, slave, *a, **k):
        func = req[1]
        if func in (0x03, 0x04):
            start, count = (req[2] << 8) | req[3], (req[4] << 8) | req[5]
            self.requests.append(("h" if func == 0x03 else "i", start, count))
            bank = self.hr if func == 0x03 else self.ir
            words = bank[start:start + count] if bank is not None else []
            if slave != self.slave or len(words) != count:
                return (None, None)
            return (slave, _payload(words))
        self.requests.append(("fc%d" % func, None, None))
        if func == 0x11 and self.fc17 is not None and slave == self.slave:
            return (slave, self.fc17)
        return (None, None)


class TestFlasherScan(unittest.TestCase):
    """The flasher scan names an MTD row. RED on main: the sensor answers holding
    0 but has no serial at 270-271, so it was listed as an MR-02m module in a
    bootloader — no signature, offered firmware."""

    def _scan(self, bus, address, baud):
        with mock.patch.object(scanner, "open_port", lambda *a, **k: None), \
                mock.patch.object(scanner, "_transact_raw", bus):
            return scanner.scan_address("COM3", address, [(baud, "N", 1)])

    def test_the_bench_dump_at_address_20_on_19200_is_an_mtd_row(self):
        for label, inputs in (("occupied", BENCH_IR),
                              ("empty room", [0, 2992, 0, 1, 1, 260, 0, 0])):
            dev = self._scan(_ScanBus(20, BENCH_HR, inputs), 20, 19200)
            self.assertIsNotNone(dev, label)
            self.assertEqual((dev.signature, dev.app_version, dev.in_bootloader,
                              dev.bootloader_version),
                             ("MTDx62-MB", "1.1.4", False, "—"), label)

    def test_the_pc_dump_at_address_1_on_9600_is_an_mtd_row(self):
        bus = _ScanBus(1, REAL_HR, REAL_IR)
        dev = self._scan(bus, 1, 9600)
        self.assertEqual((dev.signature, dev.app_version), ("MTDx62-MB", "1.1.4"))
        # Recognised before the Carel probe: no FC17 frame for a sensor.
        self.assertNotIn(("fc17", None, None), bus.requests)
        self.assertIn(("h", 7, 3), bus.requests)
        self.assertIn(("i", 4, 2), bus.requests)

    def test_an_mtd_row_is_never_a_flash_target(self):
        dev = self._scan(_ScanBus(20, BENCH_HR, BENCH_IR), 20, 19200)
        self.assertFalse(module_profiles.is_mp_module_signature_for_batch_flash(dev.signature))
        self.assertFalse(scanner.device_is_mp02_product_line_for_config(dev))

    def test_a_carel_plc_is_still_a_carel_row(self):
        """The c.pCOmini bench capture: HR0 answers, HR7..9 is an exception, so
        the MTD probe stops after one frame and FC17 names the PLC."""
        frame = bytes.fromhex((CAREL_FIXTURES / "fc17_probe2.hex").read_text(encoding="ascii").strip())
        _addr, fc17, err = modbus_rtu.parse_response(frame, expected_slave=1)
        self.assertIsNone(err)
        bus = _ScanBus(1, [0, 0], None, fc17=fc17)
        dev = self._scan(bus, 1, 19200)
        self.assertEqual(dev.signature, "CRSTDrAHAQ")
        self.assertFalse(dev.in_bootloader)
        self.assertEqual([r for r in bus.requests if r[0] == "i"], [])

    def test_a_carel_uaria_bank_is_not_an_mtd(self):
        """IR0..5 as recorded from the uAria (float words) behind an MTD-looking
        holding bank: input 3 is no status code."""
        def words(f):
            hi, lo = struct.unpack(">HH", struct.pack(">f", f))
            return [hi, lo]

        ir = words(0.0) + words(27.22) + words(43.59) + [0, 0]
        dev = self._scan(_ScanBus(1, REAL_HR, ir), 1, 19200)
        self.assertNotEqual(dev.signature, "MTDx62-MB")


class TestFingerprintGuards(unittest.TestCase):
    """scanner._apply_mtd_fingerprint alone: what it never probes."""

    def _probe(self, ir, hr, baud=9600, tcp_ep=None):
        calls = []

        def rd(kind, bank):
            def _read(port, slave, start, count, *a, **k):
                calls.append((kind, start, count))
                words = bank[start:start + count] if bank is not None else []
                return (slave, _payload(words)) if len(words) == count else (None, None)
            return _read

        base = scanner.DeviceInfo(address=1, baudrate=baud, parity="N", stopbits=1,
                                  signature="", app_version="—", bootloader_version="—",
                                  serial=0, in_bootloader=True)
        with mock.patch.multiple(scanner, _read_regs=rd("h", hr), _read_input_regs=rd("i", ir)):
            dev = scanner._apply_mtd_fingerprint(base, "COM1", 1, baud, "N", 1, tcp_ep=tcp_ep)
        return dev, calls

    def test_115200_and_tcp_get_no_probe(self):
        for kw in ({"baud": 115200}, {"tcp_ep": ("10.0.0.5", 502)}):
            dev, calls = self._probe(REAL_IR, REAL_HR, **kw)
            self.assertEqual((dev.signature, calls), ("", []), kw)

    def test_57600_is_probed(self):
        hr = list(REAL_HR)
        hr[HR_BAUD] = 57600
        dev, calls = self._probe(REAL_IR, hr, baud=57600)
        self.assertEqual(dev.signature, "MTDx62-MB")

    def test_a_failed_link_triple_skips_the_input_reads(self):
        hr = list(REAL_HR)
        hr[HR_BAUD] = 115200 & 0xFFFF
        dev, calls = self._probe(REAL_IR, hr)
        self.assertEqual(dev.signature, "")
        self.assertEqual(calls, [("h", 7, 3)])

    def test_the_probe_order_and_the_version_read(self):
        dev, calls = self._probe(REAL_IR, REAL_HR)
        self.assertEqual(calls, [("h", 7, 3), ("i", 0, 1), ("i", 3, 1), ("i", 4, 2)])
        self.assertEqual(dev.app_version, "1.1.4")

    def test_a_status_out_of_range_is_not_an_mtd(self):
        ir = list(REAL_IR)
        ir[3] = 7
        dev, _calls = self._probe(ir, REAL_HR)
        self.assertEqual(dev.signature, "")

    def test_a_lost_version_read_still_names_the_row(self):
        dev, _calls = self._probe(REAL_IR[:4], REAL_HR)
        self.assertEqual((dev.signature, dev.app_version, dev.in_bootloader),
                         ("MTDx62-MB", "—", False))

    def test_an_unexpected_probe_error_is_logged_not_swallowed(self):
        """Transport errors never reach here (_transact_raw returns (None,
        None)); anything that does is a defect — the row goes on unchanged,
        but the scan says why at WARNING."""
        base = scanner.DeviceInfo(address=1, baudrate=9600, parity="N", stopbits=1,
                                  signature="", app_version="—", bootloader_version="—",
                                  serial=0, in_bootloader=True)

        def boom(*_a, **_k):
            raise RuntimeError("probe defect")

        with mock.patch.object(scanner, "_read_regs", boom), \
                self.assertLogs(scanner.log, level="WARNING") as cm:
            dev = scanner._apply_mtd_fingerprint(base, "COM1", 1, 9600, "N", 1)
        self.assertIs(dev, base)
        self.assertIn("probe defect", "\n".join(cm.output))
        self.assertIn("COM1", "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
