#!/usr/bin/env python3
# comment-mutation-proof-exempt: Executes the real sourced installer wrappers with real GNU timeout and an inert child. Expired/invalid admission, shared action/query budget, kill grace and unchanged unset/rootfs paths are behavior assertions, not source matching.
"""Optional monotonic installer deadline preserves the existing service-policy shell."""

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.marker = self.root / "calls"
        executable = self.root / "systemctl"
        executable.write_text('''#!/bin/bash
printf '%s\n' "$*" >> "$FIXTURE_CALLS"
if [ "${FIXTURE_SLEEP:-}" = yes ]; then
    trap '' TERM
    exec /usr/bin/python3 -c 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'
fi
printf 'native-output\n'
''')
        executable.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.root) + ":/usr/bin:/bin", "FIXTURE_CALLS": str(self.marker),
                    "LOG_FILE": str(self.root / "install.log"), "SA02M_LOG_FILE": str(self.root / "install.log")}

    def run_shell(self, body, **environment):
        return subprocess.run(["bash", "-c", 'source "$1"\n' + body, "fixture", str(ROOT / "scripts/lib.sh")],
                              env={**self.env, **environment}, capture_output=True, text=True, timeout=8)

    def test_expired_invalid_and_empty_deadline_never_dispatch(self):
        for value in ("0", "", "-1", "1.2", "$(touch nope)", "999999999999999999999"):
            with self.subTest(value=value):
                result = self.run_shell("sa02m_systemctl restart fixture", SA02M_SYSTEMCTL_DEADLINE=value)
                self.assertEqual(result.returncode, 124 if value == "0" else 64, result.stderr)
                self.assertFalse(self.marker.exists())

    def test_action_and_query_share_same_absolute_deadline(self):
        body = '''read -r now unused < /proc/uptime
SA02M_SYSTEMCTL_DEADLINE=$((${now%%.*} + 2))
sa02m_systemctl restart fixture
first=$?
_sa02m_svc_query show fixture
second=$?
printf 'statuses:%s:%s\n' "$first" "$second"
'''
        before = time.monotonic()
        result = self.run_shell(body, FIXTURE_SLEEP="yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"statuses:(124|137):124")
        self.assertLess(time.monotonic() - before, 3.5)
        self.assertEqual(self.marker.read_text().splitlines(), ["restart fixture"])

    def test_default_cap_and_query_cap_keep_parent_shell_state(self):
        body = '''read -r now unused < /proc/uptime
SA02M_SYSTEMCTL_DEADLINE=$((${now%%.*} + 100))
_sa02m_systemctl_deadline_seconds 35
timeout() { printf 'timeout:%s\n' "$*"; }
kept=parent
sa02m_systemctl restart fixture
_sa02m_svc_query show fixture
printf 'parent:%s:%s\n' "$kept" "$SA02M_SYSTEMCTL_TIMEOUT_SEC"
'''
        result = self.run_shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("35\n", result.stdout)
        self.assertIn("timeout:-k 1 35 systemctl restart fixture", result.stdout)
        self.assertIn("timeout:-k 1 8 systemctl show fixture", result.stdout)
        self.assertIn("parent:parent:35", result.stdout)

    def test_unset_and_rootfs_paths_preserve_existing_semantics(self):
        result = self.run_shell("unset SA02M_SYSTEMCTL_DEADLINE; sa02m_systemctl status fixture; _sa02m_svc_query show fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("native-output"), 2)
        result = self.run_shell("sa02m_systemctl enable fixture", SA02M_ROOTFS_BUILD="1", SA02M_ROOTFS_ROOT=str(self.root),
                                SA02M_SYSTEMCTL_DEADLINE="invalid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--root=" + str(self.root) + " enable fixture", self.marker.read_text())


if __name__ == "__main__":
    unittest.main()
