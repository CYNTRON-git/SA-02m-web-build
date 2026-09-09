# -*- coding: utf-8 -*-
"""Carel map unit tests — ported from the desktop flasher tests/test_carel_ahu.py.

Fixtures are raw FC17 replies captured from the real controllers (desktop bench
COM6; the same two controllers re-read on SA-02m bench 192.168.1.135 COM3,
2026-09-03): slave 1 = c.pCOmini `CRSTDrAHAQ`, slave 2 = uAria `CRSTDm_AHU`.
The payload is sliced here (slave, function, byte-count and CRC stripped) so
this package stays free of any Modbus framing code.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_carel import carel_ahu as ca  # noqa: E402
from sa02m_carel import controls as cc  # noqa: E402


def _payload(name: str) -> bytes:
    """FC17 payload from a capture file.

    The two captures are not the same shape: the c.pCOmini one is the whole RTU
    frame off the wire ([slave][0x11][byte_count][payload][crc]), the uAria one
    was saved as the bare payload. Unwrap only when the blob really is a frame,
    so neither capture has to be re-cut to match the other.
    """
    raw = bytes.fromhex(
        (Path(__file__).parent / "fixtures" / name).read_text(encoding="ascii").strip()
    )
    if len(raw) > 5 and raw[1] == 0x11 and len(raw) == 3 + raw[2] + 2:
        return raw[3 : 3 + raw[2]]
    return raw


PROBE2 = _payload("fc17_probe2.hex")
CRSTDM = _payload("fc17_crstdm.hex")


def _known_module(sig):
    """Stand-in for the flasher's own "this signature is one of ours" predicate."""
    return str(sig).strip().upper() in ("6AI6AO", "LED")


class TestFingerprint(unittest.TestCase):
    def test_crst_fingerprint(self) -> None:
        fp = ca.parse_report_slave_id(PROBE2)
        self.assertIsNotNone(fp)
        self.assertEqual(fp.app_id, "CRSTDrAHAQ")
        self.assertEqual(fp.std_mark, "STD_E")
        self.assertEqual(fp.version_str(), "2.03.00.46")
        self.assertEqual(fp.family, ca.FAMILY_CRST)
        self.assertEqual(ca.variant_from_std_mark(fp.std_mark), ca.VARIANT_ENHANCED)

    def test_crstdm_is_uaria(self) -> None:
        fp = ca.parse_report_slave_id(CRSTDM)
        self.assertIsNotNone(fp)
        self.assertEqual(fp.app_id, "CRSTDm_AHU")
        self.assertEqual(fp.family, ca.FAMILY_UARIA)

    def test_garbage_is_not_carel(self) -> None:
        for blob in (b"", b"\x00" * 20, b"hello", b"6AI6AO" * 4):
            self.assertIsNone(ca.parse_report_slave_id(blob), blob)

    def test_uaria_version_from_ir100(self) -> None:
        # Bench slave 2 answers IR100 = 1061.
        self.assertEqual(ca.format_uaria_ir100(1061), "1.0.61")

    def test_fc17_gating_on_a_mixed_bus(self) -> None:
        # Our own modules keep HR290 and are never probed. Both rows below carry
        # serial 0 on purpose: that is the only case where the "is it one of
        # ours" check is load-bearing — with a serial present the row is already
        # identified and would be skipped anyway, so a test using one would pass
        # even with the check deleted.
        self.assertFalse(
            ca.scan_should_probe_fc17(serial=0, signature="LED", is_known_module=_known_module)
        )
        self.assertFalse(
            ca.scan_should_probe_fc17(serial=0, signature="6AI6AO", is_known_module=_known_module)
        )
        # A row we do not recognise at all still gets the probe even with a
        # serial — that is how an unknown third-party device is offered FC17.
        self.assertTrue(
            ca.scan_should_probe_fc17(serial=0, signature="WHAT-IS-THIS", is_known_module=_known_module)
        )
        # ...a Carel row and an unidentified row are.
        self.assertTrue(
            ca.scan_should_probe_fc17(
                serial=0, signature="CRSTDrAHAQ", is_known_module=_known_module
            )
        )
        self.assertTrue(
            ca.scan_should_probe_fc17(serial=0, signature="", is_known_module=_known_module)
        )


class TestScaling(unittest.TestCase):
    def test_crst_int16_x10(self) -> None:
        self.assertEqual(ca.int16_x10_to_phys(265), 26.5)  # bench IR2 supply air
        self.assertEqual(ca.int16_x10_to_phys(751), 75.1)  # bench IR4 return water
        self.assertEqual(ca.int16_x10_to_phys(0xFFF6), -1.0)
        self.assertEqual(ca.phys_to_raw_x10(23.5, 0.0, 99.0), 235)
        self.assertEqual(ca.phys_to_raw_x10(120.0, 0.0, 99.0), 990)

    def test_uaria_float32_abcd(self) -> None:
        self.assertAlmostEqual(ca.be_float32(0x41D9, 0xC28F), 27.22, places=2)
        self.assertEqual(ca.float32_to_be_words(22.0), (0x41B0, 0x0000))
        hi, lo = ca.float32_to_be_words(23.5)
        self.assertAlmostEqual(ca.be_float32(hi, lo), 23.5, places=3)


class TestUnitStatus(unittest.TestCase):
    def test_v1_and_v2_disagree_on_code_8(self) -> None:
        v1, v2 = ca.unit_status_labels(8)
        self.assertNotEqual(v1, v2)

    def test_table_choice_by_firmware_version(self) -> None:
        self.assertTrue(ca.unit_status_use_v2((2, 3, 0, 46)))  # bench CRST
        self.assertFalse(ca.unit_status_use_v2((2, 1, 0, 10)))
        self.assertTrue(ca.unit_status_use_v2(None))  # unknown -> newer table

    def test_bench_state_reads_as_running(self) -> None:
        snap = {"unit": 1, "alarms": [], "sys_on": True, "bms_run": True}
        self.assertEqual(
            ca.plant_run_state(snap, ca.FAMILY_CRST, version=(2, 3, 0, 46)), ca.PLANT_RUN
        )

    def test_alarm_beats_running(self) -> None:
        snap = {"unit": 1, "alarms": [{"code": "E01"}], "sys_on": True}
        self.assertEqual(
            ca.plant_run_state(snap, ca.FAMILY_CRST, version=(2, 3, 0, 46)), ca.PLANT_ALARM
        )


class TestAlarms(unittest.TestCase):
    def test_crst_packs_decode(self) -> None:
        packs = [0] * len(ca.IR_ALARM_PACKS)
        packs[0] = 1 << 1  # E01 fire alarm
        found = {a["code"] for a in ca.decode_alarm_packs(packs)}
        self.assertIn("E01", found)

    def test_bench_reads_no_alarm(self) -> None:
        self.assertEqual(ca.decode_alarm_packs([0] * len(ca.IR_ALARM_PACKS)), [])

    def test_uaria_alarm_discretes(self) -> None:
        found = {a["code"] for a in ca.decode_uaria_alarm_dis([116, 141])}
        self.assertEqual(found, {"A16", "A41"})

    def test_reset_coil_per_family(self) -> None:
        self.assertEqual(ca.alarm_reset_coil(ca.FAMILY_CRST), 66)
        self.assertEqual(ca.alarm_reset_coil(ca.FAMILY_UARIA), 37)


class TestStartPlans(unittest.TestCase):
    def test_crst_start_writes_ma18_before_the_bms_coil(self) -> None:
        writes, err = ca.start_write_plan(mam18=False, sys_mode_target=1, bms_on=True)
        self.assertIsNone(err)
        self.assertEqual(
            [(w.kind, w.address, w.value) for w in writes],
            [
                (ca.KIND_COIL, ca.COIL_MA18, 1),
                (ca.KIND_HOLDING, ca.HR_SYS_MODE, 1),
                (ca.KIND_COIL, ca.COIL_BMS_OFF_ON, 1),
            ],
        )

    def test_crst_stop_touches_only_the_bms_coil(self) -> None:
        writes, err = ca.start_write_plan(mam18=True, sys_mode_target=None, bms_on=False)
        self.assertIsNone(err)
        self.assertEqual(
            [(w.kind, w.address, w.value) for w in writes],
            [(ca.KIND_COIL, ca.COIL_BMS_OFF_ON, 0)],
        )

    def test_uaria_start_enables_gs04_first(self) -> None:
        writes, err = ca.uaria_start_writes(net_enable=0, on=True)
        self.assertIsNone(err)
        self.assertEqual(
            [(w.address, w.value) for w in writes],
            [(ca.COIL_UARIA_NET_ENABLE, 1), (ca.COIL_UARIA_NET_ON_OFF, 1)],
        )

    def test_uaria_start_skips_gs04_when_already_enabled(self) -> None:
        writes, _err = ca.uaria_start_writes(net_enable=1, on=True)
        self.assertEqual([w.address for w in writes], [ca.COIL_UARIA_NET_ON_OFF])

    def test_no_plan_ever_writes_the_local_terminal_coil(self) -> None:
        # uAria coil 30 is the keypad's own on/off: a BMS master writing it
        # fights the operator standing at the unit. Nothing here may reach it.
        self.assertEqual(ca.COIL_UARIA_LOCAL, 30)
        plans = []
        for net in (0, 1, None):
            for on in (True, False):
                plans.append(ca.uaria_start_writes(net_enable=net, on=on)[0])
        for mode in (None, 0, 5):
            for on in (True, False):
                plans.append(ca.start_write_plan(mam18=False, sys_mode_target=mode, bms_on=on)[0])
        for plan in plans:
            for w in plan or []:
                self.assertNotEqual(w.address, ca.COIL_UARIA_LOCAL)


class TestControlsInventory(unittest.TestCase):
    def test_families_carry_their_own_controls(self) -> None:
        crst = set(cc.control_names(cc.FAMILY_CRST))
        uaria = set(cc.control_names(cc.FAMILY_UARIA))
        self.assertIn("room_temp", crst)  # CRST has IR3
        self.assertNotIn("room_temp", uaria)  # the short uAria map has no room probe
        self.assertIn("fan_step", uaria)
        self.assertNotIn("fan_step", crst)
        for name in ("unit_on", "supply_temp", "return_water_temp", "setpoint", "alarm"):
            self.assertIn(name, crst)
            self.assertIn(name, uaria)

    def test_writable_set_is_the_one_the_bridge_subscribes(self) -> None:
        self.assertEqual(
            set(cc.writable_names(cc.FAMILY_CRST)),
            {
                "unit_on",
                "setpoint",
                "setpoint_summer",
                "net_enable",
                "sys_mode",
                "fan_supply",
                "fan_exhaust",
            },
        )

    def test_setpoint_limits_match_the_map(self) -> None:
        self.assertEqual(cc.SETPOINT_RANGE[cc.FAMILY_CRST], (ca.SP_C_MIN, ca.SP_C_MAX))
        self.assertEqual(cc.SETPOINT_RANGE[cc.FAMILY_UARIA], (ca.UARIA_SP_MIN, ca.UARIA_SP_MAX))

    def test_no_duplicate_names(self) -> None:
        names = cc.control_names()
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
