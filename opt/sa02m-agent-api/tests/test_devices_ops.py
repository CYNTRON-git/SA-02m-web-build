# -*- coding: utf-8 -*-
"""devices.history / devices.summary: the daemon's query grammar, the whole
body, and the panel session the devices daemon checks on every listener.

The grammar's home is opt/sa02m-devices/sa02m_devices/api.py (handle_history,
handle_summary, _range_key_from_qs); these tests pin what the op forwards.
"""

import ast
import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler

from sa02m_agent_api import ops as ops_mod
from sa02m_agent_api import service as svc
from sa02m_agent_api.openapi import build
from sa02m_agent_api.registry import OPS, get
from sa02m_agent_api.service import App
from sa02m_agent_api.tokens import RateLimiter
from sa02m_agent_api.websession import check_session_store

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DEVICES_PKG = os.path.join(REPO, "opt", "sa02m-devices", "sa02m_devices")


def _split(path):
    bare, _, query = path.partition("?")
    return bare, dict(urllib.parse.parse_qsl(query, keep_blank_values=True))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SA02M_AGENT_TOKEN_DIRECT"] = "1"
        os.environ["SA02M_AGENT_TOKEN_FILE"] = os.path.join(self.tmp.name, "tokens.json")
        os.environ["SA02M_AGENT_RUNTIME"] = self.tmp.name
        os.environ["SA02M_AGENT_AUDIT"] = os.path.join(self.tmp.name, "audit.jsonl")
        os.environ["SA02M_SESSION_DIR"] = os.path.join(self.tmp.name, "sessions")
        os.makedirs(os.environ["SA02M_SESSION_DIR"])
        self.app = App()
        self.app.store.path = os.environ["SA02M_AGENT_TOKEN_FILE"]
        self.app.audit_path = os.environ["SA02M_AGENT_AUDIT"]
        self.app.limiter = RateLimiter(100000)
        self.app.session.session_dir = os.environ["SA02M_SESSION_DIR"]
        self.app.sink = None
        _row, self.token = self.app.store.issue("reader", ["read"], 1, False)

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, path):
        return self.app.handle("GET", path, {"X-SA02M-Token": self.token}, b"")

    def post(self, path, args):
        return self.app.handle("POST", path, {"X-SA02M-Token": self.token},
                               json.dumps(args).encode("utf-8"))


class DevicesGrammar(_Base):
    """The op forwards the daemon's keys; ctx.unix_request is recorded."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.reply = (200, '{"ok":true}')
        self._orig = svc.Ctx.unix_request

        def fake(ctx, sock, method, path, body=b""):
            self.calls.append((sock, method, path))
            return self.reply
        svc.Ctx.unix_request = fake

    def tearDown(self):
        svc.Ctx.unix_request = self._orig
        super().tearDown()

    def test_energy_group_reaches_the_daemon(self):
        status, body = self.get(
            "/api/v1/devices/history?group=energy&range=30d&device_id=ce02m3-COM1-5")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.calls), 1)
        _sock, method, path = self.calls[0]
        self.assertEqual(method, "GET")
        bare, query = _split(path)
        self.assertEqual(bare, "/api/devices/history")
        self.assertEqual(query, {"group": "energy", "range": "30d",
                                 "device_id": "ce02m3-COM1-5"})

    def test_every_daemon_key_forwarded_and_t0_t1_dropped(self):
        status, body = self.post("/api/v1/devices/history", {
            "device_id": "mr02m-COM2-3", "kind": "mr", "metric": "ai",
            "window_s": 7200, "channel": "ai_5", "t0": 1, "t1": 2,
        })
        self.assertEqual(status, 200, body)
        _bare, query = _split(self.calls[0][2])
        self.assertEqual(query, {"device_id": "mr02m-COM2-3", "kind": "mr",
                                 "metric": "ai", "window_s": "7200", "channel": "ai_5"})

    def test_device_is_a_legacy_alias_of_device_id(self):
        self.post("/api/v1/devices/history", {"device": "carel-COM3-2", "kind": "carel"})
        _bare, query = _split(self.calls[0][2])
        self.assertEqual(query, {"device_id": "carel-COM3-2", "kind": "carel"})
        self.calls[:] = []
        self.post("/api/v1/devices/history",
                  {"device": "carel-COM3-9", "device_id": "carel-COM3-2", "kind": "carel"})
        _bare, query = _split(self.calls[0][2])
        self.assertEqual(query["device_id"], "carel-COM3-2")

    def test_only_present_keys_travel(self):
        self.get("/api/v1/devices/history?group=climate&device_id=&metric=")
        bare, query = _split(self.calls[0][2])
        self.assertEqual(bare, "/api/devices/history")
        self.assertEqual(query, {"group": "climate"})

    def test_bad_values_refused_before_the_socket(self):
        for args, key in (
            ({"range": "2d"}, "range"),
            ({"group": "boiler"}, "group"),
            ({"window_s": "abc"}, "window_s"),
            ({"window_s": True}, "window_s"),
            ({"device_id": "a/../b"}, "device_id"),
            ({"device_id": "x&group=energy"}, "device_id"),
            ({"kind": "Carel;"}, "kind"),
            ({"metric": "a b"}, "metric"),
            ({"channel": "1;2"}, "channel"),
            ({"group": ["energy"]}, "group"),
        ):
            status, body = self.post("/api/v1/devices/history", args)
            self.assertEqual(status, 400, (args, body))
            self.assertEqual(body.get("error"), "bad_request", args)
            self.assertEqual(body.get("reason"), key, args)
        self.assertEqual(self.calls, [])

    def test_summary_routes_to_the_summary_endpoint(self):
        status, body = self.get(
            "/api/v1/devices/summary?range=mtd&device_id=ce02m3-COM1-5&kwh_rub=7,5")
        self.assertEqual(status, 200, body)
        bare, query = _split(self.calls[0][2])
        self.assertEqual(bare, "/api/devices/history/summary")
        self.assertEqual(query, {"range": "mtd", "device_id": "ce02m3-COM1-5", "kwh_rub": "7,5"})
        self.calls[:] = []
        status, body = self.post("/api/v1/devices/summary", {"kwh_rub": "cheap"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body.get("reason"), "kwh_rub")
        self.assertEqual(self.calls, [])
        op = get("devices.summary")
        self.assertEqual(op.scope, "read")
        self.assertFalse(op.mutating)

    def test_whole_body_returned(self):
        # A 30d energy/Carel batch is ~150-300 KB; a cut string is not JSON.
        series = [[1759800000000 + i * 3600000, 229.4] for i in range(2000)]
        raw = json.dumps({"ok": True, "metrics": [{"metric": "voltage", "series": series}]})
        self.assertGreater(len(raw), 8000)
        self.reply = (200, raw)
        status, body = self.get("/api/v1/devices/history?group=energy&range=30d")
        self.assertEqual(status, 200)
        self.assertEqual(body["body"], raw)
        self.assertEqual(json.loads(body["body"])["metrics"][0]["series"], series)

    def test_too_large_is_an_error_not_a_body(self):
        self.reply = (502, json.dumps(
            {"ok": False, "error": "response_too_large", "limit_bytes": 1048576}))
        for path in ("/api/v1/devices/history?group=energy&range=7d",
                     "/api/v1/devices/summary?range=30d"):
            status, body = self.get(path)
            self.assertEqual(status, 502, path)
            self.assertEqual(body.get("error"), "response_too_large", path)
            self.assertEqual(body.get("limit_bytes"), 1048576, path)
            self.assertIn("window_s", body.get("hint", ""), path)
            self.assertNotIn("body", body, path)

    def test_daemon_status_passes_through(self):
        self.reply = (401, '{"ok":false,"error":"unauthorized"}')
        status, body = self.get("/api/v1/devices/history?group=energy")
        self.assertEqual(status, 401)
        self.assertFalse(body["ok"])


class ReadLimit(unittest.TestCase):
    """The socket read cap reports an overflow instead of cutting silently."""

    def test_read_capped(self):
        import io
        from sa02m_agent_api.service import read_capped, too_large_body
        self.assertEqual(read_capped(io.BytesIO(b"a" * 10), 10), (b"a" * 10, False))
        data, over = read_capped(io.BytesIO(b"a" * 11), 10)
        self.assertTrue(over)
        self.assertEqual(len(data), 10)
        self.assertEqual(too_large_body(10),
                         {"ok": False, "error": "response_too_large", "limit_bytes": 10})


class DevicesSchema(unittest.TestCase):
    def test_openapi_parameters_follow_the_daemon_grammar(self):
        ops_mod.register_all()
        doc = build(OPS)
        params = {
            p["name"]: p["schema"]
            for p in doc["paths"]["/api/v1/devices/history"]["get"]["parameters"]
        }
        self.assertEqual(set(params), {"device_id", "device", "kind", "metric", "group",
                                       "range", "window_s", "channel"})
        self.assertTrue(params["device"].get("deprecated"))
        self.assertEqual(params["range"]["enum"], list(ops_mod.HISTORY_RANGES))
        self.assertEqual(params["group"]["enum"], list(ops_mod.HISTORY_GROUPS))
        self.assertEqual(params["window_s"]["type"], "integer")
        summary = {
            p["name"] for p in doc["paths"]["/api/v1/devices/summary"]["get"]["parameters"]
        }
        self.assertEqual(summary, {"range", "device_id", "kwh_rub"})


def _dict_keys(path, name):
    """Keys of the module-level dict literal `name` (annotated or plain)."""
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target == name and isinstance(value, ast.Dict):
            return tuple(k.value for k in value.keys)
    return ()


class DevicesEnumParity(unittest.TestCase):
    """The op's two enums are copies of the daemon's homes (separate trees and
    deploys, no cross-tree import): equal, in order, and never empty."""

    def test_ranges_equal_the_daemon(self):
        home = _dict_keys(os.path.join(DEVICES_PKG, "history_ranges.py"), "RANGES")
        self.assertTrue(home, "RANGES not found in history_ranges.py")
        self.assertEqual(ops_mod.HISTORY_RANGES, home)

    def test_groups_equal_the_daemon(self):
        home = _dict_keys(os.path.join(DEVICES_PKG, "history_metrics.py"), "HISTORY_GROUPS")
        self.assertTrue(home, "HISTORY_GROUPS not found in history_metrics.py")
        self.assertEqual(ops_mod.HISTORY_GROUPS, home)


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "AF_UNIX listener needs a POSIX host")
class DevicesSocket(_Base):
    """A fake devices daemon on a real AF_UNIX socket applies the daemon's rule
    (a live panel session in the Cookie) and records what it was asked."""

    TOKEN = "cd" * 16

    def setUp(self):
        super().setUp()
        import socketserver
        self.seen = []
        self.big = [None]  # a body the fake daemon sends instead of the small one
        session_dir = self.app.session.session_dir
        seen = self.seen
        big = self.big

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                cookie = self.headers.get("Cookie")
                live = check_session_store(cookie, session_dir)
                seen.append({"path": self.path, "cookie": cookie, "live": live})
                if live:
                    raw = big[0] or b'{"ok":true,"metrics":[]}'
                    code = 200
                else:
                    raw = b'{"ok":false,"error":"unauthorized"}'
                    code = 401
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.sock_dir = tempfile.mkdtemp()
        self.sock_path = os.path.join(self.sock_dir, "api.sock")
        self.server = Server(self.sock_path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        os.environ["SA02M_DEVICES_SOCK"] = self.sock_path
        # The service session the agent API minted (tests skip the bash mint).
        self.app.session.token = self.TOKEN
        self.app.session.csrf = "csrf-test"
        self.app.session.runner = object()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("SA02M_DEVICES_SOCK", None)
        try:
            os.unlink(self.sock_path)
            os.rmdir(self.sock_dir)
        except OSError:
            pass
        super().tearDown()

    def _session(self, expiry):
        digest = hashlib.sha256(self.TOKEN.encode("ascii")).hexdigest()
        with open(os.path.join(self.app.session.session_dir, digest), "w", encoding="utf-8") as fh:
            fh.write("%d agent-api\n" % expiry)

    def test_live_service_session_passes_the_daemon_check(self):
        self._session(int(time.time()) + 3600)
        status, body = self.get("/api/v1/devices/history?group=energy&range=7d")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(len(self.seen), 1)
        self.assertTrue(self.seen[0]["live"])
        self.assertIn("session_token=" + self.TOKEN, self.seen[0]["cookie"] or "")
        bare, query = _split(self.seen[0]["path"])
        self.assertEqual(bare, "/api/devices/history")
        self.assertEqual(query, {"group": "energy", "range": "7d"})
        status, body = self.get("/api/v1/devices/summary?range=month")
        self.assertEqual(status, 200, body)
        self.assertTrue(self.seen[1]["live"])
        self.assertEqual(_split(self.seen[1]["path"])[0], "/api/devices/history/summary")

    def test_body_over_the_read_limit_is_refused_not_cut(self):
        self._session(int(time.time()) + 3600)
        orig = svc.UNIX_READ_LIMIT
        svc.UNIX_READ_LIMIT = 1000
        try:
            self.big[0] = json.dumps({"ok": True, "pad": "x" * 5000}).encode()
            status, body = self.get("/api/v1/devices/history?group=energy&range=7d")
            self.assertEqual(status, 502, body)
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"], "response_too_large")
            self.assertEqual(body["limit_bytes"], 1000)
            self.assertNotIn("body", body)
            # Exactly at the limit is still the whole, parseable body.
            exact = b'{"ok":true,"pad":"' + b"y" * (1000 - 20) + b'"}'
            self.assertEqual(len(exact), 1000)
            self.big[0] = exact
            status, body = self.get("/api/v1/devices/history?group=energy&range=7d")
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body["body"])["pad"], "y" * 980)
        finally:
            svc.UNIX_READ_LIMIT = orig

    def test_dead_session_is_a_401_not_a_success(self):
        self._session(int(time.time()) - 10)
        status, body = self.get("/api/v1/devices/history?group=energy")
        self.assertEqual(status, 401)
        self.assertFalse(body["ok"])
        self.assertFalse(self.seen[0]["live"])


if __name__ == "__main__":
    unittest.main()
