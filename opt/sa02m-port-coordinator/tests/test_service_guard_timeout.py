"""The guarded helper re-exec is bounded for start/stop, unbounded for install.

A4-10 (1.0.7.36): a wedged init.d script used to keep the guard -- and the
panel's «port_busy» -- forever. start/stop now ends in a `guard_timeout` reply;
install/uninstall (minutes of package work) keep running under the panel's own
600 s budget. No broker: ServiceMutation is replaced by a stand-in holding a
real descriptor, so subprocess.run's pass_fds path is the real one.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sa02m_ports import service_guard  # noqa: E402


class _Guard:
    def __init__(self, unit):
        self.unit = unit
        self.fd = os.open(os.devnull, os.O_RDONLY)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        os.close(self.fd)
        return False


@unittest.skipUnless(os.path.exists("/bin/sh"), "needs /bin/sh")
class GuardTimeout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.helper = Path(self.tmp.name) / "helper.sh"
        # Ignores its arguments and hangs like a wedged vendor init script.
        self.helper.write_text("#!/bin/sh\nsleep 20\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, action):
        argv = ["guard", "--run", str(self.helper), action, "mplc4", "mplc4.service"]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(service_guard, "ServiceMutation", _Guard), \
                mock.patch.object(service_guard.os, "geteuid", return_value=0), \
                mock.patch.object(service_guard, "START_STOP_TIMEOUT_S", 0.5, create=True), \
                contextlib.redirect_stdout(out):
            started = time.monotonic()
            rc = service_guard.main()
        return rc, out.getvalue(), time.monotonic() - started

    def test_hung_start_ends_with_guard_timeout(self):
        for action in ("start", "stop"):
            with self.subTest(action=action):
                rc, out, elapsed = self.run_main(action)
                self.assertLess(elapsed, 5, "the guard waited on the hung helper")
                reply = json.loads(out)
                self.assertEqual((rc, reply["ok"], reply["error"]), (1, False, "guard_timeout"))

    def test_install_and_uninstall_are_not_timed(self):
        seen = []

        def fake_run(*args, **kwargs):
            seen.append(kwargs.get("timeout"))
            return mock.Mock(returncode=0)

        for action in ("install", "uninstall"):
            with self.subTest(action=action), \
                    mock.patch.object(service_guard.subprocess, "run", side_effect=fake_run):
                rc, _, _ = self.run_main(action)
                self.assertEqual(rc, 0)
        self.assertEqual(seen, [None, None])

    def test_timeout_code_is_distinct_from_busy_and_outage(self):
        exc = service_guard.subprocess.TimeoutExpired(["sh"], 1)
        self.assertEqual(service_guard.error_code(exc), "guard_timeout")
        self.assertNotIn(service_guard.error_code(exc), {"port_busy", "coordinator_unavailable"})


if __name__ == "__main__":
    unittest.main()
