#!/usr/bin/env python3
"""CE-02m-3 energy counters reach MQTT as Wh of the PRIMARY circuit.

Defect (plan ce-energy-unit, backlog HIGH «SCALED PULSES»): the poller
published the raw uint64 of Input 580-611 under meta/units=Wh while one count
is 1/320 Wh (CE-02m-3 EN_METER_ENERGY_UNITS_PER_KWH = 320000) — every kWh and
rouble downstream was 320x too high. The promise now pinned here is the one in
docs/contracts/ce-energy-mqtt.md:
  - fw >= 1.0.7.4: Wh = raw / 320 (K cancels out);
  - fw <  1.0.7.4: Wh = raw / 320 x K/1000, K from Holding 557-559 (per phase
    for 600-611, the mean for the totals);
  - fw unknown or K unread: NO energy this cycle + a warning (fail-closed);
  - the device announces the unit with retained meta/energy_unit = "Wh".
The transport is a real MQTTPublisher with `pub` captured, so the assertions
are over the broker view (topic, payload, retain), not over a mock call.
"""
from __future__ import annotations

import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))


def _stub_missing(name: str, module: types.ModuleType) -> None:
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module


_stub_missing("yaml", types.ModuleType("yaml"))
_stub_missing("serial", types.ModuleType("serial"))
try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    _paho = types.ModuleType("paho")
    _paho_mqtt = types.ModuleType("paho.mqtt")
    _paho_client = types.ModuleType("paho.mqtt.client")
    _paho_client.Client = object
    _paho_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    _paho.mqtt = _paho_mqtt
    _paho_mqtt.client = _paho_client
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _paho_mqtt
    sys.modules["paho.mqtt.client"] = _paho_client

import bridge_dtv_ce  # noqa: E402
import bridge_mqtt  # noqa: E402
from bridge_dtv_ce import CE02M3Poller  # noqa: E402

DEV = "ce02m3-COMtest-14"
BASE = "/devices/%s" % DEV
TOTALS = ("energy_active_import", "energy_active_export",
          "energy_reactive_import", "energy_reactive_export",
          "energy_apparent")
PHASES = ("energy_active_import_a", "energy_active_import_b",
          "energy_active_import_c")


def _u64(v: int) -> list[int]:
    return [(v >> (16 * i)) & 0xFFFF for i in range(4)]


class _FakePort:
    """Register map device: Input 580-611 counters, Holding 320 version and
    557-559 CT ratios; a None version / ratio makes that read raise."""

    def __init__(self, fw, totals, phases=(0, 0, 0), k=(4000, 4000, 4000)):
        self.fw = fw
        self.k = k
        self.inp: dict[int, int] = {}
        for i, v in enumerate(totals):
            for j, w in enumerate(_u64(v)):
                self.inp[580 + i * 4 + j] = w
        for i, v in enumerate(phases):
            for j, w in enumerate(_u64(v)):
                self.inp[600 + i * 4 + j] = w
        self.holding_reads: list[tuple[int, int]] = []

    def read_input_registers(self, addr, start, count):
        return [self.inp.get(start + i, 0) for i in range(count)]

    def read_holding_registers(self, addr, start, count):
        self.holding_reads.append((start, count))
        if start == 320:
            if self.fw is None:
                raise IOError("timeout")
            return list(self.fw)[:count]
        if start == 557:
            if self.k is None:
                raise IOError("timeout")
            return list(self.k)[:count]
        return [0] * count


def _capturing_publisher():
    pub = bridge_mqtt.MQTTPublisher.__new__(bridge_mqtt.MQTTPublisher)
    pub._lock = threading.Lock()
    pub._retain = False
    pub._unchanged_republish_s = 0
    pub._last_pub = {}
    pub._ctrl_meta = {}
    pub._ctrl_meta_pub = {}
    pub._dev_meta = {}
    pub._dev_meta_pub = {}
    pub.device_online = lambda *a, **k: None
    sent: list[tuple[str, str, bool]] = []

    def _pub(topic, payload, retain=None):
        sent.append((topic, payload, pub._retain if retain is None else retain))
    pub.pub = _pub
    return pub, sent


def _poller(port, per_phase=True):
    pub, sent = _capturing_publisher()
    p = CE02M3Poller({
        "id": DEV, "type": "ce02m3", "port": "/dev/COMtest", "address": 14,
        "publish_per_phase_energy": per_phase,
    }, pub)
    patcher = mock.patch.object(p, "get_port", return_value=port)
    patcher.start()
    return p, sent, patcher


def _values(sent) -> dict[str, str]:
    pre = BASE + "/controls/"
    return {t[len(pre):]: v for t, v, _r in sent
            if t.startswith(pre) and "/" not in t[len(pre):]}


def _energy_values(sent) -> dict[str, str]:
    return {n: v for n, v in _values(sent).items() if n.startswith("energy_")}


class TestEnergyUnitPrimaryFirmware(unittest.TestCase):
    """fw >= 1.0.7.4: one count = 1/320 Wh at any K."""

    def setUp(self):
        self.port = _FakePort((1, 0, 7, 9), [320000] * 5,
                              phases=(320000, 640000, 3200))
        self.p, self.sent, patcher = _poller(self.port)
        self.addCleanup(patcher.stop)
        self.p.setup()
        self.p._poll_energy()

    def test_totals_are_watt_hours(self):
        got = _energy_values(self.sent)
        for name in TOTALS:
            self.assertEqual(got.get(name), "1000.000", name)

    def test_per_phase_are_watt_hours(self):
        got = _energy_values(self.sent)
        self.assertEqual(got.get("energy_active_import_a"), "1000.000")
        self.assertEqual(got.get("energy_active_import_b"), "2000.000")
        self.assertEqual(got.get("energy_active_import_c"), "10.000")

    def test_new_firmware_reads_no_ct_ratio(self):
        # K cancels out on >= 1.0.7.4 — reading 557-559 there is wasted bus time.
        self.assertNotIn(557, [s for s, _c in self.port.holding_reads])

    def test_per_phase_carry_watt_hour_units(self):
        units = {t: v for t, v, _r in self.sent if t.endswith("/meta/units")}
        for name in PHASES:
            self.assertEqual(units.get("%s/controls/%s/meta/units"
                                       % (BASE, name)), "Wh", name)

    def test_energy_unit_marker_is_retained_device_meta(self):
        marker = [(v, r) for t, v, r in self.sent
                  if t == BASE + "/meta/energy_unit"]
        self.assertEqual(marker, [("Wh", True)])


class TestEnergyWhFirmware(unittest.TestCase):
    """fw >= 1.0.7.11 publishes 580-647 already in integer Wh (varh, VAh):
    the bridge passes the number through, same 3-decimal format, no K."""

    def _run(self, fw, totals, phases):
        port = _FakePort(fw, totals, phases=phases)
        p, sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        p._poll_energy()
        return _energy_values(sent), port

    def test_wh_firmware_passes_raw_through(self):
        got, port = self._run((1, 0, 7, 11), [1000] * 5,
                              (1000, 2000, 7))
        for name in TOTALS:                 # incl. energy_apparent (S)
            self.assertEqual(got.get(name), "1000.000", name)
        self.assertEqual(got.get("energy_active_import_a"), "1000.000")
        self.assertEqual(got.get("energy_active_import_b"), "2000.000")
        self.assertEqual(got.get("energy_active_import_c"), "7.000")
        self.assertNotIn(557, [s for s, _c in port.holding_reads])

    def test_last_count_firmware_still_divides(self):
        got, _port = self._run((1, 0, 7, 10), [320000] * 5,
                               (320000, 640000, 3200))
        for name in TOTALS:
            self.assertEqual(got.get(name), "1000.000", name)
        self.assertEqual(got.get("energy_active_import_b"), "2000.000")
        self.assertEqual(got.get("energy_active_import_c"), "10.000")

    def test_boundary_is_exactly_1_0_7_11(self):
        # The same raw word on both sides of the gate: 320x apart.
        below, _ = self._run((1, 0, 7, 10), [3200] * 5, (3200,) * 3)
        at, _ = self._run((1, 0, 7, 11), [3200] * 5, (3200,) * 3)
        self.assertEqual(below["energy_active_import"], "10.000")
        self.assertEqual(at["energy_active_import"], "3200.000")
        self.assertEqual(below["energy_active_import_a"], "10.000")
        self.assertEqual(at["energy_active_import_a"], "3200.000")

    def test_later_build_numbers_stay_on_the_wh_side(self):
        got, _ = self._run((1, 0, 8, 0), [1234] * 5, (1,) * 3)
        self.assertEqual(got["energy_active_import"], "1234.000")


class TestEnergyUnitOldFirmware(unittest.TestCase):
    """fw < 1.0.7.4: the accumulator is secondary — x K/1000 from 557-559."""

    def test_uniform_k_converts_totals_and_phases(self):
        port = _FakePort((1, 0, 7, 3), [80000] * 5,
                         phases=(80000, 80000, 80000), k=(4000, 4000, 4000))
        p, sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        p._poll_energy()
        got = _energy_values(sent)
        for name in TOTALS + PHASES:
            self.assertEqual(got.get(name), "1000.000", name)

    def test_per_phase_k_and_mean_k_for_totals(self):
        # Ka=4000, Kb=2000, Kc=1000 → K̄ = 7000/3.
        port = _FakePort((1, 0, 7, 3), [96000] * 5,
                         phases=(80000, 160000, 320000),
                         k=(4000, 2000, 1000))
        p, sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        p._poll_energy()
        got = _energy_values(sent)
        for name in PHASES:
            self.assertEqual(got.get(name), "1000.000", name)
        # 96000 / 320 x (7000/3) / 1000 = 700 Wh
        self.assertEqual(got.get("energy_active_import"), "700.000")

    def test_ct_ratio_read_once_not_per_cycle(self):
        port = _FakePort((1, 0, 7, 3), [80000] * 5)
        p, _sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        p._poll_energy()
        p._poll_energy()
        self.assertEqual([r for r in port.holding_reads if r[0] == 557],
                         [(557, 3)])

    def test_unread_ct_ratio_publishes_no_energy(self):
        port = _FakePort((1, 0, 7, 3), [80000] * 5, k=None)
        p, sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        with self.assertLogs(p.log, level="WARNING") as cm:
            p._poll_energy()
        self.assertEqual(_energy_values(sent), {})
        self.assertTrue(any("energy" in m for m in cm.output), cm.output)

    def test_out_of_range_ct_ratio_publishes_no_energy(self):
        # 0 is not a ratio (firmware rejects it): converting with it would
        # publish a zero counter, which reads as a reset.
        port = _FakePort((1, 0, 7, 3), [80000] * 5, k=(4000, 0, 4000))
        p, sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        with self.assertLogs(p.log, level="WARNING"):
            p._poll_energy()
        self.assertEqual(_energy_values(sent), {})


class TestEnergyFailClosed(unittest.TestCase):

    def test_unknown_firmware_publishes_no_energy_and_warns(self):
        port = _FakePort(None, [320000] * 5, phases=(320000,) * 3)
        p, sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        with self.assertLogs(p.log, level="WARNING") as cm:
            p._poll_energy()
        self.assertEqual(_energy_values(sent), {})
        self.assertTrue(any("energy" in m for m in cm.output), cm.output)

    def test_warning_is_once_per_episode_not_per_cycle(self):
        port = _FakePort(None, [320000] * 5)
        p, _sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        with self.assertLogs(p.log, level="DEBUG") as cm:
            p._poll_energy()
            p._poll_energy()
            p._poll_energy()
        warnings = [m for m in cm.output if m.startswith("WARNING")]
        # The skipped energy warns once; the version read the cycles retry
        # already warned at setup, so it stays at DEBUG here.
        self.assertEqual(len([m for m in warnings if "energy" in m]), 1,
                         cm.output)
        self.assertEqual(
            [m for m in warnings if "firmware version read" in m], [],
            cm.output)
        self.assertEqual(len([m for m in cm.output
                              if "firmware version read" in m]), 3, cm.output)

    def test_version_recovers_and_energy_resumes(self):
        port = _FakePort(None, [320000] * 5)
        p, sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        with self.assertLogs(p.log, level="WARNING"):
            p._poll_energy()
        port.fw = (1, 0, 7, 9)
        p._poll_energy()
        self.assertEqual(_energy_values(sent).get("energy_active_import"),
                         "1000.000")

    def test_reboot_seen_by_uptime_forgets_version_and_ratio(self):
        # Reflash 1.0.7.3 → 1.0.7.9 without the FMB reboot event (classic
        # poll only): the uptime going backwards must drop the cached version,
        # else the old-firmware K would be applied to primary counters (x4).
        port = _FakePort((1, 0, 7, 3), [80000] * 5)
        p, sent, patcher = _poller(port, per_phase=False)
        self.addCleanup(patcher.stop)
        port.inp[105], port.inp[106] = 5000, 0
        p.setup()
        p._poll_uptime()
        p._poll_energy()
        self.assertEqual(_energy_values(sent)["energy_active_import"],
                         "1000.000")
        port.fw = (1, 0, 7, 9)
        for i, w in enumerate(_u64(320000)):
            port.inp[580 + i] = w
        port.inp[105] = 3                       # rebooted
        p._poll_uptime()
        p._poll_energy()
        self.assertEqual(_energy_values(sent)["energy_active_import"],
                         "1000.000")

    def test_fmb_reboot_invalidate_drops_ratio_too(self):
        port = _FakePort((1, 0, 7, 3), [80000] * 5, k=(4000, 4000, 4000))
        p, sent, patcher = _poller(port, per_phase=False)
        self.addCleanup(patcher.stop)
        p.setup()
        p._poll_energy()
        port.k = (2000, 2000, 2000)
        p.fmb_firmware_invalidate()
        p._poll_energy()
        self.assertEqual(_energy_values(sent)["energy_active_import"],
                         "500.000")

    def test_per_phase_read_failure_is_logged_not_swallowed(self):
        port = _FakePort((1, 0, 7, 9), [320000] * 5)
        p, _sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        real = port.read_input_registers

        def failing(addr, start, count):
            if start == 600:
                raise IOError("crc")
            return real(addr, start, count)
        port.read_input_registers = failing
        with self.assertLogs(p.log, level="DEBUG") as cm:
            p._poll_energy()
        self.assertTrue(any("per-phase energy" in m for m in cm.output),
                        cm.output)


def _markers(sent) -> list[tuple[str, bool]]:
    return [(v, r) for t, v, r in sent if t == BASE + "/meta/energy_unit"]


class TestEnergyUnitMarkerTiming(unittest.TestCase):
    """Review A1: the marker says «energy_* are converted Wh». Published at
    setup, it would sit over a raw value an old bridge left retained for as
    long as the version stays unreadable — so it waits for the first
    converted publish of every enabled energy batch, and goes out once."""

    def test_no_marker_at_setup(self):
        port = _FakePort((1, 0, 7, 9), [320000] * 5)
        p, sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        self.assertEqual(_markers(sent), [])

    def test_no_marker_while_fail_closed(self):
        port = _FakePort(None, [320000] * 5, phases=(320000,) * 3)
        p, sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        with self.assertLogs(p.log, level="WARNING"):
            p._poll_energy()
            p._poll_energy()
        self.assertEqual(_markers(sent), [])

    def test_marker_once_after_the_first_converted_publish(self):
        port = _FakePort(None, [320000] * 5, phases=(320000,) * 3)
        p, sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        with self.assertLogs(p.log, level="WARNING"):
            p._poll_energy()
        port.fw = (1, 0, 7, 9)
        p._poll_energy()
        p._poll_energy()
        self.assertEqual(_markers(sent), [("Wh", True)])
        # After the values, never before them.
        first_value = next(i for i, (t, _v, _r) in enumerate(sent)
                           if t == BASE + "/controls/energy_active_import")
        marker_at = next(i for i, (t, _v, _r) in enumerate(sent)
                         if t == BASE + "/meta/energy_unit")
        self.assertGreater(marker_at, first_value)

    def test_marker_waits_for_the_per_phase_batch_too(self):
        port = _FakePort((1, 0, 7, 9), [320000] * 5, phases=(320000,) * 3)
        p, sent, patcher = _poller(port)
        self.addCleanup(patcher.stop)
        p.setup()
        real = port.read_input_registers

        def failing(addr, start, count):
            if start == 600:
                raise IOError("crc")
            return real(addr, start, count)
        port.read_input_registers = failing
        p._poll_energy()
        self.assertEqual(_markers(sent), [])
        port.read_input_registers = real
        p._poll_energy()
        self.assertEqual(_markers(sent), [("Wh", True)])


class TestUptimeRollback(unittest.TestCase):
    """Review A7: the reboot reset must not live inside a silent except."""

    def test_uptime_read_failure_is_logged_at_debug(self):
        port = _FakePort((1, 0, 7, 9), [320000] * 5)
        p, _sent, patcher = _poller(port, per_phase=False)
        self.addCleanup(patcher.stop)
        real = port.read_input_registers

        def failing(addr, start, count):
            if start == 105:
                raise IOError("timeout")
            return real(addr, start, count)
        port.read_input_registers = failing
        with self.assertLogs(p.log, level="DEBUG") as cm:
            p._poll_uptime()
        self.assertTrue(any(m.startswith("DEBUG") and "uptime poll" in m
                            for m in cm.output), cm.output)

    def test_short_uptime_reply_is_logged_and_keeps_the_last_value(self):
        port = _FakePort((1, 0, 7, 9), [320000] * 5)
        p, _sent, patcher = _poller(port, per_phase=False)
        self.addCleanup(patcher.stop)
        port.inp[105], port.inp[106] = 5000, 0
        p._poll_uptime()
        port.read_input_registers = lambda a, s, c: [7]
        with self.assertLogs(p.log, level="DEBUG") as cm:
            p._poll_uptime()
        self.assertTrue(any("uptime poll" in m for m in cm.output), cm.output)
        self.assertEqual(p._uptime_last, 5000)

    def test_rollback_resets_version_and_ratio(self):
        port = _FakePort((1, 0, 7, 3), [80000] * 5)
        p, _sent, patcher = _poller(port, per_phase=False)
        self.addCleanup(patcher.stop)
        p.setup()
        self.assertIsNotNone(p._fw_version)
        self.assertIsNotNone(p._ct_k_x1000)
        port.inp[105], port.inp[106] = 5000, 0
        p._poll_uptime()
        port.inp[105] = 3
        p._poll_uptime()
        self.assertIsNone(p._fw_version)
        self.assertIsNone(p._ct_k_x1000)


class TestEnergyWhHelper(unittest.TestCase):

    def test_constants_match_the_firmware_contract(self):
        # CE-02m-3 EN_METER_ENERGY_UNITS_PER_KWH = 320000 → 320 per Wh.
        self.assertEqual(bridge_dtv_ce.CE_ENERGY_UNITS_PER_WH, 320)
        self.assertEqual(bridge_dtv_ce.CE_ENERGY_PRIMARY_FW, (1, 0, 7, 4))
        self.assertEqual(bridge_dtv_ce.CE_ENERGY_WH_FW, (1, 0, 7, 11))

    def test_energy_wh(self):
        self.assertEqual(bridge_dtv_ce._energy_wh(320000, 1000), 1000.0)
        self.assertEqual(bridge_dtv_ce._energy_wh(80000, 4000), 1000.0)
        self.assertEqual(bridge_dtv_ce._energy_wh(1, 1000), 0.003125)
        self.assertEqual(bridge_dtv_ce._energy_wh(1234, 1000, 1), 1234.0)


if __name__ == "__main__":
    unittest.main()
