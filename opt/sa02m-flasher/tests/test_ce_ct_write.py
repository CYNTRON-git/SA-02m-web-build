# -*- coding: utf-8 -*-
"""CE-02m-3 «Настройка ТТ»: the firmware gate on the CT-ratio writes and the
EEPROM durability read-back after them.

WHAT THESE PIN, AND WHY EACH ONE EXISTS (firmware facts quoted from the
CE-02m-3 repo, read 2026-10-07):

* THE HANG. On firmware <= 1.0.7.4 a write to Holding 557-559 persisted the CT
  block to the EEPROM inline from the Modbus handler, which runs in interrupt
  context with SysTick frozen: the EEPROM delay never returns and the WWDG
  resets the meter (CE CHANGELOG.md, «## 1.0.7.5», items 1-2). The window used
  to send that write to any firmware. The pin is at the wire: on old firmware
  NO FC06 to 557-559 leaves the daemon, and the refusal carries the machine
  code `ce_fw_too_old_for_ct_write` plus the version it read.
* 553-556 ARE NOT GATED. Phase-loss / CT inversion went through
  `eeprom_update()` (a deferred main-loop write) already on v1.0.7.4
  (`git show v1.0.7.4:Core/Src/modbus_en_meter_cache_ce.c`, the 553..556
  branch), so they stay writable on every version — refusing them would take a
  working feature from every deployed old meter for no hazard.
* THE DURABILITY READ. From 1.0.7.5 the FC06 on 557-559 is ACKed BEFORE the
  EEPROM write; a failed persist shows only on Input 65519, whose sticky bits
  survive a later success. Procedure: write, wait ~2 s, read 65519, zero means
  it landed (CE MODBUS_VARIABLES.txt, «ЗАПИСЬ 175/176/177 и 557–559»). The fake
  meter below publishes a failure only once its deferred persist has run, so a
  read taken without the wait sees 0 and would report a false «persisted».

The fake speaks real RTU frames (the FakeCarelPlc idiom of
test_carel_config.py): every assertion is about what went out on the wire.
"""
from __future__ import annotations

import ast
import re
import struct
import textwrap
import types
import unittest
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from sa02m_flasher import device_config, modbus_rtu, module_profiles

_REPO = Path(__file__).resolve().parents[3]
_SERVICE_SRC = (Path(device_config.__file__).resolve().parent / "service.py").read_text(encoding="utf-8")
_FLASHER_JS = _REPO / "www" / "network_config" / "static" / "js" / "flasher.js"

CE_DEVICE = {
    "address": 14,
    "baudrate": 115200,
    "parity": "N",
    "stopbits": 1,
    "signature": "",
    "module_type": module_profiles.MP02_CE02M3,
}

# Deferred-persist lag of the model: the main loop reaches the CT block on the
# next free write-protect slot, «до ~1–2 с» per the firmware row.
PERSIST_LAG_S = 1.5


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += float(seconds)


class FakeCeMeter:
    """A CE-02m-3 slave speaking RTU frames, with the two firmware behaviours
    this suite is about: the pre-1.0.7.5 hang on a CT-ratio write, and the
    1.0.7.5+ deferred persist reported on Input 65519."""

    def __init__(self, version: Tuple[int, int, int, int], clock: FakeClock, *, persist_ok: bool = True,
                 sticky_before: int = 0, health_readable: bool = True) -> None:
        self.slave = CE_DEVICE["address"]
        self.version = version
        self.clock = clock
        self.persist_ok = persist_ok
        self.sticky_before = sticky_before & 0xFFFF
        self.health_readable = health_readable
        self.holding: Dict[int, int] = {553: 0, 554: 0, 555: 0, 556: 0, 557: 4000, 558: 4000, 559: 4000}
        for i, w in enumerate(version):
            self.holding[device_config.REG_APP_VERSION + i] = w & 0xFFFF
        self.input_regs: Dict[int, int] = {0: module_profiles.MP02_CE02M3}
        self.reads: List[Tuple[int, int, int, float]] = []   # (func, start, count, t)
        self.writes: List[Tuple[int, int, int, float]] = []  # (func, addr, value, t)
        self.resets = 0
        self._ct_write_at: Optional[float] = None

    @property
    def new_firmware(self) -> bool:
        return self.version >= (1, 0, 7, 5)

    def _crc(self, body: bytes) -> bytes:
        return struct.pack("<H", modbus_rtu.crc16_modbus(body))

    def _regs_reply(self, func: int, words: List[int]) -> bytes:
        data = b"".join(struct.pack(">H", w & 0xFFFF) for w in words)
        body = bytes([self.slave, func, len(data)]) + data
        return body + self._crc(body)

    def health_word(self) -> int:
        word = self.sticky_before
        if self._ct_write_at is not None and self.clock.now - self._ct_write_at >= PERSIST_LAG_S:
            if not self.persist_ok:
                # RUNTIME_FAIL | SEEN_FAIL, op class 5 (side blob: CT 226..233), streak 1.
                word |= 0x0030 | (5 << 8) | (1 << 12)
            else:
                word &= 0x0FFF  # a success resets the streak only; sticky bits stay
        return word

    def __call__(self, req: bytes, timeout_ms: int) -> Optional[bytes]:
        func = req[1]
        if func in (0x03, 0x04):
            start, count = struct.unpack(">HH", req[2:6])
            self.reads.append((func, start, count, self.clock.now))
            if start == device_config.INP_EEPROM_HEALTH and count == 1:
                if not self.new_firmware or not self.health_readable:
                    return None  # the register does not exist before 1.0.7.5
                return self._regs_reply(func, [self.health_word()])
            table = self.input_regs if func == 0x04 else self.holding
            if any(start + i not in table for i in range(count)):
                return None
            return self._regs_reply(func, [table[start + i] for i in range(count)])
        if func == 0x06:
            addr, value = struct.unpack(">HH", req[2:6])
            self.writes.append((func, addr, value, self.clock.now))
            if addr in (557, 558, 559):
                if not self.new_firmware:
                    self.resets += 1  # WWDG reset inside the write — no reply
                    return None
                self._ct_write_at = self.clock.now
                self.holding[addr] = 4000 if value == 0 else min(value, 20000)
            else:
                self.holding[addr] = value
            body = req[:6]
            return body + self._crc(body)
        return None

    def wire_writes(self) -> List[Tuple[int, int]]:
        return [(addr, value) for _f, addr, value, _t in self.writes]


def _run_write(meter: FakeCeMeter, clock: FakeClock, reg: int, value: int):
    closed: List[bool] = []
    with (
        patch.object(device_config, "_open_transport", return_value=(meter, lambda: closed.append(True))),
        patch.object(device_config.time, "sleep", side_effect=clock.sleep),
        patch.object(device_config, "snapshot_for_device", return_value={"kind": "ce", "snap": True}) as snap,
    ):
        try:
            result = device_config.write_allowed_holding("/dev/ttyS6", CE_DEVICE, reg, value)
        finally:
            meter.closed = bool(closed)
    return result, snap


class TestCeCtWriteGate(unittest.TestCase):
    def test_old_firmware_refuses_ct_ratio_write_before_the_wire(self) -> None:
        clock = FakeClock()
        meter = FakeCeMeter((1, 0, 7, 4), clock)
        for reg in (557, 558, 559):
            with self.subTest(reg=reg):
                with self.assertRaises(device_config.CeFirmwareTooOldForCtWrite) as cm:
                    _run_write(meter, clock, reg, 8000)
                exc = cm.exception
                self.assertIsInstance(exc, ValueError)  # route maps ValueError -> 400
                self.assertEqual(exc.error_code, "ce_fw_too_old_for_ct_write")
                self.assertEqual(exc.fw_version, "1.0.7.4")
                self.assertIn("1.0.7.4", str(exc))
                self.assertIn("1.0.7.5", str(exc))
                self.assertTrue(meter.closed)
        self.assertEqual(meter.wire_writes(), [], "no FC06 to the CT block may reach old firmware")
        self.assertEqual(meter.resets, 0)

    def test_unreadable_firmware_version_fails_closed(self) -> None:
        clock = FakeClock()
        meter = FakeCeMeter((1, 0, 7, 6), clock)
        for i in range(4):
            del meter.holding[device_config.REG_APP_VERSION + i]
        with self.assertRaises(device_config.CeFirmwareTooOldForCtWrite) as cm:
            _run_write(meter, clock, 557, 8000)
        self.assertEqual(cm.exception.fw_version, "—")
        self.assertEqual(meter.wire_writes(), [])

    def test_gate_ignores_the_scan_record_version(self) -> None:
        # The gate reads 320-323 live: a stale scan row claiming new firmware
        # must not open the write on a meter that answers 1.0.7.4.
        clock = FakeClock()
        meter = FakeCeMeter((1, 0, 7, 4), clock)
        device = dict(CE_DEVICE, app_version="1.0.7.6")
        with (
            patch.object(device_config, "_open_transport", return_value=(meter, lambda: None)),
            patch.object(device_config.time, "sleep", side_effect=clock.sleep),
            patch.object(device_config, "snapshot_for_device", return_value={}),
        ):
            with self.assertRaises(device_config.CeFirmwareTooOldForCtWrite):
                device_config.write_allowed_holding("/dev/ttyS6", device, 557, 8000)
        self.assertEqual(meter.wire_writes(), [])

    def test_phase_loss_and_inversion_stay_writable_on_old_firmware(self) -> None:
        clock = FakeClock()
        meter = FakeCeMeter((1, 0, 7, 4), clock)
        for reg in (553, 554, 555, 556):
            result, _snap = _run_write(meter, clock, reg, 1)
            self.assertNotIn("ce_persist", result)
        self.assertEqual(meter.wire_writes(), [(553, 1), (554, 1), (555, 1), (556, 1)])
        self.assertEqual(clock.sleeps, [], "no durability wait outside the CT block")
        self.assertFalse(any(s == device_config.INP_EEPROM_HEALTH for _f, s, _c, _t in meter.reads))


class TestCeCtPersistCheck(unittest.TestCase):
    def test_new_firmware_writes_then_reads_65519_after_the_wait(self) -> None:
        clock = FakeClock()
        meter = FakeCeMeter((1, 0, 7, 5), clock)
        result, snap = _run_write(meter, clock, 557, 8000)
        self.assertEqual(meter.wire_writes(), [(557, 8000)])
        health_reads = [(f, t) for f, s, _c, t in meter.reads if s == device_config.INP_EEPROM_HEALTH]
        self.assertEqual(len(health_reads), 1)
        write_t = meter.writes[0][3]
        self.assertGreaterEqual(health_reads[0][1] - write_t, 2.0, "65519 must be read ~2 s after the write")
        persist = result["ce_persist"]
        self.assertEqual(persist["status"], "persisted")
        self.assertEqual(persist["reg"], 557)
        self.assertEqual(persist["health_word"], 0)
        self.assertTrue(result["snap"])  # the fresh snapshot still rides the reply
        snap.assert_called_once()

    def test_failed_persist_reports_not_persisted(self) -> None:
        clock = FakeClock()
        meter = FakeCeMeter((1, 0, 7, 6), clock, persist_ok=False)
        result, _snap = _run_write(meter, clock, 558, 2000)
        persist = result["ce_persist"]
        self.assertEqual(persist["status"], "not_persisted")
        self.assertEqual(persist["health_word"], 0x1530)

    def test_prior_sticky_failure_is_not_a_confirmation(self) -> None:
        clock = FakeClock()
        meter = FakeCeMeter((1, 0, 7, 6), clock, sticky_before=0x0530)
        result, _snap = _run_write(meter, clock, 559, 4000)
        persist = result["ce_persist"]
        self.assertEqual(persist["status"], "unknown")
        self.assertEqual(persist["reason"], "prior_failure")
        self.assertEqual(persist["health_word"], 0x0530)

    def test_unreadable_health_word_is_unknown_not_persisted(self) -> None:
        clock = FakeClock()
        meter = FakeCeMeter((1, 0, 7, 6), clock, health_readable=False)
        result, _snap = _run_write(meter, clock, 557, 8000)
        persist = result["ce_persist"]
        self.assertEqual(persist["status"], "unknown")
        self.assertEqual(persist["reason"], "read_failed")
        self.assertIsNone(persist["health_word"])


class TestHoldingRouteNamesTheRefusal(unittest.TestCase):
    """The holding route answers the refusal as 400 with `error_code`, so the
    window keys on the code, not on the Russian text. service.py cannot load off
    Linux (grp/cgi): the shipped method is lifted with `ast`, the
    test_carel_config.py idiom."""

    def _handler(self):
        tree = ast.parse(_SERVICE_SRC)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_handle_device_config_holding")
        src = textwrap.dedent("\n".join(_SERVICE_SRC.splitlines()[node.lineno - 1 : node.end_lineno]))
        ns: Dict[str, Any] = {}
        exec("from __future__ import annotations\n" + src, ns)  # noqa: S102
        return ns["_handle_device_config_holding"], ns

    def _run(self, write_fn) -> Dict[str, Any]:
        handler, ns = self._handler()
        seen: Dict[str, Any] = {}

        class _Self:
            def _device_config_request(self, ctx, data):
                return "/dev/ttyS6", data["device"]

            def _run_device_config_modbus(self, ctx, port, device_path, fn):
                return fn()

        ns["_read_json_body"] = lambda _self: {"port": "COM6", "device": dict(CE_DEVICE), "reg": 557, "value": 8000}
        ns["_send_json"] = lambda _self, payload, status=HTTPStatus.OK: seen.update(sent=payload, status=status)
        ns["HTTPStatus"] = HTTPStatus
        ns["device_config"] = types.SimpleNamespace(
            write_allowed_holding=write_fn,
            CeFirmwareTooOldForCtWrite=device_config.CeFirmwareTooOldForCtWrite,
            CE_CT_WRITE_MIN_FW_TEXT=device_config.CE_CT_WRITE_MIN_FW_TEXT,
        )
        handler(_Self(), types.SimpleNamespace())
        return seen

    def test_refusal_is_400_with_the_named_code(self) -> None:
        def refuse(*_a):
            raise device_config.CeFirmwareTooOldForCtWrite("1.0.7.4")

        seen = self._run(refuse)
        self.assertEqual(seen["status"], HTTPStatus.BAD_REQUEST)
        self.assertFalse(seen["sent"]["ok"])
        self.assertEqual(seen["sent"]["error_code"], "ce_fw_too_old_for_ct_write")
        self.assertEqual(seen["sent"]["fw_version"], "1.0.7.4")
        self.assertEqual(seen["sent"]["min_fw"], "1.0.7.5")
        self.assertIn("1.0.7.4", seen["sent"]["error"])

    def test_success_passes_the_persist_verdict_through(self) -> None:
        seen = self._run(lambda *_a: {"kind": "ce", "ce_persist": {"reg": 557, "status": "persisted"}})
        self.assertEqual(seen["status"], HTTPStatus.OK)
        self.assertTrue(seen["sent"]["ok"])
        self.assertEqual(seen["sent"]["ce_persist"]["status"], "persisted")


class TestCeSnapshotCtWriteHint(unittest.TestCase):
    def test_snapshot_says_whether_the_ct_block_is_writable(self) -> None:
        cases = {"1.0.7.4": False, "1.0.7.5": True, "1.0.8.0": True, "—": False, "1.0.7.-1": False}
        for version, supported in cases.items():
            with self.subTest(version=version):
                with patch.object(device_config, "_read_regs", return_value=[]):
                    ce = device_config._read_ce_snapshot(object(), 14, app_version=version)
                self.assertEqual(ce["ct_write"]["supported"], supported)
                self.assertEqual(ce["ct_write"]["min_fw"], "1.0.7.5")


class TestCeSnapshotCurrentCeiling(unittest.TestCase):
    """Input 510-513 (A x1000, uint16) saturate at 65534 = 65.534 A; past it only
    the x10 twin 514-517 carries the current, and only from fw 1.0.7.5 (before,
    the twin was derived from the clamped value and saturated with it) — CE
    CHANGELOG «## 1.0.7.5», MODBUS_VARIABLES.txt rows 510-517. Same rule as the
    bridge's `_current_amps` (bridge_dtv_ce.py). 514-517 already ride the
    48-register block read at 500, so no extra request is made."""

    def _live(self, app_version: str, i_ma: List[int], i_x10: List[int]) -> Dict[str, float]:
        regs = [0] * device_config.CE_INPUT_COUNT
        regs[10:14] = i_ma
        regs[14:18] = i_x10
        calls: List[Tuple[int, int, bool]] = []

        def fake_read_regs(send, slave, start, count, *, input_regs=False, timeout_ms=0):
            calls.append((start, count, input_regs))
            if input_regs and start == device_config.CE_INPUT_START:
                return list(regs)
            return []

        with patch.object(device_config, "_read_regs", side_effect=fake_read_regs):
            ce = device_config._read_ce_snapshot(object(), 14, app_version=app_version)
        self.assertEqual([c for c in calls if c[2]], [(500, 48, True)], "no extra read for 514-517")
        return ce["live"]

    def test_ceiling_reads_the_x10_twin_on_new_firmware(self) -> None:
        live = self._live("1.0.7.5", [65534, 1234, 65534, 0], [700, 12, 1234, 0])
        self.assertEqual(live["ia"], 70.0)
        self.assertEqual(live["ib"], 1.234)  # below the ceiling the x1000 word wins
        self.assertEqual(live["ic"], 123.4)
        self.assertEqual(live["in"], 0.0)

    def test_old_firmware_keeps_the_ceiling(self) -> None:
        live = self._live("1.0.7.4", [65534, 0, 0, 0], [655, 0, 0, 0])
        self.assertEqual(live["ia"], 65.534)

    def test_unknown_firmware_keeps_the_ceiling(self) -> None:
        live = self._live("—", [65534, 0, 0, 0], [700, 0, 0, 0])
        self.assertEqual(live["ia"], 65.534)


class TestCeWindowCtFallback(unittest.TestCase):
    """The window's fallback for an absent CT ratio is the firmware factory value
    (K×1000 = 4000; a write of 0 lands as 4000 — CE MODBUS_VARIABLES.txt row
    557), never 1000 and never 1 (K = 0.001 would scale every reading to ~0)."""

    def _ce_block(self, name: str) -> str:
        src = _FLASHER_JS.read_text(encoding="utf-8")
        start = src.index(f"function {name}(")
        depth = 0
        for i in range(src.index("{", start), len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    return src[start : i + 1]
        self.fail(f"{name} not closed in flasher.js")

    def test_render_fallback_is_factory_default(self) -> None:
        body = self._ce_block("renderCeSettingsTab")
        fallbacks = re.findall(r"cfg\.kt_[abc]\s*\?\?\s*(\w+)", body)
        self.assertEqual(len(fallbacks), 3, "three CT-ratio inputs expected")
        self.assertEqual(set(fallbacks), {"CE_CT_RATIO_DEFAULT"})
        js_default = re.search(r"const CE_CT_RATIO_DEFAULT\s*=\s*(\d+)\s*;", _FLASHER_JS.read_text(encoding="utf-8"))
        self.assertIsNotNone(js_default)
        self.assertEqual(int(js_default.group(1)), device_config.CE_CT_RATIO_DEFAULT)
        self.assertEqual(device_config.CE_CT_RATIO_DEFAULT, 4000)

    def test_save_fallback_is_factory_default(self) -> None:
        body = self._ce_block("saveCeSettings")
        kt = re.findall(r"cfg-ce-kt-[abc]'\)\.value,\s*10\)\s*\|\|\s*(\w+)", body)
        self.assertEqual(len(kt), 3)
        self.assertEqual(set(kt), {"CE_CT_RATIO_DEFAULT"})


if __name__ == "__main__":
    unittest.main()
