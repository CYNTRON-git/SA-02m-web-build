# -*- coding: utf-8 -*-
"""Unit tests for sa02m_flasher.auth."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from sa02m_flasher.auth import check_internal_token, check_session_store

_TOKEN = "a" * 64  # валидный hex-токен фиксированной длины


def _write_session(session_dir: Path, token: str, expiry: int, user: str = "admin") -> None:
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    (session_dir / digest).write_text(f"{expiry} {user}\n", encoding="utf-8")


class TestCheckSessionStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.session_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _cookie(self, token: str) -> str:
        return f"a=1; session_token={token}; path=/"

    def test_missing_header(self) -> None:
        self.assertFalse(check_session_store(None, str(self.session_dir)))
        self.assertFalse(check_session_store("", str(self.session_dir)))

    def test_valid_unexpired(self) -> None:
        _write_session(self.session_dir, _TOKEN, expiry=2000)
        self.assertTrue(check_session_store(self._cookie(_TOKEN), str(self.session_dir), now=1000))

    def test_expired(self) -> None:
        _write_session(self.session_dir, _TOKEN, expiry=1000)
        self.assertFalse(check_session_store(self._cookie(_TOKEN), str(self.session_dir), now=1000))
        self.assertFalse(check_session_store(self._cookie(_TOKEN), str(self.session_dir), now=1500))

    def test_unknown_token(self) -> None:
        _write_session(self.session_dir, _TOKEN, expiry=2000)
        other = "b" * 64
        self.assertFalse(check_session_store(self._cookie(other), str(self.session_dir), now=1000))

    def test_wrong_cookie_key(self) -> None:
        _write_session(self.session_dir, _TOKEN, expiry=2000)
        self.assertFalse(
            check_session_store(f"other={_TOKEN}", str(self.session_dir), now=1000)
        )

    def test_non_hex_token_rejected(self) -> None:
        # Попытка подстановки пути/мусора вместо hex — отвергается на валидации формата.
        # "cyntron_session" — старый статический токен: не hex → отвергается.
        for bad in ["../etc/passwd", "ZZZ", "a" * 200, "short", "cyntron_session"]:
            self.assertFalse(
                check_session_store(self._cookie(bad), str(self.session_dir), now=1000)
            )

    def test_corrupt_session_file(self) -> None:
        digest = hashlib.sha256(_TOKEN.encode("ascii")).hexdigest()
        (self.session_dir / digest).write_text("garbage\n", encoding="utf-8")
        self.assertFalse(check_session_store(self._cookie(_TOKEN), str(self.session_dir), now=1000))


class TestCheckInternalToken(unittest.TestCase):
    def test_empty_expected_allows_any(self) -> None:
        self.assertTrue(check_internal_token(None, ""))
        self.assertTrue(check_internal_token("anything", ""))

    def test_match(self) -> None:
        self.assertTrue(check_internal_token("secret-token", "secret-token"))

    def test_mismatch(self) -> None:
        self.assertFalse(check_internal_token("secret-token", "other"))
        self.assertFalse(check_internal_token(None, "secret"))

    def test_length_must_match(self) -> None:
        self.assertFalse(check_internal_token("short", "longer-secret"))


if __name__ == "__main__":
    unittest.main()
