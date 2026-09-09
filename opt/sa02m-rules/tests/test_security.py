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


def _run(code, path, pubs=None, state=None, budget=None, mechanism=None, doc=None,
         library=""):
    doc = doc if doc is not None else {"notify_queue": [], "runs": []}
    kw = {}
    if budget is not None:
        kw["budget_s"] = budget
    if mechanism is not None:
        kw["deadline_mechanism"] = mechanism
    return code_runner.run_code(
        {"id": "c1", "name": "c", "code": code}, library, state or {},
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

    # Review 1.0.6.39 F2: the `try` OUTSIDE the loop is the shape that swallows
    # the first raise and then spins again — only a mechanism that keeps
    # raising (REARMING_MECHANISMS) stops it; the loop-internal shape passes
    # on any mechanism because the raise lands outside the `try`.
    OUTER_TRY = ("try:\n"
                 "    while True:\n"
                 "        pass\n"
                 "except:\n"
                 "    pass\n"
                 "while True:\n"
                 "    pass\n")

    def test_bare_except_cannot_ride_out_the_deadline(self):
        inner = ("while True:\n"
                 "    try:\n"
                 "        pass\n"
                 "    except:\n"
                 "        pass\n")
        available = code_runner.deadline_mechanisms()
        for mech in available:
            with self.subTest(mechanism=mech, shape="inner"):
                t0 = time.monotonic()
                rec = _run(inner, self.path, budget=0.3, mechanism=mech)
                self.assertLess(time.monotonic() - t0, 5.0)
                self.assertEqual(rec["error"], "timeout")
        rearming = [m for m in code_runner.REARMING_MECHANISMS if m in available]
        self.assertTrue(rearming, "no re-arming mechanism on this host: %r" % (available,))
        for mech in rearming:
            with self.subTest(mechanism=mech, shape="outer"):
                t0 = time.monotonic()
                rec = _run(self.OUTER_TRY, self.path, budget=0.3, mechanism=mech)
                self.assertLess(time.monotonic() - t0, 5.0)
                self.assertEqual(rec["error"], "timeout")

    def test_swallowed_timeout_is_still_journaled(self):
        """Plan row A1: expiry journals last_error even when the body caught
        the raise and finished on its own (before the fix: ok=True, error='')."""
        code = ("try:\n"
                "    while True:\n"
                "        pass\n"
                "except:\n"
                "    pass\n"
                "x = 1\n")
        for mech in code_runner.deadline_mechanisms():
            with self.subTest(mechanism=mech):
                rec = _run(code, self.path, budget=0.3, mechanism=mech)
                self.assertFalse(rec["ok"])
                self.assertEqual(rec["error"], "timeout")

    def test_settrace_fallback_fires_once(self):
        """The measured boundary of the last-resort mechanism: CPython unsets
        a sys.settrace hook that raises, so after a swallowed ScenarioTimeout
        the body runs on (the bounded second loop completes and publishes),
        and the run is still journaled `timeout`. If this ever fails because
        the publish did NOT happen, CPython re-arms now: move `trace` into
        REARMING_MECHANISMS and rewrite the claim homes it names."""
        self.assertNotIn("trace", code_runner.REARMING_MECHANISMS)
        code = ("n = 0\n"
                "try:\n"
                "    while True:\n"
                "        pass\n"
                "except:\n"
                "    pass\n"
                "for i in range(1000):\n"
                "    n = n + 1\n"
                "Hub.set('d', 'on_off', n)\n")
        pubs = []
        rec = _run(code, self.path, pubs=pubs, budget=0.3, mechanism="trace")
        self.assertEqual(pubs, [("d", "on_off", 1000)])
        self.assertEqual(rec["error"], "timeout")

    @unittest.skipUnless(hasattr(sys, "monitoring"),
                         "CPython < 3.12: the non-main-thread fallback is the one-shot "
                         "settrace clock — the outer-try bound is NOT verified here")
    def test_non_main_thread_caller_is_bounded_by_the_fallback(self):
        """service.py runs on the main thread (itimer); any other caller gets
        the auto-selected fallback, which must be a re-arming one."""
        out = {}

        def body():
            out["mechs"] = code_runner.deadline_mechanisms()
            out["rec"] = _run(self.OUTER_TRY, self.path, budget=0.3)

        th = threading.Thread(target=body, daemon=True)
        t0 = time.monotonic()
        th.start()
        th.join(5.0)
        self.assertFalse(th.is_alive(), "the fallback did not stop the outer-try body")
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertNotIn("itimer", out["mechs"])
        self.assertEqual(out["mechs"][0], "monitor")
        self.assertEqual(out["rec"]["error"], "timeout")

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

    @unittest.skipUnless(hasattr(sys, "monitoring"), "sys.monitoring is CPython >= 3.12")
    def test_monitor_tool_is_released_after_the_run(self):
        mon = sys.monitoring
        self.assertIsNone(mon.get_tool(code_runner._MONITOR_TOOL_ID))
        for code in ("x = 1\n", "while True:\n    pass\n"):
            _run(code, self.path, budget=0.3, mechanism="monitor")
            self.assertIsNone(mon.get_tool(code_runner._MONITOR_TOOL_ID))
        # A taken slot drops `monitor` from the auto list instead of colliding.
        mon.use_tool_id(code_runner._MONITOR_TOOL_ID, "someone else")
        try:
            self.assertNotIn("monitor", code_runner.deadline_mechanisms())
        finally:
            mon.free_tool_id(code_runner._MONITOR_TOOL_ID)

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


class SandboxNamespaceTests(unittest.TestCase):
    """S5: the body and the shared `library` run in ONE namespace.

    With separate globals/locals a `def` in the library saw neither the env
    helpers nor its sibling functions — a function's global scope is the
    globals mapping, and the helpers were only in locals, so calling it
    raised `name 'Notify' is not defined` (reproduced by the cloud session).
    The bans do not come from the split: they come from the AST walk and
    from `__builtins__` being empty, both unchanged."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")

    def test_a_library_function_reaches_the_env_helpers(self):
        doc = {"notify_queue": [], "runs": []}
        rec = _run("warn('too hot')\n", self.path, doc=doc,
                   library="def warn(msg):\n    Notify.text(msg)\n")
        self.assertTrue(rec["ok"], rec["error"])
        self.assertEqual(doc["notify_queue"][0]["text"], "too hot")

    def test_a_library_function_reaches_another_library_function(self):
        pubs = []
        rec = _run("Hub.set('lamp', 'on_off', level())\n", self.path, pubs=pubs,
                   library=("def base():\n    return 7\n"
                            "def level():\n    return base() + 1\n"))
        self.assertTrue(rec["ok"], rec["error"])
        self.assertEqual(pubs, [("lamp", "on_off", 8)])

    def test_a_library_function_still_has_no_real_builtins(self):
        """The merged namespace must not inherit CPython's builtins: a name
        the env does not define stays undefined inside a library function."""
        rec = _run("leak()\n", self.path,
                   library="def leak():\n    return print\n")
        self.assertFalse(rec["ok"])
        self.assertIn("print", rec["error"])

    def test_the_ast_bans_still_apply_to_library_code(self):
        for library, expected in (
                ("def peek():\n    return Hub._state\n", "banned attr"),
                ("def peek():\n    return '{0._state}'.format(Hub)\n", "banned attr"),
                ("def peek():\n    _x = 1\n    return _x\n", "banned name"),
                ("def peek():\n    return getattr(Hub, 'set')\n", "banned getattr"),
                ("import os\ndef peek():\n    return os\n", "banned Import")):
            with self.subTest(library=library):
                rec = _run("peek()\n", self.path, library=library)
                self.assertEqual(rec["error"], expected)


class HttpGuardTests(unittest.TestCase):
    """A5: the board is not a request proxy into its own loopback services
    or the metadata address. The LAN is reachable by design since the
    2026-09-09 cloud-and-board decision — the module docstring is the one
    home of the policy and its reason."""

    def test_refuses_loopback_link_local_reserved_and_userinfo(self):
        for url in ("http://127.0.0.1:9999/cgi-bin/x",
                    "http://169.254.169.254/latest/meta-data",
                    "http://user:pass@example.com/",
                    "http://[::1]:1883/",
                    "http://[::ffff:127.0.0.1]/",
                    "http://[fe80::1]/",
                    "http://0.0.0.0/",
                    "http://0.0.0.1/",
                    "http://[::]/",
                    "http://224.0.0.1/",
                    "http://240.0.0.1/",
                    "ftp://example.com/",
                    "http:///path"):
            with self.subTest(url=url):
                self.assertNotEqual(http_guard.check_url(url, resolve=False), "")

    def test_refuses_the_loopback_name_and_its_subdomains(self):
        """A name, not an address: `localhost` never reaches the resolver
        (RFC 6761 reserves the whole `.localhost` tree for loopback)."""
        for url in ("http://localhost:9999/cgi-bin/x",
                    "http://LocalHost/",
                    "http://board.localhost/"):
            with self.subTest(url=url):
                self.assertNotEqual(http_guard.check_url(url, resolve=False), "")

    def test_refuses_the_legacy_inet_aton_spellings_of_loopback(self):
        """`ipaddress.ip_address` rejects these as hostnames while glibc's
        resolver accepts them as 127.0.0.1 — the static half must catch the
        spelling, not hand it to a resolver that would."""
        for url in ("http://2130706433/", "http://0177.0.0.1/",
                    "http://127.1/", "http://0x7f.1/", "http://127.0.1/"):
            with self.subTest(url=url):
                self.assertNotEqual(http_guard.check_url(url, resolve=False), "")

    def test_refuses_an_ipv4_loopback_wrapped_in_ipv6(self):
        """6to4 (`2002::/16`) and Teredo (`2001::/32`) CARRY an IPv4 address.
        Python reports a 6to4 address as `is_private` and NOT `is_loopback`,
        so since the LAN allowance of 1.0.6.41 `http://[2002:7f00:1::]` —
        127.0.0.1 in a 6to4 wrapper — would have been reached (found by the
        cloud session's 55-URL sweep, 2026-09-09). The wrapper is judged on
        the address it carries, so a wrapped LAN target stays allowed."""
        for url in ("http://[2002:7f00:1::]/",              # 6to4 127.0.0.1
                    "http://[2002:7f00:1::]:9999/cgi-bin/",
                    "http://[2001:0:c000:201:0:ffff:80ff:fffe]/",  # Teredo 127.0.0.1
                    "http://[::ffff:127.0.0.1]/"):          # mapped, already held
            with self.subTest(url=url):
                self.assertEqual(
                    http_guard.check_url(url, resolve=False),
                    "refused: loopback address")
        for url in ("http://[2002:a00:5::]/",               # 6to4 10.0.0.5
                    "http://[2001:0:c000:201:0:ffff:f5ff:fffa]/"):  # Teredo 10.0.0.5
            with self.subTest(url=url):
                self.assertEqual(http_guard.check_url(url, resolve=False), "")

    def test_refuses_a_url_past_the_length_cap_before_resolving(self):
        long_url = "https://example.com/?q=" + "a" * http_guard.URL_MAX
        with mock.patch.object(http_guard.socket, "getaddrinfo") as gai:
            self.assertNotEqual(http_guard.check_url(long_url, resolve=True), "")
        self.assertEqual(gai.call_count, 0)

    def test_private_lan_targets_are_allowed(self):
        """Cloud-and-board decision 2026-09-09: a scenario may reach the
        operator's own LAN (a NAS, a panel); the two halves refuse the same
        set, so a scenario the cloud accepts does not die silently here."""
        for url in ("http://192.168.1.136:9999/login.html",
                    "http://10.0.0.5/",
                    "http://172.16.3.4/",
                    "http://[fd00::1]/",
                    "http://100.64.0.1/"):
            with self.subTest(url=url):
                self.assertEqual(http_guard.check_url(url, resolve=False), "")

    def test_public_literal_and_unresolved_hostname_pass_static_check(self):
        self.assertEqual(http_guard.check_url("https://8.8.8.8/", resolve=False), "")
        self.assertEqual(http_guard.check_url("https://example.com/", resolve=False), "")

    def test_resolution_catches_a_hostname_that_points_inside(self):
        """The board's own fence, which the cloud half does not have: the
        name is resolved and the ANSWER is judged, so a public-looking name
        aimed at loopback is refused — a private answer now passes."""
        with mock.patch.object(http_guard.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]):
            self.assertNotEqual(http_guard.check_url("http://evil.example/", resolve=True), "")
        with mock.patch.object(http_guard.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("169.254.169.254", 80))]):
            self.assertNotEqual(http_guard.check_url("http://meta.example/", resolve=True), "")
        with mock.patch.object(http_guard.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("93.184.216.34", 80))]):
            self.assertEqual(http_guard.check_url("http://example.com/", resolve=True), "")
        with mock.patch.object(http_guard.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("192.168.1.136", 80))]):
            self.assertEqual(http_guard.check_url("http://nas.lan/", resolve=True), "")

    def test_allow_list_admits_a_listed_loopback_host_only(self):
        """Since the LAN is allowed by default, the allow-list's remaining
        job is the rare deliberate exemption: a loopback/link-local target
        the operator names explicitly."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        allow = os.path.join(td.name, "http-allow.json")
        with open(allow, "w", encoding="utf-8") as fh:
            json.dump({"hosts": ["127.0.0.1"]}, fh)
        with mock.patch.dict(os.environ, {"SA02M_RULES_HTTP_ALLOW": allow}):
            self.assertEqual(http_guard.check_url("http://127.0.0.1:9999/api", resolve=False), "")
            self.assertNotEqual(http_guard.check_url("http://127.0.0.2:9999/api", resolve=False), "")
            self.assertNotEqual(http_guard.check_url("http://localhost:9999/api", resolve=False), "")
            # An allow-listed host does NOT excuse credentials in the URL.
            self.assertNotEqual(
                http_guard.check_url("http://u:p@127.0.0.1/api", resolve=False), "")
        with mock.patch.dict(os.environ, {"SA02M_RULES_HTTP_ALLOW": os.path.join(td.name, "absent")}):
            self.assertEqual(http_guard.allowed_hosts(), set())

    def test_store_drops_an_http_action_aimed_inside(self):
        row, err = store.validate_row({
            "name": "probe",
            "action": [{"kind": "http", "url": "http://127.0.0.1:9999/cgi-bin/internal?secret=1"},
                       {"kind": "http", "url": "http://user:pass@10.0.0.5/"},
                       {"kind": "http", "url": "http://localhost:9999/cgi-bin/x"},
                       {"kind": "http", "url": "http://2130706433/cgi-bin/x"},
                       {"kind": "http", "url": "http://192.168.1.136:9999/login.html"},
                       {"kind": "http", "url": "https://example.com/hook"}]})
        self.assertEqual(err, "")
        self.assertEqual([a["url"] for a in row["action"]],
                         ["http://192.168.1.136:9999/login.html",
                          "https://example.com/hook"])

    def test_every_redirect_hop_is_re_checked(self):
        """The hop-by-hop half of the board's fence: an allowed first target
        cannot bounce the request onto a refused one."""
        def to_metadata(h):
            h.send_response(302)
            h.send_header("Location", "http://169.254.169.254/latest/meta-data")
            h.send_header("Content-Length", "0")
            h.end_headers()
        srv = _Server(to_metadata)
        self.addCleanup(srv.close)
        with mock.patch.object(http_guard, "allowed_hosts", return_value={"127.0.0.1"}):
            with self.assertRaises(http_guard.HttpRefused) as cm:
                http_guard.fetch("GET", srv.url("/start"))
        self.assertIn("refused", str(cm.exception))
        self.assertEqual(len(srv.seen), 1)

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


class HttpRunCapTests(unittest.TestCase):
    """S1: a run cannot issue an unbounded number of outbound requests —
    the `http` action and the sandbox `Http` share ONE per-run budget
    (store.HTTP_PER_RUN_MAX), so a cloud-pushed scenario cannot turn a
    board into a scanner or an amplifier by looping over targets."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        self.srv = _Server(_ok_200)
        self.addCleanup(self.srv.close)

    def test_sandbox_http_is_capped_per_run(self):
        n = store.HTTP_PER_RUN_MAX + 3
        code = "\n".join("Http.get('%s')" % self.srv.url("/p%d" % i)
                         for i in range(n))
        with mock.patch.object(http_guard, "allowed_hosts",
                               return_value={"127.0.0.1"}):
            rec = _run(code, self.path)
        self.assertFalse(rec["ok"])
        self.assertIn("http cap", rec["error"])
        self.assertEqual(len(self.srv.seen), store.HTTP_PER_RUN_MAX)

    def test_engine_http_action_is_capped_per_run(self):
        acts = [{"kind": "http", "url": self.srv.url("/a%d" % i), "method": "GET"}
                for i in range(store.HTTP_PER_RUN_MAX + 2)]
        store.save({"scenarios": [{"id": "s1", "name": "s", "enabled": True,
                                   "type": "block", "trigger": [], "condition": {},
                                   "action": acts}],
                    "runs": [], "notify_queue": [], "library": "", "vars": {}},
                   self.path)
        e = engine.Engine(lambda *_a: None, self.path)
        with mock.patch.object(http_guard, "allowed_hosts",
                               return_value={"127.0.0.1"}):
            rec = e.run_now("s1")
        self.assertIn("http cap", rec["error"])
        self.assertEqual(len(self.srv.seen), store.HTTP_PER_RUN_MAX)
        self.assertIn("http cap", e.doc["scenarios"][0]["last_error"])

    def test_a_refused_target_spends_the_budget_too(self):
        """The cap counts CALLS, not successes — else a run could probe the
        LAN by looping over targets the policy refuses."""
        code = ("for i in range(%d):\n"
                "    try:\n"
                "        Http.get('http://127.0.0.1/x')\n"
                "    except:\n"
                "        pass\n"
                "Http.get('%s')\n"
                % (store.HTTP_PER_RUN_MAX, self.srv.url("/after")))
        with mock.patch.object(http_guard, "allowed_hosts", return_value=set()):
            rec = _run(code, self.path)
        self.assertEqual(rec["error"], "http cap")
        self.assertEqual(self.srv.seen, [])

    def test_the_budget_is_per_run_not_per_process(self):
        """Two runs of the same scenario each get the full budget."""
        code = "\n".join("Http.get('%s')" % self.srv.url("/q%d" % i)
                         for i in range(store.HTTP_PER_RUN_MAX))
        with mock.patch.object(http_guard, "allowed_hosts",
                               return_value={"127.0.0.1"}):
            first = _run(code, self.path)
            second = _run(code, self.path)
        self.assertTrue(first["ok"], first["error"])
        self.assertTrue(second["ok"], second["error"])
        self.assertEqual(len(self.srv.seen), 2 * store.HTTP_PER_RUN_MAX)


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


class ServicePubFenceTests(unittest.TestCase):
    """S2: `RulesApp.pub` builds the MQTT topic — it is the LAST line before
    the wire and the narrowing point BOTH the `code` and the `block` path
    reach (Engine._write / Hub.set check upstream; this is the fence at the
    wall). A `device`/`cap` failing ID_RE / CAP_RE is refused here whatever
    let it through, so no caller can publish a wildcard or climb out of the
    device subtree."""

    class _Client:
        def __init__(self):
            self.sent = []

        def publish(self, topic, payload, qos=1, retain=False):
            self.sent.append((topic, payload, retain))

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        store.save(store.empty_doc(), self.path)
        self.client = self._Client()
        self.app = rules_service.RulesApp(self.client, self.path)
        self.client.sent = []

    def test_pub_refuses_names_that_escape_the_topic_segment(self):
        bad = (("x/controls/y/on/#", "on_off"),   # wildcard device
               ("lamp", "on_off/#"),              # wildcard cap
               ("../../sa02m-relay", "on_off"),   # traversal out of the subtree
               ("lamp", "a b"),                   # whitespace in a topic segment
               ("lamp", "a+b"),                   # single-level wildcard
               ("x" * 65, "on_off"))              # past ID_RE's length
        for device, cap in bad:
            with self.subTest(device=device, cap=cap):
                self.app.pub(device, cap, 1)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.app.state, {})

    def test_pub_still_publishes_a_valid_pair(self):
        self.app.pub("lamp", "on_off", 1)
        self.assertEqual(self.client.sent,
                         [("/devices/lamp/controls/on_off/on", "1", False)])
        self.assertEqual(self.app.state["lamp"]["on_off"], 1)

    def test_pub_keeps_the_alice_long_cap_form(self):
        """`cap_short` runs first: the long Alice form is validated in its
        short shape, not refused for carrying dots the segment never sees."""
        self.app.pub("lamp", "devices.capabilities.on_off", 0)
        self.assertEqual(self.client.sent,
                         [("/devices/lamp/controls/on_off/on", "0", False)])


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
                       "ReadWritePaths=/etc/sa02m-rules", "MemoryMax=32M",
                       # S4: the deadline cannot interrupt a single long C
                       # call, so the resource ceiling is what bounds the
                       # damage of one — CPU share and process/thread count
                       # beside the memory ceiling that already shipped.
                       "CPUQuota=50%", "TasksMax=32"):
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
