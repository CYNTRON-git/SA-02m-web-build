"""Unit tests for the mTLS connect handshake headers + FW-version helper.

The alice-client must report its firmware version and hardware variant so the
cloud dashboard footer shows real firmware instead of «—». The seam-contract
header names X-FW-Version / X-HW-Variant are fixed with the gateway and each is
sent only when non-empty (backward-compat).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client.sio_connection import AliceSocketIO  # noqa: E402
from sa02m_alice.common.fw_version import get_fw_version  # noqa: E402


class TestHandshakeHeaders(unittest.TestCase):
    def test_fw_and_hw_sent_when_non_empty(self):
        sio = AliceSocketIO(
            controller_sn="SN123",
            client_version="1.0.0",
            fw_version="1.0.5.73",
            hw_variant="sa02m-1eth",
        )
        headers = sio._build_headers()
        self.assertEqual(headers["X-FW-Version"], "1.0.5.73")
        self.assertEqual(headers["X-HW-Variant"], "sa02m-1eth")
        # Existing headers preserved.
        self.assertEqual(headers["X-Controller-SN"], "SN123")
        self.assertEqual(headers["X-Client-Version"], "1.0.0")

    def test_fw_and_hw_omitted_when_empty(self):
        sio = AliceSocketIO(
            controller_sn="SN123",
            client_version="1.0.0",
            fw_version="",
            hw_variant="",
        )
        headers = sio._build_headers()
        self.assertNotIn("X-FW-Version", headers)
        self.assertNotIn("X-HW-Variant", headers)
        # The pre-existing headers still go out.
        self.assertIn("X-Controller-SN", headers)
        self.assertIn("X-Client-Version", headers)

    def test_defaults_omit_fw_and_hw(self):
        # No fw/hw supplied → omitted (a value-less client still connects).
        sio = AliceSocketIO()
        headers = sio._build_headers()
        self.assertNotIn("X-FW-Version", headers)
        self.assertNotIn("X-HW-Variant", headers)

    def test_only_fw_present(self):
        sio = AliceSocketIO(fw_version="1.2.3", hw_variant="")
        headers = sio._build_headers()
        self.assertEqual(headers["X-FW-Version"], "1.2.3")
        self.assertNotIn("X-HW-Variant", headers)


class TestGetFwVersion(unittest.TestCase):
    def _write(self, text: str) -> str:
        fd, path = tempfile.mkstemp()
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return path

    def test_first_non_comment_line(self):
        path = self._write("# comment\n\n1.0.5.73\n1.0.5.74\n")
        self.assertEqual(get_fw_version(path), "1.0.5.73")

    def test_strips_whitespace(self):
        path = self._write("  1.0.5.73  \n")
        self.assertEqual(get_fw_version(path), "1.0.5.73")

    def test_missing_file_returns_unknown(self):
        self.assertEqual(
            get_fw_version("/nonexistent/sa02m/VERSION"), "unknown"
        )

    def test_comment_only_returns_unknown(self):
        path = self._write("# only a comment\n\n")
        self.assertEqual(get_fw_version(path), "unknown")


if __name__ == "__main__":
    unittest.main()
