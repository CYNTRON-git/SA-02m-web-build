#!/usr/bin/env python3
"""Per-MQTT-message cost on the shared ARM SoC is O(1), not a catalogue walk.

Weekly audit 2026-09-08, B6: `on_mqtt_message` ran
`registry.take_unreachable_transitions()` — a full `query_devices()` over
every device × item — on EVERY message, retained storm included (~269 topics
on the bench), and `provisioner.note()` re-read the /etc device document per
matching `/meta/*` message. Both are gated now: the sweep runs only when an
availability topic (`/devices/<id>/meta/error`, `<control>/meta/error`) can
change its answer; the provisioner reads the document once per new id and
otherwise trusts an mtime-keyed cache. Call counts, not prose.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_alice.client import auto_provision as ap  # noqa: E402
from sa02m_alice.client import device_registry as dr  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402

MAIN_PY = Path(__file__).resolve().parents[1] / "sa02m_alice" / "client" / "main.py"

DO_TOPIC = "/devices/mr02m-COM3-10/controls/do_1"
DOC = {
    "rooms": [],
    "devices": [{
        "id": "d1", "name": "Lamp", "type": "devices.types.light",
        "capabilities": [{
            "type": "devices.capabilities.on_off", "mqtt": DO_TOPIC,
            "retrievable": True, "reportable": True,
            "parameters": {"instance": "on"},
        }],
        "properties": [],
    }],
}


class _Spy(dr.DeviceRegistry):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sweeps = 0

    def query_devices(self, device_ids=None):
        self.sweeps += 1
        return super().query_devices(device_ids)


class TestUnreachableSweepIsGated(unittest.TestCase):
    def test_value_messages_never_sweep(self):
        reg = _Spy(DOC)
        for i in range(50):
            reg.note_mqtt(DO_TOPIC, str(i % 2))
            self.assertEqual(reg.take_unreachable_transitions(DO_TOPIC), [])
            reg.note_mqtt("/devices/mr02m-COM3-10/controls/uptime_s", str(i))
            reg.take_unreachable_transitions(
                "/devices/mr02m-COM3-10/controls/uptime_s")
        self.assertEqual(reg.sweeps, 0)

    def test_availability_topics_sweep_and_edge_fires_once(self):
        reg = _Spy(DOC)
        reg.note_mqtt(DO_TOPIC, "1")
        reg.note_mqtt("/devices/mr02m-COM3-10/meta/error", "r")
        stubs = reg.take_unreachable_transitions("/devices/mr02m-COM3-10/meta/error")
        self.assertEqual(reg.sweeps, 1)
        self.assertEqual([s["id"] for s in stubs], ["d1"])
        self.assertEqual(stubs[0]["error_code"], C.ERR_DEVICE_UNREACHABLE)
        # Recovery arrives on the same topic class: the sweep runs, the flag
        # resets, and the next down-edge is announced again.
        reg.note_mqtt("/devices/mr02m-COM3-10/meta/error", "")
        self.assertEqual(
            reg.take_unreachable_transitions("/devices/mr02m-COM3-10/meta/error"), [])
        self.assertEqual(reg.sweeps, 2)
        # A control-level flag is the other topic class that earns a sweep.
        # On a slave that just published live it is a busy bus, not offline
        # (contract §Error codes), so the sweep runs and finds no edge.
        reg.note_mqtt(DO_TOPIC + "/meta/error", "r")
        self.assertEqual(
            reg.take_unreachable_transitions(DO_TOPIC + "/meta/error"), [])
        self.assertEqual(reg.sweeps, 3)

    def test_no_topic_means_the_snapshot_path_still_sweeps(self):
        reg = _Spy(DOC)
        reg.take_unreachable_transitions()
        self.assertEqual(reg.sweeps, 1)

    def test_topic_classification(self):
        self.assertTrue(dr.is_availability_topic("/devices/x/meta/error"))
        self.assertTrue(dr.is_availability_topic("/devices/x/controls/do/meta/error"))
        self.assertFalse(dr.is_availability_topic("/devices/x/controls/do"))
        self.assertFalse(dr.is_availability_topic("/devices/x/meta/name"))
        self.assertFalse(dr.is_availability_topic("/devices/x/controls/do/meta/type"))
        self.assertFalse(dr.is_availability_topic(""))

    def test_main_passes_the_topic_from_on_mqtt_message(self):
        # The gate lives in the registry; main.py must hand it the topic —
        # a bare call there is the pre-1.0.6.39 per-message sweep again.
        text = MAIN_PY.read_text(encoding="utf-8")
        m = re.search(r"def on_mqtt_message\(.*?\n(?=    def |\n    # Main)", text, re.S)
        self.assertIsNotNone(m, "on_mqtt_message not found in main.py")
        body = m.group(0)
        self.assertIn("registry.take_unreachable_transitions(topic)", body)
        self.assertNotIn("registry.take_unreachable_transitions()", body)


def _write(path: str, doc: dict, mtime_ns: int) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.utime(path, ns=(mtime_ns, mtime_ns))


class TestProvisionerDoesNotRereadPerMessage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sa02m-alice-devices.conf")
        _write(self.path, {"rooms": [], "groups": [], "devices": []}, 1_000_000_000)
        self.loads = 0

        def load():
            self.loads += 1
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)

        self.prov = ap.AutoProvisioner(
            load=load, save=lambda doc: None, clock=lambda: 0.0,
            settle_s=60.0, yaml_ids=lambda: set(), path=self.path,
        )

    def test_repeated_meta_messages_read_the_document_once(self):
        for _ in range(20):
            self.prov.note("/devices/dtv-COM9-99/meta/name", "DTV")
            self.prov.note("/devices/dtv-COM9-99/meta/driver", "modbus-rtu")
        self.assertEqual(self.loads, 1)
        # A second id with the document unchanged: the mtime cache answers.
        self.prov.note("/devices/ce02m3-COM9-7/meta/name", "CE")
        self.assertEqual(self.loads, 1)

    def test_a_changed_document_is_read_again(self):
        self.prov.note("/devices/dtv-COM9-99/meta/name", "DTV")
        self.assertEqual(self.loads, 1)
        _write(self.path, {"rooms": [], "groups": [], "devices": [{
            "id": "x", "name": "X", "type": "devices.types.sensor.climate",
            "capabilities": [], "properties": [{
                "type": "devices.properties.float",
                "mqtt": "/devices/ce02m3-COM9-7/controls/voltage_a",
                "parameters": {"instance": "voltage", "unit": "unit.volt"},
            }],
        }]}, 2_000_000_000)
        self.assertIsNone(self.prov.note("/devices/ce02m3-COM9-7/meta/name", "CE"))
        self.assertEqual(self.loads, 2)

    def test_no_file_means_no_cache_and_no_stale_answer(self):
        # An injected loader with no backing file (the test-double idiom):
        # every NEW id still reads; a pending id never re-reads.
        store = {"rooms": [], "groups": [], "devices": []}
        loads = {"n": 0}

        def load():
            loads["n"] += 1
            return store

        prov = ap.AutoProvisioner(
            load=load, save=lambda doc: None, clock=lambda: 0.0,
            settle_s=60.0, yaml_ids=lambda: set(),
            path=os.path.join(self.tmp.name, "absent.conf"),
        )
        for _ in range(5):
            prov.note("/devices/dtv-COM9-99/meta/name", "DTV")
        self.assertEqual(loads["n"], 1)
        prov.note("/devices/dtv-COM9-98/meta/name", "DTV")
        self.assertEqual(loads["n"], 2)


if __name__ == "__main__":
    unittest.main()
