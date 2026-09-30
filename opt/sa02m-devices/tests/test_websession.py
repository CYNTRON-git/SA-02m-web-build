# -*- coding: utf-8 -*-
"""Unit tests for websession.py — the daemons' read-only mirror of lib_web_auth.sh.

The same file (import line aside) sits in both daemon suites, because the module
is a byte-identical copy in both packages (row `websession-parity`); a change to
the rule is proven twice, once per consumer.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from sa02m_devices.websession import (
    CSRF_HEADER,
    CSRF_REASONS,
    DEFAULT_SESSION_DIR,
    check_csrf,
    check_session_store,
    csrf_error_body,
    session_token_from_cookie,
)

_TOKEN = "a" * 64
_CSRF = "c" * 64


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _write_session(session_dir: Path, token: str, expiry: int = 2_000_000_000) -> None:
    (session_dir / _hash(token)).write_text(f"{expiry} admin\n", encoding="utf-8")


def _write_csrf(session_dir: Path, token: str, value: str) -> Path:
    p = session_dir / (_hash(token) + ".csrf")
    p.write_text(value, encoding="utf-8")
    return p


def _cookie(token: str) -> str:
    return f"theme=dark; session_token={token}; sa02m_csrf=whatever"


class TestSessionToken(unittest.TestCase):
    def test_constants_match_the_cgi_layer(self) -> None:
        # lib_web_auth.sh: SA02M_SESSION_DIR default and the header name CGI reads
        # as HTTP_X_SA02M_CSRF; the reason enum is the closed set app.js branches on.
        self.assertEqual(DEFAULT_SESSION_DIR, "/run/sa02m-web-sessions")
        self.assertEqual(CSRF_HEADER, "X-SA02M-CSRF")
        self.assertEqual(CSRF_REASONS, ("no_session", "no_token_file", "no_header", "mismatch"))

    def test_valid_hex_token_extracted(self) -> None:
        self.assertEqual(session_token_from_cookie(_cookie(_TOKEN)), _TOKEN)

    def test_shape_is_enforced(self) -> None:
        for bad in ("", "short", "Z" * 64, "../etc/passwd", "a" * 129, "not-hex-token"):
            self.assertIsNone(session_token_from_cookie(_cookie(bad)), bad)
        self.assertIsNone(session_token_from_cookie(None))
        self.assertIsNone(session_token_from_cookie("other=" + _TOKEN))


class TestCheckSessionStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_live_session(self) -> None:
        _write_session(self.d, _TOKEN, expiry=2000)
        self.assertTrue(check_session_store(_cookie(_TOKEN), str(self.d), now=1000))

    def test_expired_and_unknown_and_missing_dir(self) -> None:
        _write_session(self.d, _TOKEN, expiry=1000)
        self.assertFalse(check_session_store(_cookie(_TOKEN), str(self.d), now=1000))
        self.assertFalse(check_session_store(_cookie("b" * 64), str(self.d), now=500))
        self.assertFalse(check_session_store(_cookie(_TOKEN), str(self.d / "nope"), now=500))

    def test_corrupt_file_fails_closed(self) -> None:
        (self.d / _hash(_TOKEN)).write_text("garbage\n", encoding="utf-8")
        self.assertFalse(check_session_store(_cookie(_TOKEN), str(self.d), now=1000))
        (self.d / _hash(_TOKEN)).write_text("", encoding="utf-8")
        self.assertFalse(check_session_store(_cookie(_TOKEN), str(self.d), now=1000))


class TestCheckCsrf(unittest.TestCase):
    """The five outcomes of web_csrf_validate, plus the CR-stripped and empty files."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        _write_session(self.d, _TOKEN)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_valid(self) -> None:
        _write_csrf(self.d, _TOKEN, _CSRF + "\n")
        self.assertEqual(check_csrf(_cookie(_TOKEN), _CSRF, str(self.d)), (True, ""))

    def test_no_session(self) -> None:
        _write_csrf(self.d, _TOKEN, _CSRF)
        self.assertEqual(check_csrf(None, _CSRF, str(self.d)), (False, "no_session"))
        self.assertEqual(check_csrf(_cookie("nothex"), _CSRF, str(self.d)), (False, "no_session"))

    def test_no_token_file(self) -> None:
        self.assertEqual(check_csrf(_cookie(_TOKEN), _CSRF, str(self.d)), (False, "no_token_file"))

    def test_empty_token_file_is_no_token_file(self) -> None:
        # csrf_token.cgi re-mints with a plain `printf >`: a concurrent reader can
        # see an empty file — fail closed, and the panel's refresh+retry heals it.
        _write_csrf(self.d, _TOKEN, "")
        self.assertEqual(check_csrf(_cookie(_TOKEN), _CSRF, str(self.d)), (False, "no_token_file"))
        _write_csrf(self.d, _TOKEN, "\r\n")
        self.assertEqual(check_csrf(_cookie(_TOKEN), _CSRF, str(self.d)), (False, "no_token_file"))

    def test_no_header(self) -> None:
        _write_csrf(self.d, _TOKEN, _CSRF)
        self.assertEqual(check_csrf(_cookie(_TOKEN), None, str(self.d)), (False, "no_header"))
        self.assertEqual(check_csrf(_cookie(_TOKEN), "", str(self.d)), (False, "no_header"))

    def test_mismatch(self) -> None:
        _write_csrf(self.d, _TOKEN, _CSRF)
        self.assertEqual(check_csrf(_cookie(_TOKEN), "d" * 64, str(self.d)), (False, "mismatch"))
        # a prefix / a duplicated-header splice is a mismatch, never a partial match
        self.assertEqual(check_csrf(_cookie(_TOKEN), _CSRF[:-1], str(self.d)), (False, "mismatch"))
        self.assertEqual(check_csrf(_cookie(_TOKEN), _CSRF + ", " + _CSRF, str(self.d)), (False, "mismatch"))

    def test_cr_stripped_file_still_matches(self) -> None:
        # The bash side strips \r before comparing (a CRLF-edited file must not
        # lock every session out); the mirror must agree.
        _write_csrf(self.d, _TOKEN, _CSRF + "\r\n")
        self.assertEqual(check_csrf(_cookie(_TOKEN), _CSRF, str(self.d)), (True, ""))

    def test_another_sessions_file_is_not_consulted(self) -> None:
        other = "b" * 64
        _write_session(self.d, other)
        _write_csrf(self.d, other, _CSRF)
        self.assertEqual(check_csrf(_cookie(_TOKEN), _CSRF, str(self.d)), (False, "no_token_file"))


class TestCsrfErrorBody(unittest.TestCase):
    def test_body_is_the_cgi_shape(self) -> None:
        self.assertEqual(
            csrf_error_body("no_header"),
            {"ok": False, "error": "csrf", "error_code": "E_CSRF", "reason": "no_header"},
        )

    def test_reason_is_closed_to_the_enum(self) -> None:
        self.assertEqual(csrf_error_body("<script>")["reason"], "unknown")
        self.assertEqual(csrf_error_body("")["reason"], "unknown")


if __name__ == "__main__":
    unittest.main()
