#!/usr/bin/env python3
"""Actual PID1 timer/cgroup and cache-lease handoff in an opted-in container.

Refuses hosts and boards. Never uses the vendor runtime or real MPLC unit.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import signal
import shutil
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
UNIT = 'sa02m-test-mplc-bounded.service'


def sc(*args, **kwargs):
    kwargs.setdefault('timeout', 8)
    return subprocess.run(['systemctl', *args], text=True, capture_output=True, **kwargs)


@unittest.skipUnless(os.environ.get('SA02M_DISPOSABLE_SYSTEMD_TEST') == '1', 'isolated PID1 opt-in required')
class Pid1Lifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() or not Path('/run/systemd/container').exists() or Path('/proc/1/comm').read_text().strip() != 'systemd':
            raise RuntimeError('requires root in a disposable systemd container')
        if Path('/dev/watchdog').exists() or Path('/etc/armbian-release').exists():
            raise RuntimeError('refusing device/board environment')

    def cache_fixture(self, base):
        (base / 'cache').mkdir(mode=0o755, exist_ok=True)
        (base / 'cache/owned-v1').write_bytes(Path('/proc/sys/kernel/random/boot_id').read_bytes())
        library = base / 'cache.sh'
        library.write_text('RESULT_DIR=' + str(base / 'cache') + '\nSC=/usr/bin/systemctl\n' +
                           (ROOT / 'usr/local/lib/sa02m-service-cache.sh').read_text())
        return library

    def install_unit(self, name, body):
        path = Path('/run/systemd/system') / name
        self.assertFalse(os.path.lexists(path), 'refusing an existing fixture unit path')
        path.write_text(body)
        self.addCleanup(path.unlink, missing_ok=True)
        self.addCleanup(lambda: sc('stop', name, timeout=15))
        self.assertEqual(sc('daemon-reload').returncode, 0)

    def test_actual_pid1_job_keeps_unlocked_record_until_terminal(self):
        name = 'sa02m-test-cache-job.service'
        self.install_unit(name, '[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/bin/sleep 3\nTimeoutStartSec=5s\n')
        with tempfile.TemporaryDirectory(prefix='cache-pid1-') as folder:
            base = Path(folder)
            library = self.cache_fixture(base)
            ready = base / 'ready'
            process = subprocess.Popen(['/bin/sh', '-c', '. "$1"; svc_cache_begin "$2" || exit; '
                                        'systemctl start --no-block "$2" || exit; echo ready >"$3"; sleep 30',
                                        '_', str(library), name, str(ready)], start_new_session=True)
            try:
                end = time.monotonic() + 5
                while not ready.exists() and process.poll() is None and time.monotonic() < end:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                self.assertIn('ActiveState=activating', sc('show', '--property=ActiveState,Job', name).stdout)
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
                publish = ['/bin/sh', '-c', '. "$1"; cmd_list() { echo \'{"ok":true}\'; }; svc_list_publish', '_', str(library)]
                self.assertEqual(subprocess.run(publish, capture_output=True, timeout=10).returncode, 0)
                self.assertFalse((base / 'cache/list.json').exists())
                self.assertEqual(len(list((base / 'cache/operations').iterdir())), 1)
                self.assertEqual(sc('start', name, timeout=8).returncode, 0)
                self.assertEqual(subprocess.run(publish, capture_output=True, timeout=10).returncode, 0)
                self.assertTrue((base / 'cache/list.json').exists())
                self.assertEqual(list((base / 'cache/operations').iterdir()), [])
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)

    def test_real_managed_daemon_does_not_inherit_cache_lease(self):
        name = 'codemeter.service'
        self.install_unit(name, '[Service]\nType=simple\nExecStart=/bin/sleep 30\nTimeoutStartSec=5s\nTimeoutStopSec=5s\n')
        with tempfile.TemporaryDirectory(prefix='cache-daemon-') as folder:
            base = Path(folder)
            library = self.cache_fixture(base)
            helper = base / 'helper.sh'
            source = (ROOT / 'etc/sa02m-web-service-ctl.sh').read_text().split('ACTION=${1:-}', 1)[0]
            source = source.replace('SVC_CACHE_LIB=/usr/local/lib/sa02m-service-cache.sh', 'SVC_CACHE_LIB=' + str(library))
            source = source.replace('/var/log/sa02m_install.log', str(base / 'log'))
            source += '\nsvc_cache_begin codemeter.service && initd_run /etc/init.d/codemeter start && svc_cache_finish\n'
            helper.write_text(source)
            result = subprocess.run(['/bin/sh', str(helper)], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            pid = sc('show', '--property=MainPID', '--value', name).stdout.strip()
            self.assertNotEqual(pid, '0')
            paths = [os.readlink(fd) for fd in Path('/proc', pid, 'fd').iterdir()]
            self.assertFalse(any('/operations/lease.' in p or '/.lease.' in p for p in paths), paths)
            self.assertEqual(list((base / 'cache/operations').iterdir()), [])

    def native_fixture(self, base):
        unit = Path('/etc/systemd/system/mplc4.service')
        runtime = Path('/opt/mplc4')
        dropins = Path('/etc/systemd/system/mplc4.service.d')
        self.assertFalse(unit.exists())
        self.assertFalse(runtime.exists())
        self.assertFalse(dropins.exists())
        runtime.mkdir()
        self.addCleanup(shutil.rmtree, runtime)
        self.addCleanup(shutil.rmtree, dropins)
        self.addCleanup(unit.unlink, missing_ok=True)
        self.addCleanup(lambda: sc('stop', 'mplc4.service', timeout=20))
        library = self.cache_fixture(base)
        helper = base / 'helper.sh'
        source = (ROOT / 'etc/sa02m-web-service-ctl.sh').read_text().split('ACTION=${1:-}', 1)[0]
        source = source.replace('/usr/local/lib/sa02m-service-cache.sh', str(library))
        source = source.replace('/usr/local/lib/sa02m-mplc-policy.sh', str(ROOT / 'usr/local/lib/sa02m-mplc-policy.sh'))
        source = source.replace('/var/log/sa02m_install.log', str(base / 'log'))
        # Fresh native vendor installs have no init.d entry. The real helper
        # must therefore avoid invoking update-rc.d for a missing script.
        source += '\nmplc4_process_active() { grep -q "[0-9]" /sys/fs/cgroup/system.slice/mplc4.service/cgroup.procs 2>/dev/null; }\n'
        helper.write_text(source)
        setup = '. "$1"; mplc_policy_load && sa02m_mplc_policy_install "$2" && sa02m_mplc_wrapper_install "$2" "$3"'
        result = subprocess.run(['/bin/sh', '-c', setup, '_', str(helper), str(ROOT / 'opt/sa02m-mplc/systemd/mplc4.service'),
                                 str(ROOT / 'opt/sa02m-mplc/systemd/mplc4-native.service')], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sc('daemon-reload').returncode, 0)
        return helper, runtime, unit, dropins

    def test_native_restart_always_is_cancelled_after_failed_start(self):
        with tempfile.TemporaryDirectory(prefix='mplc-native-') as folder:
            base = Path(folder)
            helper, runtime, _, _ = self.native_fixture(base)
            launcher = runtime / 'start_mplc4.sh'
            launcher.write_text('#!/bin/sh\necho start >>/opt/mplc4/calls\ntrap "" TERM\nsleep 300\n')
            launcher.chmod(0o755)
            start = time.monotonic()
            result = subprocess.run(['/bin/sh', '-c', '. "$1"; mplc_managed_action start', '_', str(helper)],
                                    capture_output=True, text=True, timeout=95)
            elapsed = time.monotonic() - start
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual(json.loads(result.stdout)['error'], 'start_failed', result.stdout + result.stderr)
            self.assertLess(elapsed, 85)
            self.assertGreaterEqual(elapsed, 44)
            self.assertEqual((runtime / 'calls').read_text().splitlines(), ['start'])
            state = sc('show', '--property=Job,ActiveState,MainPID,ControlPID,ControlGroup,Restart', 'mplc4.service').stdout
            self.assertIn('Restart=always', state)
            self.assertRegex(state, r'ActiveState=(inactive|failed)\n')
            self.assertRegex(state, r'Job=(0)?\n')
            self.assertIn('MainPID=0', state)
            group = Path('/sys/fs/cgroup/system.slice/mplc4.service')
            self.assertTrue(not group.exists() or all(not p.read_text().strip() for p in group.rglob('cgroup.procs')))
            print(f'Native failure cancelled, empty cgroup, one launch, restart policy preserved at {elapsed:.3f}s', flush=True)

    def test_vendor_unit_rewrite_retains_finite_policy_and_override_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='mplc-policy-') as folder:
            helper, runtime, unit, dropins = self.native_fixture(Path(folder))
            (runtime / 'start_mplc4.sh').write_text('#!/bin/sh\nexit 0\n')
            (runtime / 'start_mplc4.sh').chmod(0o755)
            observe = ['/bin/sh', '-c', '. "$1"; mplc_observe', '_', str(helper)]
            checked = subprocess.run(observe, capture_output=True, text=True, timeout=10)
            self.assertEqual(checked.returncode, 0, checked.stderr + sc('show', 'mplc4.service').stdout)
            # Exact vendor profile rewrites only the base file, as install.sh does.
            unit.write_text((ROOT / 'opt/sa02m-mplc/systemd/mplc4-native.service').read_text())
            sc('daemon-reload')
            self.assertEqual(subprocess.run(observe, capture_output=True, timeout=10).returncode, 0)
            (dropins / 'zz-admin.conf').write_text('[Service]\nTimeoutStartSec=infinity\n')
            sc('daemon-reload')
            self.assertNotEqual(subprocess.run(observe, capture_output=True, timeout=10).returncode, 0)

    def test_oneshot_timeout_outlives_client_then_kills_cgroup(self):
        unit = Path('/run/systemd/system') / UNIT
        source = (ROOT / 'opt/sa02m-mplc/systemd/mplc4.service').read_text()
        fixture = Path('/run/sa02m-test-mplc-hang.sh')
        fixture.write_text('#!/bin/sh\ntrap "" TERM\necho $$ >/run/sa02m-test-mplc.pid\nsleep 300\n')
        fixture.chmod(0o755)
        source = source.replace('ExecStart=/etc/init.d/mplc4 start', 'ExecStart=' + str(fixture))
        source = source.replace('ExecStop=/etc/init.d/mplc4 stop', 'ExecStop=/bin/true')
        unit.write_text(source)
        self.addCleanup(unit.unlink, missing_ok=True)
        self.addCleanup(fixture.unlink, missing_ok=True)
        self.assertEqual(sc('daemon-reload').returncode, 0)
        effective = sc('show', '--property=TimeoutStartUSec,TimeoutStopUSec,TimeoutStartFailureMode,KillMode', UNIT)
        self.assertIn('TimeoutStartUSec=45s', effective.stdout)
        self.assertIn('TimeoutStopUSec=15s', effective.stdout)
        self.assertIn('TimeoutStartFailureMode=terminate', effective.stdout)
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            sc('start', UNIT, timeout=1)
        initial = sc('show', '--property=Job,ActiveState,ControlGroup', UNIT).stdout
        self.assertIn('ActiveState=activating', initial)
        self.assertNotIn('Job=0\n', initial)
        group = next(line.split('=', 1)[1] for line in initial.splitlines() if line.startswith('ControlGroup='))
        self.assertTrue(group)
        while time.monotonic() - started < 72:
            reply = sc('show', '--property=Job,ActiveState,Result', UNIT).stdout
            if 'ActiveState=failed' in reply and ('Job=0\n' in reply or 'Job=\n' in reply):
                break
            time.sleep(0.5)
        else:
            self.fail('PID1 did not reach a terminal timeout failure: ' + reply)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 44)
        self.assertLess(elapsed, 72)
        self.assertIn('Result=timeout', reply)
        processes = Path('/sys/fs/cgroup' + group + '/cgroup.procs')
        self.assertTrue(not processes.exists() or not processes.read_text().strip())
        print(f'PID1 client timeout at1s; final timeout failure and empty cgroup at {elapsed:.3f}s', flush=True)


if __name__ == '__main__':
    if os.environ.get('SA02M_DISPOSABLE_SYSTEMD_TEST') != '1':
        print('PARTIAL: opted-in disposable PID1 container required')
        sys.exit(77)
    unittest.main(verbosity=2)
