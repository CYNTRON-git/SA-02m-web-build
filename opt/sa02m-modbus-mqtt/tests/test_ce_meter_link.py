#!/usr/bin/env python3
"""CE-02m-3: a lost link to the measuring chip is an error, not a 0 V reading.

When the firmware loses the ATM90E32 link it answers 0 for every register of
500-679 while the meter itself keeps answering (CE-02m-3 MODBUS_VARIABLES.txt
680-686: «читаются и при потерянной связи с ATM90E32, когда 500–679 отдают
0»). The bridge must not publish those zeros as measurements: it flags every
measurement control with meta/error = "r" and clears it on recovery. On
firmware >= 1.0.7.6 the publish-cycle counter Input 686 tells a lost link
(counter frozen) from a live meter that really measures zero (counter moving).
Rule home: docs/contracts/ce-energy-mqtt.md «Потеря связи с измерительным
чипом».
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

import bridge_mqtt  # noqa: E402
from bridge_dtv_ce import CE02M3Poller  # noqa: E402

DEV = "ce02m3-COMtest-14"
CTRL = f"/devices/{DEV}/controls/"
FW_OBS = (1, 0, 7, 6)      # first firmware with Input 680-686
FW_OLD = (1, 0, 7, 5)


class _FakeMeter:
    """Input map of a CE-02m-3. `link` False = the firmware's lost-link view:
    500-679 read 0. Input 686 advances by one per read while `cycling`."""

    def __init__(self, link=True, cycling=None):
        self.link = link
        self.cycling = link if cycling is None else cycling
        self.cycles = 1000
        self.fail_686 = False
        self.reads: list[tuple[int, int]] = []

    def _live(self, reg):
        if 500 <= reg <= 502:
            return 2300                     # 230.0 V
        if 506 <= reg <= 508:
            return 3980
        if 510 <= reg <= 513:
            return 1500                     # 1.5 A
        if reg == 542:
            return 5000                     # 50.00 Hz
        if reg == 547:
            return 31                       # ASIC 31 °C
        if 518 <= reg <= 541:
            return 100 if reg % 2 == 0 else 0
        if 580 <= reg <= 611:
            return 7 if reg % 4 == 0 else 0
        return 0

    def read_input_registers(self, addr, start, count):
        self.reads.append((start, count))
        out = []
        for reg in range(start, start + count):
            if reg == 686:
                if self.fail_686:
                    raise IOError("timeout")
                if self.cycling:
                    self.cycles = (self.cycles + 1) & 0xFFFF
                out.append(self.cycles)
            elif 500 <= reg <= 679 and not self.link:
                out.append(0)
            else:
                out.append(self._live(reg))
        return out

    def read_holding_registers(self, addr, start, count):
        return [0] * count

    def read_686_count(self):
        return sum(1 for s, c in self.reads if s <= 686 < s + c)


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
    pub._ctrl_errors = {}
    sent: list[tuple[str, str]] = []
    pub.pub = lambda topic, payload, retain=None: sent.append((topic, payload))
    return pub, sent


def _poller(meter, fw=FW_OBS):
    pub, sent = _capturing_publisher()
    p = CE02M3Poller({
        "id": DEV, "type": "ce02m3", "port": "/dev/COMtest", "address": 14,
        "publish_per_phase_energy": True,
    }, pub)
    p._fw_version = fw
    patcher = mock.patch.object(p, "get_port", return_value=meter)
    patcher.start()
    return p, sent, patcher


def _values(sent):
    return [(t[len(CTRL):], v) for t, v in sent
            if t.startswith(CTRL) and "/" not in t[len(CTRL):]]


def _errors(sent):
    """Last meta/error payload per control."""
    out = {}
    for t, v in sent:
        if t.startswith(CTRL) and t.endswith("/meta/error"):
            out[t[len(CTRL):-len("/meta/error")]] = v
    return out


def _measurements(p):
    return {n for n, _u in p._power_controls() + p._energy_controls()}


class _Base(unittest.TestCase):
    def make(self, meter, fw=FW_OBS):
        p, sent, patcher = _poller(meter, fw)
        self.addCleanup(patcher.stop)
        return p, sent


class TestPowerPollLinkLost(_Base):

    def test_lost_link_publishes_no_zero_and_flags_every_measurement(self):
        meter = _FakeMeter(link=False)
        p, sent = self.make(meter)
        p._poll_power()
        p._poll_power()
        self.assertEqual(_values(sent), [], "zeros published as measurements")
        errs = _errors(sent)
        self.assertEqual(set(errs), _measurements(p))
        self.assertEqual(set(errs.values()), {"r"})
        # The meter answers: no device-level error.
        self.assertFalse([t for t, _ in sent
                          if t == f"/devices/{DEV}/meta/error"])

    def test_recovery_clears_the_error_and_publishes_values(self):
        meter = _FakeMeter(link=False)
        p, sent = self.make(meter)
        p._poll_power()
        meter.link = meter.cycling = True
        del sent[:]
        p._poll_power()
        errs = _errors(sent)
        self.assertEqual(set(errs), _measurements(p))
        self.assertEqual(set(errs.values()), {""})
        self.assertIn(("voltage_a", "230.0"), _values(sent))

    def test_live_meter_measuring_zero_is_published_once_686_moves(self):
        # Live chip, everything really 0 (incl. a 0 °C ASIC): the cycle
        # counter keeps moving, so the second poll publishes the zeros.
        meter = _FakeMeter(link=False, cycling=True)
        p, sent = self.make(meter)
        p._poll_power()
        self.assertEqual(_values(sent), [])   # first sight: no baseline yet
        p._poll_power()
        self.assertIn(("voltage_a", "0.0"), _values(sent))
        err_topic = CTRL + "voltage_a/meta/error"
        self.assertEqual([v for t, v in sent if t == err_topic], ["r", ""])
        self.assertEqual(meter.read_686_count(), 2)

    def test_healthy_poll_never_reads_686(self):
        meter = _FakeMeter(link=True)
        p, sent = self.make(meter)
        p._poll_power()
        p._poll_power()
        self.assertEqual(meter.read_686_count(), 0)
        self.assertEqual(_errors(sent), {})

    def test_old_firmware_zero_block_is_lost_without_686(self):
        for fw in (FW_OLD, None):
            with self.subTest(fw=fw):
                meter = _FakeMeter(link=False, cycling=True)
                p, sent = self.make(meter, fw)
                p._poll_power()
                p._poll_power()
                self.assertEqual(_values(sent), [])
                self.assertEqual(set(_errors(sent).values()), {"r"})
                self.assertEqual(meter.read_686_count(), 0)

    def test_686_read_failure_fails_closed(self):
        meter = _FakeMeter(link=False, cycling=True)
        meter.fail_686 = True
        p, sent = self.make(meter)
        p._poll_power()
        p._poll_power()
        self.assertEqual(_values(sent), [])
        self.assertEqual(set(_errors(sent).values()), {"r"})


class TestEnergyPollLinkLost(_Base):

    def _energy_values(self, sent):
        return [(n, v) for n, v in _values(sent) if n.startswith("energy_")]

    def test_energy_not_published_while_link_is_lost(self):
        meter = _FakeMeter(link=False)
        p, sent = self.make(meter)
        p._poll_power()
        del meter.reads[:]
        p._poll_energy()
        self.assertEqual(self._energy_values(sent), [])
        # Known-lost: the energy poll does not even touch the bus.
        self.assertEqual(meter.reads, [])

    def test_zero_energy_with_zero_cache_is_lost(self):
        # Energy poll first (no power poll yet): totals 0 and 542-547 0.
        for fw in (FW_OBS, FW_OLD):
            with self.subTest(fw=fw):
                meter = _FakeMeter(link=False)
                p, sent = self.make(meter, fw)
                p._poll_energy()
                self.assertEqual(self._energy_values(sent), [])
                self.assertEqual(_errors(sent).get("energy_active_import"), "r")

    def test_fresh_meter_zero_energy_is_published(self):
        meter = _FakeMeter(link=True)
        meter._live = lambda reg, live=meter._live: (
            0 if 580 <= reg <= 611 else live(reg))
        p, sent = self.make(meter)
        p._poll_energy()
        self.assertIn(("energy_active_import", "0.000"),
                      self._energy_values(sent))
        self.assertEqual(_errors(sent), {})

    def test_diag_and_uptime_still_published_while_lost(self):
        meter = _FakeMeter(link=False)
        p, sent = self.make(meter)
        p._poll_power()
        p._poll_uptime()
        p._poll_diag()
        names = {n for n, _ in _values(sent)}
        self.assertEqual(names, {"uptime_s", "mcu_vdd", "mcu_temp"})


if __name__ == "__main__":
    unittest.main()
