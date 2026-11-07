"""Board verification fixtures never mutate the operator's scenario store."""
import contextlib
import io
import json
import select
import shutil
import signal
import socket
import subprocess
import time
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'opt/sa02m-rules'))
sys.path.insert(0, str(ROOT / 'opt/sa02m-alice'))
from sa02m_rules import engine, store
SCRIPT = ROOT / 'scripts/dev/verify-release-on-board.sh'

def fixture_module():
    code = SCRIPT.read_text().split('# BEGIN ISOLATED FIXTURES\n', 1)[1].split('# END ISOLATED FIXTURES', 1)[0]
    namespace = {'__name__': 'fixture_test'}
    exec(compile(code, str(SCRIPT), 'exec'), namespace)
    return namespace


def thread_group_running(pid):
    """True while any thread of `pid` has not finished exiting.

    The leader's own /proc/<pid>/stat is not enough: a multithreaded leader
    reads 'Z' as soon as IT exits, while a sibling thread (here the listener's
    serve_forever or the parent watcher) is still running down — and the
    shared fd table, the listening socket with it, is released only by the
    last thread out. Probing the port in that window found it still accepting
    (CI at f9ec51e5). A task listed under /proc/<pid>/task in 'Z' or 'X' has
    passed its fd release; a vanished task or directory has been reaped.
    """
    try:
        tids = os.listdir('/proc/%d/task' % pid)
    except (FileNotFoundError, ProcessLookupError):
        return False
    for tid in tids:
        try:
            state = Path('/proc/%d/task/%s/stat' % (pid, tid)).read_text().rsplit(')', 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            continue  # reaped between the listing and the read
        if state not in ('Z', 'X'):
            return True
    return False


class SavedMqttState(unittest.TestCase):
    """Execute the shipped verifier through check 2 against a completed offline run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.web = self.root / 'www'
        (self.web / 'static/js').mkdir(parents=True)
        self.version = '1.0.7.33'
        (self.web / 'static/js/app.js').write_text('APP_VERSION="' + self.version + '"')
        for name in ('index.html', 'login.html'):
            (self.web / name).write_text('?v=' + self.version)
        name = 'install-offline-' + self.version
        self.baseline = self.root / (name + '.svc-before')
        self.baseline.write_text('nginx active\nsa02m-modbus-mqtt inactive\n')
        (self.web / 'VERSION').write_text(self.version)
        self.runner = self.root / 'runner.version'
        self.runner.write_text(self.version)
        self.log = self.root / (name + '-a1b2c3d.wrapper.log')
        self.write_log()
        self.script = self.root / 'verify.sh'
        src = SCRIPT.read_text().split('# 3 no 0-byte unit fragment', 1)[0]
        src = src.replace('/var/www/network_config', str(self.web)).replace('/root/', str(self.root) + '/')
        src = src.replace('/var/lib/sa02m-update/runner.version', str(self.runner))
        src = src.replace('EVIDENCE_UID=0', 'EVIDENCE_UID=' + str(os.getuid()))
        self.script.write_text(src + '\nexit "$F"\n')
        systemctl = self.root / 'systemctl'
        systemctl.write_text('''#!/bin/bash
case "$1" in
list-unit-files) echo "$2 enabled" ;;
show) echo "${MQTT_LOAD:-loaded}" ;;
is-active) case "$2" in
    sa02m-modbus-mqtt) echo "${MQTT_STATE:-inactive}";;
    nginx) echo "${NGINX_STATE:-active}";;
    *) echo active;;
esac ;;
esac
''')
        systemctl.chmod(0o755)

    def write_log(self):
        stamp = subprocess.check_output(['stat', '-c', '%d:%i:%z', self.baseline],
                                        env={**os.environ, 'TZ': 'UTC0', 'LC_ALL': 'C'}, text=True).strip()
        import hashlib
        digest = hashlib.sha256(self.baseline.read_bytes()).hexdigest()
        wrapper = (ROOT / 'scripts/offline-full-update.sh').read_text()
        finish = wrapper.split('# ── Step 7: post-checks ─', 1)[1].split('\n', 1)[1]
        finish = finish.replace('/var/lib/sa02m-update/runner.version', str(self.runner))
        receipt = subprocess.check_output(['bash', '-eu', '-c', '''
post_checks() { MQTT_STOP_PRESERVED=1; }
log() { printf '[08:03:02] [%s] %s\\n' "$1" "$2"; }
''' + finish], env={**os.environ, 'INSTALL_RC': '0', 'TARGET_VER': self.version, 'WEB_ROOT': str(self.web),
                    'snapshot_stamp': stamp, 'snapshot_hash': digest}, text=True)
        self.log_body = (
            '[08:00:00] [INFO] offline-refresh begin ' + self.version + '\n'
            '[08:00:00] [INFO] Установлено: 1.0.7.32  →  цель: ' + self.version + '  (дерево: /tmp/repo)\n'
            '[08:00:01] [INFO] svc-before ' + stamp + ' ' + digest + '\n'
            '[08:03:00] [OK] install.sh завершился, rc=0\n'
            '  PASS  служба sa02m-modbus-mqtt           не был запущен до обновления — состояние сохранено\n'
            '[08:03:01] [OK] ИТОГ: PASS (13/13) — обновление до ' + self.version + ' выполнено\n'
            + receipt)
        self.log.write_text(self.log_body)

    def run_check(self, *args, **states):
        env = {**os.environ, 'PATH': str(self.root) + ':' + os.environ['PATH'], **states}
        return subprocess.run(['bash', self.script, self.version, *args], env=env, capture_output=True, text=True)

    def test_current_successful_baseline_exempts_only_inactive_mqtt_when_requested(self):
        self.assertIn('FAIL 2 core units:', self.run_check().stdout)
        flag = '--preserve-mqtt-state'
        result = self.run_check(flag)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('sa02m-modbus-mqtt=inactive (preserved', result.stdout)
        for states in ({'NGINX_STATE': 'inactive'}, {'MQTT_STATE': 'failed'}, {'MQTT_STATE': 'unknown'},
                       {'MQTT_LOAD': 'masked'}):
            with self.subTest(states=states):
                self.assertIn('FAIL 2 core units:', self.run_check(flag, **states).stdout)

    def test_offline_snapshot_distinguishes_loaded_inactive_mqtt_from_an_absent_unit(self):
        wrapper = (ROOT / 'scripts/offline-full-update.sh').read_text()
        capture = wrapper.split('# Service-state snapshot BEFORE the install:', 1)[1].split('nohup setsid env', 1)[0]
        capture = '\n'.join(line for line in capture.splitlines()[1:] if not line.startswith('#'))
        script = '''SVC_BEFORE=$1
CORE_SERVICES="nginx sa02m-modbus-mqtt"
systemctl() {
    case "$1" in
        is-active) if [ "$2" = nginx ]; then echo active; else echo inactive; fi ;;
        show)
            [ "$BEFORE_LOAD" != error ] || return 1
            printf 'LoadState=%s\\nActiveState=%s\\n' "$BEFORE_LOAD" "$BEFORE_ACTIVE"
            ;;
    esac
}
log() { printf '[08:00:01] [%s] %s\\n' "$1" "$2"; }
''' + capture
        for load, active, expected in (('loaded', 'inactive', True), ('not-found', 'inactive', False),
                                       ('error', 'inactive', False), ('masked', 'inactive', False),
                                       ('loaded', 'active', False)):
            with self.subTest(load=load, active=active):
                env = {**os.environ, 'BEFORE_LOAD': load, 'BEFORE_ACTIVE': active}
                marker = subprocess.check_output(['bash', '-ec', script, 'capture', self.baseline],
                                                 text=True, env=env).strip()
                self.assertIn('[INFO] svc-before ', marker)
                self.runner.touch()
                self.write_log()
                self.log.write_text(re.sub(r'(?m)^.*\[INFO\] svc-before .+$', lambda _: marker, self.log_body))
                result = self.run_check('--preserve-mqtt-state')
                self.assertEqual(result.returncode, 0 if expected else 1, result.stdout + result.stderr)
                self.assertIn('nginx active', self.baseline.read_text().splitlines())

    def test_incomplete_stale_unsafe_or_mismatched_evidence_fails(self):
        flag = '--preserve-mqtt-state'
        for body in (self.log_body.replace('ИТОГ: PASS', 'ИТОГ: FAIL'),
                     self.log_body.replace('rc=0', 'rc=1'),
                     self.log_body + '[09:00:00] [INFO] offline-refresh begin ' + self.version + '\n'):
            self.log.write_text(body)
            self.assertIn('FAIL 2 core units:', self.run_check(flag).stdout)
        self.write_log()
        os.utime(self.log, (1, 1))
        self.assertIn('FAIL 2 core units:', self.run_check(flag).stdout)
        for state in ('active', 'unknown', 'inactive\nsa02m-modbus-mqtt active'):
            self.baseline.write_text('sa02m-modbus-mqtt ' + state + '\n')
            self.runner.touch()
            self.write_log()
            self.assertIn('FAIL 2 core units:', self.run_check(flag).stdout)
        self.baseline.write_text('sa02m-modbus-mqtt inactive\n')
        self.runner.touch()
        self.write_log()
        self.baseline.chmod(0o666)
        self.runner.touch()
        self.write_log()
        self.assertIn('FAIL 2 core units:', self.run_check(flag).stdout)
        self.baseline.unlink()
        self.assertIn('FAIL 2 core units:', self.run_check(flag).stdout)

    def test_read_only_status_does_not_erase_or_create_installation_evidence(self):
        flag = '--preserve-mqtt-state'
        header = next(line for line in self.log_body.splitlines() if 'Установлено:' in line)
        table = '\n'.join(line for line in self.log_body.splitlines() if 'PASS' in line) + '\n'
        observed = '[09:00:00] [INFO] offline-refresh observe ' + self.version + '\n' + header + '\n' + table
        self.log.write_text(self.log_body + observed)
        self.assertEqual(self.run_check(flag).returncode, 0)
        for partial in ('', self.log_body.split('ИТОГ:')[0]):
            self.log.write_text('[09:00:00] [INFO] offline-refresh begin ' + self.version + '\n' + partial + observed)
            self.assertIn('FAIL 2 core units:', self.run_check(flag).stdout)
        self.log.write_text(self.log_body + observed)
        self.runner.touch()
        self.log.touch()
        self.assertIn('FAIL 2 core units:', self.run_check(flag).stdout)


class FixtureSafety(unittest.TestCase):
    def test_operator_rows_under_the_fixture_ids_survive_a_full_unpatched_run(self):
        # The defect this pins: the old CLEAN block deleted the operator's own
        # scenarios whenever their ids collided with the fixture ids (vtmo /
        # vcap / vscn). Here the live store holds exactly such rows and the
        # SHIPPED fixture block runs end to end with NOTHING patched but the
        # store path — bytes, mtime and the live journal must all be untouched.
        module = fixture_module()
        with tempfile.TemporaryDirectory() as name:
            live = Path(name) / 'scenarios.json'
            doc = store.empty_doc()
            doc['scenarios'] = [{'id': sid, 'name': 'operator ' + sid, 'type': 'code', 'enabled': True,
                                 'code': 'Vars.set("keep", 1)'} for sid in ('vtmo', 'vcap', 'vscn')]
            store.save(doc, str(live))
            before = live.read_bytes()
            mtime = live.stat().st_mtime_ns
            output = io.StringIO()
            with mock.patch.object(store, 'DEFAULT_PATH', str(live)), contextlib.redirect_stdout(output):
                module['run_fixture_checks']()
            self.assertEqual(live.read_bytes(), before)
            self.assertEqual(live.stat().st_mtime_ns, mtime)
            self.assertFalse(Path(store.journal_path(str(live))).exists(), 'the live journal was written')
            names = [s['name'] for s in json.loads(live.read_text())['scenarios']]
            self.assertEqual(names, ['operator vtmo', 'operator vcap', 'operator vscn'])
            self.assertEqual(len([v for v in output.getvalue().splitlines() if v.startswith('PASS ')]), 6)

    def test_all_checks_use_private_store_and_real_installed_components(self):
        module = fixture_module()
        with tempfile.TemporaryDirectory() as name:
            live = Path(name) / 'scenarios.json'
            live.write_text(json.dumps({'scenarios': [{'id': k, 'name': 'operator'} for k in ('vtmo','vcap','vscn')]}))
            before = live.read_bytes()
            original = store.apply_command
            paths = []
            def guarded(body, path=store.DEFAULT_PATH):
                self.assertNotEqual(Path(path), live)
                self.assertTrue(Path(path).parent.name.startswith('sa02m-release-check-'))
                paths.append(path)
                return original(body, path)
            output = io.StringIO()
            with mock.patch.object(store, 'DEFAULT_PATH', str(live)), mock.patch.object(store, 'apply_command', guarded):
                with contextlib.redirect_stdout(output):
                    module['run_fixture_checks']()
            self.assertEqual(live.read_bytes(), before)
            self.assertEqual(len(paths), 3)
            lines = output.getvalue().splitlines()
            self.assertEqual(len([v for v in lines if v.startswith('PASS ')]), 6, lines)
            self.assertFalse(any(v.startswith(('FAIL ', 'SKIP ')) for v in lines), lines)
            self.assertFalse(Path(paths[0]).parent.exists())

    def test_cancellation_removes_only_private_store_and_closes_listener(self):
        module = fixture_module()
        for target in ('store', 'listener'):
            phases = {}
            def phase(name, value):
                phases[name] = value
                if name == target:
                    raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
                module['run_fixture_checks'](phase)
            self.assertFalse(Path(phases['store']).exists())
            if 'listener' in phases:
                with socket.socket() as probe:
                    self.assertNotEqual(probe.connect_ex(('127.0.0.1', phases['listener'])), 0)

    def test_shell_parent_sigkill_ends_worker_and_listener(self):
        # Execute the shipped worker code. The parent is only a process host;
        # killing it must close the worker's listener without relying on traps.
        worker = """
import os,sys,signal,json
from pathlib import Path
sys.path[:0]=[sys.argv[2]+'/opt/sa02m-rules',sys.argv[2]+'/opt/sa02m-alice']
code=Path(sys.argv[1]).read_text().split('# BEGIN ISOLATED FIXTURES\\n',1)[1].split('# END ISOLATED FIXTURES',1)[0]
ns={'__name__':'fixture_test'}
exec(compile(code,sys.argv[1],'exec'),ns)
parent=os.getppid()
ticks=Path('/proc/%d/stat'%parent).read_text().rsplit(')',1)[1].split()[19]
ns['watch_parent'](parent,ticks)
state={}
def phase(name,value):
 state[name]=value
 if name=='listener':
  print('READY '+json.dumps({'pid':os.getpid(),'port':value,'directory':state['store']}),flush=True)
  signal.pause()
ns['run_fixture_checks'](phase)
"""
        parent_code = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2],sys.argv[3]]); time.sleep(120)'
        parent = subprocess.Popen([sys.executable, '-c', parent_code, worker, str(SCRIPT), str(ROOT)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        ready = None
        try:
            # READY is printed at the 'listener' phase, which the worker reaches
            # only after the sandbox-deadline case (check 5) has run its full
            # engine.RUN_S budget; measured 30.1 s to that phase under WSL.
            deadline = time.monotonic() + engine.RUN_S + 30
            output = []
            while time.monotonic() < deadline:
                if not select.select([parent.stdout], [], [], 1)[0]:
                    continue
                line = parent.stdout.readline()
                output.append(line)
                if line.startswith('READY '):
                    ready = json.loads(line[6:])
                    break
            self.assertIsNotNone(ready, ''.join(output))
            parent.kill()
            parent.wait(timeout=3)
            deadline = time.monotonic() + 5
            alive = True
            while time.monotonic() < deadline:
                alive = thread_group_running(ready['pid'])
                if not alive:
                    break
                time.sleep(0.05)
            self.assertFalse(alive, 'worker survived shell SIGKILL')
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex(('127.0.0.1', ready['port'])), 0)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()
            if ready:
                try:
                    os.kill(ready['pid'], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                private = Path(ready['directory']).resolve()
                self.assertEqual(private.parent, Path(tempfile.gettempdir()).resolve())
                self.assertTrue(private.name.startswith('sa02m-release-check-'))
                shutil.rmtree(private, ignore_errors=True)
            parent.stdout.close()

if __name__ == '__main__':
    if sys.platform != 'linux' or not Path('/proc/self/stat').exists():
        reason = 'board-verifier-safety: Linux /proc and pidfd required; skipped on ' + sys.platform
        print('SKIP  ' + reason)
        if os.environ.get('QUALITY_SKIP_LOG'):
            with open(os.environ['QUALITY_SKIP_LOG'], 'a', encoding='utf-8') as fh:
                fh.write(reason + '\n')
        sys.exit(77)
    unittest.main()
