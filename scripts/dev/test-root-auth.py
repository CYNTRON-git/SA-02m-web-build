#!/usr/bin/env python3
# comment-mutation-proof-exempt: exercises the privileged helper through real sudo and separate UIDs.
"""Run only in a disposable root container; never on a device or the host."""
from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
HELPER = Path('/usr/local/sbin/sa02m-web-root-cmd.sh')
MODULE = Path('/usr/local/lib/sa02m-root-auth.py')
STATE = Path('/run/sa02m-root-auth')
GRANT = Path('/etc/sudoers.d/sa02m-root-auth-fixture')


class RootBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0 or not Path('/run/.containerenv').is_file() or os.environ.get('SA02M_ROOT_AUTH_DISPOSABLE') != 'root-auth83':
            raise RuntimeError('explicit disposable root container required')
        for path in (HELPER, MODULE, GRANT, STATE):
            if path.exists() or path.is_symlink():
                raise RuntimeError('fixture path already exists: ' + str(path))
        cls.shadow = Path('/etc/shadow').read_bytes()
        cls.password = secrets.token_hex(24)
        account = pwd.getpwnam('www-data')
        cls.uid, cls.gid = account.pw_uid, account.pw_gid
        HELPER.parent.mkdir(parents=True, exist_ok=True)
        MODULE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'etc/sa02m-web-root-cmd.sh', HELPER)
        HELPER.chmod(0o755)
        if (ROOT / 'usr/local/lib/sa02m-root-auth.py').exists():
            shutil.copyfile(ROOT / 'usr/local/lib/sa02m-root-auth.py', MODULE)
            MODULE.chmod(0o644)
        GRANT.write_text('www-data ALL=(root) NOPASSWD: /usr/local/sbin/sa02m-web-root-cmd.sh *\n')
        GRANT.chmod(0o440)
        subprocess.run(['visudo', '-cf', str(GRANT)], check=True, capture_output=True)
        subprocess.run(['chpasswd'], input=('root:' + cls.password + '\n').encode(), check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        Path('/etc/shadow').write_bytes(cls.shadow)
        for path in (HELPER, MODULE, GRANT):
            path.unlink(missing_ok=True)
        if STATE.exists():
            shutil.rmtree(STATE)

    def setUp(self):
        if STATE.is_symlink():
            STATE.unlink()
        elif STATE.exists():
            shutil.rmtree(STATE)

    def state(self):
        return json.loads((STATE / 'state.json').read_text())

    def save_state(self, value):
        (STATE / 'state.json').write_text(json.dumps(value))

    def module(self):
        spec = importlib.util.spec_from_file_location('root_auth_fixture', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def invoke(self, password='incorrect', command='exit 0', receipt=False, env=None):
        paths = []
        try:
            for prefix, content in [('pass', password), ('cmd', command)] + ([('auth', '')] if receipt else []):
                fd, name = tempfile.mkstemp(prefix='sa02m-cmd-' + prefix + '.', dir='/tmp')
                with os.fdopen(fd, 'w') as out:
                    out.write(content + ('\n' if prefix != 'auth' else ''))
                os.chown(name, self.uid, self.gid)
                paths.append(Path(name))
            process = subprocess.run(['runuser', '-u', 'www-data', '--', 'sudo', '-n', '-u', 'root', str(HELPER), *map(str, paths)],
                                     capture_output=True, timeout=21, env={**os.environ, **(env or {})})
            status = paths[2].read_text() if receipt else None
            return process, status
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    def test_direct_helper_cannot_bypass_deleted_cgi_bucket(self):
        old = Path(tempfile.mkdtemp(prefix='old-root-login-', dir='/tmp'))
        os.chown(old, self.uid, self.gid)
        try:
            for index in range(10):
                bucket = old / 'root-192.0.2.1'
                bucket.write_text('10 0\n')
                os.chown(bucket, self.uid, self.gid)
                subprocess.run(['runuser', '-u', 'www-data', '--', 'rm', str(bucket)], check=True)
                result, _ = self.invoke(env={'REMOTE_ADDR': '192.0.2.' + str(index + 1), 'SA02M_LOGIN_MAXFAIL': '999'})
                self.assertEqual(result.returncode, 77)
            denied, _ = self.invoke(self.password, 'printf should-not-run')
            self.assertEqual(denied.returncode, 78)
            self.assertEqual(denied.stdout, b'')
        finally:
            shutil.rmtree(old)

    def test_concurrent_attempts_share_exactly_one_bucket(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.invoke()[0].returncode, range(12)))
        self.assertEqual(results.count(77), 10, results)
        self.assertEqual(results.count(78), 2, results)
        self.assertEqual(len(self.state()['attempts']), 10)
        for path, mode in [(STATE, 0o700), (STATE / 'lock', 0o600), (STATE / 'state.json', 0o600)]:
            self.assertEqual(path.stat().st_uid, 0)
            self.assertEqual(path.stat().st_mode & 0o777, mode)
        denied = subprocess.run(['runuser', '-u', 'www-data', '--', 'rm', str(STATE / 'state.json')], capture_output=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(len(self.state()['attempts']), 10)

    def test_success_preserves_failures_and_real_command_exit_codes(self):
        self.assertEqual(self.invoke()[0].returncode, 77)
        for code in (0, 7, 77, 78, 79):
            result, receipt = self.invoke(self.password, 'printf "root authentication failed"; exit ' + str(code), True)
            self.assertEqual(result.returncode, code)
            self.assertEqual(receipt, 'authenticated\n')
            self.assertEqual(len(self.state()['attempts']), 1)
        result, receipt = self.invoke(receipt=True)
        self.assertEqual(result.returncode, 77)
        self.assertEqual(receipt, 'denied\n')

    def test_rolling_expiry_and_boot_identity(self):
        self.invoke()
        state = self.state()
        state['attempts'] = [time.monotonic() - 301] * 10
        self.save_state(state)
        result, receipt = self.invoke(self.password, 'id -u', True)
        self.assertEqual((result.returncode, result.stdout, receipt), (0, b'0\n', 'authenticated\n'))
        self.assertEqual(self.state()['attempts'], [])
        state['attempts'] = []
        state['boot_id'] = '00000000-0000-0000-0000-000000000000'
        self.save_state(state)
        result, receipt = self.invoke(self.password, 'printf must-not-run', True)
        self.assertEqual((result.returncode, result.stdout, receipt), (79, b'', 'unavailable\n'))

    def test_malformed_and_unsafe_state_fail_closed(self):
        self.invoke()
        valid = self.state()
        variants = ['broken', [], {**valid, 'version': True}, {**valid, 'attempts': [True]},
                    {**valid, 'attempts': [float('nan')]}, {**valid, 'attempts': [time.monotonic() + 100]},
                    {**valid, 'attempts': [1] * 11}, {**valid, 'extra': 1}]
        for value in variants:
            with self.subTest(state=type(value).__name__):
                (STATE / 'state.json').write_text(value if isinstance(value, str) else json.dumps(value))
                result, receipt = self.invoke(self.password, 'printf must-not-run', True)
                self.assertEqual((result.returncode, result.stdout, receipt), (79, b'', 'unavailable\n'))
        self.save_state(valid)
        target = STATE / 'state.json'
        for action, undo in [(lambda: target.chmod(0o644), lambda: target.chmod(0o600)),
                             (lambda: os.chown(target, self.uid, self.gid), lambda: os.chown(target, 0, 0)),
                             (lambda: os.link(target, STATE / 'hardlink'), lambda: (STATE / 'hardlink').unlink())]:
            action()
            try:
                self.assertEqual(self.invoke(self.password)[0].returncode, 79)
            finally:
                undo()
        target.rename(STATE / 'retained')
        target.symlink_to(STATE / 'retained')
        self.assertEqual(self.invoke(self.password)[0].returncode, 79)
        target.unlink()
        (STATE / 'retained').rename(target)
        STATE.chmod(0o755)
        self.assertEqual(self.invoke(self.password)[0].returncode, 79)
        STATE.chmod(0o700)

    def test_lock_timeout_preserves_state_and_budget(self):
        self.invoke()
        before = (STATE / 'state.json').read_bytes()
        with (STATE / 'lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            start = time.monotonic()
            result, receipt = self.invoke(self.password, receipt=True)
            elapsed = time.monotonic() - start
        self.assertEqual((result.returncode, receipt), (79, 'unavailable\n'))
        self.assertGreaterEqual(elapsed, 1.8)
        self.assertLess(elapsed, 4)
        self.assertEqual((STATE / 'state.json').read_bytes(), before)

    def test_killed_verifier_keeps_reservation_and_releases_lock(self):
        code = ('import importlib.util,os,signal;'
                's=importlib.util.spec_from_file_location("auth",' + repr(str(MODULE)) + ');'
                'm=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
                'm.verify_password=lambda value:os.kill(os.getpid(),signal.SIGKILL);'
                '\nwith m.Throttle() as t:t.authenticate("synthetic")\n')
        result = subprocess.run(['/usr/bin/python3', '-I', '-c', code], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, -9)
        self.assertEqual(len(self.state()['attempts']), 1)
        self.assertEqual(self.invoke()[0].returncode, 77)
        self.assertEqual(len(self.state()['attempts']), 2)

    def test_write_failure_never_reaches_password_oracle(self):
        module = self.module()
        with module.Throttle() as throttle, mock.patch.object(module.os, 'replace', side_effect=OSError(28, 'No space')) as replace, \
                mock.patch.object(module, 'verify_password') as oracle:
            with self.assertRaises(OSError):
                throttle.authenticate('synthetic')
            replace.assert_called_once()
            oracle.assert_not_called()
        self.assertFalse((STATE / 'state.json').exists())
        self.assertEqual(list(STATE.glob('.state-*')), [])

    def test_success_cleanup_failure_never_executes_command(self):
        module = self.module()
        actual_save = module.Throttle.save
        saves = []
        def fail_second_save(throttle, attempts):
            saves.append(list(attempts))
            if len(saves) == 2:
                raise OSError(28, 'No space')
            return actual_save(throttle, attempts)
        with mock.patch.object(module, 'read_input', side_effect=['synthetic', 'printf must-not-run']), \
                mock.patch.object(module, 'verify_password', return_value=True), \
                mock.patch.object(module.Throttle, 'save', fail_second_save), mock.patch.object(module.os, 'execv') as execute:
            self.assertEqual(module.main(['pass', 'cmd']), 79)
            execute.assert_not_called()
        self.assertEqual(len(saves), 2)
        self.assertEqual(len(self.state()['attempts']), 1)

    def test_hostile_input_files_refuse_before_authentication(self):
        paths = []
        try:
            for prefix, content in [('pass', self.password), ('cmd', 'printf must-not-run'), ('auth', '')]:
                fd, name = tempfile.mkstemp(prefix='sa02m-cmd-' + prefix + '.', dir='/tmp')
                with os.fdopen(fd, 'w') as out:
                    out.write(content)
                os.chown(name, self.uid, self.gid)
                paths.append(Path(name))
            def run():
                return subprocess.run(['runuser', '-u', 'www-data', '--', 'sudo', '-n', '-u', 'root', str(HELPER), *map(str, paths)],
                                      capture_output=True, timeout=5)
            original = paths[0].read_bytes()
            for index in range(3):
                target = paths[index]
                target.chmod(0o644)
                self.assertEqual(run().returncode, 79)
                target.chmod(0o600)
                os.chown(target, 65534, 65534)
                self.assertEqual(run().returncode, 79)
                os.chown(target, self.uid, self.gid)
                linked = Path(str(target) + '-link')
                os.link(target, linked)
                self.assertEqual(run().returncode, 79)
                linked.unlink()
                target.rename(linked)
                target.symlink_to(linked)
                self.assertNotEqual(run().returncode, 0)
                target.unlink()
                linked.rename(target)
            def replace_bytes(path, data):
                # Linux protected_regular forbids root O_CREAT on another UID's /tmp file.
                fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
                try:
                    self.assertEqual(os.write(fd, data), len(data))
                finally:
                    os.close(fd)
            replace_bytes(paths[0], b'x' * 4097)
            self.assertEqual(run().returncode, 79)
            replace_bytes(paths[0], original)
            replace_bytes(paths[1], b'x' * 1001)
            self.assertEqual(run().returncode, 79)
            self.assertFalse(STATE.exists())
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    def test_real_cgi_receipts_login_independence_and_exit_codes(self):
        directory = Path(tempfile.mkdtemp(prefix='root-auth-cgi-', dir='/tmp'))
        os.chown(directory, self.uid, self.gid)
        token, csrf = 'b' * 64, 'c' * 64
        session = directory / hashlib.sha256(token.encode()).hexdigest()
        session.write_text(str(int(time.time()) + 3600) + ' tester\n')
        (directory / (session.name + '.csrf')).write_text(csrf + '\n')
        for path in directory.iterdir():
            os.chown(path, self.uid, self.gid)
        cgi = ROOT / 'www/network_config/cgi-bin/cmd_exec.cgi'

        def request(password, command='id -u', mode='root'):
            body = urlencode({'mode': mode, 'cmd': command, 'root_password': password}).encode()
            env = {**os.environ, 'HTTP_COOKIE': 'session_token=' + token, 'HTTP_X_SA02M_CSRF': csrf,
                   'REQUEST_METHOD': 'POST', 'CONTENT_LENGTH': str(len(body)), 'SA02M_SESSION_DIR': str(directory),
                   'SA02M_LOGIN_DIR': str(directory / 'login'), 'REMOTE_ADDR': '192.0.2.24'}
            result = subprocess.run(['runuser', '-u', 'www-data', '--', 'bash', str(cgi)], input=body,
                                    capture_output=True, env=env, timeout=22)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertNotIn(self.password.encode(), result.stdout + result.stderr)
            return json.loads(result.stdout.split(b'\n\n', 1)[1])
        try:
            for code in (77, 78, 79):
                result = request(self.password, 'printf marker; exit ' + str(code))
                self.assertEqual((result['ok'], result['rc'], result['output'], result['root_authenticated']), (True, code, 'marker', True))
            for _ in range(10):
                self.assertEqual(request('wrong')['error'], 'root_auth_failed')
            self.assertEqual(request(self.password)['error'], 'root_locked')
            self.assertEqual(request('', 'id -u', 'web')['output'].strip(), str(self.uid))
            self.assertFalse((directory / 'login').exists())
        finally:
            shutil.rmtree(directory)


if __name__ == '__main__':
    unittest.main(verbosity=2)
