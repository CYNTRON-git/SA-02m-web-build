"""Scenes marked «в Алису» become virtual switches (1.0.6.41, A14).

Two halves. The PROJECTION half pins `config/scene_devices.py` — which store
rows earn a device, what the Yandex id looks like, what the capability
carries. The WIRING half goes through the REAL `DeviceRegistry` (the
`test_ahu_status_wiring.py` lesson: a helper with green unit tests and no
production caller ships nothing), and pins the three registry traps the plan
found:

  T1  a device whose only topic was never seen is announced DEVICE_UNREACHABLE
      by the 60 s snapshot — a scene switch would show up dead in the app.
  T2  the SECOND «включи» after `STATUS_STALE_S` is refused, because the first
      action marked the topic live and nothing republishes a command topic.
  T3  two boards on one Yandex account collide on the device id unless it is
      board-keyed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ALICE_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
RULES_ROOT = os.path.abspath(os.path.join(TESTS_DIR, "..", "..", "sa02m-rules"))
for _p in (ALICE_ROOT, RULES_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import models, scene_devices  # noqa: E402

ROOMS = [{"id": "r1", "name": "Гостиная"}, {"id": "r2", "name": "Кухня"}]


def _scene(sid="s1", **over):
    row = {
        "id": sid,
        "name": "Вечер",
        "type": "scene",
        "enabled": True,
        "alice_expose": True,
        "captured_from": {"room_id": "r1"},
        "action": [{"kind": "set", "device": "lamp", "cap": "on_off", "value": 1}],
    }
    row.update(over)
    return row


def _doc(*rows):
    return {"scenarios": list(rows), "library": "", "runs": [], "notify_queue": [],
            "vars": {}}


class ProjectionTests(unittest.TestCase):
    def project(self, doc, board_key="BOARD1"):
        return scene_devices.exposed_scene_devices(doc, ROOMS, board_key)

    def test_exposed_enabled_scene_becomes_one_switch(self):
        rows = self.project(_doc(_scene()))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "scene-BOARD1-s1")
        self.assertEqual(row["name"], "Вечер")
        self.assertEqual(row["type"], scene_devices.SCENE_DEVICE_TYPE)
        self.assertEqual(row["type"], "devices.types.switch")
        self.assertEqual(row["room_id"], "r1")
        self.assertEqual(row["scene_id"], "s1")
        self.assertEqual(row["properties"], [])
        self.assertEqual(len(row["capabilities"]), 1)
        cap = row["capabilities"][0]
        self.assertEqual(cap["type"], "devices.capabilities.on_off")
        self.assertEqual(cap["mqtt"], "/devices/sa02m-rules-s1/controls/run")
        self.assertIs(cap["retrievable"], False)
        self.assertIs(cap["reportable"], False)
        self.assertEqual(cap["parameters"], {"split": True})

    def test_id_stays_inside_the_alice_id_charset(self):
        row = self.project(_doc(_scene()))[0]
        self.assertTrue(models.id_ok(row["id"]), row["id"])

    def test_not_exposed_or_wrong_type_or_disabled_is_absent(self):
        self.assertEqual(self.project(_doc(_scene(alice_expose=False))), [])
        self.assertEqual(self.project(_doc(_scene(**{"alice_expose": None}))), [])
        scene_no_flag = _scene()
        scene_no_flag.pop("alice_expose")
        self.assertEqual(self.project(_doc(scene_no_flag)), [])
        self.assertEqual(self.project(_doc(_scene(type="block"))), [])
        self.assertEqual(self.project(_doc(_scene(type="logic"))), [])
        self.assertEqual(self.project(_doc(_scene(enabled=False))), [])

    def test_string_true_is_not_a_flag(self):
        """Fuzz: the store only ever writes the bool, but a hand-edited or
        legacy document must fail CLOSED, not expose the scene."""
        self.assertEqual(self.project(_doc(_scene(alice_expose="true"))), [])
        self.assertEqual(self.project(_doc(_scene(alice_expose=1))), [])

    def test_room_is_dropped_when_the_room_no_longer_exists(self):
        row = self.project(_doc(_scene(captured_from={"room_id": "gone"})))[0]
        self.assertNotIn("room_id", row)
        row = self.project(_doc(_scene(captured_from={"group_id": "g1"})))[0]
        self.assertNotIn("room_id", row)

    def test_group_id_is_not_read(self):
        row = self.project(_doc(_scene(captured_from={"room_id": "r2",
                                                      "group_id": "g9"})))[0]
        self.assertEqual(row["room_id"], "r2")
        self.assertNotIn("group_id", row)
        self.assertNotIn("group_id", json.dumps(row))

    def test_board_key_makes_two_boards_differ(self):
        """T3 — one Yandex account, two SA-02m: the ids must not collide."""
        a = self.project(_doc(_scene()), board_key="BOARD1")[0]["id"]
        b = self.project(_doc(_scene()), board_key="BOARD2")[0]["id"]
        self.assertNotEqual(a, b)

    def test_board_key_is_sanitised_and_bounded(self):
        key = scene_devices.sanitise_board_key("../etc/passwd +#/x")
        self.assertTrue(models.id_ok(key), key)
        self.assertNotIn("/", key)
        self.assertNotIn("+", key)
        self.assertNotIn("#", key)
        self.assertLessEqual(len(key), scene_devices.BOARD_KEY_MAX)
        self.assertEqual(scene_devices.sanitise_board_key(""),
                         scene_devices.BOARD_KEY_FALLBACK)
        self.assertEqual(scene_devices.sanitise_board_key(None),
                         scene_devices.BOARD_KEY_FALLBACK)

    def test_an_id_that_cannot_be_built_is_skipped_not_truncated(self):
        """A truncated id could collide with another scene's; fail closed."""
        long_sid = "s" * 64
        self.assertTrue(len(long_sid) <= 64)
        self.assertEqual(self.project(_doc(_scene(sid=long_sid))), [])
        self.assertEqual(self.project(_doc(_scene(sid="bad/sid"))), [])
        self.assertEqual(self.project(_doc(_scene(sid=""))), [])
        self.assertEqual(self.project(_doc(_scene(sid=None))), [])

    def test_garbage_rows_never_raise(self):
        self.assertEqual(scene_devices.exposed_scene_devices(None, ROOMS, "B"), [])
        self.assertEqual(scene_devices.exposed_scene_devices({}, ROOMS, "B"), [])
        self.assertEqual(
            scene_devices.exposed_scene_devices({"scenarios": ["x", None, 3]},
                                                ROOMS, "B"), [])
        self.assertEqual(
            scene_devices.exposed_scene_devices(_doc(_scene()), None, "B")[0].get("room_id"),
            None)

    def test_name_falls_back_to_the_scene_id(self):
        row = self.project(_doc(_scene(name="")))[0]
        self.assertEqual(row["name"], "s1")

    def test_two_scenes_keep_document_order(self):
        rows = self.project(_doc(_scene("s1"), _scene("s2", name="Ночь")))
        self.assertEqual([r["scene_id"] for r in rows], ["s1", "s2"])


class FingerprintTests(unittest.TestCase):
    def test_run_state_does_not_change_the_fingerprint(self):
        """The journal write (A16) must not rebuild the Alice catalogue."""
        before = scene_devices.exposure_fingerprint(_doc(_scene()))
        doc = _doc(_scene(last_run=1234.5, last_error="write cap"))
        doc["runs"] = [{"ts": 1, "id": "s1"}]
        self.assertEqual(scene_devices.exposure_fingerprint(doc), before)

    def test_flag_name_enabled_and_room_all_change_it(self):
        base = scene_devices.exposure_fingerprint(_doc(_scene()))
        self.assertNotEqual(scene_devices.exposure_fingerprint(
            _doc(_scene(alice_expose=False))), base)
        self.assertNotEqual(scene_devices.exposure_fingerprint(
            _doc(_scene(name="Утро"))), base)
        self.assertNotEqual(scene_devices.exposure_fingerprint(
            _doc(_scene(enabled=False))), base)
        self.assertNotEqual(scene_devices.exposure_fingerprint(
            _doc(_scene(captured_from={"room_id": "r2"}))), base)
        self.assertNotEqual(scene_devices.exposure_fingerprint(
            _doc(_scene(), _scene("s2"))), base)

    def test_fingerprint_never_raises_on_garbage(self):
        self.assertEqual(scene_devices.exposure_fingerprint(None), ())
        self.assertEqual(scene_devices.exposure_fingerprint({"scenarios": 5}), ())


class LoadRulesDocTests(unittest.TestCase):
    def test_absent_stack_is_an_empty_projection_not_a_crash(self):
        with mock.patch.object(scene_devices, "rules_store", return_value=None):
            self.assertEqual(scene_devices.load_rules_doc(), {})

    def test_reads_the_real_store(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "scenarios.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"scenarios": [_scene()]}, fh, ensure_ascii=False)
            with mock.patch.dict(os.environ, {"SA02M_RULES_PATH": path}):
                doc = scene_devices.load_rules_doc()
        self.assertEqual(len(doc.get("scenarios") or []), 1)

    def test_an_unreadable_store_is_an_empty_projection(self):
        with mock.patch.dict(os.environ, {"SA02M_RULES_PATH": os.path.join(
                tempfile.gettempdir(), "sa02m-no-such-store.json")}):
            self.assertEqual(scene_devices.load_rules_doc().get("scenarios") or [], [])


class _Wiring(unittest.TestCase):
    """Registry-level base: a real store on disk, a real DeviceRegistry."""

    BOARD = "BOARD1"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.rules_path = os.path.join(self._td.name, "scenarios.json")
        self.write_store(_scene())
        self._env = mock.patch.dict(os.environ,
                                    {"SA02M_RULES_PATH": self.rules_path})
        self._env.start()
        self._board = mock.patch.object(scene_devices, "board_key",
                                        return_value=self.BOARD)
        self._board.start()
        self.now = 1000.0

    def tearDown(self):
        self._board.stop()
        self._env.stop()
        self._td.cleanup()

    def write_store(self, *rows):
        with open(self.rules_path, "w", encoding="utf-8") as fh:
            json.dump({"scenarios": list(rows), "library": "", "vars": {}}, fh,
                      ensure_ascii=False)

    def devices_doc(self):
        return {
            "rooms": [dict(r) for r in ROOMS],
            "devices": [{
                "id": "lamp",
                "name": "Свет",
                "type": "devices.types.light",
                "capabilities": [{
                    "type": "devices.capabilities.on_off",
                    "mqtt": "/devices/SA-02m/controls/do_1",
                    "retrievable": True,
                    "reportable": True,
                }],
                "properties": [],
            }],
        }

    def registry(self, doc=None, profile=C.PROFILE_YANDEX):
        return DeviceRegistry(doc if doc is not None else self.devices_doc(),
                              profile=profile, clock=lambda: self.now)

    def scene_dev_id(self, sid="s1"):
        return "scene-%s-%s" % (self.BOARD, sid)

    @staticmethod
    def on_off(value):
        return {"type": "devices.capabilities.on_off",
                "state": {"instance": "on", "value": value}}


class RegistryCatalogueTests(_Wiring):
    def test_exposed_scene_reaches_yandex_discovery(self):
        entries = self.registry().discovery_devices(C.PROFILE_YANDEX)
        by_id = {e["id"]: e for e in entries}
        self.assertIn("lamp", by_id)
        entry = by_id.get(self.scene_dev_id())
        self.assertIsNotNone(entry, sorted(by_id))
        self.assertEqual(entry["name"], "Вечер")
        self.assertEqual(entry["type"], "devices.types.switch")
        self.assertEqual(entry["room"], "Гостиная")
        self.assertEqual(len(entry["capabilities"]), 1)
        cap = entry["capabilities"][0]
        self.assertEqual(cap["type"], "devices.capabilities.on_off")
        self.assertIs(cap["retrievable"], False)
        self.assertIs(cap["reportable"], False)
        self.assertEqual(cap["parameters"], {"split": True})
        self.assertNotIn("mqtt", cap)

    def test_cloud_profile_does_not_list_scenes(self):
        """F4 — the cloud page has scenarios first-class; a second tile lies."""
        ids = [e["id"] for e in
               self.registry(profile=C.PROFILE_CLOUD).discovery_devices(
                   C.PROFILE_CLOUD)]
        self.assertIn("lamp", ids)
        self.assertNotIn(self.scene_dev_id(), ids)

    def test_the_stored_device_document_is_never_written(self):
        doc = self.devices_doc()
        with mock.patch("sa02m_alice.common.config_store.save_devices",
                        side_effect=AssertionError("save_devices called")):
            reg = self.registry(doc)
            reg.reload(self.devices_doc())
        self.assertEqual([d["id"] for d in doc["devices"]], ["lamp"])

    def test_the_run_topic_is_subscribed(self):
        topics = self.registry().subscribe_topics()
        self.assertIn("/devices/sa02m-rules-s1/controls/run", topics)

    def test_unmarking_the_scene_drops_the_device_on_reload(self):
        reg = self.registry()
        self.assertIn(self.scene_dev_id(),
                      [e["id"] for e in reg.discovery_devices()])
        self.write_store(_scene(alice_expose=False))
        reg.reload(self.devices_doc())
        self.assertNotIn(self.scene_dev_id(),
                         [e["id"] for e in reg.discovery_devices()])

    def test_no_rules_stack_leaves_the_physical_catalogue_intact(self):
        with mock.patch.object(scene_devices, "rules_store", return_value=None):
            ids = [e["id"] for e in self.registry().discovery_devices()]
        self.assertEqual(ids, ["lamp"])

    def test_two_boards_on_one_account_do_not_collide(self):
        """T3 — both boards mint `s1`; only the board key keeps them apart."""
        first = self.registry().discovery_devices()
        with mock.patch.object(scene_devices, "board_key",
                               return_value="BOARD2"):
            second = self.registry().discovery_devices()
        a = [e["id"] for e in first if e["id"] != "lamp"]
        b = [e["id"] for e in second if e["id"] != "lamp"]
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertNotEqual(a[0], b[0])


class NeverSeenTopicTests(_Wiring):
    """T1 — a device whose only topic was never published is announced
    DEVICE_UNREACHABLE by the 60 s snapshot. That is RIGHT for a Modbus coil
    and WRONG for a command topic no poller ever writes."""

    def test_the_physical_device_still_goes_down_when_never_seen(self):
        """Non-vacuity: the machinery that would have flagged the scene is
        live — the physical device with the same silence IS announced."""
        reg = self.registry()
        stubs = reg.take_unreachable_transitions()
        self.assertEqual([s["id"] for s in stubs], ["lamp"])

    def test_the_scene_switch_is_never_announced_unreachable(self):
        reg = self.registry()
        stubs = reg.take_unreachable_transitions()
        self.assertNotIn(self.scene_dev_id(), [s["id"] for s in stubs])

    def test_query_of_the_scene_carries_no_error_code(self):
        reg = self.registry()
        entry = reg.query_devices([self.scene_dev_id()])[0]
        self.assertEqual(entry["id"], self.scene_dev_id())
        self.assertNotIn("error_code", entry)
        self.assertEqual(entry["capabilities"], [])
        self.assertEqual(entry["properties"], [])

    def test_a_scene_never_reports_a_state_it_cannot_know(self):
        """`reportable:false`: even a stray publish on the command topic must
        not become a device_state event."""
        reg = self.registry()
        topic = "/devices/sa02m-rules-s1/controls/run"
        reg.note_mqtt(topic, "1")
        self.assertEqual(reg.state_blocks_for_topic(topic), [])
        self.assertEqual(reg.query_devices([self.scene_dev_id()])[0]
                         .get("capabilities"), [])


class ActionTests(_Wiring):
    def test_on_and_off_publish_the_run_topic(self):
        reg = self.registry()
        results, pubs = reg.apply_actions(
            [{"id": self.scene_dev_id(), "capabilities": [self.on_off(True)]}])
        self.assertEqual(pubs, [("/devices/sa02m-rules-s1/controls/run/on", "1")])
        self.assertEqual(results[0]["capabilities"][0]["status"], C.STATUS_DONE)
        _r, pubs = reg.apply_actions(
            [{"id": self.scene_dev_id(), "capabilities": [self.on_off(False)]}])
        self.assertEqual(pubs, [("/devices/sa02m-rules-s1/controls/run/on", "0")])

    def test_second_command_after_the_stale_window_still_publishes(self):
        """T2 — the first action marks the topic live and nothing republishes
        a command topic, so the freshness refusal kills every later «включи»
        once STATUS_STALE_S has passed."""
        reg = self.registry()
        _r, pubs = reg.apply_actions(
            [{"id": self.scene_dev_id(), "capabilities": [self.on_off(True)]}])
        self.assertEqual(len(pubs), 1)
        self.now += C.STATUS_STALE_S + 1
        results, pubs = reg.apply_actions(
            [{"id": self.scene_dev_id(), "capabilities": [self.on_off(True)]}])
        self.assertEqual(pubs, [("/devices/sa02m-rules-s1/controls/run/on", "1")],
                         results)
        self.assertEqual(results[0]["capabilities"][0]["status"], C.STATUS_DONE)

    def test_a_deleted_scene_answers_device_unreachable(self):
        """F3 — a stale app tile until the user refreshes the device list."""
        reg = self.registry()
        self.write_store()
        reg.reload(self.devices_doc())
        results, pubs = reg.apply_actions(
            [{"id": self.scene_dev_id(), "capabilities": [self.on_off(True)]}])
        self.assertEqual(pubs, [])
        cap = results[0]["capabilities"][0]
        self.assertEqual(cap["status"], C.STATUS_ERROR)
        self.assertEqual(cap["error_code"], C.ERR_DEVICE_UNREACHABLE)


class FullConfigTests(_Wiring):
    """The «Умный дом» card reads the same projection the daemon does — one
    home, so the web page and the Alice account never disagree about which
    scenes are exposed."""

    def setUp(self):
        super().setUp()
        self._devices_conf = mock.patch.object(
            C, "DEVICES_CONF", os.path.join(self._td.name, "devices.conf"))
        self._devices_conf.start()
        self.addCleanup(self._devices_conf.stop)
        with open(C.DEVICES_CONF, "w", encoding="utf-8") as fh:
            json.dump(self.devices_doc(), fh, ensure_ascii=False)

    def full_config(self):
        from sa02m_alice.config import api as config_api
        with mock.patch.object(config_api, "probe_gateway",
                               return_value={"available": False}):
            return config_api.full_config()

    def test_scene_devices_shape(self):
        rows = self.full_config()["scene_devices"]
        self.assertEqual(rows, [{
            "id": self.scene_dev_id(),
            "scene_id": "s1",
            "name": "Вечер",
            "room_id": "r1",
            "enabled": True,
        }])

    def test_no_rules_stack_is_an_empty_list_not_a_missing_key(self):
        with mock.patch.object(scene_devices, "rules_store", return_value=None):
            cfg = self.full_config()
        self.assertEqual(cfg["scene_devices"], [])

    def test_the_stored_devices_are_untouched(self):
        cfg = self.full_config()
        self.assertEqual([d["id"] for d in cfg["devices"]["devices"]], ["lamp"])

    def test_an_unmarked_scene_is_absent(self):
        self.write_store(_scene(alice_expose=False))
        self.assertEqual(self.full_config()["scene_devices"], [])


if __name__ == "__main__":
    unittest.main()
