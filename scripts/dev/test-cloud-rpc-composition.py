#!/usr/bin/env python3
"""Actual fleet HTTP -> gateway hub -> SA handlers/store, on isolated loopback.

Run explicitly with CLOUD_RPC_CLOUD_ROOT set to the reviewed cloud checkout.
No stub replaces the hub RPC, Socket.IO registration, SA handler or persistence.
Only endpoint configuration, private file paths and fault injection are seams.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import dataclasses
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

SA = Path(__file__).resolve().parents[2]
CLOUD = Path(os.environ["CLOUD_RPC_CLOUD_ROOT"]).resolve()
for path in (SA / "opt/sa02m-alice", SA / "opt/sa02m-alice/tests",
             SA / "opt/sa02m-rules", CLOUD / "alice-gateway",
             CLOUD / "alice-gateway/tests"):
    sys.path.insert(0, str(path))

import test_cloud_delete_events as delete_fixture
from test_scenario_events import _rules_store_fresh
from test_cloud_entry import DEV, INTERNAL, TOKEN_SECRET, _make_settings, mint
from sa02m_alice.common import config_store, constants as C
from sa02m_alice.config import api
from sa02m_alice.client.device_registry import DeviceRegistry
from sa02m_alice.client.sio_connection import AliceSocketIO
from sa02m_alice.client.sio_handlers import SioHandlers
from cyntron_alice_gateway import internal as internal_module
from cyntron_alice_gateway.app import create_app
import uvicorn

OTHER = "sa02m-112233445566"
DELETE = delete_fixture.DELETE


def wait_for(check, message, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        threading.Event().wait(.01)
    raise AssertionError(message)


def request_http(port, method, path, data=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(method, path, body=data, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw.decode("utf-8", "replace")
        return response.status, dict(response.getheaders()), body
    finally:
        conn.close()


class Gateway:
    def __init__(self, directory):
        directory.mkdir(parents=True)
        cfg = dataclasses.replace(_make_settings(str(directory)),
                                  control_internal_secret=INTERNAL,
                                  control_token_secret=TOKEN_SECRET,
                                  trusted_peers="127.0.0.1")
        route = directory / "route"
        route.write_text("Iface Destination Gateway Flags\nlo 0000007F 00000000 0001\n")
        with mock.patch.object(internal_module, "_PROC_NET_ROUTE", str(route)):
            self.app = create_app(cfg)
        self.hub = self.app.other_asgi_app.state.hub
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, lifespan="off", loop="asyncio", log_level="warning"))
        self.loop = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        wait_for(lambda: self.server.started, "isolated gateway failed to start")

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.server.serve(sockets=[self.socket]))
        finally:
            remaining = asyncio.all_tasks(self.loop)
            for task in remaining:
                task.cancel()
            if remaining:
                self.loop.run_until_complete(asyncio.gather(*remaining, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def read(self, function):
        async def run():
            return function()
        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(timeout=5)

    def close(self):
        asyncio.run_coroutine_threadsafe(self.hub.sio.shutdown(), self.loop).result(timeout=5)
        self.server.should_exit = True
        self.thread.join(5)
        self.socket.close()
        assert not self.thread.is_alive(), "gateway thread was not cleaned up"
        self.app.other_asgi_app.state.store.close()


def backend_main(directory, gateway_port):
    """A separate real backend process, with its own SQLite and no workers."""
    os.environ.update(CLOUD_DB=str(directory / "cloud.db"),
                      CONTROL_HUB_ADDR="127.0.0.1:" + str(gateway_port),
                      CONTROL_HUB_SECRET=INTERNAL)
    sys.path.insert(0, str(CLOUD / "backend"))
    import server
    server.init_db()
    db = server.db()
    for user in ("owner", "other"):
        db.execute("INSERT INTO operators(username,pw_hash,created,role) VALUES(?,?,?,?)",
                   (user, server.pw_hash("synthetic-password"), int(time.time()), "user"))
    for device, owner in ((DEV, "owner"), (OTHER, "other")):
        db.execute("INSERT INTO devices(id,owner,enrolled,revoked,created) VALUES(?,?,1,0,?)",
                   (device, owner, int(time.time())))
    db.commit(); db.close()
    instance = server.Threading(("127.0.0.1", 0), server.H)
    (directory / "backend.port").write_text(str(instance.server_address[1]))
    instance.serve_forever()


class Peer:
    def __init__(self, gateway, fixture, device=DEV):
        self.fixture = fixture
        self.received = []
        self.responses = []
        self.mode = "normal"
        self.held = threading.Event()
        self.registry = DeviceRegistry(profile=C.PROFILE_CLOUD)
        self.socket = AliceSocketIO(profile=C.PROFILE_CLOUD,
                                    token_provider=lambda: mint(sub=device), on_event=self.receive)
        self.handler = SioHandlers(self.registry, profile=C.PROFILE_CLOUD,
                                   publish_mqtt=fixture.published,
                                   emit_response=self.respond, on_unlink=fixture.unlinked)
        # Simulate the cloud-entry reverse proxy's explicit routing header.
        # The token is verified by the real gateway on the actual connection.
        original = self.socket._build_headers
        with mock.patch.object(self.socket, "_connect_target", return_value=(
                "http://127.0.0.1:" + str(gateway.port), "controller/socket.io", False, None)), \
             mock.patch.object(self.socket, "_build_headers", side_effect=lambda token="": {
                 **original(token), "X-Control-Entry": "cloud"}):
            self.socket.connect()

    def receive(self, event, data):
        self.received.append((event, copy.deepcopy(data)))
        if event == DELETE and self.mode == "drop":
            return
        self.handler.handle(event, data)

    def respond(self, data):
        self.responses.append(copy.deepcopy(data))
        if self.mode == "hold" and "ok" in data:
            self.held.set()
            return
        if self.mode == "wrong-id" and "ok" in data:
            data = {**data, "request_id": "unrelated-id"}
        self.socket.emit_response(data)

    def close(self):
        self.socket.disconnect()


class Composition(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cloud-rpc-composition-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.fixture = delete_fixture.CloudDeleteEvents()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.gateway = Gateway(self.directory / "gateway")
        self.addCleanup(self.gateway.close)
        self.backend_log = (self.directory / "backend.log").open("wb")
        self.addCleanup(self.backend_log.close)
        self.backend = subprocess.Popen([sys.executable, __file__, "--backend", str(self.directory), str(self.gateway.port)],
                                        stdout=self.backend_log, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop_backend)
        wait_for(lambda: (self.directory / "backend.port").exists() or self.backend.poll() is not None,
                 "backend did not start")
        self.assertIsNone(self.backend.poll(), (self.directory / "backend.log").read_text())
        self.port = int((self.directory / "backend.port").read_text())
        self.cookies = {}
        for user in ("owner", "other"):
            status, headers, _ = request_http(self.port, "POST", "/login",
                                      urlencode({"username": user, "password": "synthetic-password"}),
                                      {"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"})
            self.assertEqual(status, 302)
            self.cookies[user] = headers["Set-Cookie"].split(";", 1)[0]
        self.peer = Peer(self.gateway, self.fixture)
        self.addCleanup(self.peer.close)
        wait_for(lambda: "carel-uaria" in self.cached_ids(), "actual initial list did not populate gateway cache")

    def stop_backend(self):
        if self.backend.poll() is None:
            self.backend.terminate()
        self.backend.wait(timeout=5)
        self.assertIsNotNone(self.backend.returncode)

    def snapshot(self):
        return self.gateway.read(lambda: self.gateway.hub.cache.snapshot("cloud:" + DEV, True))

    def cached_ids(self):
        return {row["id"] for row in self.snapshot()["catalog"]}

    def control(self, body, user="owner", device=DEV):
        status, _, obj = request_http(self.port, "POST", "/api/v1/devices/" + device + "/control",
                              json.dumps(body), {"Content-Type": "application/json", "Cookie": self.cookies[user],
                                                 "Sec-Fetch-Site": "same-origin"})
        return status, obj

    def delete(self, **kwargs):
        return self.control({"device": "carel-uaria", "delete": True}, **kwargs)

    def reset_catalogue(self):
        config_store.save_devices(copy.deepcopy(self.fixture.doc))
        self.peer.registry.reload()
        # Refresh via the real hub request, not direct cache seeding.
        result = asyncio.run_coroutine_threadsafe(
            self.gateway.hub._refresh_catalog("cloud:" + DEV), self.gateway.loop).result(timeout=10)
        wait_for(lambda: "carel-uaria" in self.cached_ids(), "catalogue reset did not arrive over the wire")
        return result

    def test_actual_delete_paths_and_boundaries(self):
        before = Path(C.DEVICES_CONF).read_bytes()
        for kwargs in ({"user": "other"}, {"device": OTHER}):
            count = len(self.peer.received)
            self.assertEqual(self.delete(**kwargs)[0], 404)
            self.assertEqual(len(self.peer.received), count)
            self.assertEqual(Path(C.DEVICES_CONF).read_bytes(), before)
        stamp = self.snapshot()["catalog_ts"]
        self.assertEqual(self.delete(), (200, {"ok": True}))
        self.fixture.assert_deleted(self.peer.registry)
        self.assertNotIn("carel-uaria", self.cached_ids())
        self.assertEqual(self.snapshot()["catalog_ts"], stamp)
        event, request = [row for row in self.peer.received if row[0] == DELETE][-1]
        self.assertTrue(request["request_id"])
        self.assertEqual(self.peer.responses[-1]["request_id"], request["request_id"])
        self.assertEqual(self.delete(), (404, {"ok": False, "error": "not_found"}))

        self.reset_catalogue()
        for mode in ("drop", "wrong-id"):
            self.peer.mode = mode
            status, response = self.delete()
            self.assertEqual((status, response), (502, {"ok": False, "error": "timeout"}))
            self.assertIn("carel-uaria", self.cached_ids())
            self.peer.mode = "normal"
            if mode == "wrong-id":
                self.assertEqual(self.delete(), (404, {"ok": False, "error": "not_found"}))
                self.assertNotIn("carel-uaria", self.cached_ids())
                self.reset_catalogue()
            else:
                self.assertEqual(config_store.load_devices(), self.fixture.doc)

        with mock.patch.object(api, "save_devices", side_effect=OSError("private fixture sentinel")):
            self.assertEqual(self.delete(), (502, {"ok": False, "error": "internal"}))
        self.assertEqual(config_store.load_devices(), self.fixture.doc)
        self.assertIn("carel-uaria", self.cached_ids())
        with mock.patch("sa02m_alice.client.device_registry.load_devices", side_effect=OSError("private fixture sentinel")):
            self.assertEqual(self.delete(), (502, {"ok": False, "error": "internal"}))
        self.assertIn("carel-uaria", self.cached_ids())
        self.assertNotIn("carel-uaria", {d["id"] for d in config_store.load_devices()["devices"]})
        self.assertEqual(self.delete(), (404, {"ok": False, "error": "not_found"}))
        self.assertNotIn("carel-uaria", self.cached_ids())
        self.reset_catalogue()

        # The same request ID on a second authenticated controller must not
        # complete the first controller's pending RPC. Socket.IO call supplies
        # a protocol ACK barrier, proving the gateway processed that packet.
        other = Peer(self.gateway, self.fixture, OTHER)
        self.addCleanup(other.close)
        self.peer.mode = "hold"
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.delete)
            self.assertTrue(self.peer.held.wait(5))
            response = copy.deepcopy(self.peer.responses[-1])
            other.socket._sio.call("alice_devices_response", response, timeout=3)
            self.assertFalse(pending.done())
            self.assertTrue(self.gateway.read(lambda: not self.gateway.hub._pending[("cloud:" + DEV, response["request_id"])].done()))
            self.peer.socket.emit_response(response)
            self.assertEqual(pending.result(timeout=5), (200, {"ok": True}))
        self.peer.mode = "normal"
        self.reset_catalogue()
        self.peer.close()
        wait_for(lambda: self.gateway.read(lambda: "cloud:" + DEV not in self.gateway.hub._sn_to_sid), "disconnect not observed")
        self.assertEqual(self.delete(), (409, {"ok": False, "error": "offline"}))
        self.assertEqual(config_store.load_devices(), self.fixture.doc)
        self.assertIn("carel-uaria", self.cached_ids())

    def test_actual_scenario_response_updates_cache_without_list(self):
        store = _rules_store_fresh(str(self.directory / "rules.json"))
        with mock.patch.object(api, "_rules_store", return_value=store), mock.patch.object(store, "RULES_ENGINE", 3):
            # Seed a real private document, then use a valid command that does
            # not ask fleet completeness checks to refresh the device list.
            seeded = api.apply_scenarios({"name": "Composition scenario"})
            self.assertTrue(seeded["ok"])
            count = sum(event == C.EVT_DEVICES_LIST for event, _ in self.peer.received)
            status, response = self.control({"scenario": {"ack_notify": True}})
            self.assertEqual(status, 200, response)
            self.assertTrue(response["ok"])
            self.assertEqual(self.snapshot()["rules_engine"], 3)
            self.assertEqual(self.snapshot()["scenarios"], response["scenarios"])
            self.assertEqual(sum(event == C.EVT_DEVICES_LIST for event, _ in self.peer.received), count)
            self.assertEqual(self.peer.responses[-1]["rules_engine"], 3)
            # The gateway owns the engine marker in its output; the fleet's
            # existing success envelope deliberately contains only documents.
            status, _, gateway_response = request_http(
                self.gateway.port, "POST", "/internal/v1/controllers/" + DEV + "/scenarios",
                json.dumps({"ack_notify": True}),
                {"Content-Type": "application/json", "X-Internal-Secret": INTERNAL})
            self.assertEqual(status, 200, gateway_response)
            self.assertTrue(gateway_response["ok"])
            self.assertEqual(gateway_response["rules_engine"], 3)
            self.assertEqual(gateway_response["scenarios"], seeded["scenarios"])
            self.assertEqual(gateway_response["request_id"], self.peer.responses[-1]["request_id"])
            self.assertEqual(sum(event == C.EVT_DEVICES_LIST for event, _ in self.peer.received), count)
            with mock.patch.object(api, "_rules_store", return_value=None):
                status, response = self.control({"scenario": {"ack_notify": True}})
            self.assertEqual(status, 502)
            self.assertFalse(response["ok"])
            self.assertNotIn("rules_engine", self.peer.responses[-1])
            self.assertEqual(self.snapshot()["rules_engine"], 3)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--backend":
        backend_main(Path(sys.argv[2]), int(sys.argv[3]))
    else:
        unittest.main(verbosity=2)
