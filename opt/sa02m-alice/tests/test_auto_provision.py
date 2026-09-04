"""MQTT auto-provision of DTV / CE-02m-3 into the Alice document."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client import auto_provision as ap  # noqa: E402
from sa02m_alice.common import config_store  # noqa: E402


def _dtv_topics(mid: str):
    return {
        "/devices/%s/controls/temp_bme280" % mid,
        "/devices/%s/controls/humidity_bme280" % mid,
        "/devices/%s/controls/pressure_bme280_kpa" % mid,
        "/devices/%s/controls/eco2_bme680" % mid,
        "/devices/%s/controls/tvoc_zmod" % mid,
        "/devices/%s/controls/presence" % mid,
    }


def _ce_topics(mid: str, *, per_phase_energy: bool = True):
    out = set()
    for ph in "abc":
        out.add("/devices/%s/controls/voltage_%s" % (mid, ph))
        out.add("/devices/%s/controls/current_%s" % (mid, ph))
        out.add("/devices/%s/controls/power_%s" % (mid, ph))
        if per_phase_energy:
            out.add("/devices/%s/controls/energy_active_import_%s" % (mid, ph))
    out.add("/devices/%s/controls/energy_active_import" % mid)
    return out


def _insts(dev):
    return [
        (p.get("parameters") or {}).get("instance")
        for p in (dev.get("properties") or [])
        if isinstance(p, dict)
    ]


def _mqtt_ids(dev):
    out = []
    for p in dev.get("properties") or []:
        if isinstance(p, dict):
            out.append(p.get("mqtt") or "")
    return out


def _fixture_15():
    """The live stand shape: 15 devices, DTV/CE prefixes already mapped."""
    devices = [
        {
            "id": "2becf4cb-7578-4c50-9b83-9d3d3055d0d1",
            "name": "Температура в цеху",
            "type": "devices.types.sensor.climate",
            "capabilities": [],
            "properties": [
                {
                    "type": "devices.properties.float",
                    "mqtt": "/devices/dtv-COM4-3/controls/temp_bme280",
                    "parameters": {
                        "instance": "temperature",
                        "unit": "unit.temperature.celsius",
                    },
                }
            ],
        },
        {
            "id": "a59e6004-b2b7-46da-a0a3-054e37e37bd0",
            "name": "Температура 12АИ канал 7",
            "type": "devices.types.sensor.climate",
            "capabilities": [],
            "properties": [
                {
                    "type": "devices.properties.float",
                    "mqtt": "/devices/mr02m-COM4-12/controls/ai_7",
                    "parameters": {
                        "instance": "temperature",
                        "unit": "unit.temperature.celsius",
                    },
                }
            ],
        },
        {
            "id": "2ef5d7c0-cb52-48a6-87bc-d6c79df4e38f",
            "name": "ДТВ цех",
            "type": "devices.types.sensor.climate",
            "capabilities": [],
            "properties": [
                {
                    "type": "devices.properties.float",
                    "mqtt": "/devices/dtv-COM4-3/controls/humidity_bme280",
                    "parameters": {"instance": "humidity", "unit": "unit.percent"},
                }
            ],
        },
        {
            "id": "8a60f8a4-a5d9-45fb-a46b-fcddc80e03f0",
            "name": "Анализатор фаза А",
            "type": "devices.types.smart_meter.electricity",
            "capabilities": [],
            "properties": [
                {
                    "type": "devices.properties.float",
                    "mqtt": "/devices/ce02m3-COM2-14/controls/voltage_a",
                    "parameters": {"instance": "voltage", "unit": "unit.volt"},
                }
            ],
        },
        {
            "id": "8fc79c84-1632-458c-bcde-8aa1156ab6e9",
            "name": "Анализатор фаза В",
            "type": "devices.types.smart_meter.electricity",
            "capabilities": [],
            "properties": [
                {
                    "type": "devices.properties.float",
                    "mqtt": "/devices/ce02m3-COM2-14/controls/voltage_b",
                    "parameters": {"instance": "voltage", "unit": "unit.volt"},
                }
            ],
        },
        {
            "id": "cb7cedfd-2e33-48e2-bc4c-d7df5e2631a8",
            "name": "Анализатор фаза С",
            "type": "devices.types.smart_meter.electricity",
            "capabilities": [],
            "properties": [
                {
                    "type": "devices.properties.float",
                    "mqtt": "/devices/ce02m3-COM2-14/controls/energy_active_import_c",
                    "parameters": {
                        "instance": "electricity_meter",
                        "unit": "unit.kilowatt_hour",
                    },
                    "scale": 0.001,
                }
            ],
        },
        {
            "id": "57972a98-8805-405c-8acd-af6396258785",
            "name": "Сигнализация ДТВ",
            "type": "devices.types.other",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "mqtt": "/devices/dtv-COM4-3/controls/buzzer",
                    "parameters": {"instance": "on"},
                }
            ],
            "properties": [],
        },
        {
            "id": "bench-lamp",
            "name": "Сирена стенд",
            "type": "devices.types.other",
            "icon": "siren",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "mqtt": "/devices/SA-02m/controls/alarm_led",
                    "parameters": {"instance": "on"},
                }
            ],
            "properties": [],
        },
        {
            "id": "bench-switch-1",
            "name": "Свет 1",
            "type": "devices.types.switch",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "mqtt": "/devices/mr02m-COM3-10/controls/do_1",
                    "parameters": {"instance": "on"},
                }
            ],
            "properties": [],
        },
        {
            "id": "bench-socket-2",
            "name": "Свет 2",
            "type": "devices.types.socket",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "mqtt": "/devices/mr02m-COM3-10/controls/do_2",
                    "parameters": {"instance": "on"},
                }
            ],
            "properties": [],
        },
        {
            "id": "carel-pcomini",
            "name": "Карел c.pCOmini",
            "type": "devices.types.ventilation",
            "icon": "fan",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "mqtt": "/devices/carel-COM3-1/controls/unit_on",
                    "parameters": {"instance": "on"},
                }
            ],
            "properties": [],
        },
        {
            "id": "carel-uaria",
            "name": "Карел uAria",
            "type": "devices.types.ventilation",
            "icon": "fan",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "mqtt": "/devices/carel-COM3-2/controls/unit_on",
                    "parameters": {"instance": "on"},
                }
            ],
            "properties": [],
        },
        {
            "id": "led-strip",
            "name": "LED лента",
            "type": "devices.types.light",
            "icon": "bulb",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "mqtt": "/devices/led-COM3-13/controls/power",
                    "parameters": {"instance": "on"},
                }
            ],
            "properties": [],
        },
        {
            "id": "bench-light-3",
            "name": "Спальня",
            "type": "devices.types.light",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "mqtt": "/devices/mr02m-COM3-10/controls/do_3",
                    "parameters": {"instance": "on"},
                }
            ],
            "properties": [],
        },
        {
            "id": "bench-light-4",
            "name": "Гостиная",
            "type": "devices.types.light",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "mqtt": "/devices/mr02m-COM3-10/controls/do_4",
                    "parameters": {"instance": "on"},
                }
            ],
            "properties": [],
        },
    ]
    return {"rooms": [{"id": "bench", "name": "Стенд", "devices": []}], "devices": devices}


class TestMetaParse(unittest.TestCase):
    def test_test_meta_name_and_yaml_ids(self):
        self.assertTrue(ap.is_test_meta_name("DTV test"))
        self.assertTrue(ap.is_test_meta_name("CE-02m-3 test"))
        self.assertFalse(ap.is_test_meta_name("DTV-RS-485 (COM4 addr=3)"))
        self.assertEqual(
            ap.mqtt_ids_from_yaml_doc(
                {"devices": [{"id": "dtv-COM4-3"}, {"id": "ce02m3-COM2-14"}]}
            ),
            {"dtv-COM4-3", "ce02m3-COM2-14"},
        )
        self.assertFalse(ap.may_provision("dtv-COM9-99", None))
        self.assertTrue(
            ap.may_provision("dtv-COM9-99", None, yaml_ids={"dtv-COM9-99"})
        )
        self.assertTrue(
            ap.may_provision("dtv-COM9-99", _dtv_topics("dtv-COM9-99"))
        )

    def test_meta_topics(self):
        self.assertEqual(
            ap.mqtt_id_from_meta_topic("/devices/dtv-COM9-99/meta/name"),
            "dtv-COM9-99",
        )
        self.assertEqual(
            ap.mqtt_id_from_meta_topic("/devices/ce02m3-COM9-99/meta/driver"),
            "ce02m3-COM9-99",
        )
        self.assertIsNone(ap.mqtt_id_from_meta_topic("/devices/dtv-COM9-99/controls/temp_bme280"))
        self.assertTrue(ap.is_dtv_id("dtv-COM9-99"))
        self.assertTrue(ap.is_ce_id("ce02m3-COM9-99"))
        self.assertFalse(ap.is_dtv_id("ce02m3-COM9-99"))


class TestProvisionNew(unittest.TestCase):
    def test_dtv_from_meta_and_topics(self):
        mid = "dtv-COM9-99"
        doc = {"rooms": [], "devices": []}
        _doc, added = ap.provision(doc, mid, _dtv_topics(mid))
        self.assertEqual(len(added), 1)
        dev = added[0]
        self.assertEqual(dev["type"], "devices.types.sensor.climate")
        self.assertEqual(dev["name"], "ДТВ COM9 99")
        self.assertEqual(
            _insts(dev),
            ["temperature", "humidity", "pressure", "co2_level", "tvoc", "motion"],
        )
        self.assertIn("/devices/dtv-COM9-99/controls/temp_bme280", _mqtt_ids(dev))
        press = [p for p in dev["properties"] if (p.get("parameters") or {}).get("instance") == "pressure"][0]
        self.assertEqual(press.get("scale"), 7.50062)

    def test_dtv_only_topics_that_exist(self):
        mid = "dtv-COM9-99"
        present = {
            "/devices/%s/controls/temp_bme680" % mid,
            "/devices/%s/controls/presence" % mid,
        }
        _doc, added = ap.provision({"rooms": [], "devices": []}, mid, present)
        self.assertEqual(_insts(added[0]), ["temperature", "motion"])
        self.assertIn("temp_bme680", added[0]["properties"][0]["mqtt"])

    def test_ce_three_phases_with_per_phase_energy(self):
        mid = "ce02m3-COM9-99"
        _doc, added = ap.provision({"rooms": [], "devices": []}, mid, _ce_topics(mid))
        self.assertEqual(len(added), 3)
        self.assertEqual(
            [d["name"] for d in added],
            ["Анализатор COM9 99 А", "Анализатор COM9 99 В", "Анализатор COM9 99 С"],
        )
        for dev, ph in zip(added, "abc"):
            self.assertEqual(dev["type"], "devices.types.smart_meter.electricity")
            self.assertEqual(_insts(dev), ["amperage", "power", "voltage", "electricity_meter"])
            meter = [p for p in dev["properties"] if (p.get("parameters") or {}).get("instance") == "electricity_meter"][0]
            self.assertEqual(meter["scale"], 0.001)
            self.assertTrue(meter["mqtt"].endswith("energy_active_import_%s" % ph))

    def test_ce_total_energy_on_phase_c_only(self):
        mid = "ce02m3-COM9-99"
        _doc, added = ap.provision(
            {"rooms": [], "devices": []}, mid, _ce_topics(mid, per_phase_energy=False)
        )
        self.assertEqual(len(added), 3)
        self.assertNotIn("electricity_meter", _insts(added[0]))
        self.assertNotIn("electricity_meter", _insts(added[1]))
        self.assertIn("electricity_meter", _insts(added[2]))
        meter = [p for p in added[2]["properties"] if (p.get("parameters") or {}).get("instance") == "electricity_meter"][0]
        self.assertTrue(meter["mqtt"].endswith("energy_active_import"))
        self.assertFalse(meter["mqtt"].endswith("energy_active_import_c"))

    def test_second_pass_is_noop(self):
        mid = "dtv-COM9-99"
        doc = {"rooms": [], "devices": []}
        ap.provision(doc, mid, _dtv_topics(mid))
        ids_after = [d["id"] for d in doc["devices"]]
        _doc, added = ap.provision(doc, mid, _dtv_topics(mid))
        self.assertEqual(added, [])
        self.assertEqual([d["id"] for d in doc["devices"]], ids_after)

    def test_meta_only_without_yaml_does_not_add(self):
        for mid in ("dtv-COM9-99", "ce02m3-COM9-99"):
            _doc, added = ap.provision({"rooms": [], "devices": []}, mid, None)
            self.assertEqual(added, [], mid)

    def test_yaml_listed_meta_only_uses_full_template(self):
        _doc, added = ap.provision(
            {"rooms": [], "devices": []},
            "dtv-COM9-99",
            None,
            yaml_ids={"dtv-COM9-99"},
        )
        self.assertEqual(len(added), 1)
        self.assertEqual(len(added[0]["properties"]), 6)

    def test_test_meta_name_is_ignored_even_with_yaml_and_live(self):
        mid = "dtv-COM9-99"
        _doc, added = ap.provision(
            {"rooms": [], "devices": []},
            mid,
            _dtv_topics(mid),
            yaml_ids={mid},
            meta_name="DTV test",
        )
        self.assertEqual(added, [])

    def test_yaml_plus_live_controls_still_adds(self):
        mid = "dtv-COM9-99"
        _doc, added = ap.provision(
            {"rooms": [], "devices": []},
            mid,
            _dtv_topics(mid),
            yaml_ids={mid},
        )
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["name"], "ДТВ COM9 99")


class TestFixtureUntouched(unittest.TestCase):
    def test_existing_15_stay_when_already_mapped(self):
        doc = _fixture_15()
        self.assertEqual(len(doc["devices"]), 15)
        before = json.dumps(doc, sort_keys=True, ensure_ascii=False)
        _d, a1 = ap.provision(doc, "dtv-COM4-3", _dtv_topics("dtv-COM4-3"))
        _d, a2 = ap.provision(doc, "ce02m3-COM2-14", _ce_topics("ce02m3-COM2-14"))
        self.assertEqual(a1, [])
        self.assertEqual(a2, [])
        self.assertEqual(len(doc["devices"]), 15)
        self.assertEqual(json.dumps(doc, sort_keys=True, ensure_ascii=False), before)

    def test_new_ids_append_without_touching_the_15(self):
        doc = _fixture_15()
        snap = [(d["id"], d["name"], d["type"]) for d in doc["devices"]]
        ap.provision(doc, "dtv-COM9-99", _dtv_topics("dtv-COM9-99"))
        ap.provision(doc, "ce02m3-COM9-99", _ce_topics("ce02m3-COM9-99"))
        self.assertEqual(len(doc["devices"]), 19)
        self.assertEqual([(d["id"], d["name"], d["type"]) for d in doc["devices"][:15]], snap)


class TestCommitAndWatcher(unittest.TestCase):
    def test_commit_persists_then_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "devices.conf")
            config_store.save_devices({"rooms": [], "devices": []}, path)
            added = ap.commit_provision("dtv-COM9-99", _dtv_topics("dtv-COM9-99"), path=path)
            self.assertEqual(len(added), 1)
            again = ap.commit_provision("dtv-COM9-99", _dtv_topics("dtv-COM9-99"), path=path)
            self.assertEqual(again, [])
            loaded = config_store.load_devices(path)
            self.assertEqual(len(loaded["devices"]), 1)

    def test_auto_provisioner_meta_then_controls(self):
        store = {"rooms": [], "devices": []}
        clock = {"t": 0.0}

        def load():
            return store

        def save(doc):
            snap = json.loads(json.dumps(doc))
            store.clear()
            store.update(snap)

        prov = ap.AutoProvisioner(load=load, save=save, clock=lambda: clock["t"], settle_s=1.0)
        extra = prov.note("/devices/dtv-COM9-99/meta/name", "DTV-RS-485 (COM9 addr=99)")
        self.assertEqual(extra, "/devices/dtv-COM9-99/controls/+")
        self.assertFalse(prov.tick())
        for topic in _dtv_topics("dtv-COM9-99"):
            self.assertIsNone(prov.note(topic, "1"))
        clock["t"] = 1.1
        self.assertTrue(prov.tick())
        self.assertEqual(len(store["devices"]), 1)
        clock["t"] = 3.0
        self.assertFalse(prov.tick())
        extra2 = prov.note("/devices/dtv-COM9-99/meta/driver", "modbus-rtu")
        self.assertIsNone(extra2)

    def test_auto_provisioner_skips_mapped_prefix(self):
        store = _fixture_15()
        n = len(store["devices"])
        prov = ap.AutoProvisioner(
            load=lambda: store,
            save=lambda doc: None,
            clock=lambda: 10.0,
            settle_s=0.0,
        )
        extra = prov.note("/devices/dtv-COM4-3/meta/name", "DTV")
        self.assertIsNone(extra)
        self.assertFalse(prov.tick())
        self.assertEqual(len(store["devices"]), n)

    def test_auto_provisioner_meta_only_does_not_add(self):
        store = {"rooms": [], "devices": []}
        clock = {"t": 0.0}
        prov = ap.AutoProvisioner(
            load=lambda: store,
            save=lambda doc: store.update(doc),
            clock=lambda: clock["t"],
            settle_s=1.0,
            yaml_ids=lambda: set(),
        )
        for mid, payload in (
            ("dtv-COM9-99", "DTV-RS-485 (COM9 addr=99)"),
            ("ce02m3-COM9-99", "CE-02m-3 (COM9 addr=99)"),
        ):
            extra = prov.note("/devices/%s/meta/name" % mid, payload)
            self.assertEqual(extra, "/devices/%s/controls/+" % mid)
        clock["t"] = 1.1
        self.assertFalse(prov.tick())
        self.assertEqual(store["devices"], [])

    def test_auto_provisioner_test_name_is_ignored(self):
        store = {"rooms": [], "devices": []}
        clock = {"t": 0.0}
        prov = ap.AutoProvisioner(
            load=lambda: store,
            save=lambda doc: store.update(doc),
            clock=lambda: clock["t"],
            settle_s=0.0,
            yaml_ids=lambda: {"dtv-COM9-99"},
        )
        extra = prov.note("/devices/dtv-COM9-99/meta/name", "DTV test")
        self.assertIsNone(extra)
        for topic in _dtv_topics("dtv-COM9-99"):
            self.assertIsNone(prov.note(topic, "1"))
        clock["t"] = 1.0
        self.assertFalse(prov.tick())
        self.assertEqual(store["devices"], [])

    def test_auto_provisioner_yaml_plus_live_still_adds(self):
        store = {"rooms": [], "devices": []}
        clock = {"t": 0.0}

        def save(doc):
            snap = json.loads(json.dumps(doc))
            store.clear()
            store.update(snap)

        prov = ap.AutoProvisioner(
            load=lambda: store,
            save=save,
            clock=lambda: clock["t"],
            settle_s=1.0,
            yaml_ids=lambda: {"dtv-COM9-99"},
        )
        extra = prov.note("/devices/dtv-COM9-99/meta/name", "DTV-RS-485 (COM9 addr=99)")
        self.assertEqual(extra, "/devices/dtv-COM9-99/controls/+")
        for topic in _dtv_topics("dtv-COM9-99"):
            self.assertIsNone(prov.note(topic, "21.5"))
        clock["t"] = 1.1
        self.assertTrue(prov.tick())
        self.assertEqual(len(store["devices"]), 1)
        ids = [(d["id"], d["name"], d["type"]) for d in store["devices"]]
        clock["t"] = 3.0
        self.assertFalse(prov.tick())
        self.assertEqual([(d["id"], d["name"], d["type"]) for d in store["devices"]], ids)


if __name__ == "__main__":
    unittest.main()
