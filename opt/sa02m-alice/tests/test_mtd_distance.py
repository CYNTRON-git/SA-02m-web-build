"""MTD distance remains meters and is isolated from the Yandex profile."""
import copy
import concurrent.futures
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sa02m_alice.client import auto_provision as ap
from sa02m_alice.client.device_registry import DeviceRegistry
from sa02m_alice.common import constants as C
from sa02m_alice.common.config_store import load_devices, save_devices
from sa02m_alice.config import models

MID = "mtdx62-mb-COM3-20"
BASE = "/devices/" + MID
DISTANCE = BASE + "/controls/target_distance"
CONTROLS = {"presence_status", "illuminance", "target_distance"}


def old_doc():
    dev = ap.build_mtd(MID, {"presence_status", "illuminance"})
    dev.update(id="stable-mtd", name="My presence", room_id="room1", alice_visible=False)
    return {"rooms": [{"id": "room1", "name": "Room"}], "devices": [dev]}


class TestMtdDistance(unittest.TestCase):
    def test_new_sensor_distance_has_exact_unit_and_no_scale(self):
        doc, changed = ap.provision({"devices": []}, MID, CONTROLS)
        self.assertEqual(len(changed), 1)
        prop = doc["devices"][0]["properties"][-1]
        self.assertEqual(prop["parameters"], {"instance": "target_distance", "unit": "unit.length.meter"})
        self.assertEqual(prop["mqtt"], DISTANCE)
        self.assertIs(prop["cloud_only"], True)
        self.assertNotIn("scale", prop)
        self.assertIsNone(models.validate_device(doc["devices"][0])[1])
        prop.pop("cloud_only")
        self.assertEqual(models.validate_device(doc["devices"][0])[1], "invalid float property instance")

    def test_existing_preserves_every_field_and_is_idempotent(self):
        doc = old_doc()
        before = copy.deepcopy(doc)
        _, changed = ap.provision(doc, MID, {DISTANCE})
        self.assertEqual(len(changed), 1)
        self.assertEqual(len(doc["devices"]), 1)
        self.assertEqual(len(doc["devices"][0]["properties"]), 3)
        restored = copy.deepcopy(doc)
        restored["devices"][0]["properties"].pop()
        self.assertEqual(restored, before)
        self.assertEqual(ap.provision(doc, MID, CONTROLS)[1], [])

    def test_missing_live_control_and_test_device_are_unchanged(self):
        for topics, name in [(None, None), ({"illuminance"}, None), ({DISTANCE + "/meta/type"}, None), (CONTROLS, "test sensor")]:
            doc = old_doc()
            before = copy.deepcopy(doc)
            self.assertEqual(ap.provision(doc, MID, topics, yaml_ids={MID}, meta_name=name)[1], [])
            self.assertEqual(doc, before)

    def test_explicit_mapping_and_ambiguous_devices_are_unchanged(self):
        for case in ("instance", "topic", "ambiguous", "wrong_type"):
            doc = old_doc()
            dev = doc["devices"][0]
            if case in ("instance", "topic"):
                prop = copy.deepcopy(dev["properties"][1])
                if case == "instance":
                    prop["parameters"]["instance"] = "target_distance"
                else:
                    prop["mqtt"] = DISTANCE
                dev["properties"].append(prop)
            elif case == "ambiguous":
                doc["devices"].append(copy.deepcopy(dev))
            else:
                dev["type"] = "devices.types.other"
            before = copy.deepcopy(doc)
            self.assertEqual(ap.provision(doc, MID, CONTROLS)[1], [])
            self.assertEqual(doc, before)

    def test_validation_failure_does_not_mutate(self):
        doc = old_doc()
        before = copy.deepcopy(doc)
        with mock.patch.object(models, "validate_device", return_value=(None, "invalid")):
            self.assertEqual(ap.provision(doc, MID, CONTROLS)[1], [])
        self.assertEqual(doc, before)

    def test_existing_nullable_capabilities_are_preserved(self):
        doc = old_doc()
        doc["devices"][0]["capabilities"] = None
        self.assertIsNone(models.validate_device(doc["devices"][0])[1])
        self.assertEqual(len(ap.provision(doc, MID, CONTROLS)[1]), 1)
        self.assertIsNone(doc["devices"][0]["capabilities"])

    def test_runtime_retained_upgrade_late_distance_and_failed_save_retry(self):
        for late in (False, True):
            doc = old_doc()
            saved = []
            clock = [0.0]
            def save(value):
                saved.append(copy.deepcopy(value))
                doc.clear()
                doc.update(copy.deepcopy(value))
            with tempfile.TemporaryDirectory() as td:
                watcher = ap.AutoProvisioner(load=lambda: doc, save=save,
                    clock=lambda: clock[0], yaml_ids=lambda: set(), path=td + "/devices.json")
                self.assertEqual(watcher.note(BASE + "/meta/name", "Presence"), BASE + "/controls/+")
                watcher.note(BASE + "/controls/presence_status", "0")
                if late:
                    clock[0] = 2
                    self.assertFalse(watcher.tick())
                watcher.note(DISTANCE, "0")
                clock[0] += 2
                before = copy.deepcopy(doc)
                with mock.patch.object(watcher, "_save", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        watcher.tick()
                self.assertEqual(doc, before)
                self.assertTrue(watcher.tick())
                self.assertEqual(len(saved), 1)
                self.assertEqual(len(doc["devices"][0]["properties"]), 3)
                watcher.note(BASE + "/meta/driver", "mtd")
                watcher.note(DISTANCE, "1.48")
                clock[0] += 2
                self.assertFalse(watcher.tick())

    def test_profile_discovery_queries_and_callbacks_zero_and_positive(self):
        doc = old_doc()
        doc["devices"][0].pop("alice_visible")
        ap.provision(doc, MID, CONTROLS)
        for profile in (C.PROFILE_CLOUD, C.PROFILE_YANDEX):
            reg = DeviceRegistry(doc, profile=profile)
            props = reg.discovery_devices(profile)[0]["properties"]
            distances = [p for p in props if p["parameters"]["instance"] == "target_distance"]
            self.assertEqual(len(distances), int(profile == C.PROFILE_CLOUD))
            self.assertEqual(DISTANCE in reg.mqtt_topics(), profile == C.PROFILE_CLOUD)
            for value in (0.0, 1.48):
                reg.note_mqtt(DISTANCE, str(value))
                query = reg.query_devices(["stable-mtd"])[0]
                states = [p["state"] for p in query.get("properties", []) if p["state"]["instance"] == "target_distance"]
                blocks = reg.state_blocks_for_topic(DISTANCE)
                if profile == C.PROFILE_CLOUD:
                    self.assertEqual(states, [{"instance": "target_distance", "value": value}])
                    self.assertEqual(blocks[0]["properties"][0]["state"]["value"], value)
                else:
                    self.assertEqual(states, [])
                    self.assertEqual(blocks, [])

    def test_atomic_commit_concurrent_writers_and_save_failure(self):
        with tempfile.TemporaryDirectory() as td:
            path = td + "/devices.json"
            save_devices(old_doc(), path)
            original = Path(path).read_bytes()
            with mock.patch.object(ap, "save_devices", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    ap.commit_provision(MID, CONTROLS, path=path, yaml_ids=set())
            self.assertEqual(Path(path).read_bytes(), original)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(ap.commit_provision, MID, CONTROLS, path=path, yaml_ids=set()) for _ in range(2)]
                changes = [f.result(timeout=5) for f in futures]
            self.assertEqual(sorted(map(len, changes)), [0, 1])
            doc = load_devices(path)
            self.assertEqual(len(doc["devices"]), 1)
            self.assertEqual(doc["devices"][0]["id"], "stable-mtd")
            self.assertEqual(len(doc["devices"][0]["properties"]), 3)

    def test_runtime_file_cache_keeps_eligible_sensor_and_reloads_operator_changes(self):
        with tempfile.TemporaryDirectory() as td:
            path = td + "/devices.json"
            save_devices(old_doc(), path)
            clock = [0.0]
            watcher = ap.AutoProvisioner(load=lambda: load_devices(path), save=lambda doc: save_devices(doc, path),
                clock=lambda: clock[0], yaml_ids=lambda: set(), path=path)
            self.assertFalse(watcher._is_mapped(MID))
            self.assertFalse(watcher._is_mapped(MID))
            self.assertEqual(watcher.note(BASE + "/meta/name", "Presence"), BASE + "/controls/+")
            watcher.note(DISTANCE, "1.48")
            edited = load_devices(path)
            edited["devices"][0]["name"] = "Renamed by operator"
            save_devices(edited, path)
            clock[0] = 2
            self.assertTrue(watcher.tick())
            final = load_devices(path)
            self.assertEqual(final["devices"][0]["name"], "Renamed by operator")
            self.assertTrue(watcher._is_mapped(MID))
