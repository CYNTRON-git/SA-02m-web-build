"""Security floors of the scenario engine (audit 1.0.6.39, verdict A):
sandbox execution bound and escapes, outbound HTTP target policy, `cap`
charset, publish-failure containment, unit hardening parity."""
from __future__ import annotations

import http.server
import json
import os
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import code_runner, engine, http_guard, store  # noqa: E402
from sa02m_rules import service as rules_service  # noqa: E402

REPO = os.path.abspath(os.path.join(ROOT, "..", ".."))
UNIT = os.path.join(REPO, "etc", "systemd", "system", "sa02m-rules.service")


def _run(code, path, pubs=None, state=None, budget=None, mechanism=None, doc=None):
    doc = doc if doc is not None else {"notify_queue": [], "runs": []}
    kw = {}
    if budget is not None:
        kw["budget_s"] = budget
    if mechanism is not None:
        kw["deadline_mechanism"] = mechanism
    return code_runner.run_code(
        {"id": "c1", "name": "c", "code": code}, "", state or {},
        lambda d, c, v: (pubs if pubs is not None else []).append((d, c, v)),
        1.0, 55.0, 37.0, doc, path, {}, **kw)


class _Server:
    """Loopback HTTP server with a scripted handler; records every request."""

    def __init__(self, handler_fn):
        seen = self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _any(self):
                seen.append((self.command, self.path))
                # Consume the body first: answering before the client has
                # finished writing it resets the socket on Windows (10053).
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                handler_fn(self)

            do_GET = do_POST = _any

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path="/"):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _ok_200(h, body=b"ok"):
    h.send_response(200)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


class SandboxBoundTests(unittest.TestCase):
    """A1 + A11: a `type=code` body cannot wedge the engine."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")

    def test_infinite_loop_returns_within_budget(self):
        for mech in code_runner.deadline_mechanisms():
            with self.subTest(mechanism=mech):
                t0 = time.monotonic()
                rec = _run("while True:\n    pass\n", self.path, budget=0.3,
                           mechanism=mech)
                self.assertLess(time.monotonic() - t0, 5.0)
                self.assertFalse(rec["ok"])
                self.assertEqual(rec["error"], "timeout")

    def test_bare_except_cannot_ride_out_the_deadline(self):
        code = ("while True:\n"
                "    try:\n"
                "        pass\n"
                "    except:\n"
                "        pass\n")
        for mech in code_runner.deadline_mechanisms():
            with self.subTest(mechanism=mech):
                t0 = time.monotonic()
                rec = _run(code, self.path, budget=0.3, mechanism=mech)
                self.assertLess(time.monotonic() - t0, 5.0)
                self.assertEqual(rec["error"], "timeout")

    def test_engine_runs_code_under_the_run_budget(self):
        """The engine passes RUN_S down; a wedged body journals last_error."""
        pubs = []
        doc = {"scenarios": [{"id": "c1", "name": "c", "enabled": True,
                              "type": "code", "trigger": [], "condition": {},
                              "action": [], "code": "while True:\n    pass\n"}],
               "runs": [], "notify_queue": [], "library": "", "vars": {}}
        store.save(doc, self.path)
        e = engine.Engine(lambda d, c, v: pubs.append((d, c, v)), self.path)
        with mock.patch.object(engine, "RUN_S", 0.3):
            t0 = time.monotonic()
            rec = e.run_now("c1")
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(rec["error"], "timeout")
        self.assertEqual(e.doc["scenarios"][0]["last_error"], "timeout")

    @unittest.skipUnless(hasattr(signal, "setitimer"), "POSIX-only mechanism")
    def test_itimer_restores_the_previous_alarm_handler(self):
        prev = signal.getsignal(signal.SIGALRM)
        _run("x = 1\n", self.path, budget=1.0, mechanism="itimer")
        self.assertIs(signal.getsignal(signal.SIGALRM), prev)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_trace_hook_is_removed_after_the_run(self):
        self.assertIsNone(sys.gettrace())
        _run("x = 1\n", self.path, budget=1.0, mechanism="trace")
        self.assertIsNone(sys.gettrace())

    def test_underscore_names_and_attrs_are_banned(self):
        rec = _run("x = Hub._state\n", self.path)
        self.assertEqual(rec["error"], "banned attr")
        rec = _run("_x = 1\n", self.path)
        self.assertEqual(rec["error"], "banned name")

    def test_open_and_friends_are_banned(self):
        for name in ("open", "getattr", "__import__", "exec", "eval"):
            with self.subTest(name=name):
                rec = _run("%s('x')\n" % name, self.path)
                self.assertIn("banned", rec["error"])

    def test_write_cap_holds(self):
        pubs = []
        code = "\n".join("Hub.set('d%d', 'on_off', 1)" % i for i in range(12))
        rec = _run(code, self.path, pubs=pubs)
        self.assertEqual(rec["error"], "write cap")
        self.assertEqual(len(pubs), store.MAX_WRITES)


class SandboxFormatEscapeTests(unittest.TestCase):
    """A6: str.format attribute access defeated the AST walk."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")

    def test_format_reaches_no_private_state(self):
        doc = {"notify_queue": [], "runs": []}
        state = {"lamp": {"on_off": 1}}
        probes = (
            "Notify.text('{0._state}'.format(Hub))",
            "Notify.text('{0.set.__globals__[__builtins__][__import__]}'.format(Hub))",
            "Notify.text('{0._state}'.format_map({'0': Hub}))",
            "s = '{0._state}'\nNotify.text(s.format(Hub))",
            "Notify.text(str.format('{0._state}', Hub))",
        )
        for code in probes:
            with self.subTest(code=code):
                rec = _run(code, self.path, state=state, doc=doc)
                self.assertFalse(rec["ok"])
                self.assertIn("banned", rec["error"])
        leaked = json.dumps(doc.get("notify_queue") or [])
        self.assertNotIn("lamp", leaked)
        self.assertNotIn("__import__", leaked)

    def test_plain_strings_and_percent_formatting_still_work(self):
        doc = {"notify_queue": [], "runs": []}
        rec = _run("Notify.text('lamp is %s' % Hub.get('lamp'))", self.path,
                   state={"lamp": {"on_off": 1}}, doc=doc)
        self.assertTrue(rec["ok"], rec["error"])
        self.assertEqual(doc["notify_queue"][0]["text"], "lamp is 1")


class HttpGuardTests(unittest.TestCase):
    """A5: the board is not a request proxy into its own LAN."""

    def test_refuses_loopback_private_link_local_and_userinfo(self):
        for url in ("http://127.0.0.1:9999/cgi-bin/x",
                    "http://192.168.1.1/",
                    "http://10.0.0.5/",
                    "http://172.16.3.4/",
                    "http://169.254.169.254/latest/meta-data",
                    "http://user:pass@example.com/",
                    "http://[::1]:1883/",
                    "http://[::ffff:127.0.0.1]/",
                    "http://0.0.0.0/",
                    "ftp://example.com/",
                    "http:///path"):
            with self.subTest(url=url):
                self.assertNotEqual(http_guard.check_url(url, resolve=False), "")

    def test_public_literal_and_unresolved_hostname_pass_static_check(self):
        self.assertEqual(http_guard.check_url("https://8.8.8.8/", resolve=False), "")
        self.assertEqual(http_guard.check_url("https://example.com/", resolve=False), "")

    def test_resolution_catches_a_hostname_that_points_inside(self):
        with mock.patch.object(http_guard.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]):
            self.assertNotEqual(http_guard.check_url("http://evil.example/", resolve=True), "")
        with mock.patch.object(http_guard.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 80))]):
            self.assertEqual(http_guard.check_url("http://example.com/", resolve=True), "")

    def test_allow_list_admits_a_listed_host_only(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        allow = os.path.join(td.name, "http-allow.json")
        with open(allow, "w", encoding="utf-8") as fh:
            json.dump({"hosts": ["192.168.1.50"]}, fh)
        with mock.patch.dict(os.environ, {"SA02M_RULES_HTTP_ALLOW": allow}):
            self.assertEqual(http_guard.check_url("http://192.168.1.50/api", resolve=False), "")
            self.assertNotEqual(http_guard.check_url("http://192.168.1.51/api", resolve=False), "")
        with mock.patch.dict(os.environ, {"SA02M_RULES_HTTP_ALLOW": os.path.join(td.name, "absent")}):
            self.assertEqual(http_guard.allowed_hosts(), set())

    def test_store_drops_an_http_action_aimed_inside(self):
        row, err = store.validate_row({
            "name": "probe",
            "action": [{"kind": "http", "url": "http://127.0.0.1:9999/cgi-bin/internal?secret=1"},
                       {"kind": "http", "url": "http://user:pass@10.0.0.5/"},
                       {"kind": "http", "url": "https://example.com/hook"}]})
        self.assertEqual(err, "")
        self.assertEqual([a["url"] for a in row["action"]], ["https://example.com/hook"])

    def test_sandbox_http_does_not_reach_loopback(self):
        srv = _Server(_ok_200)
        self.addCleanup(srv.close)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        rec = _run("Http.get('%s')" % srv.url("/cgi-bin/internal?secret=1"),
                   os.path.join(td.name, "s.json"))
        self.assertFalse(rec["ok"])
        self.assertIn("refused", rec["error"])
        self.assertEqual(srv.seen, [])

    def test_engine_http_action_refuses_loopback_and_journals(self):
        srv = _Server(_ok_200)
        self.addCleanup(srv.close)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "s.json")
        # Bypass the store (which now drops the row) — a scenarios.json written
        # by an older release may still carry such a row.
        store.save({"scenarios": [{"id": "s1", "name": "s", "enabled": True,
                                   "type": "block", "trigger": [], "condition": {},
                                   "action": [{"kind": "http", "url": srv.url("/x"),
                                               "method": "GET"}]}],
                    "runs": [], "notify_queue": [], "library": "", "vars": {}}, path)
        e = engine.Engine(lambda *_a: None, path)
        rec = e.run_now("s1")
        self.assertIn("refused", rec["error"])
        self.assertEqual(srv.seen, [])

    def test_redirects_are_capped(self):
        def bounce(h):
            h.send_response(302)
            h.send_header("Location", "/again")
            h.send_header("Content-Length", "0")
            h.end_headers()
        srv = _Server(bounce)
        self.addCleanup(srv.close)
        with mock.patch.object(http_guard, "allowed_hosts", return_value={"127.0.0.1"}):
            with self.assertRaises(http_guard.HttpRefused) as cm:
                http_guard.fetch("GET", srv.url("/start"))
        self.assertIn("redirect", str(cm.exception))
        self.assertEqual(len(srv.seen), http_guard.MAX_REDIRECTS + 1)

    def test_response_size_is_capped(self):
        big = b"x" * (http_guard.MAX_RESPONSE_BYTES * 3)
        srv = _Server(lambda h: _ok_200(h, big))
        self.addCleanup(srv.close)
        with mock.patch.object(http_guard, "allowed_hosts", return_value={"127.0.0.1"}):
            status, nbytes = http_guard.fetch("GET", srv.url("/big"))
        self.assertEqual(status, 200)
        self.assertEqual(nbytes, http_guard.MAX_RESPONSE_BYTES)

    def test_allow_listed_host_is_reached_from_the_sandbox(self):
        srv = _Server(_ok_200)
        self.addCleanup(srv.close)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        allow = os.path.join(td.name, "http-allow.json")
        with open(allow, "w", encoding="utf-8") as fh:
            json.dump({"hosts": ["127.0.0.1"]}, fh)
        with mock.patch.dict(os.environ, {"SA02M_RULES_HTTP_ALLOW": allow}):
            rec = _run("Vars.set('r', Http.post('%s', 'hi'))" % srv.url("/hook"),
                       os.path.join(td.name, "s.json"))
        self.assertTrue(rec["ok"], rec["error"])
        self.assertEqual(srv.seen, [("POST", "/hook")])


class CapCharsetTests(unittest.TestCase):
    """A2: `cap` is charset-validated like `device`, and a bad publish never
    escapes the engine."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")

    def test_store_rejects_wildcard_and_traversal_caps(self):
        bad = ("on_off/#", "../../sa02m-relay/controls/relay", "a+b", "x y", "")
        for cap in bad:
            with self.subTest(cap=cap):
                row, err = store.validate_row({
                    "name": "t",
                    "trigger": [{"kind": "state", "device": "lamp", "cap": cap,
                                 "op": "==", "value": 1}],
                    "condition": {"all": [{"kind": "state", "device": "lamp",
                                           "cap": cap, "op": "==", "value": 1}]},
                    "action": [{"kind": "set", "device": "lamp", "cap": cap, "value": 1},
                               {"kind": "toggle", "device": "lamp", "cap": cap},
                               {"kind": "ramp", "device": "lamp", "cap": cap,
                                "to": 5, "seconds": 10}]})
                self.assertEqual(err, "")
                if cap == "":
                    continue  # empty falls back to on_off by contract
                self.assertEqual(row["trigger"], [])
                self.assertEqual(row["condition"], {})
                self.assertEqual(row["action"], [])
        row, _ = store.validate_row({
            "name": "t",
            "action": [{"kind": "set", "device": "lamp", "cap": "di_1_short", "value": 1}]})
        self.assertEqual(row["action"][0]["cap"], "di_1_short")

    def test_sandbox_hub_set_rejects_bad_names(self):
        pubs = []
        rec = _run("Hub.set('lamp', 'on_off/#', 1)", self.path, pubs=pubs)
        self.assertFalse(rec["ok"])
        self.assertEqual(pubs, [])
        rec = _run("Hub.set('../x', 'on_off', 1)", self.path, pubs=pubs)
        self.assertFalse(rec["ok"])
        self.assertEqual(pubs, [])

    def test_engine_write_refuses_bad_names(self):
        e = engine.Engine(lambda *_a: None, self.path)
        self.assertFalse(e._write("lamp", "on_off/#", 1, None))
        self.assertFalse(e._write("../x", "on_off", 1, None))
        self.assertTrue(e._write("lamp", "on_off", 1, None))

    def test_raising_publish_does_not_crash_boot_or_tick(self):
        store.save({"scenarios": [{"id": "s1", "name": "t", "enabled": True,
                                   "type": "block",
                                   "trigger": [{"kind": "boot"}], "condition": {},
                                   "action": [{"kind": "set", "device": "lamp",
                                               "cap": "on_off", "value": 1}]}],
                    "runs": [], "notify_queue": [], "library": "", "vars": {}},
                   self.path)

        class RaisingClient:
            def publish(self, *_a, **_k):
                raise ValueError("Invalid topic")

        app = rules_service.RulesApp(RaisingClient(), self.path)
        app.boot()   # rule_enabled state publish + the boot scenario's write
        app.tick()
        self.assertEqual(len(app.engine.doc["runs"]), 1)


class UnitHardeningTests(unittest.TestCase):
    """A3: the one unit that executes remote-supplied Python carries at least
    its siblings' hardening block (sa02m-cloud-control / sa02m-alice-client)."""

    def test_unit_carries_the_parity_block(self):
        self.assertTrue(os.path.isfile(UNIT), UNIT)
        with open(UNIT, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh
                     if ln.strip() and not ln.lstrip().startswith("#")]
        for needle in ("User=root", "NoNewPrivileges=true", "ProtectHome=true",
                       "PrivateTmp=true", "ProtectSystem=strict",
                       "ReadWritePaths=/etc/sa02m-rules", "MemoryMax=32M"):
            self.assertIn(needle, lines, needle)


class StorePermissionTests(unittest.TestCase):
    """A21: the atomic write keeps the installer's 0644 (mkstemp is 0600)."""

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_saved_store_is_world_readable(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "scenarios.json")
        store.save(store.empty_doc(), path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode) & 0o644, 0o644)


if __name__ == "__main__":
    unittest.main()
