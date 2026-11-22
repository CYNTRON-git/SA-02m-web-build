#!/usr/bin/env python3
# comment-mutation-proof-exempt: Exercises the shipped helper with real PTYs and runtime faults, not static source needles.
"""Regression tests for the telemetry-only shared descriptor scan."""
from contextlib import contextmanager, redirect_stdout
import errno
import io
import itertools
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / 'scripts/sa02m-rs485-stats.sh'


def program():
    """Load the exact inline program; there is no second scanner in the test."""
    source = HELPER.read_text().split("<<'PY_RS485_BATCH'\n", 1)[1].split('\nPY_RS485_BATCH', 1)[0]
    namespace = {'__name__': 'rs485_scanner_under_test'}
    exec(compile(source, str(HELPER) + ':inline', 'exec'), namespace)
    return namespace


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rs485-batch-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.trace = self.base / 'probes'
        self.env = dict(os.environ, PATH=str(self.bin), PROBE_LOG=str(self.trace))
        for name in ('fuser', 'lsof'):
            p = self.bin / name
            p.write_text('#!/bin/bash\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$PROBE_LOG"\n[ "$1" = "${PROBE_OCCUPIED:-}" ]\n')
            p.chmod(0o755)
        (self.bin / 'python3').symlink_to(sys.executable)
        self.devices = []
        for _ in range(5):
            master, slave = os.openpty()
            self.addCleanup(os.close, master)
            self.devices.append(os.ttyname(slave))
            os.close(slave)

    def run_helper(self, command, *args):
        return subprocess.run(['/bin/bash', str(HELPER), command, *args], env=self.env,
                              capture_output=True, text=True, timeout=6, check=False)

    def probes(self):
        return self.trace.read_text().splitlines() if self.trace.exists() else []

    def expect_rows(self, result, devices, busy=()):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ''.join(p+':'+str(int(p in busy))+'\n' for p in devices))

    def call_main(self, namespace=None):
        namespace = namespace or program()
        output = io.StringIO()
        with patch.object(sys, 'argv', ['-', *self.devices]), redirect_stdout(output):
            code = namespace['main']()
        return code, output.getvalue()

    def test_successful_batch_avoids_per_port_process_scans(self):
        result = self.run_helper('inuse-batch', *self.devices)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, ''.join(p+':0\n' for p in self.devices))
        self.assertEqual(self.probes(), [], 'successful batch must not scan processes separately per port')

    def test_real_pty_handles_in_one_process(self):
        busy = (self.devices[1], self.devices[3])
        for device in busy:
            self.addCleanup(os.close, os.open(device, os.O_RDWR | os.O_NOCTTY))
        self.expect_rows(self.run_helper('inuse-batch', *self.devices), self.devices, busy)
        self.assertEqual(self.probes(), [])

    def test_real_pty_handles_in_distinct_processes(self):
        workers = []
        try:
            for device in (self.devices[0], self.devices[4]):
                code = 'import os,sys;f=os.open(sys.argv[1],os.O_RDWR|os.O_NOCTTY);print("ready",flush=True);sys.stdin.readline()'
                child = subprocess.Popen([sys.executable, '-c', code, device], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, text=True)
                workers.append(child)
                self.assertEqual(child.stdout.readline().strip(), 'ready')
            self.expect_rows(self.run_helper('inuse-batch', *self.devices), self.devices,
                             (self.devices[0], self.devices[4]))
            self.assertEqual(self.probes(), [])
        finally:
            for child in workers:
                child.communicate('\n', timeout=3)

    def test_character_identity_aliases_duplicates_and_literal_metacharacters(self):
        alias = self.base / 'alias space;$(touch SHOULD_NOT_EXIST)\'"'
        alias.symlink_to(self.devices[0])
        self.addCleanup(os.close, os.open(str(alias), os.O_RDWR | os.O_NOCTTY))
        devices = [str(alias), self.devices[0], self.devices[0], self.devices[1]]
        self.expect_rows(self.run_helper('inuse-batch', *devices), devices, (str(alias), self.devices[0]))
        self.assertEqual(self.probes(), [])
        self.assertFalse((ROOT / 'SHOULD_NOT_EXIST').exists())

    def test_one_process_table_enumeration_for_successful_batch(self):
        namespace = program()
        original = os.scandir
        roots = []
        def counted(path):
            if path == '/proc':
                roots.append(path)
            return original(path)
        with patch('os.scandir', counted):
            code, output = self.call_main(namespace)
        self.assertEqual(code, 0)
        self.assertEqual(output, ''.join(p+':0\n' for p in self.devices))
        self.assertEqual(roots, ['/proc'])

    def test_missing_python_uses_exact_legacy_fallback(self):
        (self.bin / 'python3').unlink()
        self.env['PROBE_OCCUPIED'] = self.devices[2]
        self.expect_rows(self.run_helper('inuse-batch', *self.devices), self.devices, (self.devices[2],))
        expected = []
        for device in self.devices:
            expected.append('fuser '+device)
            if device != self.devices[2]:
                expected.append('lsof '+device)
        self.assertEqual(self.probes(), expected)

    def test_incomplete_proc_scan_has_no_partial_rows_before_fallback(self):
        shim = self.bin / 'python3'
        shim.unlink()
        shim.write_text('#!'+sys.executable+'\n'+'''import errno,os,sys
from unittest.mock import patch
source=sys.stdin.read()
sys.argv=['-',*sys.argv[4:]]
original=os.scandir
def denied(path):
    if str(path).startswith('/proc/') and str(path).endswith('/fd'):
        raise PermissionError(errno.EACCES, 'injected unreadable process')
    return original(path)
with patch('os.scandir', denied):
    exec(compile(source,'actual-inline-scanner','exec'),{'__name__':'__main__'})
''')
        shim.chmod(0o755)
        self.env['PROBE_OCCUPIED'] = self.devices[0]
        self.expect_rows(self.run_helper('inuse-batch', *self.devices), self.devices, (self.devices[0],))
        self.assertEqual(len(self.probes()), 9)

    def test_missing_regular_relative_and_empty_targets_preserve_fallback(self):
        regular = self.base / 'regular'; regular.write_text('fixture')
        devices = [self.devices[0], str(self.base / 'absent'), str(regular), 'relative', '']
        self.env['PROBE_OCCUPIED'] = str(regular)
        self.expect_rows(self.run_helper('inuse-batch', *devices), devices, (str(regular),))
        self.assertEqual(len(self.probes()), 7)  # Empty target keeps tty_in_use's early refusal.

    def test_more_than_five_targets_preserves_fallback(self):
        devices = self.devices + [self.devices[0]]
        self.expect_rows(self.run_helper('inuse-batch', *devices), devices)
        self.assertEqual(len(self.probes()), 12)

    def test_single_port_cli_keeps_existing_exit_and_probe_behavior(self):
        self.env['PROBE_OCCUPIED'] = self.devices[0]
        result = self.run_helper('inuse', self.devices[0])
        self.assertEqual((result.returncode, result.stdout), (0, ''))
        self.assertEqual(self.probes(), ['fuser '+self.devices[0]])
        self.trace.unlink()
        result = self.run_helper('inuse', self.devices[1])
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertEqual(self.probes(), ['fuser '+self.devices[1], 'lsof '+self.devices[1]])

    def test_driver_cli_keeps_fixed_path(self):
        cat = self.bin / 'cat'
        cat.write_text('#!/bin/bash\n[ "$#" = 1 ] && [ "$1" = /proc/tty/driver/serial ] || exit 9\nprintf "driver-fixture\\n"\n')
        cat.chmod(0o755)
        result = self.run_helper('driver')
        self.assertEqual((result.returncode, result.stdout), (0, 'driver-fixture\n'))
        self.assertEqual(self.probes(), [])

    def test_usage_refusals_unchanged(self):
        for command, args in (('inuse-batch', []), ('inuse', []), ('unknown', [])):
            with self.subTest(command=command):
                result = self.run_helper(command, *args)
                self.assertEqual((result.returncode, result.stdout), (2, ''))
                self.assertIn('Usage:', result.stderr)
        self.assertEqual(self.probes(), [])

    def test_disappearing_pid_and_descriptor_are_tolerated(self):
        namespace = program()
        original = os.scandir
        @contextmanager
        def appended(path):
            with original(path) as entries:
                if path == '/proc':
                    gone = SimpleNamespace(name='99999999', path='/proc/99999999')
                    yield itertools.chain((gone,), entries)
                elif path == '/proc/'+str(os.getpid())+'/fd':
                    gone = SimpleNamespace(path=path+'/99999999')
                    yield itertools.chain((gone,), entries)
                else:
                    yield entries
        with patch('os.scandir', appended):
            code, output = self.call_main(namespace)
        self.assertEqual((code, output), (0, ''.join(p+':0\n' for p in self.devices)))

    def test_permission_denied_or_other_descriptor_errors_are_not_free(self):
        namespace = program()
        original = os.stat
        for error in (errno.EACCES, errno.EPERM, errno.EIO):
            def denied(path, *args, **kwargs):
                if str(path).startswith('/proc/') and '/fd/' in str(path):
                    raise OSError(error, 'injected incomplete evidence')
                return original(path, *args, **kwargs)
            with self.subTest(error=error), patch('os.stat', denied):
                self.assertEqual(self.call_main(namespace), (1, ''))

    def test_unreadable_process_directory_is_not_free(self):
        namespace = program(); original = os.scandir
        def denied(path):
            if str(path).startswith('/proc/'):
                raise PermissionError(errno.EACCES, 'injected unreadable directory')
            return original(path)
        with patch('os.scandir', denied):
            self.assertEqual(self.call_main(namespace), (1, ''))

    def test_scan_work_limits_emit_no_partial_results(self):
        for limit in ('MAX_PROCESSES', 'MAX_DESCRIPTORS'):
            namespace = program(); namespace[limit] = 0
            with self.subTest(limit=limit):
                self.assertEqual(self.call_main(namespace), (1, ''))

    def test_monotonic_deadline_emits_no_partial_results(self):
        namespace = program()
        with patch('time.monotonic', side_effect=(0.0, namespace['SCAN_SECONDS']+1.0)):
            self.assertEqual(self.call_main(namespace), (1, ''))

    def test_blocked_syscall_is_bounded_by_alarm(self):
        namespace = program(); original = os.scandir
        def delayed(path):
            if path == '/proc':
                time.sleep(5)
            return original(path)
        started = time.monotonic()
        with patch('os.scandir', delayed):
            self.assertEqual(self.call_main(namespace), (1, ''))
        elapsed = time.monotonic()-started
        self.assertGreaterEqual(elapsed, namespace['SCAN_SECONDS'] * .8)
        self.assertLess(elapsed, namespace['SCAN_SECONDS'] + .8)


if __name__ == '__main__':
    unittest.main(verbosity=2)
