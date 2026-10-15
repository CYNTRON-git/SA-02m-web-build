# -*- coding: utf-8 -*-
"""MTDx62-MB settings window backend, its device_config dispatch and the
flasher-scan recognition.

HONESTY LABEL: the register banks are dumps of a REAL unit (PC COM6, 9600 8N1
addr 1, and bench 1.135 COM3 19200 addr 20, both 2026-09-29). How the MTD takes
new link settings is MEASURED (1.0.6.69 docs/contracts/mtd-presence.md §1): an
FC06 to HR7 / HR8 applies AT ONCE, sends NO reply on the old line, and survives
a power cycle — the fake's "silent" mode. A settings-register FC06 replies
normally. The two other modes (replies and applies at once; applies only after
a power cycle) are robustness cases a different firmware could show. What
these tests prove: the daemon refuses every out-of-range / never-written /
off-template write BEFORE the bus, echoes the read-back, takes its ranges from
the shipped template, and after a link change always knows where the sensor
answers. The bus is modelled; nothing here touches a serial port.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sa02m_flasher import module_profiles, mtd_config  # noqa: E402

TEMPLATES_DIR = ROOT.parent / "sa02m-modbus-mqtt" / "templates"
TEMPLATE = TEMPLATES_DIR / mtd_config.TEMPLATE_FILE

REAL_IR = [1, 3471, 148, 1, 1, 260, 0, 0]
REAL_HR = [600, 60, 10, 30, 7, 7, 60, 1, 9600, 0, 2, 0, 600, 1]

HR_ID, HR_BAUD, HR_PARITY = mtd_config.HR_DEVICE_ID, mtd_config.HR_BAUD, mtd_config.HR_PARITY


def _payload(words):
    out = bytearray()
    for w in words:
        out += bytes([(w >> 8) & 0xFF, w & 0xFF])
    return bytes(out)


class TemplateEnv(unittest.TestCase):
    """Every test reads the SHIPPED template unless it points elsewhere."""

    def setUp(self):
        self.use_templates(TEMPLATES_DIR)

    def use_templates(self, path):
        patcher = mock.patch.dict(os.environ, {mtd_config.TEMPLATE_DIR_ENV: str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def template_copy(self, edit):
        """A temp dir holding the shipped template after `edit(channels)`."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        doc = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        edit(doc["device"]["channels"])
        Path(tmp, mtd_config.TEMPLATE_FILE).write_text(json.dumps(doc), encoding="utf-8")
        return tmp


class FakeMtd:
    """One sensor on a line. `applies`: "live" (link settings take effect on the
    write) or "restart" (only after power_cycle). `reply`: False for a LINK
    register models the measured MTD — the write is applied, no FC06 reply
    comes back ("silent"). `ignore`: registers whose write is accepted but not
    stored (a device refusing a value). `lose_after_write`: that many reads
    addressed to it after each write get no reply (a noisy line losing a
    frame); `lose` does the same for the next reads from now; `lose_as` is
    what such a read returns (silence, or a garbled "CRC" frame).
    `garbles_other_parity`: a frame on its address + baud but another parity
    still gets an answer, which the mismatched framing garbles (UNMEASURED —
    a firmware that does not check parity)."""

    def __init__(self, applies="live", reply=True, ignore=(), lose_after_write=0,
                 lose_as="Таймаут", garbles_other_parity=False):
        self.ir = list(REAL_IR)
        self.hr = list(REAL_HR)
        self.addr, self.baud, self.parity = 1, 9600, "N"
        self.applies = applies
        self.reply = reply
        self.ignore = set(ignore)
        self.lose_after_write = lose_after_write
        self.lose = 0
        self.lose_as = lose_as
        self.garbles_other_parity = garbles_other_parity
        self.writes = []
        self.reads = []

    def open_conn(self, line):
        conn = (int(line.get("address") or 1), int(line.get("baudrate") or 0),
                str(line.get("parity") or "N"))
        return conn, (lambda: None)

    def _answers(self, send, slave):
        _a, baud, parity = send
        return slave == self.addr and baud == self.baud and parity == self.parity

    def _read(self, bank, send, slave, start, count):
        self.reads.append((bank is self.ir, start, count))
        if (self.garbles_other_parity and slave == self.addr and send[1] == self.baud
                and send[2] != self.parity):
            return None, "CRC"
        if not self._answers(send, slave):
            return None, "Таймаут"
        if self.lose > 0:
            self.lose -= 1
            return None, self.lose_as
        if start + count > len(bank):
            return None, "Исключение Modbus: 2"
        return _payload(bank[start:start + count]), None

    def read_holding(self, send, slave, start, count, timeout_ms=500):
        return self._read(self.hr, send, slave, start, count)

    def read_input_regs(self, send, slave, start, count, timeout_ms=500):
        return self._read(self.ir, send, slave, start, count)

    def write_single(self, send, slave, reg, value, timeout_ms=500):
        if not self._answers(send, slave):
            return "Таймаут"
        self.writes.append((reg, value))
        self.lose = self.lose_after_write
        if reg not in self.ignore:
            self.hr[reg] = value
        if self.applies == "live":
            self.power_cycle()
        if not self.reply and reg in mtd_config.NETWORK_REGS:
            return "Таймаут"        # applied, but the reply never comes (measured)
        return None

    def power_cycle(self):
        self.addr = self.hr[HR_ID]
        self.baud = self.hr[HR_BAUD]
        self.parity = mtd_config.PARITY_REG_TO_CHAR[self.hr[HR_PARITY]]

    def patch(self):
        return mock.patch.multiple(mtd_config, read_holding=self.read_holding,
                                   read_input_regs=self.read_input_regs,
                                   write_single=self.write_single)


DEVICE = {"address": 1, "baudrate": 9600, "parity": "N", "stopbits": 1,
          "signature": "MTDx62-MB", "app_version": "1.1.4", "serial": 0}


def fake_at(addr, baud=9600, parity=0, **kw):
    dev = FakeMtd(**kw)
    dev.hr[HR_ID], dev.hr[HR_BAUD], dev.hr[HR_PARITY] = addr, baud, parity
    dev.power_cycle()
    return dev


class Bus:
    """Several devices on one RS-485 line. Every device addressed on its own
    address + baud + parity takes a write; replies from more than one device
    read as ONE frame when identical (the worst case — nothing tells them
    apart) and collide into a CRC error when they differ. `late`: devices that
    join the line after the first write."""

    def __init__(self, devs, late=()):
        self.devs = list(devs)
        self.late = list(late)

    def open_conn(self, line):
        return (int(line.get("address") or 1), int(line.get("baudrate") or 0),
                str(line.get("parity") or "N")), (lambda: None)

    def _read(self, name, send, slave, start, count):
        got = [getattr(d, name)(send, slave, start, count)
               for d in self.devs if d._answers(send, slave)]
        got = [g for g in got if g != (None, "Таймаут")]
        if not got:
            return None, "Таймаут"
        return got[0] if len({repr(g) for g in got}) == 1 else (None, "CRC")

    def read_holding(self, send, slave, start, count, timeout_ms=500):
        return self._read("read_holding", send, slave, start, count)

    def read_input_regs(self, send, slave, start, count, timeout_ms=500):
        return self._read("read_input_regs", send, slave, start, count)

    def write_single(self, send, slave, reg, value, timeout_ms=500):
        out = [d.write_single(send, slave, reg, value)
               for d in self.devs if d._answers(send, slave)]
        self.devs += self.late
        self.late = []
        if not out:
            return "Таймаут"
        return out[0] if len(set(out)) == 1 else "CRC"

    def patch(self):
        return mock.patch.multiple(mtd_config, read_holding=self.read_holding,
                                   read_input_regs=self.read_input_regs,
                                   write_single=self.write_single)


class TestIdentity(unittest.TestCase):
    def test_the_shipped_template_is_there(self):
        """Non-vacuity for every refusal below: with no template every write is
        refused, so an «out of range» refusal would pass for the wrong reason."""
        self.assertTrue(TEMPLATE.is_file(), TEMPLATE)

    def test_a_scan_row_is_an_mtd_by_its_model_signature(self):
        self.assertTrue(mtd_config.device_is_mtd(DEVICE))
        self.assertTrue(mtd_config.device_is_mtd({"signature": " mtdx62-mb "}))
        self.assertFalse(mtd_config.device_is_mtd({"signature": "6AI6AO"}))
        self.assertFalse(mtd_config.device_is_mtd({}))

    def test_an_mtd_is_never_a_flash_target(self):
        self.assertFalse(module_profiles.is_mp_module_signature_for_batch_flash("MTDx62-MB"))
        self.assertIsNone(module_profiles.code_from_signature("MTDx62-MB"))

    def test_mtd_link_has_zero_imports(self):
        """The bridge suite loads mtd_link.py by path, without this package."""
        import ast
        tree = ast.parse((ROOT / "sa02m_flasher" / "mtd_link.py").read_text(encoding="utf-8"))
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [])

    def test_version_and_status_decode(self):
        self.assertEqual(mtd_config.version_text(1, 260), "1.1.4")
        self.assertEqual(mtd_config.status_text(1), "норма")
        self.assertEqual(mtd_config.status_text(7), mtd_config.STATUS_UNKNOWN)


class TestSnapshot(TemplateEnv):
    def test_every_register_reaches_the_window(self):
        dev = FakeMtd()
        with dev.patch():
            snap = mtd_config.read_snapshot((1, 9600, "N"), 1, DEVICE)
        self.assertEqual(snap["kind"], "mtd")
        self.assertFalse(snap["bus_mode_supported"])
        self.assertEqual(snap["info"]["app_version"], "1.1.4")
        self.assertEqual(snap["info"]["radar_version"], "0.0.0")
        self.assertEqual(snap["info"]["line"], {"baudrate": 9600, "parity": "N", "stopbits": 1})
        self.assertEqual(snap["network"], {"address": 1, "baudrate": 9600, "parity": "N",
                                           "stopbits": 1, "writable": True,
                                           "bauds": [1200, 2400, 4800, 9600, 19200, 38400, 57600],
                                           "parities": ["N"]})
        live = snap["mtd"]["live"]
        self.assertEqual((live["presence"], live["illuminance_lux"], live["target_distance_m"]),
                         (1, 347.1, 1.48))
        self.assertEqual((live["device_status"], live["device_status_text"]), (1, "норма"))
        self.assertEqual(snap["mtd"]["active_reporting"], {"raw": 2, "on": False})
        # F5: HR12 / HR13 are shown, never offered as settings.
        self.assertEqual((snap["mtd"]["report_interval_s"], snap["mtd"]["mac_code"]), (600, 1))
        fields = {f["reg"]: f for f in snap["mtd"]["settings"]}
        self.assertEqual(sorted(fields), [0, 1, 2, 3, 4, 5, 6, 11])
        self.assertTrue(all(f["writable"] for f in fields.values()))
        self.assertEqual(fields[0]["value"], "6.00")
        self.assertEqual(fields[3]["value"], "30")
        self.assertEqual((fields[11]["min"], fields[11]["max"], fields[11]["value"]),
                         (0.0, 420.0, "0.0"))
        self.assertEqual(snap["mtd"]["never_written"], [7, 8, 9, 10])
        self.assertTrue(snap["mtd"]["template_ok"])

    def test_the_ranges_are_the_templates(self):
        doc = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        want = {c["address"]: (c["min"], c["max"]) for c in doc["device"]["channels"]
                if c["reg_type"] == "holding"}
        self.assertEqual(len(want), 7)
        dev = FakeMtd()
        with dev.patch():
            snap = mtd_config.read_snapshot((1, 9600, "N"), 1, DEVICE)
        got = {f["reg"]: (f["min"], f["max"]) for f in snap["mtd"]["settings"] if f["reg"] != 11}
        self.assertEqual(got, want)

    def test_the_snapshot_is_two_reads(self):
        dev = FakeMtd()
        with dev.patch():
            mtd_config.read_snapshot((1, 9600, "N"), 1, DEVICE)
        self.assertEqual(dev.reads, [(True, 0, 8), (False, 0, 14)])

    def test_a_silent_sensor_is_not_answered(self):
        dev = FakeMtd()
        with dev.patch():
            snap = mtd_config.read_snapshot((9, 9600, "N"), 9, DEVICE)
        self.assertFalse(snap["mtd"]["answered"])
        self.assertTrue(all(f["value"] is None for f in snap["mtd"]["settings"]))

    def test_a_missing_template_makes_every_setting_read_only(self):
        self.use_templates(tempfile.gettempdir() + "/no-such-mtd-template-dir")
        dev = FakeMtd()
        with dev.patch():
            snap = mtd_config.read_snapshot((1, 9600, "N"), 1, DEVICE)
        self.assertFalse(snap["mtd"]["template_ok"])
        self.assertTrue(snap["mtd"]["settings"])
        self.assertFalse(any(f["writable"] for f in snap["mtd"]["settings"]))


class TestWriteSetting(TemplateEnv):
    def _write(self, reg, raw, dev=None):
        dev = dev or FakeMtd()
        with dev.patch():
            res = mtd_config.write_setting((1, 9600, "N"), 1, reg, raw)
        return res, dev

    def _refused(self, reg, raw):
        dev = FakeMtd()
        with dev.patch():
            with self.assertRaises(ValueError, msg=(reg, raw)):
                mtd_config.write_setting((1, 9600, "N"), 1, reg, raw)
        self.assertEqual((dev.writes, dev.reads), ([], []), (reg, raw))

    def test_out_of_range_never_reaches_the_bus(self):
        for reg, raw in ((4, 10), (4, 0), (5, 10), (3, 4), (3, 1501),
                         (0, 59), (0, 1001), (1, 1001), (2, 501), (6, 1001),
                         (11, 4201), (0, -1)):
            self._refused(reg, raw)

    def test_a_non_integer_word_never_reaches_the_bus(self):
        for raw in (2.5, "100", None, True):
            self._refused(0, raw)

    def test_link_and_reporting_registers_are_written_by_nobody(self):
        for reg in (7, 8, 9, 10, 12, 13, 14, 290, -1):
            self._refused(reg, 1)

    def test_an_in_range_write_is_one_fc06_and_a_read_back(self):
        res, dev = self._write(0, 235)
        self.assertEqual(dev.writes, [(0, 235)])
        self.assertEqual(dev.reads, [(False, 0, 1)])
        self.assertEqual(res, {"reg": 0, "written": 235, "read_back": 235, "applied": True})

    def test_the_bounds_themselves_are_writable(self):
        for reg, raw in ((0, 60), (0, 1000), (3, 5), (3, 1500), (4, 1), (4, 9), (11, 0), (11, 4200)):
            res, dev = self._write(reg, raw)
            self.assertEqual(dev.writes, [(reg, raw)])

    def test_the_reply_is_what_the_device_holds(self):
        dev = FakeMtd()
        real = dev.write_single

        def clamping(send, slave, reg, value, timeout_ms=500):
            err = real(send, slave, reg, value, timeout_ms)
            dev.hr[reg] = 5
            return err

        dev.write_single = clamping
        res, _dev = self._write(4, 8, dev)
        self.assertEqual((res["written"], res["read_back"], res["applied"]), (8, 5, False))

    def test_the_light_threshold_is_writable(self):
        res, dev = self._write(11, 100)
        self.assertEqual(dev.writes, [(11, 100)])

    def test_the_range_follows_the_template(self):
        def narrow(channels):
            next(c for c in channels if c["address"] == 4)["max"] = 5
        self.use_templates(self.template_copy(narrow))
        self._refused(4, 6)
        _res, dev = self._write(4, 5)
        self.assertEqual(dev.writes, [(4, 5)])

    def test_a_missing_template_refuses_every_write(self):
        self.use_templates(tempfile.gettempdir() + "/no-such-mtd-template-dir")
        for reg, raw in ((0, 100), (4, 5), (11, 10)):
            self._refused(reg, raw)
        with self.assertRaisesRegex(ValueError, "Шаблон датчика не найден"):
            mtd_config.check_setting(11, 10)

    def test_an_unparsable_template_refuses_every_write(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        Path(tmp, mtd_config.TEMPLATE_FILE).write_text("{ not json", encoding="utf-8")
        self.use_templates(tmp)
        self._refused(0, 100)

    def test_a_channel_without_a_range_is_read_only(self):
        def drop(channels):
            ch = next(c for c in channels if c["address"] == 4)
            del ch["min"], ch["max"]
        self.use_templates(self.template_copy(drop))
        self._refused(4, 5)
        ok, rows = mtd_config.settings_table()
        self.assertTrue(ok)
        self.assertFalse(next(r for r in rows if r["reg"] == 4)["writable"])
        _res, dev = self._write(5, 5)               # the others stay writable
        self.assertEqual(dev.writes, [(5, 5)])

    def test_holding_10_is_refused_even_when_a_template_marks_it_writable(self):
        def add(channels):
            channels.append({"name": "active_reporting", "reg_type": "holding", "address": 10,
                             "format": "u16", "readonly": False, "min": 1, "max": 2})
            channels.append({"name": "slave_id", "reg_type": "holding", "address": 7,
                             "format": "u16", "readonly": False, "min": 1, "max": 247})
        self.use_templates(self.template_copy(add))
        self._refused(10, 2)
        self._refused(7, 5)

    def test_the_hard_deny_holds_even_if_the_settings_table_offers_it(self):
        """The executor guard is independent of every table above it."""
        rogue = [{"reg": reg, "name": "rogue", "title": "", "units": "", "scale": 1,
                  "min": 0, "max": 65535, "writable": True} for reg in (7, 8, 9, 10)]
        with mock.patch.object(mtd_config, "settings_table", return_value=(True, rogue)):
            for reg in (7, 8, 9, 10):
                self._refused(reg, 1)


class TestNetwork(TemplateEnv):
    def _apply(self, dev, network, device=None):
        with dev.patch():
            return mtd_config.apply_network(dict(device or DEVICE), network, dev.open_conn)

    def test_validation_before_any_bus_traffic(self):
        for net in ({"address": 0, "baudrate": 9600, "parity": "N"},
                    {"address": 248, "baudrate": 9600, "parity": "N"},
                    {"address": 2, "baudrate": 115200, "parity": "N"},
                    {"address": 2, "baudrate": 9601, "parity": "N"},
                    {"address": "a", "baudrate": 9600, "parity": "N"},
                    {"address": True, "baudrate": 9600, "parity": "N"},
                    {"address": 2, "baudrate": 9600, "parity": "X"}):
            dev = FakeMtd()
            with self.assertRaises(ValueError, msg=net):
                self._apply(dev, net)
            self.assertEqual((dev.writes, dev.reads), ([], []), net)

    def test_parity_odd_or_even_is_refused_with_zero_frames(self):
        """F1: the bridge opens every RS-485 handle 8N1 — the window moves the
        sensor TO N only, and a request for O/E never reaches the bus."""
        for parity in ("O", "E", "o", "even"):
            dev = FakeMtd()
            with self.assertRaisesRegex(ValueError, "только N", msg=parity):
                self._apply(dev, {"address": 2, "baudrate": 9600, "parity": parity})
            self.assertEqual((dev.writes, dev.reads), ([], []), parity)

    def test_a_device_that_is_not_an_mtd_is_refused_before_any_write(self):
        dev = FakeMtd()
        dev.hr[HR_BAUD] = 1234                       # not an MTD link triple
        with self.assertRaisesRegex(RuntimeError, "не опознан"):
            self._apply(dev, {"address": 2, "baudrate": 9600, "parity": "N"})
        self.assertEqual(dev.writes, [])

    def test_unchanged_settings_write_nothing(self):
        dev = FakeMtd()
        res = self._apply(dev, {"address": 1, "baudrate": 9600, "parity": "N"})
        self.assertEqual((dev.writes, res["applies_after_restart"], res["error"]), ([], False, None))

    def test_a_sensor_that_applies_at_once_is_reconnected_at_the_new_line(self):
        dev = FakeMtd("live")
        res = self._apply(dev, {"address": 2, "baudrate": 19200, "parity": "N"})
        self.assertEqual(dev.writes, [(7, 2), (8, 19200)])     # address first
        self.assertFalse(res["applies_after_restart"])
        self.assertEqual((res["device"]["address"], res["device"]["baudrate"],
                          res["device"]["parity"]), (2, 19200, "N"))

    def test_a_sensor_on_even_parity_is_moved_to_none(self):
        dev = fake_at(1, parity=2, reply=False)
        res = self._apply(dev, {"address": 1, "baudrate": 9600, "parity": "N"},
                          device=dict(DEVICE, parity="E"))
        self.assertEqual(dev.writes, [(9, 0)])
        self.assertIsNone(res["error"])
        self.assertEqual(res["device"]["parity"], "N")
        self.assertEqual(dev.parity, "N")

    def test_a_sensor_that_applies_after_power_cycle_is_named_so(self):
        dev = FakeMtd("restart")
        res = self._apply(dev, {"address": 2, "baudrate": 19200, "parity": "N"})
        self.assertEqual(dev.writes, [(7, 2), (8, 19200)])
        self.assertTrue(res["applies_after_restart"])
        self.assertEqual((res["device"]["address"], res["device"]["baudrate"]), (1, 9600))
        dev.power_cycle()
        self.assertEqual((dev.addr, dev.baud), (2, 19200))

    # ── the MEASURED behaviour: applied at once, no FC06 reply ──────────────
    def test_silent_address_change(self):
        dev = FakeMtd("live", reply=False)
        res = self._apply(dev, {"address": 20, "baudrate": 9600, "parity": "N"})
        self.assertEqual(dev.writes, [(7, 20)])
        self.assertIsNone(res["error"])
        self.assertFalse(res["applies_after_restart"])
        self.assertEqual((res["device"]["address"], res["device"]["baudrate"]), (20, 9600))

    def test_silent_baud_change(self):
        dev = FakeMtd("live", reply=False)
        res = self._apply(dev, {"address": 1, "baudrate": 19200, "parity": "N"})
        self.assertEqual(dev.writes, [(8, 19200)])
        self.assertEqual((res["device"]["address"], res["device"]["baudrate"]), (1, 19200))

    def test_silent_address_and_baud_together_end_where_the_sensor_is(self):
        """The bench change (1 @9600 → 20 @19200): both registers written, the
        window reconnects where the sensor now answers — never half-applied."""
        dev = FakeMtd("live", reply=False)
        res = self._apply(dev, {"address": 20, "baudrate": 19200, "parity": "N"})
        self.assertEqual(dev.writes, [(7, 20), (8, 19200)])
        self.assertEqual((dev.addr, dev.baud), (20, 19200))
        self.assertIsNone(res["error"])
        self.assertEqual((res["device"]["address"], res["device"]["baudrate"]), (20, 19200))
        self.assertEqual(res["written"], [(7, 20), (8, 19200)])

    def test_silent_address_baud_and_parity_to_none(self):
        dev = fake_at(1, parity=1, reply=False)
        res = self._apply(dev, {"address": 20, "baudrate": 19200, "parity": "N"},
                          device=dict(DEVICE, parity="O"))
        self.assertEqual(dev.writes, [(7, 20), (8, 19200), (9, 0)])
        self.assertEqual((res["device"]["address"], res["device"]["baudrate"],
                          res["device"]["parity"]), (20, 19200, "N"))

    def test_silent_and_deferred_is_named_so(self):
        """No reply AND not yet in force: the register holds the new value on the
        old line — «applies after restart», not an error."""
        dev = FakeMtd("restart", reply=False)
        res = self._apply(dev, {"address": 20, "baudrate": 19200, "parity": "N"})
        self.assertEqual(dev.writes, [(7, 20), (8, 19200)])
        self.assertTrue(res["applies_after_restart"])
        self.assertEqual((res["device"]["address"], res["device"]["baudrate"]), (1, 9600))

    def test_a_refused_second_register_reports_where_the_sensor_is(self):
        """Address applied, baud refused: the reply says so AND names the line the
        sensor answers on (20 @9600), so the window reconnects there."""
        dev = FakeMtd("live", reply=False, ignore={HR_BAUD})
        res = self._apply(dev, {"address": 20, "baudrate": 19200, "parity": "N"})
        self.assertTrue(res["error"])
        self.assertEqual((res["device"]["address"], res["device"]["baudrate"]), (20, 9600))
        self.assertEqual((dev.addr, dev.baud), (20, 9600))
        self.assertEqual(res["written"], [(7, 20)])

    def test_a_sensor_that_answers_nowhere_is_an_error(self):
        dev = FakeMtd("live")
        real = dev.write_single

        def vanish(send, slave, reg, value, timeout_ms=500):
            err = real(send, slave, reg, value, timeout_ms)
            dev.addr = 99                       # gone off the line
            return err

        dev.write_single = vanish
        with self.assertRaisesRegex(RuntimeError, "не отвечает ни на"):
            self._apply(dev, {"address": 2, "baudrate": 9600, "parity": "N"})

    def test_one_lost_frame_after_a_write_is_not_answers_nowhere(self):
        """A noisy line drops the first reply after each write: one retry per
        location probe finds the sensor where it is."""
        dev = FakeMtd("live", reply=False, lose_after_write=1)
        res = self._apply(dev, {"address": 20, "baudrate": 19200, "parity": "N"})
        self.assertIsNone(res["error"])
        self.assertEqual(dev.writes, [(7, 20), (8, 19200)])
        self.assertEqual((res["device"]["address"], res["device"]["baudrate"]), (20, 19200))

    def test_a_sensor_answering_a_foreign_parity_refuses_a_parity_change_safely(self):
        """A sensor on E that still answers its own address at N (garbled): the
        garbled reply reads as another device there, so the move to N is refused
        and nothing is written — fail-closed, never half-applied (UNMEASURED)."""
        dev = fake_at(1, parity=2, reply=False, garbles_other_parity=True)
        with self.assertRaisesRegex(RuntimeError, "адресе 1, 9600 N уже отвечает другое устройство"):
            self._apply(dev, {"address": 1, "baudrate": 9600, "parity": "N"},
                        device=dict(DEVICE, parity="E"))
        self.assertEqual(dev.writes, [])
        self.assertEqual((dev.addr, dev.baud, dev.parity), (1, 9600, "E"))

    def test_one_garbled_frame_after_a_write_is_retried_too(self):
        dev = FakeMtd("live", reply=False, lose_after_write=1, lose_as="CRC")
        res = self._apply(dev, {"address": 20, "baudrate": 19200, "parity": "N"})
        self.assertIsNone(res["error"])
        self.assertEqual((res["device"]["address"], res["device"]["baudrate"]), (20, 19200))

    def test_every_write_and_located_line_is_logged(self):
        dev = FakeMtd("live", reply=False)
        with self.assertLogs(mtd_config.log, level="INFO") as cm:
            with dev.patch():
                mtd_config.apply_network(dict(DEVICE), {"address": 20, "baudrate": 19200,
                                                        "parity": "N"}, dev.open_conn, port="/dev/COM3")
        text = "\n".join(cm.output)
        self.assertIn("/dev/COM3 адресе 1, 9600 N write HR7 = 20", text)
        self.assertIn("/dev/COM3 адресе 20, 9600 N write HR8 = 19200", text)
        self.assertIn("located on адресе 20, 19200 N", text)


class TestNetworkSharedBus(TemplateEnv):
    """The target line may already be taken. Nothing is written until every
    line the change passes through is known to be free, and the sensor is
    recognised on a line only by its whole link triple (HR7..HR9)."""

    BENCH = {"address": 20, "baudrate": 19200, "parity": "N"}

    def _apply(self, bus, network):
        with bus.patch():
            return mtd_config.apply_network(dict(DEVICE), network, bus.open_conn)

    def _refused(self, bus, network, occupied):
        with self.assertRaises(RuntimeError) as cm:
            self._apply(bus, network)
        for dev in bus.devs:
            self.assertEqual(dev.writes, [], "a refusal writes nothing to anybody")
        self.assertIn(occupied, str(cm.exception))
        return str(cm.exception)

    def test_a_refusing_sensor_never_hands_its_next_write_to_a_neighbour(self):
        a, b = fake_at(1, reply=False, ignore={HR_ID}), fake_at(20, reply=False)
        self._refused(Bus([a, b]), self.BENCH, "адресе 20, 9600 N")
        self.assertEqual(((a.addr, a.baud), (b.addr, b.baud)), ((1, 9600), (20, 9600)))

    def test_an_address_already_in_use_is_refused_before_any_write(self):
        a, b = fake_at(1, reply=False), fake_at(20, reply=False)
        msg = self._refused(Bus([a, b]), self.BENCH, "адресе 20, 9600 N")
        self.assertIn("другое устройство", msg)

    def test_a_neighbour_that_differs_in_one_register_is_refused_too(self):
        a, b = fake_at(1, reply=False), fake_at(20, reply=False)
        b.hr[3] = 99
        self._refused(Bus([a, b]), self.BENCH, "адресе 20, 9600 N")

    def test_a_device_on_the_new_baud_at_the_same_address_is_refused(self):
        a, b = fake_at(1, reply=False), fake_at(1, 19200, reply=False)
        self._refused(Bus([a, b]), {"address": 1, "baudrate": 19200, "parity": "N"},
                      "адресе 1, 19200 N")

    def test_the_final_line_is_checked_too(self):
        """Address + baud: the neighbour sits only on the END line (20 @19200)."""
        a, b = fake_at(1, reply=False), fake_at(20, 19200, reply=False)
        self._refused(Bus([a, b]), self.BENCH, "адресе 20, 19200 N")

    def test_a_foreign_device_on_the_target_line_is_refused_before_any_write(self):
        a, f = fake_at(1, reply=False, ignore={HR_ID}), fake_at(20)
        f.hr[HR_ID], f.hr[HR_BAUD] = 5, 1234   # not an MTD bank
        self._refused(Bus([a, f]), self.BENCH, "адресе 20, 9600 N")

    def test_a_device_answering_with_a_modbus_exception_is_there(self):
        """A foreign device with no HR7..HR9 answers «exception 2» — that is a
        device on the line, not a free line."""
        a, f = fake_at(1, reply=False), fake_at(20)
        f.hr = f.hr[:5]
        self._refused(Bus([a, f]), self.BENCH, "адресе 20, 9600 N")

    def test_two_devices_colliding_on_the_target_line_are_there(self):
        a, b, c = fake_at(1, reply=False), fake_at(20, reply=False), fake_at(20, reply=False)
        c.hr[HR_PARITY] = 1
        self._refused(Bus([a, b, c]), self.BENCH, "адресе 20, 9600 N")

    def test_a_noisy_neighbour_that_drops_one_frame_is_still_seen(self):
        a, b = fake_at(1, reply=False), fake_at(20, reply=False)
        b.lose = 1
        self._refused(Bus([a, b]), self.BENCH, "адресе 20, 9600 N")

    def test_neighbours_on_other_lines_are_left_alone(self):
        a, b, c = fake_at(1, reply=False), fake_at(5, reply=False), fake_at(20, 38400)
        res = self._apply(Bus([a, b, c]), self.BENCH)
        self.assertIsNone(res["error"])
        self.assertEqual((a.addr, a.baud), (20, 19200))
        self.assertEqual((b.writes, c.writes), ([], []))
        self.assertEqual((res["device"]["address"], res["device"]["baudrate"]), (20, 19200))

    def test_the_sensor_is_recognised_by_its_whole_link_triple(self):
        """A device that shows up on the new line only after the pre-check,
        holding HR7 = 20 but another baud in HR8, is not the sensor — the change
        stops with the sensor's real line (1 @9600) and the neighbour gets
        nothing."""
        a = fake_at(1, reply=False, ignore={HR_ID})
        late = fake_at(20)
        late.hr[HR_BAUD] = 38400                          # registers ≠ its line
        res = self._apply(Bus([a], late=[late]), self.BENCH)
        self.assertTrue(res["error"])
        self.assertEqual((res["device"]["address"], res["device"]["baudrate"]), (1, 9600))
        self.assertEqual(late.writes, [])
        self.assertEqual(a.writes, [(HR_ID, 20)])


if __name__ == "__main__":
    unittest.main()
