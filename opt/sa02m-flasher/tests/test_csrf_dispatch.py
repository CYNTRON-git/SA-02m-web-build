# -*- coding: utf-8 -*-
"""Behavioural + ordering test for the flasher daemon's CSRF gate in `_dispatch`
(docs/decisions/selective-csrf-policy.md «Демоны»; quality row daemon-csrf-behaviour).

WHY THIS EXISTS
Audit 2026-09-24 (M3): the CSRF token stood on every mutating CGI but on NEITHER
HTTP daemon, while the threat model said «на ВСЕХ». `/flash` and `/ports/release`
— the heaviest actions of the panel — were reachable by a same-site page on
another port of the board with the victim's session. The rule now is ONE line
in `_dispatch`: every POST needs the panel's X-SA02M-CSRF, checked after
`_check_auth()` and before the first route, refused as HTTP 200 + the CGI-shaped
E_CSRF body so app.js's one reaction covers this layer.

WHY IT EXECUTES EXTRACTED SOURCE INSTEAD OF IMPORTING service.py
Same reason as test_health_lease.py: `sa02m_flasher.service` cannot be imported
off-Linux (`import grp`, `import cgi` on 3.13+). The SHIPPED `_dispatch`,
`_check_auth`, `_internal_caller`, `_send_json`, `_send_error`, `_discard_body` are lifted with
`ast` and bound into a stub Handler that records which route handler was reached,
then driven over a real http.server on an ephemeral TCP port with real Cookie /
X-SA02M-CSRF headers. The logic under test is the real logic, byte for byte.

Proven RED on the 1.0.6.56 service.py (no gate): POST /ports/release without a
token reached the handler (cases 1, 2, 4), the ordering pins found no
`check_csrf(` line, and `_internal_caller` did not exist.
"""
from __future__ import annotations

import ast
import hashlib
import http.client
import json
import logging
import re
import tempfile
import textwrap
import threading
import types
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sa02m_flasher.auth import check_internal_token
from sa02m_flasher.websession import check_csrf, check_session_store, csrf_error_body

_PKG = Path(__file__).resolve().parent.parent / "sa02m_flasher"
_SERVICE_SRC = (_PKG / "service.py").read_text(encoding="utf-8")
_LINES = _SERVICE_SRC.splitlines()

_TOKEN = "a" * 64
_CSRF = "c" * 64


def _src_of(node: ast.AST) -> str:
    return textwrap.dedent("\n".join(_LINES[node.lineno - 1 : node.end_lineno]))


def _extract() -> dict:
    """The shipped functions, exec'd in a namespace that supplies their module globals."""
    tree = ast.parse(_SERVICE_SRC)
    top = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    handler = next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler"), None
    )
    if handler is None:
        raise AssertionError("service.py no longer defines class Handler")
    methods = {n.name: n for n in handler.body if isinstance(n, ast.FunctionDef)}
    for want in ("_send_json", "_send_error"):
        if want not in top:
            raise AssertionError(f"service.py no longer defines {want}()")
    if "_dispatch" not in methods or "_check_auth" not in methods:
        raise AssertionError("Handler lost _dispatch()/_check_auth() — the routing under test is gone")
    chunks = [_src_of(top["_send_json"]), _src_of(top["_send_error"])]
    if "_discard_body" in top:  # absent on the pre-gate tree (the RED run)
        chunks.append(_src_of(top["_discard_body"]))
    for name in ("_check_auth", "_dispatch", "_internal_caller"):
        if name in methods:
            chunks.append(_src_of(methods[name]))
    ns: dict = {
        "json": json,
        "re": re,
        "urlparse": urlparse,
        "parse_qs": parse_qs,
        "HTTPStatus": HTTPStatus,
        "BaseHTTPRequestHandler": BaseHTTPRequestHandler,
        "Any": object,
        "check_csrf": check_csrf,
        "check_session_store": check_session_store,
        "check_internal_token": check_internal_token,
        "csrf_error_body": csrf_error_body,
        "log": logging.getLogger("test_csrf_dispatch"),
        "_ROUTE_JOB_EVENTS_RE": re.compile(r"^/jobs/([0-9a-fA-F]+)/events$"),
        "_ROUTE_JOB_ID_RE": re.compile(r"^/jobs/([0-9a-fA-F]+)$"),
        "health_payload": lambda ctx: {"ok": True, "version": "test", "poll_locked": False},
    }
    exec("from __future__ import annotations\n" + "\n\n".join(chunks), ns)  # noqa: S102
    return ns


_NS = _extract()


class _StubHandler(BaseHTTPRequestHandler):
    """The shipped dispatch bound to a handler whose route methods only record."""

    protocol_version = "HTTP/1.1"
    REACHED: list = []

    def log_message(self, *_a) -> None:  # silence
        pass

    def __getattr__(self, name: str):
        if name.startswith("_handle_"):
            def _recorder(ctx, *args, **kwargs):
                type(self).REACHED.append(name)
                return _NS["_send_json"](self, {"ok": True, "reached": name})
            return _recorder
        raise AttributeError(name)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET", self.path)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST", self.path)


_StubHandler._dispatch = _NS["_dispatch"]
_StubHandler._check_auth = _NS["_check_auth"]
if "_internal_caller" in _NS:
    _StubHandler._internal_caller = _NS["_internal_caller"]
else:  # the untouched (pre-gate) tree: keep the behavioural cases runnable; the pin test reports it
    _StubHandler._internal_caller = lambda self: False


def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode("ascii")).hexdigest()


class TestCsrfDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self._tmp.name)
        (self.sdir / _hash(_TOKEN)).write_text("2000000000 admin\n", encoding="utf-8")
        (self.sdir / (_hash(_TOKEN) + ".csrf")).write_text(_CSRF + "\n", encoding="utf-8")
        self.ctx = types.SimpleNamespace(
            cfg=types.SimpleNamespace(session_dir=str(self.sdir), internal_token=""),
            jobs=types.SimpleNamespace(list_jobs=lambda: [], list_active_jobs=lambda: []),
        )
        _StubHandler.REACHED = []
        self.srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
        self.srv.context = self.ctx  # type: ignore[attr-defined]
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.t.start()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self._tmp.cleanup()

    def _req(self, method: str, path: str, *, cookie=True, csrf=None, extra=None, body=None):
        h = {}
        if cookie:
            h["Cookie"] = f"session_token={_TOKEN}"
        if csrf is not None:
            h["X-SA02M-CSRF"] = csrf
        if extra:
            h.update(extra)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
        finally:
            c.close()
        try:
            j = json.loads(raw.decode("utf-8"))
        except ValueError:
            j = None
        return r.status, j

    # ── the gate ──────────────────────────────────────────────────────────────
    def test_1_post_without_token_is_refused_before_the_handler(self) -> None:
        status, j = self._req("POST", "/ports/release", body={"port": "COM5"})
        self.assertEqual(status, 200, "the refusal rides HTTP 200 like the CGI layer (F1)")
        self.assertEqual(j, {"ok": False, "error": "csrf", "error_code": "E_CSRF", "reason": "no_header"})
        self.assertEqual(_StubHandler.REACHED, [], "the route handler ran on a token-less POST")

    def test_2_stale_token_is_mismatch_and_not_reached(self) -> None:
        status, j = self._req("POST", "/flash", csrf="d" * 64, body={})
        self.assertEqual((status, j and j.get("error_code"), j and j.get("reason")), (200, "E_CSRF", "mismatch"))
        self.assertEqual(_StubHandler.REACHED, [])

    def test_3_valid_token_reaches_the_handler(self) -> None:
        status, j = self._req("POST", "/ports/release", csrf=_CSRF, body={"port": "COM5"})
        self.assertEqual(status, 200)
        self.assertEqual(j, {"ok": True, "reached": "_handle_ports_release"})
        self.assertEqual(_StubHandler.REACHED, ["_handle_ports_release"])

    def test_4_missing_token_file_is_no_token_file(self) -> None:
        (self.sdir / (_hash(_TOKEN) + ".csrf")).unlink()
        status, j = self._req("POST", "/cancel", csrf=_CSRF, body={"job_id": "x"})
        self.assertEqual((status, j and j.get("reason")), (200, "no_token_file"))
        self.assertEqual(_StubHandler.REACHED, [])

    def test_5_get_needs_only_the_session(self) -> None:
        status, j = self._req("GET", "/status")
        self.assertEqual((status, j), (200, {"ok": True, "reached": "_handle_status"}))
        status, j = self._req("GET", "/ports?quick=1")
        self.assertEqual(status, 200)
        self.assertEqual(_StubHandler.REACHED, ["_handle_status", "_handle_ports"])

    def test_6_auth_precedes_csrf_and_health_precedes_both(self) -> None:
        status, j = self._req("POST", "/ports/release", cookie=False, csrf=_CSRF, body={})
        self.assertEqual((status, j), (401, {"error": "unauthorized"}), "no session → 401, not E_CSRF")
        status, j = self._req("GET", "/health", cookie=False)
        self.assertEqual((status, j and j.get("ok")), (200, True))
        self.assertEqual(_StubHandler.REACHED, [])

    def test_7_every_post_route_is_behind_the_gate(self) -> None:
        """Enumerate the shipped POST routes from the source and drive each one
        token-less: none may reach its handler. A route added below the gate is
        covered by construction; this proves no route was added ABOVE it."""
        routes = re.findall(r'if method == "POST" and p == "([^"]+)":', _SERVICE_SRC)
        self.assertGreaterEqual(len(routes), 15, "the POST route sweep stopped seeing service.py")
        for p in routes:
            _StubHandler.REACHED = []
            status, j = self._req("POST", p, body={})
            self.assertEqual((p, status, j and j.get("error_code")), (p, 200, "E_CSRF"))
            self.assertEqual(_StubHandler.REACHED, [], f"{p} reached its handler without a token")

    def test_8_local_internal_token_caller_is_not_a_browser(self) -> None:
        """X-SA02M-Auth is blanked by nginx on both flasher locations (gate
        flasher-auth-header-strip): a non-empty value can only come from a local
        caller on the unix socket, which has no panel session and no CSRF token by
        construction. INTERNAL_TOKEN stays as documented — unchanged, inert by
        default — so that caller is exempt from the CSRF gate…"""
        self.ctx.cfg.internal_token = "s3cret"
        status, j = self._req("POST", "/cancel", cookie=False, extra={"X-SA02M-Auth": "s3cret"}, body={})
        self.assertEqual((status, j), (200, {"ok": True, "reached": "_handle_cancel"}))
        # …while a SESSION-authenticated POST is never exempt, whatever the config.
        _StubHandler.REACHED = []
        status, j = self._req("POST", "/cancel", body={})
        self.assertEqual((status, j and j.get("error_code")), (200, "E_CSRF"))
        self.assertEqual(_StubHandler.REACHED, [])
        # …and a WRONG internal token falls back to the session path (401 without a cookie).
        status, j = self._req("POST", "/cancel", cookie=False, extra={"X-SA02M-Auth": "nope"}, body={})
        self.assertEqual(status, 401)


class TestDispatchOrderingPins(unittest.TestCase):
    """Source-level pins — the behavioural cases cannot see a reorder that keeps
    one route above the gate for a route they do not name."""

    def _line_of(self, needle: str) -> int:
        for i, line in enumerate(_LINES, 1):
            if needle in line:
                return i
        raise AssertionError(f"service.py no longer contains {needle!r}")

    def test_internal_caller_helper_exists(self) -> None:
        self.assertTrue("_internal_caller" in _NS, "Handler._internal_caller() is missing — the local-seam exemption is undefined")

    def test_csrf_call_sits_between_auth_and_the_first_post_route(self) -> None:
        auth = self._line_of("if not self._check_auth()")
        csrf = self._line_of("check_csrf(")
        first_post = min(i for i, l in enumerate(_LINES, 1) if 'if method == "POST" and p ==' in l)
        self.assertLess(auth, csrf, "the CSRF check runs before the session check")
        self.assertLess(csrf, first_post, "a POST route is dispatched above the CSRF check")

    def test_health_still_precedes_the_gate(self) -> None:
        self.assertLess(self._line_of('p == "/health"'), self._line_of("check_csrf("))

    def test_refusal_is_the_shared_body(self) -> None:
        idx = self._line_of("check_csrf(")
        following = "\n".join(_LINES[idx : idx + 4])
        self.assertIn("csrf_error_body(", following, "the refusal body drifted from the CGI-shaped one home")


if __name__ == "__main__":
    unittest.main()
