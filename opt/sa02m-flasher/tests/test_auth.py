# -*- coding: utf-8 -*-
"""Unit tests for sa02m_flasher.auth."""
from __future__ import annotations

import unittest

from sa02m_flasher.auth import check_internal_token, check_session


class TestCheckSession(unittest.TestCase):
    def test_missing_header(self) -> None:
        self.assertFalse(check_session(None, "session_token=cyntron_session"))
        self.assertFalse(check_session("", "session_token=cyntron_session"))

    def test_invalid_expected_format(self) -> None:
        self.assertFalse(check_session("session_token=x", ""))
        self.assertFalse(check_session("session_token=x", "noequals"))

    def test_match_exact_cookie(self) -> None:
        self.assertTrue(
            check_session("session_token=cyntron_session", "session_token=cyntron_session")
        )

    def test_match_among_other_cookies(self) -> None:
        hdr = "a=1; session_token=cyntron_session; path=/"
        self.assertTrue(check_session(hdr, "session_token=cyntron_session"))

    def test_wrong_value(self) -> None:
        self.assertFalse(check_session("session_token=other", "session_token=cyntron_session"))

    def test_wrong_key(self) -> None:
        self.assertFalse(check_session("other=cyntron_session", "session_token=cyntron_session"))


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
