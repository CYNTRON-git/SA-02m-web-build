"""A conf that EXISTS but cannot be read (EACCES — e.g. the daemon's read ACL
was lost; docs/contracts/homekit-bridge.md §13) is not «disabled».
configparser.read() skips a file it cannot open without a word, so the bridge
used to read defaults (enabled = false) and stop as if the operator had
switched it off. Now: `missing_deps` + reason `conf_unreadable`, exit 0 (no
restart loop), at start and when it happens while running. An ABSENT conf
still means disabled.

The unreadable file is simulated by making open() of exactly that path raise
PermissionError: the suite runs as root, which reads a 0000 file anyway, and
configparser resolves open() from builtins at call time, so the pre-fix code
path meets the same error a real EACCES gives it."""

from __future__ import annotations

import builtins
import contextlib
import os
import unittest
from unittest import mock

from sa02m_homekit import config
from sa02m_homekit import constants as C
from sa02m_homekit import main

from ._harness import Harness


@contextlib.contextmanager
def unreadable(path):
    real_open = builtins.open

    def guarded(file, *a, **kw):
        if isinstance(file, (str, bytes, os.PathLike)) and os.fspath(file) == path:
            raise PermissionError(13, "Permission denied", path)
        return real_open(file, *a, **kw)

    with mock.patch("builtins.open", guarded):
        yield


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_an_unreadable_conf_is_flagged_not_disabled(self):
        self.h.conf(enabled=True)
        with unreadable(self.h.conf_path):
            conf = config.load(self.h.conf_path)
        self.assertTrue(getattr(conf, "unreadable", False))
        self.assertFalse(conf.enabled)
        self.assertTrue(conf.warnings)

    def test_an_absent_conf_is_still_plain_disabled(self):
        conf = config.load(self.h.conf_path + ".absent")
        self.assertFalse(getattr(conf, "unreadable", False))
        self.assertFalse(conf.enabled)
        self.assertEqual(conf.warnings, [])

    def test_a_non_utf8_conf_reads_like_a_malformed_one(self):
        # The Home Connect twin's rule (sa02m_homeconnect/config.py): bytes that
        # are not UTF-8 are a malformed conf — defaults plus a warning, like a
        # configparser error — never an exception that restarts the daemon
        # every RestartSec.
        with open(self.h.conf_path, "wb") as fh:
            fh.write(b"[bridge]\nenabled = true\n# \xff\xfe\n")
        conf = config.load(self.h.conf_path)
        self.assertFalse(conf.enabled)
        self.assertFalse(getattr(conf, "unreadable", False))
        self.assertTrue(any("unreadable conf" in w for w in conf.warnings), conf.warnings)

    def test_a_readable_conf_is_read(self):
        self.h.conf(enabled=True)
        conf = config.load(self.h.conf_path)
        self.assertTrue(conf.enabled)
        self.assertFalse(getattr(conf, "unreadable", False))


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.conf(enabled=True)

    def assert_conf_unreadable(self):
        st = self.h.status_json()
        self.assertEqual(st["state"], C.STATE_MISSING_DEPS, st)
        self.assertEqual(st["reason"], "conf_unreadable", st)

    def test_at_start_it_is_a_standby_with_a_reason(self):
        with unreadable(self.h.conf_path), self.assertLogs("sa02m_homekit", "ERROR"):
            self.h.start()
            self.assertEqual(self.h.join(), main.EXIT_OK)
        self.assert_conf_unreadable()
        self.assertEqual(self.h.runners, [])
        self.assertIsNone(self.h.mqtt)

    def test_while_running_it_stops_with_the_reason_not_as_disabled(self):
        self.h.start()
        self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        with unreadable(self.h.conf_path), self.assertLogs("sa02m_homekit", "ERROR"):
            self.assertEqual(self.h.join(timeout=5.0), main.EXIT_OK)
        self.assert_conf_unreadable()
        self.assertTrue(self.h.runners[0].stopped)


if __name__ == "__main__":
    unittest.main()
