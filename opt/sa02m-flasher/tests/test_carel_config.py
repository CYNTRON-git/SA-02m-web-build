# -*- coding: utf-8 -*-
"""Config-window backend for a Carel AHU: what the snapshot reads, and what a
command writes.

WHAT THESE PIN, AND WHY EACH ONE EXISTS

* THE DEAD READS. A Carel PLC answers none of the CYNTRON identity/network
  registers (290, 270-271, 320, 330, 110-112, 122, 128). The generic snapshot
  path asks for all of them, so a Carel device costs eight consecutive timeouts
  — about five seconds inside a window that refreshes once a second. The pin is
  at the wire: every request the snapshot emits is decoded from its PDU, and a
  read that covers a forbidden address FAILS.
* THE BENCH VALUES. Both controllers were read on 2026-09-03 (192.168.1.135,
  COM3, 19200 8N1); the register tables below are those readings. The snapshot
  must decode them into exactly the physical values the Operator saw on the
  PLC's own display — int16 x10 for c.pCOmini, float32 ABCD for uAria.
* uARIA COIL 30. Coil 30 is the local-terminal switch of the uAria: writing it
  takes the unit away from its own keypad. No action may reach it. The pin walks
  EVERY action of both families and asserts no write frame carries address 30.
* THE START ORDER. c.pCOmini start is coil 130 (Ma18, network enable), a settle,
  then coil 65 (BMS run). Without the settle the PLC takes the run command
  before the enable is in force and the plant does not start — a failure with no
  error anywhere.

The register map itself is never restated here: every address comes from the
shared package through the flasher's one import seam, module_profiles.carel_ahu().
service.py is NOT imported (it needs `grp`/`cgi` and cannot load off Linux) —
the new route's handler is extracted with `ast` and executed against stubs, the
idiom of test_health_lease.py.
"""
from __future__ import annotations

import ast
import struct
import textwrap
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from sa02m_flasher import carel_poll, device_config, modbus_rtu, module_profiles

ca = module_profiles.carel_ahu()

_PKG = Path(__file__).resolve().parent.parent / "sa02m_flasher"
_SERVICE_SRC = (_PKG / "service.py").read_text(encoding="utf-8")

# CYNTRON identity + line registers. A Carel PLC answers none of them.
FORBIDDEN_REGS = (
    device_config.REG_SIGNATURE,      # 290
    device_config.REG_SERIAL_LO,      # 270
    device_config.REG_APP_VERSION,    # 320
    device_config.REG_BOOTLOADER_VER, # 330
    device_config.REG_NET_BAUD,       # 110
    device_config.REG_NET_PARITY,     # 111
    device_config.REG_NET_STOP,       # 112
    device_config.REG_FAST_MODBUS,    # 122
    device_config.REG_NET_ADDR,       # 128
)


def _f32_words(value: float) -> Tuple[int, int]:
    raw = struct.unpack(">I", struct.pack(">f", value))[0]
    return (raw >> 16) & 0xFFFF, raw & 0xFFFF


# ── the two controllers as they answered on the bench ───────────────────────
# c.pCOmini, slave 1, CRSTDrAHAQ, application 2.03.00.46, variant E.
CRST_IR: Dict[int, int] = {1: 0, 2: 265, 3: 0, 4: 751, 21: 100, 50: 272, 116: 1}
CRST_IR.update({addr: 0 for addr in range(301, 318)})
CRST_HR: Dict[int, int] = {49: 0, 50: 1, 51: 272, 52: 205, 53: 800, 54: 800}
CRST_COILS: Dict[int, bool] = {
    65: True, 66: False, 67: False,
    129: False, 130: True, 131: False, 132: False,
}
CRST_DI: Dict[int, bool] = {9: True, 95: True, 96: True}

# uAria, slave 2, CRSTDm_AHU, IR100 = 1061 -> firmware 1.0.61.
UARIA_IR: Dict[int, int] = {addr: 0 for addr in range(0, 36)}
UARIA_IR[2], UARIA_IR[3] = _f32_words(27.22)
UARIA_IR[4], UARIA_IR[5] = _f32_words(43.59)
UARIA_IR[26] = 1
UARIA_HR: Dict[int, int] = {}
UARIA_HR[30], UARIA_HR[31] = _f32_words(22.0)
UARIA_HR[32], UARIA_HR[33] = _f32_words(22.0)
UARIA_HR[34] = 2
UARIA_HR[195], UARIA_HR[196] = _f32_words(22.0)
UARIA_HR[197] = 7
UARIA_COILS: Dict[int, bool] = {0: True, 13: True, 30: True}
UARIA_DI: Dict[int, bool] = {0: True, 52: True, 53: True}

CRST_DEVICE = {
    "address": 1,
    "baudrate": 19200,
    "parity": "N",
    "stopbits": 1,
    "signature": "CRSTDrAHAQ",
    "app_version": "2.03.00.46",
    "carel_variant": "E",
}
UARIA_DEVICE = {
    "address": 2,
    "baudrate": 19200,
    "parity": "N",
    "stopbits": 1,
    "signature": "CRSTDm_AHU",
    "app_version": "1.0.61",
    "carel_variant": "",
}


class FakeCarelPlc:
    """A Modbus RTU slave built from the bench tables, speaking real frames.

    Every request is decoded from its PDU and recorded, so a test can assert on
    what actually went out on the wire rather than on which helper was called.
    An address outside the tables answers NOTHING — the real PLC's behaviour, and
    the reason a dead read costs a full timeout instead of an error.
    """

    def __init__(
        self,
        slave: int,
        *,
        input_regs: Dict[int, int],
        holding: Dict[int, int],
        coils: Dict[int, bool],
        discrete: Dict[int, bool],
    ) -> None:
        self.slave = slave
        self.input_regs = dict(input_regs)
        self.holding = dict(holding)
        self.coils = dict(coils)
        self.discrete = dict(discrete)
        self.reads: List[Tuple[int, int, int]] = []   # (func, start, count)
        self.writes: List[Tuple[int, int, Any]] = []  # (func, address, value)

    # -- frame helpers ------------------------------------------------------
    def _crc(self, body: bytes) -> bytes:
        return struct.pack("<H", modbus_rtu.crc16_modbus(body))

    def _read_reply(self, func: int, data: bytes) -> bytes:
        body = bytes([self.slave, func, len(data)]) + data
        return body + self._crc(body)

    def _echo(self, req: bytes) -> bytes:
        body = req[:6]
        return body + self._crc(body)

    def _regs_reply(self, func: int, table: Dict[int, int], start: int, count: int) -> Optional[bytes]:
        if any(start + i not in table for i in range(count)):
            return None  # PLC keeps silent — the caller sees a timeout
        data = b"".join(struct.pack(">H", table[start + i] & 0xFFFF) for i in range(count))
        return self._read_reply(func, data)

    def _bits_reply(self, func: int, table: Dict[int, bool], start: int, count: int) -> Optional[bytes]:
        if not any(start + i in table for i in range(count)):
            return None
        nbytes = (count + 7) // 8
        data = bytearray(nbytes)
        for i in range(count):
            if table.get(start + i):
                data[i // 8] |= 1 << (i % 8)
        return self._read_reply(func, bytes(data))

    # -- the SendRtuFn contract --------------------------------------------
    def __call__(self, req: bytes, timeout_ms: int) -> Optional[bytes]:
        func = req[1]
        if func in (0x01, 0x02, 0x03, 0x04):
            start, count = struct.unpack(">HH", req[2:6])
            self.reads.append((func, start, count))
            if func == 0x03:
                return self._regs_reply(func, self.holding, start, count)
            if func == 0x04:
                return self._regs_reply(func, self.input_regs, start, count)
            table = self.coils if func == 0x01 else self.discrete
            return self._bits_reply(func, table, start, count)
        if func == 0x05:
            addr, raw = struct.unpack(">HH", req[2:6])
            self.writes.append((func, addr, bool(raw)))
            self.coils[addr] = bool(raw)
            return self._echo(req)
        if func == 0x06:
            addr, value = struct.unpack(">HH", req[2:6])
            self.writes.append((func, addr, value))
            self.holding[addr] = value
            return self._echo(req)
        if func == 0x10:
            addr, count = struct.unpack(">HH", req[2:6])
            words = [
                struct.unpack(">H", req[7 + 2 * i : 9 + 2 * i])[0] for i in range(count)
            ]
            self.writes.append((func, addr, tuple(words)))
            for i, w in enumerate(words):
                self.holding[addr + i] = w
            return self._echo(req)
        return None

    # -- assertions helpers -------------------------------------------------
    def read_covers(self, address: int) -> bool:
        return any(
            func in (0x03, 0x04) and start <= address < start + count
            for func, start, count in self.reads
        )

    def written_coils(self) -> List[int]:
        """Только адреса КАТУШЕК: держащий регистр 30 (уставка uAria) — другой
        адресный простор, и путать их значило бы проверять не то."""
        return [addr for func, addr, _value in self.writes if func in (0x05, 0x0F)]


def crst_plc() -> FakeCarelPlc:
    return FakeCarelPlc(
        1, input_regs=CRST_IR, holding=CRST_HR, coils=CRST_COILS, discrete=CRST_DI
    )


def uaria_plc() -> FakeCarelPlc:
    return FakeCarelPlc(
        2, input_regs=UARIA_IR, holding=UARIA_HR, coils=UARIA_COILS, discrete=UARIA_DI
    )


def _snapshot(plc: FakeCarelPlc, device: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    closed: List[bool] = []
    with (
        patch.object(
            device_config,
            "_open_transport",
            return_value=(plc, lambda: closed.append(True)),
        ),
        patch.object(device_config.time, "sleep"),  # блочный ретрай в/в не ждёт
    ):
        payload = device_config.snapshot_for_device("/dev/ttyS4", device, **kwargs)
    assert closed, "порт не закрыт"
    return payload


def _carel_write(plc: FakeCarelPlc, device: Dict[str, Any], action: str, params=None) -> Dict[str, Any]:
    with (
        patch.object(device_config, "_open_transport", return_value=(plc, lambda: None)),
        patch.object(device_config.time, "sleep") as sleep_mock,
    ):
        payload = device_config.carel_write("/dev/ttyS4", device, action, params)
    payload["_sleeps"] = [c.args[0] for c in sleep_mock.call_args_list]
    return payload


class TestSharedMapAvailable(unittest.TestCase):
    def test_seam_resolves(self) -> None:
        """Non-vacuity floor: every case below reads addresses from the shared
        package. Without it they would all pass by never asserting anything."""
        self.assertIsNotNone(ca, "sa02m_carel не разрешается через шов пакета")


class TestSnapshotDoesNotProbeDeadRegisters(unittest.TestCase):
    def test_crst_snapshot_never_reads_identity_or_network_block(self) -> None:
        plc = crst_plc()
        _snapshot(plc, CRST_DEVICE)
        self.assertTrue(plc.reads, "снимок не сделал ни одного чтения")
        for reg in FORBIDDEN_REGS:
            self.assertFalse(
                plc.read_covers(reg),
                f"снимок c.pCOmini прочитал регистр {reg} — восемь таймаутов в такте окна",
            )

    def test_uaria_snapshot_never_reads_identity_or_network_block(self) -> None:
        plc = uaria_plc()
        _snapshot(plc, UARIA_DEVICE)
        self.assertTrue(plc.reads)
        for reg in FORBIDDEN_REGS:
            self.assertFalse(plc.read_covers(reg), f"снимок uAria прочитал регистр {reg}")

    def test_live_identity_and_network_helpers_are_not_called(self) -> None:
        """The wire pin above is the real one; this catches the same regression
        one layer up, where a helper could be reintroduced with new addresses."""
        plc = crst_plc()
        with (
            patch.object(device_config, "_read_live_identity") as ident,
            patch.object(device_config, "_read_network") as net,
        ):
            _snapshot(plc, CRST_DEVICE)
        ident.assert_not_called()
        net.assert_not_called()

    def test_io_block_is_read_only_on_its_tab(self) -> None:
        """The входы/выходы block is two IR sweeps plus a long DI read — it must
        not ride along on every poll of the plant page."""
        idle = crst_plc()
        _snapshot(idle, CRST_DEVICE)
        self.assertFalse(idle.read_covers(29), "блок в/в прочитан без открытой вкладки")
        opened = crst_plc()
        payload = _snapshot(opened, CRST_DEVICE, active_tab=device_config.CAREL_IO_TAB)
        self.assertTrue(opened.read_covers(29))
        self.assertIn("io_u", payload["carel"])


    def test_uaria_io_tab_survives_a_judged_away_reading(self) -> None:
        """The uAria I/O tab builds its analog column from the snapshot itself.

        When the outdoor reading is judged away it becomes None, and None used
        to be copied straight into the row builder, where float(None) took the
        whole snapshot down with a 500 — on the bench configuration, whose
        outdoor probe is exactly the unfitted one.
        """
        payload = _snapshot(uaria_plc(), UARIA_DEVICE,
                            active_tab=device_config.CAREL_IO_TAB)
        rows = payload["carel"]["io_u"]
        self.assertTrue(rows, "колонка аналоговых входов uAria пуста")
        # uAria names its analog inputs U1..U3; U1 is the outdoor probe.
        outdoor = [r for r in rows if r["tag"] == "U1"]
        self.assertEqual(len(outdoor), 1)
        # The engineering column still shows what the register said.
        self.assertAlmostEqual(outdoor[0]["value"], 0.0, places=2)
        # while the plant page reports no reading for the same probe.
        self.assertIsNone(payload["carel"]["oat"])


class TestCrstSnapshotDecodesBenchValues(unittest.TestCase):
    def setUp(self) -> None:
        self.plc = crst_plc()
        self.payload = _snapshot(self.plc, CRST_DEVICE)
        self.snap = self.payload["carel"]

    def test_kind_and_identity_come_from_the_scan_row(self) -> None:
        self.assertEqual(self.payload["kind"], "carel")
        self.assertEqual(self.payload["family"], ca.FAMILY_CRST)
        self.assertFalse(self.payload["bus_mode_supported"])
        info = self.payload["info"]
        self.assertEqual(info["signature"], "CRSTDrAHAQ")
        self.assertEqual(info["app_version"], "2.03.00.46")
        self.assertEqual(info["carel_variant"], "E")
        self.assertEqual(info["variant_label"], "Enhanced")
        self.assertEqual(info["model"], "c.pCOmini")
        self.assertEqual(info["line"], {"baudrate": 19200, "parity": "N", "stopbits": 1})

    def test_network_block_is_read_only(self) -> None:
        self.assertFalse(self.payload["network"]["writable"])
        self.assertEqual(self.payload["network"]["address"], 1)

    def test_temperatures_are_int16_x10(self) -> None:
        # IR1..4 = [0, 265, 0, 751] -> 0.0 / 26.5 / (нет датчика) / 75.1 °C
        # Outdoor read exactly 0 on the bench: an unfitted optional input, not
        # a measurement. This asserted 0.0 until 1.0.6.31 and was pinning the
        # defect — the window now reports no reading, like the bridge.
        self.assertIsNone(self.snap["oat"])
        self.assertAlmostEqual(self.snap["sat"], 26.5, places=2)
        self.assertAlmostEqual(self.snap["rwt"], 75.1, places=2)

    def test_valve_setpoint_and_status(self) -> None:
        self.assertAlmostEqual(self.snap["valve"], 100.0, places=2)   # IR21
        self.assertAlmostEqual(self.snap["disp_sp"], 27.2, places=2)  # IR50
        self.assertEqual(self.snap["unit"], 1)                        # IR116

    def test_holding_49_54_block(self) -> None:
        # HR49..54 = [0, 1, 272, 205, 800, 800]
        self.assertEqual(self.snap["mode"], 0)
        self.assertAlmostEqual(self.snap["sp_w"], 27.2, places=2)
        self.assertAlmostEqual(self.snap["sp_s"], 20.5, places=2)
        self.assertAlmostEqual(self.snap["fan_sa"], 80.0, places=2)
        self.assertAlmostEqual(self.snap["fan_ea"], 80.0, places=2)

    def test_coils_and_discrete_inputs(self) -> None:
        self.assertTrue(self.snap["bms_run"])    # coil 65
        self.assertFalse(self.snap["season"])    # coil 67
        self.assertFalse(self.snap["ma17"])      # coil 129
        self.assertTrue(self.snap["ma18"])       # coil 130
        self.assertTrue(self.snap["keyboard_on"])  # DI95
        self.assertTrue(self.snap["sys_on"])       # DI96
        self.assertTrue(self.snap["pump"])         # DI9

    def test_no_alarms_and_derived_headline(self) -> None:
        self.assertEqual(self.snap["alarms"], [])
        self.assertEqual(self.snap["distat"], [])
        self.assertEqual(self.snap["plant_state"], ca.PLANT_RUN)
        # Application 2.03.00.46 >= 2.02.xx.52, so the v2 UnitStatus table.
        self.assertEqual(self.snap["unit_status_algo"], "v2")
        self.assertEqual(self.snap["unit_status_text"], ca.UNIT_STATUS_V2[1])
        self.assertEqual(self.snap["alarm_reset_coil"], ca.COIL_ALARM_RESET)


class TestUariaSnapshotDecodesBenchValues(unittest.TestCase):
    def setUp(self) -> None:
        self.plc = uaria_plc()
        self.payload = _snapshot(self.plc, UARIA_DEVICE)
        self.snap = self.payload["carel"]

    def test_identity(self) -> None:
        self.assertEqual(self.payload["family"], ca.FAMILY_UARIA)
        self.assertEqual(self.payload["info"]["model"], "uAria")
        self.assertEqual(self.payload["info"]["app_version"], "1.0.61")

    def test_float32_abcd_analogues(self) -> None:
        # IR2/IR4 are two-word float32, big-endian ABCD. Read CDAB they decode
        # to garbage in the 1e-30 range, not to a temperature.
        self.assertAlmostEqual(self.snap["sat"], 27.22, places=2)
        self.assertAlmostEqual(self.snap["rwt"], 43.59, places=2)
        # Outdoor read exactly 0 on the bench: an unfitted optional input, not
        # a measurement. This asserted 0.0 until 1.0.6.31 and was pinning the
        # defect — the window now reports no reading, like the bridge.
        self.assertIsNone(self.snap["oat"])
        self.assertAlmostEqual(self.snap["valve"], 0.0, places=2)
        self.assertAlmostEqual(self.snap["fan"], 0.0, places=2)
        self.assertEqual(self.snap["unit"], 1)

    def test_setpoints_season_and_fan_step(self) -> None:
        self.assertAlmostEqual(self.snap["sp_w"], 22.0, places=2)
        self.assertAlmostEqual(self.snap["sp_s"], 22.0, places=2)
        self.assertEqual(self.snap["season_code"], 2)
        self.assertEqual(self.snap["fan_sp"], 7)
        self.assertAlmostEqual(self.snap["fan_min"], 22.0, places=2)
        self.assertAlmostEqual(
            self.snap["fan_calc"], ca.uaria_fan_step_to_pct(7, 22.0), places=2
        )

    def test_coils_and_discrete_inputs(self) -> None:
        self.assertTrue(self.snap["uaria_run"])    # coil 0
        self.assertTrue(self.snap["gs04"])         # coil 13
        self.assertTrue(self.snap["uaria_local"])  # coil 30 — read only, never written
        self.assertTrue(self.snap["pump"])         # DI52
        self.assertTrue(self.snap["fan_on"])       # DI53
        self.assertFalse(self.snap["heat_on"])
        self.assertFalse(self.snap["crit"])
        self.assertEqual(self.snap["alarms"], [])
        self.assertEqual([row["code"] for row in self.snap["distat"]], ["fan"])

    def test_derived_headline_uses_the_uaria_table(self) -> None:
        self.assertEqual(self.snap["plant_state"], ca.PLANT_RUN)
        self.assertEqual(self.snap["unit_status_algo"], "uaria")
        self.assertEqual(self.snap["unit_status_text"], ca.UARIA_UNIT_STATUS[1])
        self.assertEqual(self.snap["alarm_reset_coil"], ca.COIL_UARIA_ALARM_RESET)

    def test_snapshot_writes_nothing(self) -> None:
        self.assertEqual(self.plc.writes, [])


class TestCrstCommands(unittest.TestCase):
    def test_start_enables_the_network_settles_then_runs(self) -> None:
        plc = crst_plc()
        out = _carel_write(plc, CRST_DEVICE, "start")
        writes = [(f, a, v) for f, a, v in plc.writes]
        self.assertEqual(
            writes[:2],
            [(0x05, ca.COIL_MA18, True), (0x05, ca.COIL_BMS_OFF_ON, True)],
            "порядок пуска c.pCOmini: сначала разрешение сети, затем команда пуска",
        )
        self.assertIn(
            ca.START_MA18_SETTLE_S,
            out["_sleeps"],
            "между разрешением сети и пуском нет выдержки — ПЛК не запустится",
        )

    def test_stop_touches_only_the_run_coil(self) -> None:
        plc = crst_plc()
        _carel_write(plc, CRST_DEVICE, "stop")
        self.assertEqual(plc.writes, [(0x05, ca.COIL_BMS_OFF_ON, False)])

    def test_sys_mode_is_written_between_enable_and_run(self) -> None:
        plc = crst_plc()
        _carel_write(plc, CRST_DEVICE, "sys_mode", {"value": 3})
        self.assertEqual(
            plc.writes,
            [
                (0x05, ca.COIL_MA18, True),
                (0x06, ca.HR_SYS_MODE, 3),
                (0x05, ca.COIL_BMS_OFF_ON, True),
            ],
        )

    def test_sys_mode_zero_stops_by_mode(self) -> None:
        plc = crst_plc()
        _carel_write(plc, CRST_DEVICE, "sys_mode", {"value": 0})
        self.assertEqual(plc.writes, [(0x06, ca.HR_SYS_MODE, 0)])

    def test_setpoints_are_single_registers_x10(self) -> None:
        plc = crst_plc()
        _carel_write(plc, CRST_DEVICE, "sp_winter", {"value": 21.5})
        _carel_write(plc, CRST_DEVICE, "sp_summer", {"value": 24})
        self.assertEqual(
            plc.writes,
            [(0x06, ca.HR_SP_WINTER, 215), (0x06, ca.HR_SP_SUMMER, 240)],
        )

    def test_fan_setpoints_are_percent_x10(self) -> None:
        plc = crst_plc()
        _carel_write(plc, CRST_DEVICE, "fan_supply", {"value": 65})
        _carel_write(plc, CRST_DEVICE, "fan_exhaust", {"value": 70})
        self.assertEqual(
            plc.writes,
            [(0x06, ca.HR_FAN_SUPPLY, 650), (0x06, ca.HR_FAN_EXHAUST, 700)],
        )

    def test_alarm_reset_is_a_five_second_pulse(self) -> None:
        plc = crst_plc()
        out = _carel_write(plc, CRST_DEVICE, "alarm_reset")
        self.assertEqual(
            plc.writes,
            [
                (0x05, ca.COIL_ALARM_RESET, True),
                (0x05, ca.COIL_ALARM_RESET, False),
            ],
        )
        self.assertIn(ca.ALARM_RESET_PULSE_S, out["_sleeps"])

    def test_net_enable_writes_ma18(self) -> None:
        plc = crst_plc()
        _carel_write(plc, CRST_DEVICE, "net_enable", {"enable": False})
        self.assertEqual(plc.writes, [(0x05, ca.COIL_MA18, False)])

    def test_uaria_only_actions_refused(self) -> None:
        for action in ("fan_step",):
            with self.assertRaises(ValueError):
                _carel_write(crst_plc(), CRST_DEVICE, action, {"value": 3})


class TestUariaCommands(unittest.TestCase):
    def test_start_writes_the_run_coil_and_leaves_gs04_alone_when_set(self) -> None:
        plc = uaria_plc()  # coil 13 already 1 on the bench
        _carel_write(plc, UARIA_DEVICE, "start")
        self.assertEqual(plc.writes, [(0x05, ca.COIL_UARIA_NET_ON_OFF, True)])

    def test_start_sets_gs04_first_when_it_reads_zero(self) -> None:
        plc = uaria_plc()
        plc.coils[ca.COIL_UARIA_NET_ENABLE] = False
        _carel_write(plc, UARIA_DEVICE, "start")
        self.assertEqual(
            plc.writes,
            [
                (0x05, ca.COIL_UARIA_NET_ENABLE, True),
                (0x05, ca.COIL_UARIA_NET_ON_OFF, True),
            ],
        )

    def test_start_refuses_when_gs04_cannot_be_read(self) -> None:
        plc = uaria_plc()
        del plc.coils[ca.COIL_UARIA_NET_ENABLE]  # PLC silent on that coil
        with self.assertRaises(ValueError):
            _carel_write(plc, UARIA_DEVICE, "start")
        self.assertEqual(plc.writes, [])

    def test_stop_writes_the_run_coil_off(self) -> None:
        plc = uaria_plc()
        _carel_write(plc, UARIA_DEVICE, "stop")
        self.assertEqual(plc.writes, [(0x05, ca.COIL_UARIA_NET_ON_OFF, False)])

    def test_setpoint_is_one_fc16_of_two_float32_words(self) -> None:
        """Two single-register writes would leave the PLC reading half of the old
        value and half of the new one for a whole scan — the setpoint must land
        atomically, in ONE FC16 frame."""
        plc = uaria_plc()
        _carel_write(plc, UARIA_DEVICE, "sp_winter", {"value": 23.5})
        self.assertEqual(len(plc.writes), 1)
        func, addr, words = plc.writes[0]
        self.assertEqual(func, 0x10)
        self.assertEqual(addr, ca.HR_UARIA_SP)
        self.assertEqual(words, _f32_words(23.5))

    def test_summer_setpoint_goes_to_its_own_pair(self) -> None:
        plc = uaria_plc()
        _carel_write(plc, UARIA_DEVICE, "sp_summer", {"value": 25.0})
        self.assertEqual(plc.writes, [(0x10, ca.HR_UARIA_SP_SUMMER, _f32_words(25.0))])

    def test_fan_step_is_a_single_register(self) -> None:
        plc = uaria_plc()
        _carel_write(plc, UARIA_DEVICE, "fan_step", {"value": 4})
        self.assertEqual(plc.writes, [(0x06, ca.HR_UARIA_FAN_SP, 4)])

    def test_alarm_reset_uses_the_uaria_coil(self) -> None:
        plc = uaria_plc()
        out = _carel_write(plc, UARIA_DEVICE, "alarm_reset")
        self.assertEqual(
            plc.writes,
            [
                (0x05, ca.COIL_UARIA_ALARM_RESET, True),
                (0x05, ca.COIL_UARIA_ALARM_RESET, False),
            ],
        )
        self.assertIn(ca.ALARM_RESET_PULSE_S, out["_sleeps"])

    def test_crst_only_actions_refused(self) -> None:
        for action in ("sys_mode", "fan_supply", "fan_exhaust"):
            with self.assertRaises(ValueError):
                _carel_write(uaria_plc(), UARIA_DEVICE, action, {"value": 1})


class TestUariaLocalCoilIsUnreachable(unittest.TestCase):
    """Coil 30 hands the unit back to its own keypad. Nothing the web offers may
    reach it — pinned by walking every action of both families, not by reading
    the code."""

    CASES: Tuple[Tuple[str, Dict[str, Any]], ...] = (
        ("start", {}),
        ("stop", {}),
        ("alarm_reset", {}),
        ("net_enable", {"enable": True}),
        ("net_enable", {"enable": False}),
        ("sys_mode", {"value": 0}),
        ("sys_mode", {"value": 3}),
        ("sp_winter", {"value": 21.0}),
        ("sp_summer", {"value": 24.0}),
        ("fan_supply", {"value": 60}),
        ("fan_exhaust", {"value": 60}),
        ("fan_step", {"value": 5}),
    )

    def test_no_plan_of_any_action_reaches_coil_30(self) -> None:
        """At the PLAN, before the executor's guard can hide it.

        The guard in _carel_run_writes REFUSES such a write, so a wire-level walk
        alone would see an exception, no frame, and call that a pass — the plan
        would be wrong and the pin would stay green. This case reads the plans.
        """
        walked = 0
        for family in (ca.FAMILY_CRST, ca.FAMILY_UARIA):
            for action, params in self.CASES:
                for net_enable in (0, 1, None):
                    try:
                        plan = device_config._carel_write_plan(
                            ca, family, action, params, net_enable=net_enable
                        )
                    except ValueError:
                        continue  # действие не для этого семейства
                    walked += 1
                    coils = [
                        int(w.address) for w in plan if w.kind == ca.KIND_COIL
                    ]
                    self.assertNotIn(
                        ca.COIL_UARIA_LOCAL,
                        coils,
                        f"план {family}/{action}{params} содержит катушку местного управления",
                    )
        self.assertGreaterEqual(
            walked, len(self.CASES), "перебор планов перестал что-либо строить"
        )

    def test_every_action_of_both_families_avoids_coil_30(self) -> None:
        walked = 0
        for family, factory, device in (
            ("crst", crst_plc, CRST_DEVICE),
            ("uaria", uaria_plc, UARIA_DEVICE),
        ):
            for action, params in self.CASES:
                for gs04 in (True, False):
                    plc = factory()
                    plc.coils[ca.COIL_UARIA_NET_ENABLE] = gs04
                    try:
                        _carel_write(plc, device, action, params)
                    except (ValueError, RuntimeError):
                        pass  # refused actions write nothing at all
                    walked += 1
                    self.assertNotIn(
                        ca.COIL_UARIA_LOCAL,
                        plc.written_coils(),
                        f"{family}/{action}{params} записала катушку местного управления",
                    )
        self.assertEqual(
            walked,
            len(self.CASES) * 4,
            "перебор действий перестал покрывать оба семейства — проверка стала пустой",
        )

    def test_the_action_list_is_the_one_walked(self) -> None:
        """Non-vacuity: a new action added to the backend but not to CASES would
        otherwise never be walked, and the guarantee above would go stale."""
        self.assertEqual(
            sorted({action for action, _params in self.CASES}),
            sorted(device_config.CAREL_ACTIONS),
        )

    def test_the_executor_refuses_a_plan_that_reaches_coil_30(self) -> None:
        """Defence in depth: no plan produces it, and if one ever did, the frame
        would not go out."""
        plc = uaria_plc()
        rogue = [ca.CarelWrite(ca.KIND_COIL, ca.COIL_UARIA_LOCAL, 1)]
        with self.assertRaises(ValueError):
            device_config._carel_run_writes(ca, plc, 2, ca.FAMILY_UARIA, rogue)
        self.assertEqual(plc.writes, [])


class TestRefusals(unittest.TestCase):
    def test_unknown_action_refused_before_the_port_is_opened(self) -> None:
        with patch.object(device_config, "_open_transport") as opener:
            with self.assertRaises(ValueError):
                device_config.carel_write("/dev/ttyS4", CRST_DEVICE, "reboot", {})
        opener.assert_not_called()

    def test_carel_write_refuses_a_non_carel_device(self) -> None:
        with patch.object(device_config, "_open_transport") as opener:
            with self.assertRaises(ValueError):
                device_config.carel_write(
                    "/dev/ttyS4", {"address": 1, "baudrate": 9600, "signature": "6AI6AO"},
                    "start", {},
                )
        opener.assert_not_called()

    def test_single_holding_endpoint_refuses_carel(self) -> None:
        with patch.object(device_config, "_open_transport") as opener:
            with self.assertRaises(ValueError):
                device_config.write_allowed_holding("/dev/ttyS4", CRST_DEVICE, 51, 215)
        opener.assert_not_called()

    def test_single_coil_endpoint_refuses_carel(self) -> None:
        with patch.object(device_config, "_open_transport") as opener:
            with self.assertRaises(ValueError):
                device_config.write_allowed_coil("/dev/ttyS4", CRST_DEVICE, 65, True)
        opener.assert_not_called()

    def test_network_apply_refuses_carel(self) -> None:
        """The snapshot says network.writable=false; the endpoint must agree, or
        the write lands in registers the PLC uses for something else."""
        with patch.object(device_config, "_open_transport") as opener:
            with self.assertRaises(ValueError):
                device_config.apply_network_settings(
                    "/dev/ttyS4", CRST_DEVICE,
                    {"baudrate": 9600, "parity": "N", "stopbits": 1, "address": 1},
                )
        opener.assert_not_called()

    def test_kind_from_identity_answers_carel_for_a_plc_signature(self) -> None:
        self.assertEqual(device_config._kind_from_identity("CRSTDrAHAQ", None), "carel")
        self.assertEqual(device_config._kind_from_identity("CRSTDm_AHU", None), "carel")
        self.assertEqual(device_config._kind_from_identity("6AI6AO", None), "mr")


def _extract_method(class_name: str, method: str) -> Any:
    """exec one method of service.py in an isolated namespace (service.py itself
    imports grp/cgi and cannot load off Linux — the test_health_lease idiom)."""
    tree = ast.parse(_SERVICE_SRC)
    lines = _SERVICE_SRC.splitlines()
    cls = next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name),
        None,
    )
    if cls is None:
        raise AssertionError(f"service.py больше не определяет класс {class_name}")
    node = next(
        (n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method),
        None,
    )
    if node is None:
        raise AssertionError(f"service.py больше не определяет {class_name}.{method}()")
    src = textwrap.dedent("\n".join(lines[node.lineno - 1 : node.end_lineno]))
    ns: Dict[str, Any] = {}
    exec("from __future__ import annotations\n" + src, ns)  # noqa: S102
    return ns[method], ns


class TestCarelWriteRoute(unittest.TestCase):
    ROUTE = '/device_config/carel_write'

    def test_route_is_dispatched_and_sits_behind_auth(self) -> None:
        lines = _SERVICE_SRC.splitlines()
        route = next(
            (i for i, ln in enumerate(lines, 1) if f'p == "{self.ROUTE}"' in ln), None
        )
        auth = next(
            (i for i, ln in enumerate(lines, 1) if "if not self._check_auth()" in ln), None
        )
        self.assertIsNotNone(route, "маршрут carel_write не разбирается в _dispatch")
        self.assertIsNotNone(auth)
        self.assertLess(
            auth, route, "маршрут записи в ПЛК оказался выше проверки сессии"
        )
        following = "\n".join(lines[route : route + 2])
        self.assertIn("_handle_device_config_carel_write(ctx)", following)

    def test_handler_runs_under_the_port_lease(self) -> None:
        handler, ns = _extract_method("Handler", "_handle_device_config_carel_write")
        seen: Dict[str, Any] = {}

        class _DeviceConfig:
            @staticmethod
            def carel_write(device_path, device, action, params):
                seen["call"] = (device_path, device, action, params)
                return {"kind": "carel", "action": action}

        class _Self:
            def _device_config_request(self, ctx, data):
                return "/dev/ttyS4", data["device"]

            def _run_device_config_modbus(self, ctx, port, device_path, fn):
                seen["leased"] = (port, device_path)
                return fn()

        ns["_read_json_body"] = lambda _self: {
            "port": "COM3",
            "device": dict(CRST_DEVICE),
            "action": "start",
            "params": {"value": 1},
        }
        ns["_send_json"] = lambda _self, payload: seen.update(sent=payload)
        ns["device_config"] = _DeviceConfig
        handler(_Self(), types.SimpleNamespace())

        self.assertEqual(seen["leased"], ("COM3", "/dev/ttyS4"))
        self.assertEqual(seen["call"][2], "start")
        self.assertEqual(seen["call"][3], {"value": 1})
        self.assertTrue(seen["sent"]["ok"])
        self.assertEqual(seen["sent"]["action"], "start")

    def test_handler_tolerates_a_missing_params_object(self) -> None:
        handler, ns = _extract_method("Handler", "_handle_device_config_carel_write")
        seen: Dict[str, Any] = {}

        class _DeviceConfig:
            @staticmethod
            def carel_write(device_path, device, action, params):
                seen["params"] = params
                return {}

        class _Self:
            def _device_config_request(self, ctx, data):
                return "/dev/ttyS4", data["device"]

            def _run_device_config_modbus(self, ctx, port, device_path, fn):
                return fn()

        ns["_read_json_body"] = lambda _self: {
            "port": "COM3",
            "device": dict(CRST_DEVICE),
            "action": "stop",
            "params": "not-an-object",
        }
        ns["_send_json"] = lambda _self, payload: None
        ns["device_config"] = _DeviceConfig
        handler(_Self(), types.SimpleNamespace())
        self.assertEqual(seen["params"], {})


class TestPollerUsesTheSharedMapOnly(unittest.TestCase):
    def test_no_bare_serial_port_in_the_poller(self) -> None:
        """Every Modbus access rides the leased device_config transport; a bare
        pyserial handle would open the line behind the lease's back."""
        src = (_PKG / "carel_poll.py").read_text(encoding="utf-8")
        self.assertNotIn("import serial", src)
        self.assertNotIn("serial.Serial", src)
        self.assertIn("module_profiles", src)

    def test_snapshot_returns_none_when_the_plc_is_silent(self) -> None:
        silent = FakeCarelPlc(1, input_regs={}, holding={}, coils={}, discrete={})
        self.assertIsNone(carel_poll.read_carel_snapshot(silent, 1, ca.FAMILY_CRST))
        self.assertIsNone(carel_poll.read_carel_snapshot(silent, 1, ca.FAMILY_UARIA))


if __name__ == "__main__":
    unittest.main()
