#!/usr/bin/env python3
# comment-mutation-proof-exempt: behavioural harness; runs shipped helpers and real CGI authentication/update guards with command recorders, real flock and process liveness.
"""Execute shipped system helpers/CGIs with only fixed OS paths retargeted.

No host reboot, systemctl mutation or sudo is possible: every such executable
is a recorder; no executable shell body is extracted or replaced.
"""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(os.environ.get('SYSTEM_ACTIONS_ROOT', Path(__file__).resolve().parents[2]))


class Actions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='system-actions-')
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(self.tmp.name)
        self.bin = self.t / 'bin'
        self.bin.mkdir()
        self.calls = self.t / 'calls'
        self.log = self.t / 'install.log'
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
                        CALLS=str(self.calls), FIXTURE=str(self.t),
                        SA02M_SESSION_DIR=str(self.t / 'sessions'),
                        SA02M_UPDATE_STATEDIR=str(self.t / 'update'),
                        SA02M_WEB_BUILD_STATEDIR=str(self.t / 'legacy'))
        recorder = '''#!/usr/bin/python3
import json, os, pathlib, sys
t=pathlib.Path(os.environ['FIXTURE']); name=pathlib.Path(sys.argv[0]).name
with open(os.environ['CALLS'],'a') as f: f.write(json.dumps([name]+sys.argv[1:])+'\\n')
if name == 'systemctl' and 'show' in sys.argv:
    if '--value' in sys.argv: print('inactive')
    else: print((t/'manager-state').read_text() if (t/'manager-state').exists() else 'Job=\\nActiveState=active')
    sys.exit(0)
if name == 'systemctl' and 'fcgiwrap.service' in sys.argv and (t/'fcgi.rc').exists(): sys.exit(int((t/'fcgi.rc').read_text()))
code=t/(name+'.rc')
sys.exit(int(code.read_text()) if code.exists() else 0)
'''
        for name in ('systemctl', 'systemd-run', 'reboot', 'shutdown', 'sync', 'nginx', 'sudo', 'sleep', 'nohup'):
            (self.bin / name).write_text(recorder)
            (self.bin / name).chmod(0o755)
        self.replacements = {
            '/var/log/sa02m_install.log': str(self.log),
            '/run/sa02m-web-reboot.lock': str(self.t / 'reboot.lock'),
            '/run/sa02m-web-restart.lock': str(self.t / 'restart.lock'),
            '/proc/sysrq-trigger': str(self.t / 'sysrq'),
        }
        for name in ('systemctl', 'systemd-run', 'reboot', 'shutdown', 'sync', 'nginx', 'sleep'):
            for prefix in ('/usr/bin/', '/usr/sbin/', '/sbin/', '/bin/'):
                self.replacements[prefix + name] = str(self.bin / name)
        for name in ('reboot', 'restart-services'):
            self.replacements['/usr/local/sbin/sa02m-web-' + name + '.sh'] = str(self.t / (name + '.sh'))
        for name in ('reboot', 'restart-services'):
            self.copy(ROOT / ('etc/sa02m-web-' + name + '.sh'), self.t / (name + '.sh'))

    def copy(self, src, dst):
        content = src.read_text()
        pattern = '|'.join(re.escape(key) for key in sorted(self.replacements, key=len, reverse=True))
        content = re.sub(pattern, lambda match: self.replacements[match[0]], content)
        dst.write_text(content)
        dst.chmod(0o755)

    def run_helper(self, name='reboot', *args):
        return subprocess.run(['sh', str(self.t / (name + '.sh')), *args], env=self.env,
                              text=True, capture_output=True, timeout=15)

    def recorded(self):
        return [json.loads(s) for s in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_admission_is_manager_owned(self):
        for name in ('reboot', 'restart-services'):
            self.calls.unlink(missing_ok=True)
            result = self.run_helper(name)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = self.recorded()
            self.assertEqual([c[0] for c in calls], ['systemd-run'])
            self.assertIn('--property=Type=exec', calls[0])
            self.assertIn('--collect', calls[0])
            self.assertEqual(calls[0][-2:], [str(self.t / (name + '.sh')), '--worker'])

    def test_admission_failure_never_runs_inline(self):
        (self.t / 'systemd-run.rc').write_text('1')
        for name in ('reboot', 'restart-services'):
            self.calls.unlink(missing_ok=True)
            self.assertNotEqual(self.run_helper(name).returncode, 0)
            self.assertEqual([c[0] for c in self.recorded()], ['systemd-run'])

    def test_bad_modes_do_nothing(self):
        for name in ('reboot', 'restart-services'):
            for args in (['bad'], ['--worker', 'extra'], ['--fallback', 'x'], ['--fallback', '29'], ['--fallback', '301']):
                self.calls.unlink(missing_ok=True)
                self.assertNotEqual(self.run_helper(name, *args).returncode, 0)
                self.assertEqual(self.recorded(), [])

    def test_reboot_worker_order(self):
        result = self.run_helper('reboot', '--worker')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        self.assertEqual([c[0] for c in calls], ['sleep', 'sync', 'systemd-run', 'systemctl'])
        self.assertIn('--property=DefaultDependencies=no', calls[2])
        self.assertEqual(calls[2][-2:], ['--fallback', '75'])
        self.assertEqual(calls[-1], ['systemctl', 'reboot', '--no-block'])

    def test_fallback_must_arm_before_reboot(self):
        (self.t / 'systemd-run.rc').write_text('1')
        self.assertNotEqual(self.run_helper('reboot', '--worker').returncode, 0)
        self.assertFalse(any(c[0] in ('reboot', 'systemctl') for c in self.recorded()))

    def test_restart_only_web_and_no_false_done(self):
        result = self.run_helper('restart-services', '--worker')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c for c in self.recorded() if c[0] == 'systemctl'],
                         [['systemctl', 'restart', 'nginx.service'], ['systemctl', 'restart', 'fcgiwrap.service']])
        self.calls.unlink()
        self.log.unlink()
        (self.t / 'systemctl.rc').write_text('124')
        self.assertNotEqual(self.run_helper('restart-services', '--worker').returncode, 0)
        self.assertNotIn('done', self.log.read_text())
        self.assertEqual([c for c in self.recorded() if c[0] in ('systemctl', 'nginx')],
                         [['systemctl', 'restart', 'nginx.service']])

    def test_failed_log_blocks_admission(self):
        self.log.mkdir()
        for name in ('reboot', 'restart-services'):
            self.assertNotEqual(self.run_helper(name).returncode, 0)
        self.assertEqual(self.recorded(), [])

    def test_bounded_admission_lock_and_missing_executables(self):
        for name, lockname in (('reboot', 'reboot.lock'), ('restart-services', 'restart.lock')):
            with (self.t / lockname).open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                started = time.monotonic()
                self.assertNotEqual(self.run_helper(name).returncode, 0)
                self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(self.recorded(), [])
        for executable in ('systemd-run', 'systemctl'):
            (self.bin / executable).chmod(0o644)
            self.assertNotEqual(self.run_helper().returncode, 0)
            self.assertEqual(self.recorded(), [])
            (self.bin / executable).chmod(0o755)
        helper = self.t / 'reboot.sh'
        helper.write_text(helper.read_text().replace('/usr/bin/timeout', str(self.t / 'missing-timeout')))
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertEqual(self.recorded(), [])

    def test_force_only_after_dispatch_failure_or_fallback(self):
        (self.t / 'systemctl.rc').write_text('1')
        self.assertEqual(self.run_helper('reboot', '--worker').returncode, 0)
        calls = self.recorded()
        self.assertEqual(calls[-3:], [['systemctl', 'reboot', '--no-block'], ['systemctl', 'reboot', '--force', '--force'], ['reboot', '-f']])
        (self.t / 'systemctl.rc').unlink()
        for delay in ('30', '75', '300'):
            self.calls.unlink()
            self.assertEqual(self.run_helper('reboot', '--fallback', delay).returncode, 0)
            self.assertEqual(self.recorded(), [['sleep', delay], ['sync'], ['systemctl', 'reboot', '--force', '--force']])

    def test_reload_only_after_terminal_nginx_failure(self):
        (self.t / 'systemctl.rc').write_text('1')
        (self.t / 'fcgi.rc').write_text('0')
        self.assertEqual(self.run_helper('restart-services', '--worker').returncode, 0)
        self.assertIn(['nginx', '-s', 'reload'], self.recorded())
        for state in ('Job=42 /job/42\nActiveState=activating', '', 'Job=\nActiveState=deactivating'):
            self.calls.unlink(); self.log.unlink()
            (self.t / 'manager-state').write_text(state)
            self.assertNotEqual(self.run_helper('restart-services', '--worker').returncode, 0)
            self.assertFalse(any(c[0] == 'nginx' for c in self.recorded()))
            self.assertNotIn('done', self.log.read_text())
        (self.t / 'systemctl.rc').unlink()
        (self.t / 'fcgi.rc').write_text('1')
        self.calls.unlink(); self.log.unlink()
        self.assertNotEqual(self.run_helper('restart-services', '--worker').returncode, 0)
        self.assertNotIn('done', self.log.read_text())

    def cgi(self, name, method='POST', csrf=True, session=True, stage='done', live=False):
        cgi_dir = self.t / 'cgi'
        cgi_dir.mkdir(exist_ok=True)
        source = ROOT / 'www/network_config/cgi-bin'
        for lib in ('lib_web_auth.sh', 'lib_web_update.sh'):
            shutil.copyfile(source / lib, cgi_dir / lib)
        self.copy(source / (name + '.cgi'), cgi_dir / (name + '.cgi'))
        minted = subprocess.check_output(['bash', '-c', '. "$1"; tok=$(web_session_create); printf "%s\\n" "$tok"; web_csrf_token_for_session "$tok"', 'mint', str(cgi_dir / 'lib_web_auth.sh')], env=self.env, text=True).splitlines()
        upd = self.t / 'update'; upd.mkdir(exist_ok=True)
        (upd / 'transaction.json').write_text(json.dumps({'stage': stage}))
        process = subprocess.Popen(['bash', '-c', 'exec -a sa02m-update-runner /bin/sleep 20']) if live else None
        (upd / 'update.lock').write_text(str(process.pid if process else 99999999))
        env = dict(self.env, REQUEST_METHOD=method, HTTP_COOKIE='session_token=' + (minted[0] if session else 'bad'), HTTP_X_SA02M_CSRF=minted[1] if csrf else 'bad')
        try:
            return subprocess.check_output(['bash', '-c', 'trap wait EXIT; . "$0"', str(cgi_dir / (name + '.cgi'))], env=env, text=True, timeout=12)
        finally:
            if process:
                process.terminate(); process.wait()

    def test_cgi_admission_checked(self):
        (self.t / 'sudo.rc').write_text('1')
        for name in ('reboot', 'restart'):
            body = self.cgi(name)
            self.assertIn('"ok":false', body)
            self.assertNotIn('"ok":true', body)
        (self.t / 'sudo.rc').unlink()
        for name in ('reboot', 'restart'):
            self.assertIn('"accepted":true', self.cgi(name))

    def test_cgi_guards_and_stale_update(self):
        for name in ('reboot', 'restart'):
            for kw, marker in (({'method': 'GET'}, 'method_not_allowed'), ({'csrf': False}, 'E_CSRF'), ({'session': False}, '/login.html'), ({'stage': 'applying', 'live': True}, 'E_UPDATE_RUNNING')):
                self.calls.unlink(missing_ok=True)
                self.assertIn(marker, self.cgi(name, **kw))
                self.assertFalse(any(c[0] in ('sudo', 'nohup') for c in self.recorded()))
            self.assertIn('"accepted":true', self.cgi(name, stage='verifying'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
