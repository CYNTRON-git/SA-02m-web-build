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
