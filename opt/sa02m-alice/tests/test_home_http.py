"""Real HTTP requests prove auth/CSRF/limits before the dispatch boundary."""
import hashlib
import http.client
from http.server import ThreadingHTTPServer
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "sa02m-devices"))
from sa02m_alice.home.server import Handler
from sa02m_alice.home.bridge import request
from sa02m_alice.client.sio_handlers import SioHandlers
from sa02m_alice.common import constants as C
from sa02m_alice.home.contract import APIError


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

    def test_internal_errors_log_only_fixed_operation_and_exception_type(self):
        secret = "PRIVATE_SENTINEL"
        for error, status, code in ((OSError, 503, "temporarily_unavailable"),
                                    (ValueError, 503, "temporarily_unavailable"),
                                    (RuntimeError, 500, "internal")):
            with self.subTest(error=error.__name__):
                stderr = io.StringIO()
                exception = error(secret + "\npassword=secret\x1b /private/path " + self.token)
                with patch.object(self.server.dispatcher, "request", side_effect=exception), \
                        redirect_stderr(stderr), self.assertLogs("sa02m-home.server") as logs:
                    result = self.call("POST", "/api/home/v1/controllers/SA-test/snapshot?private=" + secret,
                                       json.dumps({"private": secret}), headers={"X-Private": secret})
                self.assertEqual(result, (status, {"ok": False, "error": code}))
                self.assertEqual(len(logs.records), 1)
                record = logs.records[0]
                self.assertEqual(record.getMessage(), "home operation=http_request error_type=" + error.__name__)
                self.assertIsNone(record.exc_info)
                self.assertIsNone(record.stack_info)
                output = "\n".join(logs.output) + stderr.getvalue()
                for forbidden in (secret, self.token, self.csrf, "SA-test", "password", "/private", "Traceback"):
                    self.assertNotIn(forbidden, output)

    def test_expected_client_refusals_do_not_log_internal_errors(self):
        with self.assertNoLogs("sa02m-home.server"):
            self.assertEqual(self.call(auth=False)[0], 401)
            self.assertEqual(self.call("POST", body="{")[0], 400)
            with patch.object(self.server.dispatcher, "request", side_effect=APIError(409, "conflict")):
                self.assertEqual(self.call(), (409, {"ok": False, "error": "conflict"}))


class BridgeTests(unittest.TestCase):
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
