"""Real HTTP requests prove auth/CSRF/limits before the dispatch boundary."""
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "sa02m-devices"))
from sa02m_alice.home.server import Handler
from sa02m_alice.home.bridge import request
from sa02m_alice.client.sio_handlers import SioHandlers
from sa02m_alice.common import constants as C


class Dummy:
    controller_id = "SA-test"
    calls = []

    def broker_available(self):
        return True

    def request(self, *args):
        self.calls.append(args)
        return 200, {"ok": True}

    def meta(self):
        return {"ok": True, "api_version": "1.0"}

    def envelope(self, **fields):
        return {"ok": True, **fields}


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token, self.csrf = "a" * 64, "b" * 64
        self.session = Path(self.tmp.name) / hashlib.sha256(self.token.encode()).hexdigest()
        self.session.write_text(str(int(time.time()) + 1000) + " admin\n")
        self.session.with_suffix(".csrf").write_text(self.csrf + "\n")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.session_dir = self.tmp.name
        self.server.dispatcher = Dummy()
        self.server.dispatcher.calls = []
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def call(self, method="GET", path="/api/home/v1/controllers/SA-test/snapshot", body=None, *, auth=True, csrf=True, headers=None):
        hdr = {"Content-Type": "application/json"}
        if auth:
            hdr["Cookie"] = "session_token=" + self.token
        if csrf:
            hdr["X-SA02M-CSRF"] = self.csrf
        hdr.update(headers or {})
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            conn.request(method, path, body, hdr)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_internal_failure_after_command_dispatch_reports_unknown(self):
        with patch.object(self.server.dispatcher, "request", side_effect=RuntimeError("receipt storage unavailable")) as dispatch:
            status, result = self.call("POST", "/api/home/v1/controllers/SA-test/commands", b"{}")
            self.assertEqual(status, 500)
            self.assertIs(result["outcome_unknown"], True)
            dispatch.assert_called_once()
            self.assertNotIn("outcome_unknown", self.call()[1])

    def test_no_session_no_dispatch_including_global_meta(self):
        for path in ("/api/home/v1/meta", "/api/home/v1/controllers", "/api/home/v1/controllers/SA-test/snapshot"):
            self.assertEqual(self.call(path=path, auth=False)[0], 401)
        self.assertFalse(self.server.dispatcher.calls)

    def test_expired_session_fails(self):
        self.session.write_text("1 admin\n")
        self.assertEqual(self.call()[0], 401)

    def test_controller_online_is_real_broker_availability(self):
        self.assertTrue(self.call(path="/api/home/v1/controllers")[1]["controllers"][0]["online"])
        self.server.dispatcher.broker_available = lambda: False
        self.assertFalse(self.call(path="/api/home/v1/controllers")[1]["controllers"][0]["online"])

    def test_post_requires_matching_csrf_and_is_not_replayed(self):
        path = "/api/home/v1/controllers/SA-test/lease"
        self.assertEqual(self.call("POST", path, b"{}", csrf=False)[0], 403)
        self.assertEqual(self.call("POST", path, b"{}", headers={"X-SA02M-CSRF": "wrong"})[0], 403)
        self.assertFalse(self.server.dispatcher.calls)
        self.assertEqual(self.call("POST", path, b"{}")[0], 200)
        self.assertEqual(len(self.server.dispatcher.calls), 1)

    def test_malformed_oversized_nonobject_body_never_dispatches(self):
        path = "/api/home/v1/controllers/SA-test/commands"
        for raw, status in ((b"[1]", 400), (b"{", 400), (b"x" * 65537, 413)):
            self.assertEqual(self.call("POST", path, raw)[0], status)
        self.assertFalse(self.server.dispatcher.calls)

    def test_controller_scope_and_path_traversal(self):
        for path in ("/api/home/v1/controllers/other/snapshot", "/api/home/v1/controllers/SA-test-other/snapshot", "/api/home/v1/controllers/SA-test%2f../snapshot"):
            self.assertEqual(self.call(path=path)[0], 404)
        self.assertFalse(self.server.dispatcher.calls)

    def test_tcp_cannot_use_root_peer_door(self):
        self.assertIn(self.call("POST", "/internal/home/v1")[0], (403, 503))
        self.assertFalse(self.server.dispatcher.calls)

    def test_csrf_disabled_mutation_is_detected(self):
        with patch("sa02m_alice.home.server.check_csrf", return_value=(True, "")):
            with self.assertRaises(AssertionError):
                self.assertEqual(self.call("POST", "/api/home/v1/controllers/SA-test/lease", b"{}", csrf=False)[0], 403)


class BridgeTests(unittest.TestCase):
    def test_result_of_interrupted_command_depends_on_dispatch(self):
        command = {"method": "POST", "path": "/commands", "body": {}}
        connection = Mock()
        connection.connect.side_effect = ConnectionRefusedError()
        before = request(command, lambda: connection)
        self.assertIs(before["body"]["outcome_unknown"], False)
        connection.request.assert_not_called()

        connection = Mock()
        connection.getresponse.side_effect = TimeoutError()
        after = request(command, lambda: connection)
        self.assertEqual(after["status"], 503)
        self.assertIs(after["body"]["outcome_unknown"], True)
        connection.request.assert_called_once()
        reading = request({"method": "GET", "path": "/history"}, lambda: connection)
        self.assertNotIn("outcome_unknown", reading["body"])

    def test_cloud_encoder_accounts_for_text_and_the_full_event(self):
        from sa02m_alice.home import bridge

        query = {"request_id": "catalog-read", "method": "GET", "path": "/snapshot"}
        payload = {"status": 200, "body": {"ok": True, "name": "Свет в мастерской"}}
        envelope = {"request_id": query["request_id"], "payload": payload}
        wire = "42" + json.dumps(["alice_devices_response", envelope], ensure_ascii=True, separators=(",", ":"))
        with patch.object(bridge, "request", return_value=payload):
            with patch.object(bridge, "CLOUD_PACKET_BYTES", len(wire)):
                self.assertEqual(bridge.cloud_reply(query), envelope)
            with patch.object(bridge, "CLOUD_PACKET_BYTES", len(wire) - 1):
                rejected = bridge.cloud_reply(query)
        self.assertEqual(rejected["payload"]["status"], 413)
        self.assertEqual(rejected["request_id"], query["request_id"])
        self.assertNotIn("outcome_unknown", rejected["payload"]["body"])

    def test_invalid_path_is_rejected_before_socket(self):
        def forbidden():
            self.fail("socket must not be touched")
        self.assertEqual(request({"method": "POST", "path": "/exec"}, forbidden)["status"], 404)

    def test_missing_dispatcher_is_unavailable_not_success(self):
        def absent():
            raise FileNotFoundError()
        self.assertEqual(request({"method": "GET", "path": "/snapshot"}, absent)["status"], 503)

    def test_cloud_only_event_and_transport_id_passthrough(self):
        output = []
        data = {"request_id": "transport", "method": "GET", "path": "/meta"}
        for profile in (C.PROFILE_YANDEX, C.PROFILE_CLOUD):
            handler = SioHandlers(None, publish_mqtt=lambda *_: self.fail("legacy publish called"), emit_response=output.append, profile=profile)
            with patch("sa02m_alice.home.bridge.request", return_value={"status": 200, "body": {"ok": True}}):
                handler.handle("home_api_v1", data)
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["request_id"], "transport")


if __name__ == "__main__":
    unittest.main()
