#!/usr/bin/env python3
"""CE-02m-3: control meta lands under the SAME name the value is published on.

Defect (backlog 2026-10-07): setup published meta/readonly + meta/units=W for a
control named `total` while _poll_power publishes the value as `power_total` —
`power_total` had no units and `.../controls/total/meta/*` was a retained
orphan on every board. The same survey found valued controls with no meta at
all and meta for channels the config turns off.

The check is over the broker view, not one name: a real MQTTPublisher with its
transport captured, one full CE cycle (setup + power + energy + uptime + diag)
over a fake port, for every config in CONFIGS, then
  - no meta topic for a control name that never received a value (orphans —
    incl. a channel disabled in channels_enabled or a phase left out of
    `phases`),
  - every valued control carries meta/readonly,
  - every valued control carries meta/units except the dimensionless ones
    (DIMENSIONLESS); with every channel on the unit-less set must EQUAL it, so
    a stale exemption fails too.
"""
from __future__ import annotations

import re
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

# No unit by definition: cos φ is a ratio (firmware 543-546 «×0.001», no unit)
# and ct_ratio_x1000 is the CT ratio K x1000.
DIMENSIONLESS = frozenset({"pf_a", "pf_b", "pf_c", "pf_total",
                           "ct_ratio_x1000"})

_ALL_ON = {
    "voltages": True, "line_voltages": True, "currents": True,
    "power_active": True, "power_reactive": True,
    "power_apparent": True, "power_factor": True,
    "frequency": True, "energy": True,
}

CONFIGS = {
    "all_on": {"phases": ["A", "B", "C"], "publish_per_phase_energy": True,
               "channels_enabled": _ALL_ON},
    # Shipped defaults: apparent power off, per-phase energy off.
    "defaults": {},
    "one_phase_most_off": {
        "phases": ["A"],
        "channels_enabled": {
            "voltages": True, "line_voltages": False, "currents": True,
            "power_active": True, "power_reactive": False,
            "power_apparent": False, "power_factor": False,
            "frequency": False, "energy": False,
        },
    },
}


class _FakePort:
    """Answers every read with `count` non-zero registers (no 0x8000 gaps)."""

    def read_input_registers(self, addr, start, count):
        return [(start + i) % 0x7FFF + 1 for i in range(count)]

    def read_holding_registers(self, addr, start, count):
        return [1] * count


def _capturing_publisher():
    pub = bridge_mqtt.MQTTPublisher.__new__(bridge_mqtt.MQTTPublisher)
    pub._lock = threading.Lock()
    pub._retain = True
    pub._unchanged_republish_s = 0
    pub._last_pub = {}
    pub._ctrl_meta = {}
    pub._ctrl_meta_pub = {}
    pub._dev_meta = {}
    pub._dev_meta_pub = {}
    topics: list[str] = []
    pub.pub = lambda topic, payload, retain=None: topics.append(topic)
    return pub, topics


def _full_cycle_topics(extra_cfg: dict) -> list[str]:
    pub, topics = _capturing_publisher()
    cfg = {"id": DEV, "type": "ce02m3", "port": "/dev/COMtest", "address": 14}
    cfg.update(extra_cfg)
    poller = CE02M3Poller(cfg, pub)
    with mock.patch.object(poller, "get_port", return_value=_FakePort()):
        poller.setup()
        poller._poll_power()
        poller._poll_energy()
        poller._poll_uptime()
        poller._poll_diag()
    return topics


_VALUE_RE = re.compile(r"^/devices/%s/controls/([^/]+)$" % re.escape(DEV))
_META_RE = re.compile(
    r"^/devices/%s/controls/([^/]+)/meta(?:/([^/]+))?$" % re.escape(DEV))


def _split(topics):
    valued: set[str] = set()
    meta: dict[str, set[str]] = {}
    for t in topics:
        m = _VALUE_RE.match(t)
        if m:
            valued.add(m.group(1))
            continue
        m = _META_RE.match(t)
        if m:
            meta.setdefault(m.group(1), set()).add(m.group(2) or "<blob>")
    return valued, meta


class TestCe02m3MetaNames(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.runs = {n: _split(_full_cycle_topics(c))
                     for n, c in CONFIGS.items()}

    def test_capture_is_not_vacuous(self):
        # An empty capture would pass the orphan check trivially.
        valued, meta = self.runs["all_on"]
        self.assertIn("power_total", valued)
        self.assertIn("voltage_a", valued)
        self.assertGreaterEqual(len(valued), 40)
        self.assertGreaterEqual(len(meta), 40)
        valued, _ = self.runs["one_phase_most_off"]
        self.assertIn("voltage_a", valued)
        self.assertNotIn("voltage_b", valued)

    def test_no_meta_for_a_name_that_never_gets_a_value(self):
        for cfg, (valued, meta) in self.runs.items():
            with self.subTest(cfg=cfg):
                orphans = {n: sorted(k) for n, k in meta.items()
                           if n not in valued}
                self.assertEqual(
                    orphans, {},
                    "meta published under names no value is published on")

    def test_every_valued_control_is_readonly(self):
        for cfg, (valued, meta) in self.runs.items():
            with self.subTest(cfg=cfg):
                self.assertEqual(
                    sorted(n for n in valued
                           if "readonly" not in meta.get(n, set())), [])

    def test_every_valued_control_carries_units_unless_dimensionless(self):
        for cfg, (valued, meta) in self.runs.items():
            with self.subTest(cfg=cfg):
                unitless = {n for n in valued
                            if "units" not in meta.get(n, set())}
                self.assertEqual(sorted(unitless - DIMENSIONLESS), [])
        valued, meta = self.runs["all_on"]
        self.assertEqual(
            {n for n in valued if "units" not in meta.get(n, set())},
            set(DIMENSIONLESS),
            "a DIMENSIONLESS name now carries units or is no longer valued")

    def test_power_total_carries_watts(self):
        _, meta = self.runs["all_on"]
        self.assertIn("units", meta.get("power_total", set()))
        self.assertIn("readonly", meta.get("power_total", set()))


if __name__ == "__main__":
    unittest.main()
