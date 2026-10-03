# -*- coding: utf-8 -*-
"""Tokens, the path fence, the router, and the CGI splitter."""

import hashlib
import json
import os
import stat
import tempfile
import time
import unittest

from sa02m_agent_api.cgi_adapter import split_cgi
from sa02m_agent_api.fence import FenceError, resolve, write_text
from sa02m_agent_api.service import App, _nodered_basic
from sa02m_agent_api.tokens import RateLimiter, TokenStore


class Tokens(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SA02M_AGENT_TOKEN_DIRECT"] = "1"
        os.environ["SA02M_AGENT_RUNTIME"] = self.tmp.name
        self.store = TokenStore(os.path.join(self.tmp.name, "tokens.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_and_expiry(self):
        row, raw = self.store.issue("lab", ["config"], 1, False)
        self.assertNotIn(raw, json.dumps(self.store.load()))
        self.assertIn("read", row["scopes"])
        self.assertIn("config", row["scopes"])
        self.assertNotIn("admin", row["scopes"])
        self.assertEqual(self.store.authenticate(raw)["id"], row["id"])
        self.assertIsNone(self.store.authenticate(raw + "aa"))
        self.assertTrue(self.store.revoke(row["id"]))
        self.assertIsNone(self.store.authenticate(raw))

        row, raw = self.store.issue("old", ["read"], 1, False)
        rows = self.store.load()
        for item in rows:
            if item["id"] == row["id"]:
                item["expires"] = int(time.time()) - 10
        self.store.save(rows)
        self.assertIsNone(self.store.authenticate(raw))


class Fence(unittest.TestCase):
    def test_rejects_escape_and_symlink(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "sub"))
        path = write_text("sub/a.txt", "hi", root)
        self.assertTrue(path.endswith("a.txt"))
        self.assertEqual(resolve("sub/a.txt", root), path)
        with self.assertRaises(FenceError):
            resolve("../outside", root)
        with self.assertRaises(FenceError):
            resolve("/etc/passwd", root)
        link = os.path.join(root, "sub", "link")
        try:
            os.symlink(path, link)
        except OSError:
            return
        with self.assertRaises(FenceError):
            resolve("sub/link", root)


class Router(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SA02M_AGENT_TOKEN_DIRECT"] = "1"
        os.environ["SA02M_AGENT_TOKEN_FILE"] = os.path.join(self.tmp.name, "tokens.json")
        os.environ["SA02M_AGENT_RUNTIME"] = self.tmp.name
        os.environ["SA02M_SESSION_DIR"] = os.path.join(self.tmp.name, "sessions")
        os.makedirs(os.environ["SA02M_SESSION_DIR"])
        self.app = App()
        self.app.store.path = os.environ["SA02M_AGENT_TOKEN_FILE"]
        self.app.limiter = RateLimiter(100000)
        self.app.session.session_dir = os.environ["SA02M_SESSION_DIR"]
        self.app.sink = lambda name: {"ok": True}
        _row, self.token = self.app.store.issue("t", ["read"], 1, False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_and_token_skip_csrf(self):
        status, body = self.app.handle("GET", "/health", {}, b"")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body = self.app.handle(
            "POST", "/api/v1/system/info",
            {"X-SA02M-Token": self.token}, b"{}",
        )
        self.assertEqual(status, 200)
        self.assertIn("system.info", self.app.sides)

    def test_token_cannot_mint(self):
        status, body = self.app.handle(
            "POST", "/api/v1/admin/tokens",
            {"X-SA02M-Token": self.token}, b"{}",
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "token_cannot_mint")
        self.assertEqual(self.app.sides, [])

    def test_session_post_needs_csrf(self):
        tok = "ab" * 16
        digest = hashlib.sha256(tok.encode("ascii")).hexdigest()
        sess = os.path.join(os.environ["SA02M_SESSION_DIR"], digest)
        with open(sess, "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time()) + 3600) + " panel\n")
        status, body = self.app.handle(
            "POST", "/api/v1/admin/tokens",
            {"Cookie": "session_token=" + tok}, b"{}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("error_code"), "E_CSRF")
        self.assertEqual(self.app.sides, [])

    def test_get_reads_query_args_and_refuses_mutating(self):
        seen = {}

        def sink(name):
            seen["name"] = name
            return {"ok": True}
        self.app.sink = sink
        status, body = self.app.handle(
            "GET", "/api/v1/rules/get?id=scene7", {"X-SA02M-Token": self.token}, b"")
        self.assertEqual(status, 200, body)
        self.assertEqual(seen.get("name"), "rules.get")
        status, body = self.app.handle(
            "GET", "/api/v1/rules/get", {"X-SA02M-Token": self.token}, b"")
        self.assertEqual(status, 400)
        status, body = self.app.handle(
            "GET", "/api/v1/mqtt/publish?device=a&control=b&value=1",
            {"X-SA02M-Token": self.token}, b"")
        self.assertEqual(status, 405)
        self.assertEqual(self.app.sides, [])

    def test_long_op_returns_job_and_job_is_owner_scoped(self):
        _row, admin = self.app.store.issue("a", ["admin"], 1, False)
        self.app.admin_per_min = 100000
        status, body = self.app.handle(
            "POST", "/api/v1/shell/exec", {"X-SA02M-Token": admin},
            b'{"cmd":"true","mode":"web"}')
        self.assertEqual(status, 200, body)
        jid = body.get("job_id")
        self.assertRegex(jid or "", r"^[0-9a-f]{16}$")
        deadline = time.time() + 5
        job = None
        while time.time() < deadline:
            status, job = self.app.handle(
                "GET", "/api/v1/jobs/" + jid, {"X-SA02M-Token": admin}, b"")
            self.assertEqual(status, 200)
            if job["job"]["status"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(job["job"]["status"], "done", job)
        status, other = self.app.handle(
            "GET", "/api/v1/jobs/" + jid, {"X-SA02M-Token": self.token}, b"")
        self.assertEqual(status, 404)
        status, body = self.app.handle(
            "POST", "/api/v1/shell/exec", {"X-SA02M-Token": admin},
            b'{"cmd":"true","mode":"web","wait":true}')
        self.assertEqual(status, 200)
        self.assertNotIn("job_id", body)

    def test_rules_ops_build_the_store_body(self):
        from sa02m_agent_api import ops as ops_mod
        from sa02m_agent_api.registry import get
        _row, cfg = self.app.store.issue("c", ["config"], 1, False)
        bodies = []
        self.app.sink = None

        class Fake:
            def rules_apply(self, body):
                bodies.append(body)
                return 200, {"ok": True}
        real = ops_mod.Op
        self.assertIsNotNone(real)
        import sa02m_agent_api.service as svc
        orig = svc.Ctx.rules_apply
        svc.Ctx.rules_apply = lambda self, body: Fake().rules_apply(body)
        try:
            for path, raw in (
                ("/api/v1/rules/run", b'{"id":"s1"}'),
                ("/api/v1/rules/delete", b'{"id":"s1"}'),
                ("/api/v1/rules/upsert", b'{"id":"s1","name":"n","confirm":"x"}'),
                ("/api/v1/rules/replace", b'{"scenarios":[]}'),
                ("/api/v1/rules/library", b'{"library":"var a=1;"}'),
            ):
                status, body = self.app.handle("POST", path, {"X-SA02M-Token": cfg}, raw)
                self.assertEqual(status, 200, (path, body))
        finally:
            svc.Ctx.rules_apply = orig
        self.assertEqual(bodies[0], {"run_now": True, "id": "s1"})
        self.assertEqual(bodies[1], {"delete": True, "id": "s1"})
        self.assertEqual(bodies[2], {"upsert": [{"id": "s1", "name": "n"}]})
        self.assertEqual(bodies[3], {"replace": True, "scenarios": []})
        self.assertEqual(bodies[4], {"library": "var a=1;"})
        self.assertIsNotNone(get("rules.upsert"))


class CgiSplit(unittest.TestCase):
    def test_redirect_and_json(self):
        status, body = split_cgi(
            "Status: 302 Found\nLocation: /\n\n", 0,
        )
        self.assertEqual(status, 302)
        self.assertTrue(body["ok"])
        self.assertEqual(body["redirect"], "/")
        status, body = split_cgi('Content-Type: application/json\n\n{"ok":true}\n', 0)
        self.assertEqual(body["ok"], True)


class NodeRedEnv(unittest.TestCase):
    def test_basic_only_from_a_private_file(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "nodered.env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("NODERED_USER=alice\nNODERED_PASS=secret\n")
        os.chmod(path, 0o640)
        os.environ["SA02M_NODERED_ENV"] = path
        if os.lstat(path).st_mode & stat.S_IWOTH:
            self.skipTest("this platform does not store the mode bit")
        self.assertTrue(_nodered_basic().startswith("Basic "))
        os.chmod(path, 0o666)
        self.assertEqual(_nodered_basic(), "")


if __name__ == "__main__":
    unittest.main()
