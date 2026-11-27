#!/usr/bin/env python3
"""Real PID1 worker admission, inherited coordinator lease and caller death.

Vendor bodies alone are harmless fixture callbacks. The shell dispatch, cache
module, coordinator reexec, broker socket and kernel leases are shipped code.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get('SA02M_DISPOSABLE_SYSTEMD_TEST') == '1', 'isolated PID1 opt-in required')
class ServiceWorker(unittest.TestCase):
    def setUp(self):
        if os.geteuid() or not Path('/run/systemd/container').exists() or Path('/proc/1/comm').read_text().strip() != 'systemd':
            raise RuntimeError('requires root in a disposable systemd container')
        if Path('/dev/watchdog').exists() or Path('/etc/armbian-release').exists():
            raise RuntimeError('refusing board environment')
        self.tmp = tempfile.TemporaryDirectory(prefix='service-worker-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.result = self.base / 'result'
        self.ready = self.base / 'ready'
        self.helper = self.base / 'helper.sh'
        module = self.base / 'cache.sh'
        module.write_text((ROOT / 'usr/local/lib/sa02m-service-cache.sh').read_text().replace(
            'RESULT_DIR=/var/run/sa02m-svcctl', 'RESULT_DIR=' + str(self.result)))
        source = (ROOT / 'etc/sa02m-web-service-ctl.sh').read_text()
        source = source.replace('/usr/local/lib/sa02m-service-cache.sh', str(module))
        source = source.replace('/usr/local/sbin/sa02m-web-service-ctl.sh', str(self.helper))
        source = source.replace('RESULT_DIR=/var/run/sa02m-svcctl', 'RESULT_DIR=' + str(self.result))
        source = source.replace('/var/log/sa02m_install.log', str(self.base / 'log'))
        source = source.replace('SA02M_STACKS_LIB=/usr/local/lib/sa02m-stacks-policy.sh', 'SA02M_STACKS_LIB=/nonexistent')
        self.callback = '''
fixture_install() {
    echo $$ > READY_PATH
    sleep 2
    emit_result '{"ok":true,"action":"install","id":"mplc4"}'
}
mplc4_install() { fixture_install; }
nodered_install() { fixture_install; }
svc_runtime_cgroup_proof() { return 0; }
cmd_list() { echo '{"ok":true,"services":[]}'; }
'''.replace('READY_PATH', str(self.ready))
        self.source = source.replace('ACTION=${1:-}', self.callback + '\nACTION=${1:-}', 1)
        self.helper.write_text(self.source)
        self.unit = 'sa02m-svcctl-install-mplc4.service'
        self.addCleanup(lambda: subprocess.run(['systemctl', 'stop', self.unit], capture_output=True, timeout=20))

    def broker(self):
        sys.path.insert(0, str(ROOT / 'opt/sa02m-port-coordinator/tests'))
        import test_process_ownership as ownership
        self.ownership = ownership
        h = ownership.ProcessOwnership()
        h.setUp()
        # The broker fixture owns a private readiness socket; real systemd
        # clients must not inherit its unconsumed sd_notify destination.
        os.environ.pop('NOTIFY_SOCKET', None)
        self.addCleanup(h.tearDown)
        package = self.base / 'coordinator'
        shutil.copytree(ROOT / 'opt/sa02m-port-coordinator/sa02m_ports', package / 'sa02m_ports')
        client = package / 'sa02m_ports/client.py'
        client.write_text(client.read_text().replace('/run/sa02m-port-coordinator', h.root + '/run'))
        guard = package / 'sa02m_ports/service_guard.py'
        guard.write_text(guard.read_text().replace('/usr/local/lib/sa02m-service-cache.sh', str(self.base / 'cache.sh')))
        self.helper.write_text(self.source.replace('/opt/sa02m-port-coordinator', str(package)))
        unit = Path('/run/systemd/system/mplc4.service')
        self.assertFalse(unit.exists())
        unit.write_text('[Service]\nType=oneshot\nExecStart=/bin/true\n')
        self.addCleanup(unit.unlink, missing_ok=True)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        return h

    def wait_ready(self, process):
        end = time.monotonic() + 10
        while not self.ready.exists() and process.poll() is None and time.monotonic() < end:
            time.sleep(0.03)
        details = '\n'.join(path.read_text()[-5000:] for path in (self.base / 'log', self.base / 'parent.out') if path.exists())
        self.assertTrue(self.ready.exists(), f'worker not admitted, exit={process.poll()}: {details}')

    def publish(self):
        result = subprocess.run(['/bin/sh', str(self.helper), 'list'], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {'ok': True, 'services': []})

    def handoff_case(self, mode):
        self.result.mkdir(mode=0o755)
        (self.result / 'owned-v1').write_bytes(Path('/proc/sys/kernel/random/boot_id').read_bytes())
        # Exercise the shipped identity/cgroup/stop code under its actual fixed
        # worker unit. Only vendor files become harmless sleep/stop fixtures.
        self.unit = 'sa02m-svcctl-install-codesys.service'
        binary = self.base / 'codesyscontrol.bin'
        binary.symlink_to('/bin/sleep')
        pidfile = self.base / 'vendor.pid'
        stopped = self.base / 'stop-called'
        stop = self.base / 'vendor-initd'
        stop.write_text('#!/bin/sh\necho called >' + str(stopped) + '\n' +
                        ('sleep 300\n' if mode == 'hung' else
                         'kill "$(cat ' + str(pidfile) + ')"\nwhile test -e /proc/$(cat ' + str(pidfile) + '); do sleep 0.02; done\n'))
        stop.chmod(0o755)
        foreign = None
        if mode == 'foreign':
            foreign = subprocess.Popen([str(binary), '300'])
            self.addCleanup(foreign.wait, timeout=5)
            self.addCleanup(foreign.terminate)
        text = (ROOT / 'etc/sa02m-web-service-ctl.sh').read_text().split('ACTION=${1:-}', 1)[0]
        text = text.replace('/usr/local/lib/sa02m-service-cache.sh', str(self.base / 'cache.sh'))
        text = text.replace('/var/run/codesyscontrol.pid', str(pidfile))
        text = text.replace("'/etc/init.d/codesyscontrol', 'stop'", repr(str(stop)) + ", 'stop'")
        text = text.replace('/var/log/sa02m_install.log', str(self.base / 'log'))
        if mode == 'hung':
            text = text.replace("'-k', '5', '45'", "'-k', '1', '1'").replace('timeout=52', 'timeout=4')
        text += '\nSVC_WORKER_UNIT=' + self.unit + '\nSA02M_SVC_WORKER_PID=$$\n'
        text += 'RESULT_DIR=' + str(self.result) + '\nsvc_cache_begin codesyscontrol.service || exit 1\n'
        text += '( ' + str(binary) + ' 300 & echo $! >' + str(pidfile) + ' )\nsleep 0.1\n'
        if mode == 'wrong_pid':
            text += 'echo 1 >' + str(pidfile) + '\n'
        text += 'codesys_vendor_handoff || exit 1\n'
        if mode == 'normal':
            runtime_unit = Path('/run/systemd/system/codesyscontrol.service')
            self.assertFalse(runtime_unit.exists())
            runtime_unit.write_text('[Service]\nType=simple\nExecStart=' + str(binary) + ' 300\nTimeoutStopSec=2s\n')
            self.addCleanup(runtime_unit.unlink, missing_ok=True)
            self.addCleanup(lambda: subprocess.run(['systemctl', 'stop', 'codesyscontrol.service'], capture_output=True, timeout=5))
            subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=5)
            text += 'initd_run /etc/init.d/codesyscontrol start\n'
        self.helper.write_text(text)
        result = subprocess.run(['systemd-run', '--quiet', '--wait', '--pipe', '--collect',
                                 '--unit=' + self.unit, '--service-type=exec', '--property=RuntimeMaxSec=15s',
                                 '--property=TimeoutStopSec=2s', '/bin/sh', str(self.helper)],
                                capture_output=True, text=True, timeout=20)
        if mode == 'normal':
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(stopped.exists())
            pid = subprocess.run(['systemctl', 'show', '--property=MainPID', '--value', 'codesyscontrol.service'],
                                 capture_output=True, text=True, check=True, timeout=5).stdout.strip()
            self.assertNotEqual(pid, '0')
            descriptors = [os.readlink(fd) for fd in Path('/proc', pid, 'fd').iterdir()]
            self.assertFalse(any('lease.' in name for name in descriptors), descriptors)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual(stopped.exists(), mode == 'hung')
            if foreign is not None:
                self.assertIsNone(foreign.poll(), 'foreign daemon was signalled')
        return result

    def test_proven_fresh_vendor_daemon_handoff(self):
        self.handoff_case('normal')

    def test_foreign_vendor_daemon_refuses_direct_stop(self):
        self.handoff_case('foreign')

    def test_wrong_vendor_pidfile_refuses_direct_stop(self):
        self.handoff_case('wrong_pid')

    def test_vendor_handoff_stop_is_bounded_and_not_green(self):
        self.handoff_case('hung')

    def test_internal_mode_is_rejected_outside_owned_pid1_unit(self):
        reply = subprocess.run(['/bin/sh', str(self.helper), '--vendor-worker', 'install', 'node-red', 'a'*32],
                               capture_output=True, text=True, timeout=10)
        self.assertNotEqual(reply.returncode, 0)
        self.assertEqual(json.loads(reply.stdout)['error'], 'invalid_worker')
        self.assertFalse(self.ready.exists())
        self.assertFalse(self.result.exists())

    def test_worker_owns_guard_after_admission_caller_sigkill(self):
        self.result.mkdir(mode=0o755)
        (self.result / 'owned-v1').write_bytes(Path('/proc/sys/kernel/random/boot_id').read_bytes())
        h = self.broker()
        output = (self.base / 'parent.out').open('w')
        self.addCleanup(output.close)
        process = subprocess.Popen(['/bin/sh', str(self.helper), 'install', 'mplc4', 'a'*32],
                                   stdout=output, stderr=output, start_new_session=True)
        try:
            self.wait_ready(process)
            self.assertEqual((self.result / 'list.gen').read_text(), '1\n', 'worker/reexec double registration')
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            with self.assertRaises(self.ownership.PortError):
                with h.lease():
                    pass
            self.publish()
            self.assertFalse((self.result / 'list.json').exists())
            end = time.monotonic() + 10
            while time.monotonic() < end:
                state = subprocess.run(['systemctl', 'is-active', self.unit], capture_output=True, text=True, timeout=3).stdout.strip()
                if state not in {'active', 'activating', 'deactivating'}:
                    break
                time.sleep(0.1)
            else:
                self.fail('worker did not finish')
            self.publish()
            self.assertTrue((self.result / 'list.json').exists(), (self.base / 'log').read_text())
            self.assertEqual(list((self.result / 'operations').iterdir()), [])
            self.assertEqual((self.result / 'list.gen').read_text(), '3\n')
            with h.lease():
                pass
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)


if __name__ == '__main__':
    if os.environ.get('SA02M_DISPOSABLE_SYSTEMD_TEST') != '1':
        print('PARTIAL: opted-in disposable PID1 container required')
        sys.exit(77)
    unittest.main(verbosity=2)
