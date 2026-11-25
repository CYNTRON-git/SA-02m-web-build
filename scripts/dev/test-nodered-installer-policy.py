#!/usr/bin/env python3
# comment-mutation-proof-exempt: executes the shipped shell installer in an isolated fixture with only its fixed helper path retargeted; asserts real argument, policy, exit and no-side-effect behavior.
"""Exercise 07 routing separately from the real verifier/transaction suite."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
LIB = r'''
declare -A SA02M_SVC_EN SA02M_SVC_ACT
check_root() { :; }
log() { printf '%s\n' "$*" >> "$FIXTURE_LOG"; }
sa02m_stack_verdict() { printf '%s\n' "$VERDICT"; }
sa02m_svc_capture() { SA02M_SVC_EN[nodered.service]=$ORIGINAL_ENABLED; SA02M_SVC_ACT[nodered.service]=$ORIGINAL_ACTIVE; }
sa02m_svc_apply() { printf 'apply %s\n' "$*" >> "$FIXTURE_LOG"; }
sa02m_stack_policy_set() { printf 'policy %s\n' "$*" >> "$FIXTURE_LOG"; return "${POLICY_EXIT:-0}"; }
'''
HELPER = '''import json,os,pathlib,sys
pathlib.Path(os.environ['FIXTURE_ARGS']).write_text(json.dumps(sys.argv[1:]))
print(os.environ.get('HELPER_RESULT','{"ok":true,"id":"node-red","version":"4.1.13"}'))
if os.environ.get('HELPER_EXIT', '0') != '0':
    raise SystemExit(1)
raise SystemExit(0)
'''


class InstallerPolicyTests(unittest.TestCase):
    def run_case(self, arguments=(), **changes):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            helper = directory / 'vendor-helper.py'
            helper.write_text(HELPER)
            script = (ROOT / 'scripts/07-nodered.sh').read_text()
            pin = 'HELPER=/usr/local/lib/sa02m-vendor-install.py'
            self.assertEqual(script.count(pin), 1, 'production fixed-path seam must be unambiguous')
            (directory / '07-nodered.sh').write_text(script.replace(pin, 'HELPER=' + str(helper)))
            (directory / 'lib.sh').write_text(LIB)
            environment = dict(os.environ, VERDICT='install', ORIGINAL_ENABLED='absent', ORIGINAL_ACTIVE='inactive',
                               FIXTURE_LOG=str(directory / 'calls'), FIXTURE_ARGS=str(directory / 'args'), LOG_FILE=str(directory / 'stderr'))
            for key in ('SA02M_ROOTFS_BUILD', 'SA02M_NODERED_DIR', 'NODERED_USER', 'NODERED_PORT'):
                environment.pop(key, None)
            environment.update(changes)
            result = subprocess.run(['bash', str(directory / '07-nodered.sh'), *arguments], env=environment,
                                    capture_output=True, text=True, timeout=5)
            calls = (directory / 'calls').read_text() if (directory / 'calls').exists() else ''
            args = json.loads((directory / 'args').read_text()) if (directory / 'args').exists() else None
            return result.returncode, args, calls

    def test_refresh_and_disabled_skip_before_helper(self):
        for verdict in ('overlay', 'skip-absent', 'skip-disabled'):
            with self.subTest(verdict=verdict):
                rc, args, calls = self.run_case(VERDICT=verdict)
                self.assertEqual(rc, 0); self.assertIsNone(args)
                self.assertNotIn('apply ', calls); self.assertNotIn('policy ', calls)

    def test_first_install_activates_once_through_helper(self):
        rc, args, calls = self.run_case()
        self.assertEqual(rc, 0)
        self.assertEqual(args[:3], ['install', 'node-red', '--activate'])
        self.assertEqual(calls.count('apply '), 1)
        self.assertIn('policy NODERED present', calls)

    def test_no_start_preserves_then_uses_enabled_policy(self):
        rc, args, calls = self.run_case(['--no-start'])
        self.assertEqual(rc, 0); self.assertIn('--preserve', args)
        self.assertIn('app enabled norestart', calls)

    def test_existing_runtime_does_not_skip_dependency_refresh(self):
        rc, args, calls = self.run_case(ORIGINAL_ENABLED='disabled')
        self.assertEqual(rc, 0); self.assertIn('--preserve', args)
        self.assertNotIn('--activate', args)

    def test_custom_account_port_and_offline_directory(self):
        rc, args, _ = self.run_case(['--nodered-user', 'fixture-user', '--port', '1999'], SA02M_NODERED_DIR='/fixture/payload')
        self.assertEqual(rc, 0)
        for option, value in (('--user', 'fixture-user'), ('--port', '1999'), ('--offline-dir', '/fixture/payload')):
            self.assertEqual(args[args.index(option) + 1], value)

    def test_unavailable_optional_payload_is_warn_without_present_stamp(self):
        rc, _, calls = self.run_case(HELPER_EXIT='1', HELPER_RESULT='{"ok":false,"error":"payload_unavailable"}')
        self.assertEqual(rc, 0); self.assertIn('WARN', calls)
        self.assertNotIn('apply ', calls); self.assertNotIn('policy ', calls)

    def test_bad_hash_or_mask_propagates_failure_without_policy_change(self):
        for error in ('payload_hash_mismatch', 'service_masked'):
            with self.subTest(error=error):
                rc, _, calls = self.run_case(HELPER_EXIT='1', HELPER_RESULT=json.dumps({'ok': False, 'error': error}))
                self.assertNotEqual(rc, 0); self.assertNotIn('apply ', calls); self.assertNotIn('policy ', calls)

    def test_non_boolean_success_is_rejected(self):
        rc, _, calls = self.run_case(HELPER_RESULT='{"ok":"true","id":"node-red"}')
        self.assertNotEqual(rc, 0); self.assertNotIn('policy ', calls)

    def test_policy_write_failure_is_not_success(self):
        rc, _, calls = self.run_case(POLICY_EXIT='1')
        self.assertNotEqual(rc, 0); self.assertIn('ERR', calls)

    def test_rootfs_does_not_launch_or_install(self):
        rc, args, calls = self.run_case(SA02M_ROOTFS_BUILD='1')
        self.assertEqual(rc, 0); self.assertIsNone(args); self.assertNotIn('apply ', calls)

    def test_missing_cli_value_is_bounded_without_install(self):
        rc, args, _ = self.run_case(['--port'])
        self.assertEqual(rc, 2); self.assertIsNone(args)


if __name__ == '__main__':
    unittest.main()
