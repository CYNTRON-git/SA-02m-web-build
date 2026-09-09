"""Unit tests: the web API's `cloud_control` block + enable/disable actions (1.0.6.26).

Validating tests for docs/contracts/alice-mqtt-mapping.md §Profiles (web API
paragraph): the flag gates exactly like client_enabled, `cloud_enrolled` is
tri-state like mtls.cert_present (the root client's status file first, a local
stat only when the dir is traversable, never a false False), and the factory
reset helper clears both flags.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.common import config_store, constants as C  # noqa: E402
from sa02m_alice.config import api  # noqa: E402

PROBE_DOWN = {"ok": True, "available": False, "error": "gateway_unreachable", "url": "https://x/v1.0/ping"}


class _CloudApiBase(unittest.TestCase):
    """Temp etc/var/run/cloud homes patched onto the constants module (the
    idiom of test_cert_status: modules already hold `C`, so patch its
    attributes)."""

    KEYS = (
        "ETC_DIR", "CLIENT_CONF", "DEVICES_CONF", "SERVER_CONF", "VAR_DIR", "CERT_FILE",
        "KEY_FILE", "CA_FILE", "PENDING_CLAIM_FILE", "STATUS_FILE", "STATUS_FILE_CLOUD",
        "CLOUD_AGENT_CONF", "CLOUD_DEVICE_SECRET",
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = {k: getattr(C, k) for k in self.KEYS}
        self.addCleanup(self._restore)
        etc = os.path.join(self.tmp.name, "etc")
        os.makedirs(etc)
        self.cloud_dir = os.path.join(self.tmp.name, "cloud")
        C.ETC_DIR = etc
        C.CLIENT_CONF = os.path.join(etc, "sa02m-alice-client.conf")
        C.DEVICES_CONF = os.path.join(etc, "sa02m-alice-devices.conf")
        C.SERVER_CONF = os.path.join(etc, "sa02m-alice-server.conf")
        C.VAR_DIR = os.path.join(self.tmp.name, "var")
        C.CERT_FILE = os.path.join(C.VAR_DIR, "device.crt.pem")
        C.KEY_FILE = os.path.join(C.VAR_DIR, "device.key.pem")
        C.CA_FILE = os.path.join(C.VAR_DIR, "ca.crt.pem")
        C.PENDING_CLAIM_FILE = os.path.join(C.VAR_DIR, "pending_claim.json")
        C.STATUS_FILE = os.path.join(self.tmp.name, "run", "status.json")
        C.STATUS_FILE_CLOUD = os.path.join(self.tmp.name, "run", "status-cloud.json")
        C.CLOUD_AGENT_CONF = os.path.join(self.cloud_dir, "agent.conf")
        C.CLOUD_DEVICE_SECRET = os.path.join(self.cloud_dir, "device_secret")
        with open(C.CLIENT_CONF, "w", encoding="utf-8") as fh:
            fh.write("[client]\nclient_enabled = false\n")
        with open(C.DEVICES_CONF, "w", encoding="utf-8") as fh:
            fh.write('{"rooms":[],"devices":[]}\n')
        with open(C.SERVER_CONF, "w", encoding="utf-8") as fh:
            fh.write("[gateway]\nhttp_url = https://alice.cyntron.ru\n")

    def _restore(self):
        for k, v in self._old.items():
            setattr(C, k, v)

    def write_cloud_status(self, **payload):
        os.makedirs(os.path.dirname(C.STATUS_FILE_CLOUD), exist_ok=True)
        with open(C.STATUS_FILE_CLOUD, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def enroll(self):
        os.makedirs(self.cloud_dir, exist_ok=True)
        with open(C.CLOUD_AGENT_CONF, "w", encoding="utf-8") as fh:
            fh.write("[cloud]\ndevice_id = sa02m-abc\n")
        with open(C.CLOUD_DEVICE_SECRET, "w", encoding="utf-8") as fh:
            fh.write("s3cr3t\n")

    def full(self):
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_DOWN):
            return api.full_config()


class TestCloudControlBlock(_CloudApiBase):
    def test_default_disabled_block_present(self):
        cc = self.full()["cloud_control"]
        self.assertFalse(cc["enabled"])
        self.assertEqual(cc["state"], C.STATE_DISABLED)
        self.assertIn("cloud_enrolled", cc)
        self.assertIn("cloud_check", cc)

    def test_client_flag_wins_over_local_check(self):
        # No identity files locally, but the root client says present → True.
        self.write_cloud_status(state="connected", ts=1, identity_present=True)
        cc = self.full()["cloud_control"]
        self.assertIs(cc["cloud_enrolled"], True)
        self.assertEqual(cc["cloud_check"], api.CERT_CHECK_CLIENT)
        self.assertEqual(cc["state"], C.STATE_DISABLED)  # flag off → disabled, not the file's state

    def test_missing_cloud_dir_is_definite_false(self):
        cc = self.full()["cloud_control"]
        self.assertIs(cc["cloud_enrolled"], False)
        self.assertEqual(cc["cloud_check"], api.CERT_CHECK_LOCAL)

    def test_local_identity_files_read_when_dir_traversable(self):
        self.enroll()
        cc = self.full()["cloud_control"]
        self.assertIs(cc["cloud_enrolled"], True)
        self.assertEqual(cc["cloud_check"], api.CERT_CHECK_LOCAL)

    def test_local_check_never_opens_the_secret(self):
        # M1: presence only. The secret is 0600 root on a board; a www-data
        # open() would raise, and the old path turned that into a false False
        # that locked the cloud-control button on an enrolled board.
        self.enroll()
        real_open = open

        def guarded_open(path, *a, **kw):
            if os.fspath(path) == C.CLOUD_DEVICE_SECRET:
                raise PermissionError("secret must not be opened by the web API")
            return real_open(path, *a, **kw)

        with mock.patch("builtins.open", side_effect=guarded_open):
            cc = self.full()["cloud_control"]
        self.assertIs(cc["cloud_enrolled"], True)
        self.assertEqual(cc["cloud_check"], api.CERT_CHECK_LOCAL)

    def test_unreadable_agent_conf_is_unknown_not_false(self):
        self.enroll()
        real_open = open

        def guarded_open(path, *a, **kw):
            if os.fspath(path) == C.CLOUD_AGENT_CONF:
                raise PermissionError("conf not readable")
            return real_open(path, *a, **kw)

        with mock.patch("builtins.open", side_effect=guarded_open):
            cc = self.full()["cloud_control"]
        self.assertIsNone(cc["cloud_enrolled"])
        self.assertEqual(cc["cloud_check"], api.CERT_CHECK_UNREADABLE)

    def test_secret_without_identity_in_conf_is_false(self):
        os.makedirs(self.cloud_dir, exist_ok=True)
        with open(C.CLOUD_AGENT_CONF, "w", encoding="utf-8") as fh:
            fh.write("[cloud]\napi_url = https://x/api/v1\n")
        with open(C.CLOUD_DEVICE_SECRET, "w", encoding="utf-8") as fh:
            fh.write("s3cr3t\n")
        cc = self.full()["cloud_control"]
        self.assertIs(cc["cloud_enrolled"], False)

    def test_web_api_module_does_not_import_the_secret_reader(self):
        # The only open() of the device secret must stay in the root client.
        import sa02m_alice.config.api as api_mod

        self.assertFalse(hasattr(api_mod, "cloud_identity_present"))
        self.assertFalse(hasattr(api_mod, "read_cloud_identity"))

    def test_untraversable_dir_is_unknown_not_false(self):
        self.enroll()
        with mock.patch("sa02m_alice.config.api.os.access", return_value=False):
            cc = self.full()["cloud_control"]
        self.assertIsNone(cc["cloud_enrolled"])
        self.assertEqual(cc["cloud_check"], api.CERT_CHECK_UNREADABLE)

    def test_non_bool_status_value_is_ignored(self):
        self.write_cloud_status(state="connected", ts=1, identity_present="yes")
        cc = self.full()["cloud_control"]
        self.assertIs(cc["cloud_enrolled"], False)  # fell through to the local (missing dir) check

    def test_enabled_reads_state_ts_error_from_status_file(self):
        config_store.set_cloud_control_enabled(True)
        self.write_cloud_status(state="error", ts=1234, error="revoked", identity_present=True)
        cc = self.full()["cloud_control"]
        self.assertTrue(cc["enabled"])
        self.assertEqual(cc["state"], "error")
        self.assertEqual(cc["ts"], 1234)
        self.assertEqual(cc["error"], "revoked")


class TestCloudControlActions(_CloudApiBase):
    def test_enable_disable_flip_only_the_cloud_flag(self):
        code, out = api.dispatch("POST", "/", {"action": "cloud_control_enable"})
        self.assertEqual(code, 200)
        self.assertTrue(out["ok"])
        self.assertTrue(out["cloud_control_enabled"])
        self.assertEqual(out["restart_unit"], "sa02m-cloud-control")
        self.assertTrue(config_store.cloud_control_enabled())
        self.assertFalse(config_store.client_enabled())
        code, out = api.dispatch("POST", "/", {"action": "cloud_control_disable"})
        self.assertFalse(out["cloud_control_enabled"])
        self.assertFalse(config_store.cloud_control_enabled())

    def test_yandex_enable_leaves_cloud_flag_alone(self):
        config_store.set_cloud_control_enabled(True)
        api.dispatch("POST", "/", {"action": "enable"})
        self.assertTrue(config_store.client_enabled())
        self.assertTrue(config_store.cloud_control_enabled())
        api.dispatch("POST", "/", {"action": "disable"})
        self.assertTrue(config_store.cloud_control_enabled())

    def test_profile_enabled_dispatches_per_profile(self):
        config_store.set_cloud_control_enabled(True)
        self.assertTrue(config_store.profile_enabled(C.PROFILE_CLOUD))
        self.assertFalse(config_store.profile_enabled(C.PROFILE_YANDEX))

    def test_unknown_action_still_not_found(self):
        code, out = api.dispatch("POST", "/", {"action": "cloud_control_nuke"})
        self.assertEqual(code, 404)
        self.assertFalse(out["ok"])

    def test_reset_mappings_clears_both_flags(self):
        config_store.set_client_enabled(True)
        config_store.set_cloud_control_enabled(True)
        out = api.reset_mappings()
        self.assertFalse(out["client_enabled"])
        self.assertFalse(out["cloud_control_enabled"])
        self.assertFalse(config_store.client_enabled())
        self.assertFalse(config_store.cloud_control_enabled())

    def test_flag_toggle_keeps_operator_keys(self):
        with open(C.CLIENT_CONF, "w", encoding="utf-8") as fh:
            fh.write("[client]\nclient_enabled = true\nlog_level = DEBUG\n")
        config_store.set_cloud_control_enabled(True)
        cfg = config_store.default_client_cfg()
        self.assertTrue(cfg.getboolean("client", "client_enabled"))
        self.assertEqual(cfg.get("client", "log_level"), "DEBUG")
        self.assertTrue(cfg.getboolean("client", "cloud_control_enabled"))


from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.client.sio_handlers import SioHandlers  # noqa: E402
from sa02m_alice.common.config_store import load_devices, save_devices  # noqa: E402


class TestRenameDevice(_CloudApiBase):
    def setUp(self):
        super().setUp()
        save_devices({"rooms": [], "devices": [{
            "id": "lamp", "name": "Лампа", "type": "devices.types.light",
            "capabilities": [], "properties": [],
        }]})

    def test_rename_cyrillic(self):
        r = api.rename_device("lamp", "Лампа кухни")
        self.assertTrue(r["ok"])
        self.assertEqual(r["name"], "Лампа кухни")
        self.assertEqual(load_devices()["devices"][0]["name"], "Лампа кухни")

    def test_internal_spaces_kept(self):
        r = api.rename_device("lamp", "  Лампа  кухни  ")
        self.assertTrue(r["ok"])
        self.assertEqual(r["name"], "Лампа  кухни")

    def test_empty_at_too_long_non_str_rejected(self):
        for name in ("", "   ", "bad@name", "x" * 65, None, 1):
            r = api.rename_device("lamp", name)
            self.assertFalse(r["ok"], name)
            self.assertEqual(r.get("error"), "invalid_name")
        self.assertEqual(load_devices()["devices"][0]["name"], "Лампа")

    def test_unknown_device(self):
        r = api.rename_device("nope", "X")
        self.assertEqual(r.get("error"), "not_found")

    def test_dispatch(self):
        code, body = api.dispatch("POST", "/", {"action": "rename_device", "id": "lamp", "name": "Стенд"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["name"], "Стенд")

    def test_sio_rename_reloads_and_emits(self):
        emitted = []
        h = SioHandlers(
            DeviceRegistry(),
            publish_mqtt=lambda *_a: None,
            emit_response=lambda d: emitted.append(d),
            profile=C.PROFILE_CLOUD,
        )
        h.handle(C.EVT_DEVICES_RENAME, {"request_id": "r1", "device": "lamp", "name": "Новое"})
        self.assertEqual(emitted[0]["ok"], True)
        self.assertEqual(emitted[0]["name"], "Новое")
        self.assertEqual(load_devices()["devices"][0]["name"], "Новое")


class TestApplyRooms(_CloudApiBase):
    def setUp(self):
        super().setUp()
        save_devices({"rooms": [], "devices": [
            {"id": "lamp", "name": "Лампа", "type": "devices.types.light",
             "capabilities": [], "properties": []},
            {"id": "sock", "name": "Розетка", "type": "devices.types.socket",
             "capabilities": [], "properties": []},
        ]})

    def test_create_bind_unbind_and_delete_unassigns(self):
        created = api.apply_rooms({"name": "Кухня", "devices": ["lamp"]})
        self.assertTrue(created["ok"], created)
        rid = created["room"]["id"]
        self.assertEqual(created["room"]["name"], "Кухня")
        doc = load_devices()
        self.assertEqual(doc["devices"][0]["room_id"], rid)
        self.assertEqual(doc["devices"][1].get("room_id") or "", "")
        moved = api.apply_rooms({"id": rid, "name": "Кухня", "devices": ["sock"]})
        self.assertTrue(moved["ok"], moved)
        doc = load_devices()
        self.assertEqual(doc["devices"][0].get("room_id") or "", "")
        self.assertEqual(doc["devices"][1]["room_id"], rid)
        dropped = api.apply_rooms({"id": rid, "delete": True})
        self.assertTrue(dropped["ok"], dropped)
        doc = load_devices()
        self.assertEqual(doc["rooms"], [])
        self.assertEqual(doc["devices"][0].get("room_id") or "", "")
        self.assertEqual(doc["devices"][1].get("room_id") or "", "")
        self.assertEqual(len(doc["devices"]), 2)

    def test_delete_room_cgi_unassigns(self):
        created = api.apply_rooms({"name": "Коридор", "devices": ["lamp"]})
        rid = created["room"]["id"]
        r = api.delete_room(rid)
        self.assertTrue(r["ok"])
        doc = load_devices()
        self.assertEqual(doc["devices"][0].get("room_id") or "", "")
        self.assertEqual(doc["devices"][0]["id"], "lamp")

    def test_invalid_name_rejected(self):
        for name in ("", "   ", "bad@name", "x" * 65, None, 1):
            r = api.apply_rooms({"name": name})
            self.assertFalse(r["ok"], name)
            self.assertIn(r.get("error"), ("invalid_name", "invalid_room"))
        self.assertEqual(load_devices()["rooms"], [])

    def test_unknown_room_edit_is_not_found(self):
        r = api.apply_rooms({"id": "nope", "name": "X"})
        self.assertEqual(r.get("error"), "not_found")

    def test_sio_rooms_reloads_and_emits(self):
        emitted = []
        h = SioHandlers(
            DeviceRegistry(),
            publish_mqtt=lambda *_a: None,
            emit_response=lambda d: emitted.append(d),
            profile=C.PROFILE_CLOUD,
        )
        h.handle(C.EVT_DEVICES_ROOMS, {"request_id": "r1", "name": "Зал", "devices": ["lamp"]})
        self.assertEqual(emitted[0]["ok"], True)
        self.assertEqual(emitted[0]["room"]["name"], "Зал")
        self.assertEqual(emitted[0]["devices"][0]["room"], "Зал")
        self.assertEqual(load_devices()["devices"][0]["room_id"], emitted[0]["room"]["id"])
        listed = h.registry.discovery_devices(profile=C.PROFILE_CLOUD)
        self.assertEqual(listed[0].get("room_id"), emitted[0]["room"]["id"])
        self.assertIn({"id": emitted[0]["room"]["id"], "name": "Зал"},
                      h.registry.listed_rooms())


class TestUpsertDeviceRooms(_CloudApiBase):
    """`upsert_device` keeps `room.devices` and `dev.room_id` in agreement.

    Membership is stored on both sides (docs/contracts/alice-mqtt-mapping.md
    §Room membership). The UPDATE branch used to return before the attach
    block, so editing a device into another room left the old room still
    listing it and the new room never naming it; and a shape-valid `room_id`
    naming no room was stored as a dangling reference the UI cannot resolve.
    """

    LAMP = {
        "id": "lamp", "name": "Лампа", "type": "devices.types.light",
        "capabilities": [], "properties": [],
    }

    def setUp(self):
        super().setUp()
        save_devices({"rooms": [], "devices": [dict(self.LAMP)]})
        # Both rooms come back carrying a `devices` list — `validate_room`
        # always writes one. The pre-list document shape is its own case.
        self.kitchen = api.apply_rooms({"name": "Кухня", "devices": ["lamp"]})["room"]["id"]
        self.hall = api.apply_rooms({"name": "Зал"})["room"]["id"]

    def _members(self):
        return {r["id"]: list(r.get("devices") or []) for r in load_devices()["rooms"]}

    def _socket(self, **extra):
        dev = {"id": "sock", "name": "Розетка", "type": "devices.types.socket",
               "capabilities": [], "properties": []}
        dev.update(extra)
        return dev

    def test_update_moves_the_device_to_the_new_room(self):
        r = api.upsert_device(dict(self.LAMP, room_id=self.hall))
        self.assertTrue(r["ok"], r)
        self.assertEqual(load_devices()["devices"][0]["room_id"], self.hall)
        members = self._members()
        self.assertEqual(members[self.hall], ["lamp"])
        self.assertEqual(members[self.kitchen], [])

    def test_update_clearing_the_room_unassigns_everywhere(self):
        r = api.upsert_device(dict(self.LAMP, room_id=""))
        self.assertTrue(r["ok"], r)
        stored = load_devices()["devices"][0]
        self.assertNotIn("room_id", stored)
        self.assertEqual(self._members()[self.kitchen], [])

    def test_create_still_attaches(self):
        r = api.upsert_device(self._socket(room_id=self.hall))
        self.assertTrue(r["ok"], r)
        members = self._members()
        self.assertEqual(members[self.hall], ["sock"])
        self.assertEqual(members[self.kitchen], ["lamp"])

    def test_resaving_the_same_room_lists_the_device_once(self):
        # The sync removes before it appends — an unchanged edit (the common
        # case: a binding change) must not grow the room's list.
        for _ in range(3):
            self.assertTrue(api.upsert_device(dict(self.LAMP, room_id=self.kitchen))["ok"])
        self.assertEqual(self._members()[self.kitchen], ["lamp"])

    def test_unknown_room_refused_on_update_and_nothing_changes(self):
        r = api.upsert_device(dict(self.LAMP, room_id="ghost"))
        self.assertFalse(r["ok"], r)
        self.assertEqual(r.get("error"), "invalid_room")
        self.assertEqual(r.get("message"), "room not found")
        self.assertEqual(load_devices()["devices"][0].get("room_id"), self.kitchen)
        self.assertEqual(self._members()[self.kitchen], ["lamp"])

    def test_unknown_room_refused_on_create(self):
        r = api.upsert_device(self._socket(room_id="ghost"))
        self.assertFalse(r["ok"], r)
        self.assertEqual(r.get("error"), "invalid_room")
        self.assertEqual([d["id"] for d in load_devices()["devices"]], ["lamp"])

    def test_delete_device_leaves_no_dangling_membership(self):
        # Already true on this line — `delete_device` strips both sides. A
        # preservation pin, green before and after: the contract paragraph
        # states it, so a check has to hold it.
        self.assertTrue(api.delete_device("lamp")["ok"])
        self.assertEqual(self._members()[self.kitchen], [])

    def test_room_without_a_devices_key_keeps_its_shape(self):
        # A document written before the list existed carries rooms with no
        # `devices` key: joining one creates the list, a room nobody joined
        # keeps its shape (no empty key appears out of nowhere).
        save_devices({"rooms": [{"id": "r1", "name": "Кухня"},
                                {"id": "r2", "name": "Зал"}],
                      "devices": [dict(self.LAMP)]})
        r = api.upsert_device(dict(self.LAMP, room_id="r1"))
        self.assertTrue(r["ok"], r)
        rooms = {x["id"]: x for x in load_devices()["rooms"]}
        self.assertEqual(rooms["r1"].get("devices"), ["lamp"])
        self.assertNotIn("devices", rooms["r2"])


class TestApplyGroups(_CloudApiBase):
    def setUp(self):
        super().setUp()
        save_devices({"rooms": [], "groups": [], "devices": [
            {"id": "lamp", "name": "Лампа", "type": "devices.types.light",
             "capabilities": [], "properties": []},
            {"id": "sock", "name": "Розетка", "type": "devices.types.socket",
             "capabilities": [], "properties": []},
        ]})

    def test_create_move_and_delete_keeps_devices(self):
        created = api.apply_groups({"name": "Зал", "device_ids": ["lamp"]})
        self.assertTrue(created["ok"], created)
        gid = created["group"]["id"]
        self.assertEqual(created["group"]["name"], "Зал")
        self.assertEqual(created["group"]["device_ids"], ["lamp"])
        doc = load_devices()
        self.assertEqual(len(doc["devices"]), 2)
        self.assertEqual(doc["groups"][0]["device_ids"], ["lamp"])
        moved = api.apply_groups({"id": gid, "name": "Зал", "device_ids": ["lamp", "sock"]})
        self.assertTrue(moved["ok"], moved)
        doc = load_devices()
        self.assertEqual(doc["groups"][0]["device_ids"], ["lamp", "sock"])
        dropped = api.apply_groups({"id": gid, "delete": True})
        self.assertTrue(dropped["ok"], dropped)
        doc = load_devices()
        self.assertEqual(doc["groups"], [])
        self.assertEqual([d["id"] for d in doc["devices"]], ["lamp", "sock"])

    def test_invalid_name_rejected(self):
        for name in ("", "   ", "bad@name", "x" * 65, None, 1):
            r = api.apply_groups({"name": name})
            self.assertFalse(r["ok"], name)
            self.assertIn(r.get("error"), ("invalid_name", "invalid_group"))
        self.assertEqual(load_devices()["groups"], [])

    def test_unknown_group_edit_is_not_found(self):
        r = api.apply_groups({"id": "nope", "name": "X"})
        self.assertEqual(r.get("error"), "not_found")

    def test_sio_groups_reloads_and_emits(self):
        emitted = []
        h = SioHandlers(
            DeviceRegistry(),
            publish_mqtt=lambda *_a: None,
            emit_response=lambda d: emitted.append(d),
            profile=C.PROFILE_CLOUD,
        )
        h.handle(C.EVT_DEVICES_GROUPS, {"request_id": "g1", "name": "Зал",
                                        "device_ids": ["lamp"]})
        self.assertEqual(emitted[0]["ok"], True)
        self.assertEqual(emitted[0]["group"]["name"], "Зал")
        self.assertEqual(emitted[0]["group"]["device_ids"], ["lamp"])
        listed = h.registry.listed_groups()
        self.assertEqual(listed[0]["name"], "Зал")
        listed_devs = h.registry.discovery_devices(profile=C.PROFILE_CLOUD)
        self.assertTrue(all("groups" not in d for d in listed_devs))


class TestUpsertRoomMembership(_CloudApiBase):
    """`upsert_room` is the CREATE/RENAME path and never unbinds by omission.

    `models.validate_room` fills `devices: []` when the body omits the key and
    the handler stored the validated row wholesale, so a plain rename WIPED the
    room's membership while every device kept a `room_id` naming it — the room
    side of the desync class `upsert_device` had. A body that omits `devices`
    preserves the stored list; a body that carries one rebinds BOTH sides
    exactly like the atomic path `apply_rooms`
    (docs/contracts/alice-mqtt-mapping.md §Room membership).
    """

    LAMP = {"id": "lamp", "name": "Лампа", "type": "devices.types.light",
            "capabilities": [], "properties": []}
    SOCK = {"id": "sock", "name": "Розетка", "type": "devices.types.socket",
            "capabilities": [], "properties": []}

    def setUp(self):
        super().setUp()
        save_devices({"rooms": [], "groups": [],
                      "devices": [dict(self.LAMP), dict(self.SOCK)]})
        self.kitchen = api.apply_rooms({"name": "Кухня", "devices": ["lamp"]})["room"]["id"]
        self.hall = api.apply_rooms({"name": "Зал"})["room"]["id"]

    def _members(self):
        return {r["id"]: list(r.get("devices") or []) for r in load_devices()["rooms"]}

    def _dev(self, device_id):
        for d in load_devices()["devices"]:
            if d.get("id") == device_id:
                return d
        self.fail("device %s vanished" % device_id)

    def test_rename_preserves_the_stored_membership(self):
        out = api.upsert_room({"id": self.kitchen, "name": "Кухня 2"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["room"]["name"], "Кухня 2")
        self.assertEqual(out["room"].get("devices"), ["lamp"])
        self.assertEqual(self._members()[self.kitchen], ["lamp"])
        self.assertEqual(self._dev("lamp").get("room_id"), self.kitchen)

    def test_rename_keeps_a_pre_list_room_shape(self):
        # A document written before the list existed carries no `devices` key:
        # a rename must not conjure one (`_sync_room_membership`'s rule).
        save_devices({"rooms": [{"id": "r1", "name": "Кухня"}],
                      "devices": [dict(self.LAMP, room_id="r1")]})
        out = api.upsert_room({"id": "r1", "name": "Кухня 2"})
        self.assertTrue(out["ok"], out)
        # The response keeps its shape (`devices` always present) while the
        # STORED row keeps its own — the two shapes are separate rules.
        self.assertEqual(out["room"]["devices"], [])
        stored = load_devices()["rooms"][0]
        self.assertEqual(stored["name"], "Кухня 2")
        self.assertNotIn("devices", stored)
        self.assertEqual(self._dev("lamp").get("room_id"), "r1")

    def test_a_carried_list_rebinds_both_sides(self):
        out = api.upsert_room({"id": self.kitchen, "name": "Кухня",
                               "devices": ["sock"]})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self._members()[self.kitchen], ["sock"])
        self.assertEqual(self._dev("sock").get("room_id"), self.kitchen)
        # A dropped member loses the key outright — an empty string would still
        # serialise into the stored document.
        self.assertNotIn("room_id", self._dev("lamp"))

    def test_a_carried_list_drops_the_id_from_its_previous_room(self):
        out = api.upsert_room({"id": self.hall, "name": "Зал",
                               "devices": ["lamp"]})
        self.assertTrue(out["ok"], out)
        members = self._members()
        self.assertEqual(members[self.hall], ["lamp"])
        self.assertEqual(members[self.kitchen], [])
        self.assertEqual(self._dev("lamp").get("room_id"), self.hall)

    def test_an_unknown_device_is_refused_and_nothing_is_stored(self):
        before = load_devices()
        out = api.upsert_room({"id": self.kitchen, "name": "Кухня 2",
                               "devices": ["ghost"]})
        self.assertFalse(out["ok"], out)
        self.assertEqual(out.get("error"), "not_found")
        self.assertEqual(load_devices(), before)

    def test_create_with_devices_binds_them(self):
        out = api.upsert_room({"name": "Спальня", "devices": ["sock"]})
        self.assertTrue(out["ok"], out)
        rid = out["room"]["id"]
        self.assertEqual(self._members()[rid], ["sock"])
        self.assertEqual(self._dev("sock").get("room_id"), rid)

    def test_create_without_devices_binds_nobody(self):
        # Preservation pin — green before and after.
        out = api.upsert_room({"name": "Спальня"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self._members()[out["room"]["id"]], [])
        self.assertEqual(self._dev("lamp").get("room_id"), self.kitchen)

    def test_delete_room_leaves_no_device_naming_it(self):
        # Preservation pin: `delete_room` already clears `room_id`, and the
        # contract paragraph asserts it — so a check has to hold it.
        self.assertTrue(api.delete_room(self.kitchen)["ok"])
        self.assertNotIn("room_id", self._dev("lamp"))
        self.assertEqual([r["id"] for r in load_devices()["rooms"]], [self.hall])

    def test_group_rename_preserves_its_device_ids(self):
        # The neighbour checked for the same class: `apply_groups` is the only
        # group writer and it already preserves `device_ids` when the body
        # omits them. Preservation pin — green before and after.
        gid = api.apply_groups({"name": "Свет",
                                "device_ids": ["lamp", "sock"]})["group"]["id"]
        out = api.apply_groups({"id": gid, "name": "Свет 2"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["group"]["device_ids"], ["lamp", "sock"])
        self.assertEqual(load_devices()["groups"][0]["device_ids"], ["lamp", "sock"])


if __name__ == "__main__":
    unittest.main()
