"""Cloud catalogue deletion through the actual registered SA callback/API/store."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

ALICE_ROOT = Path(__file__).resolve().parents[1]
if str(ALICE_ROOT) not in sys.path:
    sys.path.insert(0, str(ALICE_ROOT))

if __package__:
    from .test_cloud_control_api import _CloudApiBase
else:
    from test_cloud_control_api import _CloudApiBase
from sa02m_alice.common import config_store, constants as C
from sa02m_alice.config import api, inventory, scene_devices, topics
from sa02m_alice.client.device_registry import DeviceRegistry
from sa02m_alice.client.sio_connection import AliceSocketIO
from sa02m_alice.client.sio_handlers import SioHandlers

DELETE = "alice_devices_delete"


class RecordingSocket:
    def __init__(self):
        self.callbacks = {}
        self.lifecycle = {}
        self.responses = []

    def event(self, callback):
        self.lifecycle[callback.__name__] = callback
        return callback

    def on(self, name, callback):
        self.callbacks[name] = callback

    def connect(self, url, **kwargs):
        assert url == "http://127.0.0.1"
        self.lifecycle["connect"]()

    def get_sid(self):
        return "isolated-peer"

    def emit(self, event, data):
        self.responses.append((event, data))


class CloudDeleteEvents(_CloudApiBase):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.protected = {}
        for name in ("CERT_FILE", "KEY_FILE", "CA_FILE", "PENDING_CLAIM_FILE",
                     "CLOUD_AGENT_CONF", "CLOUD_DEVICE_SECRET"):
            p = Path(getattr(C, name)); p.parent.mkdir(parents=True, exist_ok=True)
            body = ("synthetic fixture " + name + "\n").encode()
            p.write_bytes(body); self.protected[p] = body
        bridge = root / "bridge.yaml"
        bridge.write_text("devices:\n- id: carel-uaria\n  type: carel\n")
        self.protected[bridge] = bridge.read_bytes()
        self.patch(topics, "YAML_CANDIDATES", (str(bridge),))
        self.patch(topics, "_controller_device_id", lambda: "fixture-controller")
        self.patch(inventory.bridge_families, "INDEX", str(root / "bridge-index.json"))
        self.patch(inventory.bridge_families, "PRODUCER", str(root / "bridge-producer.py"))
        self.patch(scene_devices, "rules_store", lambda: None)
        env = mock.patch.dict(os.environ, {"SA02M_MQTT_LIVE_CACHE": str(root / "live-cache")})
        env.start(); self.addCleanup(env.stop)
        self.published = mock.Mock()
        self.unlinked = mock.Mock()
        self.doc = {
            "metadata": {"keep": [1, 2]},
            "rooms": [{"id": "r1", "name": "Room", "color": "blue", "devices": ["carel-uaria", "lamp"]},
                      {"id": "r2", "name": "Empty"}],
            "groups": [{"id": "g1", "name": "Group", "icon": "ahu", "device_ids": ["carel-uaria", "lamp"]}],
            "devices": [
                {"id": "carel-uaria", "name": "Unavailable synthetic Carel", "type": "devices.types.ventilation",
                 "capabilities": [{"type": "devices.capabilities.on_off", "retrievable": True,
                                   "mqtt": "/devices/carel-COM9-55/controls/on"}], "properties": []},
                {"id": "carel-uaria-extra", "name": "Exact-match neighbour", "type": "devices.types.other",
                 "capabilities": [], "properties": []},
                {"id": "lamp", "name": "Keep", "room_id": "r1", "type": "devices.types.light",
                 "capabilities": [], "properties": []}],
        }
        config_store.save_devices(copy.deepcopy(self.doc))

    def patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        self.published.assert_not_called()
        self.unlinked.assert_not_called()
        for path, body in self.protected.items():
            self.assertEqual(path.read_bytes(), body, str(path))
        super().tearDown()

    def make(self, profile=C.PROFILE_CLOUD):
        registry = DeviceRegistry(profile=profile)
        responses = []
        handler = SioHandlers(registry, profile=profile, publish_mqtt=self.published,
                              emit_response=responses.append, on_unlink=self.unlinked)
        return handler, registry, responses

    def connected(self, profile=C.PROFILE_CLOUD):
        registry = DeviceRegistry(profile=profile)
        peer = RecordingSocket()
        sio = AliceSocketIO(profile=profile, token_provider=lambda: "synthetic-token",
                            on_event=lambda event, data: handler.handle(event, data))
        handler = SioHandlers(registry, profile=profile, publish_mqtt=self.published,
                              emit_response=sio.emit_response, on_unlink=self.unlinked)
        with mock.patch("sa02m_alice.client.sio_connection.import_socketio",
                        return_value=types.SimpleNamespace(Client=lambda **kwargs: peer)), \
             mock.patch.object(sio, "_connect_target", return_value=("http://127.0.0.1", "control/socket.io", False, None)):
            sio.connect()
        return peer, handler, registry

    def assert_deleted(self, registry):
        expected = copy.deepcopy(self.doc)
        expected["devices"] = expected["devices"][1:]
        expected["rooms"][0]["devices"] = ["lamp"]
        expected["groups"][0]["device_ids"] = ["lamp"]
        self.assertEqual(config_store.load_devices(), expected)
        ids = {d["id"] for d in registry.discovery_devices(profile=C.PROFILE_CLOUD)}
        self.assertEqual(ids, {"carel-uaria-extra", "lamp"})
        self.assertEqual(DeviceRegistry(profile=C.PROFILE_CLOUD).discovery_devices(profile=C.PROFILE_CLOUD),
                         registry.discovery_devices(profile=C.PROFILE_CLOUD))

    def test_actual_cloud_registration_includes_delete_and_yandex_excludes_it(self):
        cloud, _, _ = self.connected()
        yandex, _, _ = self.connected(C.PROFILE_YANDEX)
        self.assertIn(DELETE, cloud.callbacks)
        self.assertNotIn(DELETE, yandex.callbacks)
        self.assertNotIn(C.EVT_CONTROLLER_UNLINK, cloud.callbacks)
        self.assertIn(C.EVT_CONTROLLER_UNLINK, yandex.callbacks)

    def test_actual_dispatch_replies_and_changes_only_catalogue(self):
        handler, registry, responses = self.make()
        handler.handle(DELETE, {"request_id": "direct-rpc", "device": "carel-uaria"})
        self.assertEqual(responses, [{"request_id": "direct-rpc", "ok": True, "error": None}])
        self.assert_deleted(registry)

    def test_registered_callback_uses_real_persistence_reload_and_response_channel(self):
        peer, _, registry = self.connected()
        self.assertIn(DELETE, peer.callbacks)
        peer.callbacks[DELETE]({"request_id": " hub-id \t", "device": " carel-uaria "})
        self.assertEqual(peer.responses, [("alice_devices_response", {"request_id": " hub-id \t", "ok": True, "error": None})])
        self.assert_deleted(registry)

    def test_invalid_device_has_correlated_error_and_no_side_effect(self):
        handler, registry, responses = self.make()
        before = Path(C.DEVICES_CONF).read_bytes()
        with mock.patch.object(api, "delete_device", wraps=api.delete_device) as delete, \
             mock.patch.object(registry, "reload", wraps=registry.reload) as reload:
            for device in (None, "", " \t", False, True, 0, [], {}, "x" * 129, " " + "x" * 127 + " "):
                with self.subTest(device=device):
                    responses.clear()
                    handler.handle(DELETE, {"request_id": "invalid", "device": device})
                    self.assertEqual(responses, [{"request_id": "invalid", "ok": False, "error": "invalid_device"}])
            delete.assert_not_called(); reload.assert_not_called()
        self.assertEqual(Path(C.DEVICES_CONF).read_bytes(), before)

    def test_no_undocumented_id_alias(self):
        handler, _, responses = self.make()
        handler.handle(DELETE, {"request_id": "alias", "id": "carel-uaria"})
        self.assertEqual(responses, [{"request_id": "alias", "ok": False, "error": "invalid_device"}])
        self.assertEqual(config_store.load_devices(), self.doc)

    def test_old_existing_ids_up_to_128_are_removable(self):
        for length in (65, 128):
            with self.subTest(length=length):
                did = "x" * length
                config_store.save_devices({"devices": [{"id": did, "name": "Legacy", "capabilities": []}]})
                handler, _, responses = self.make()
                handler.handle(DELETE, {"request_id": "legacy", "device": did})
                self.assertEqual(responses, [{"request_id": "legacy", "ok": True, "error": None}])
                self.assertEqual(config_store.load_devices()["devices"], [])

    def test_invalid_correlation_and_non_objects_do_not_mutate_or_reply(self):
        handler, registry, responses = self.make()
        with mock.patch.object(api, "delete_device", wraps=api.delete_device) as delete, \
             mock.patch.object(registry, "reload", wraps=registry.reload) as reload:
            for rid in (None, "", " \t", False, True, 1, {}, []):
                handler.handle(DELETE, {"request_id": rid, "device": "carel-uaria"})
            for data in (None, [], True, 0, "carel-uaria"):
                handler.handle(DELETE, data)
            delete.assert_not_called(); reload.assert_not_called()
        self.assertEqual(responses, [])
        self.assertEqual(config_store.load_devices(), self.doc)

    def test_direct_yandex_dispatch_is_ignored(self):
        handler, registry, responses = self.make(C.PROFILE_YANDEX)
        with mock.patch.object(api, "delete_device", wraps=api.delete_device) as delete, \
             mock.patch.object(registry, "reload", wraps=registry.reload) as reload:
            handler.handle(DELETE, {"request_id": "yandex", "device": "carel-uaria"})
            delete.assert_not_called(); reload.assert_not_called()
        self.assertEqual(responses, [])
        self.assertEqual(config_store.load_devices(), self.doc)

    def test_genuine_not_found_reconciles_without_rewriting(self):
        handler, registry, responses = self.make()
        api.delete_device("carel-uaria")
        before = Path(C.DEVICES_CONF).read_bytes()
        with mock.patch.object(api, "save_devices", wraps=api.save_devices) as save:
            handler.handle(DELETE, {"request_id": "missing", "device": "carel-uaria"})
            save.assert_not_called()
        self.assertEqual(responses, [{"request_id": "missing", "ok": False, "error": "not_found"}])
        self.assertEqual(Path(C.DEVICES_CONF).read_bytes(), before)
        self.assert_deleted(registry)

    def test_write_failure_is_internal_and_never_retried(self):
        handler, registry, responses = self.make()
        before = Path(C.DEVICES_CONF).read_bytes()
        with mock.patch.object(api, "save_devices", side_effect=OSError("private sentinel")) as save, \
             mock.patch.object(registry, "reload", wraps=registry.reload) as reload:
            handler.handle(DELETE, {"request_id": "write", "device": "carel-uaria"})
            save.assert_called_once(); reload.assert_not_called()
        self.assertEqual(responses, [{"request_id": "write", "ok": False, "error": "internal"}])
        self.assertEqual(Path(C.DEVICES_CONF).read_bytes(), before)

    def test_reload_failure_after_save_is_internal_then_retry_reconciles(self):
        handler, registry, responses = self.make()
        with mock.patch.object(api, "save_devices", wraps=api.save_devices) as save:
            with mock.patch("sa02m_alice.client.device_registry.load_devices", side_effect=OSError("private sentinel")):
                handler.handle(DELETE, {"request_id": "first", "device": "carel-uaria"})
            self.assertEqual(responses, [{"request_id": "first", "ok": False, "error": "internal"}])
            self.assertNotIn("carel-uaria", [d["id"] for d in config_store.load_devices()["devices"]])
            self.assertIn("carel-uaria", [d["id"] for d in registry.discovery_devices(profile=C.PROFILE_CLOUD)])
            handler.handle(DELETE, {"request_id": "retry", "device": "carel-uaria"})
            save.assert_called_once()
        self.assertEqual(responses[-1], {"request_id": "retry", "ok": False, "error": "not_found"})
        self.assert_deleted(registry)

    def test_failed_emit_is_unknown_ack_and_does_not_repeat_mutation(self):
        peer, _, registry = self.connected()
        self.assertIn(DELETE, peer.callbacks)
        with mock.patch.object(peer, "emit", side_effect=ConnectionError("transport failed")) as emit, \
             mock.patch.object(api, "delete_device", wraps=api.delete_device) as delete:
            with self.assertRaises(ConnectionError):
                peer.callbacks[DELETE]({"request_id": "lost-ack", "device": "carel-uaria"})
            delete.assert_called_once_with("carel-uaria")
            emit.assert_called_once()
        self.assert_deleted(registry)


if __name__ == "__main__":
    unittest.main()
