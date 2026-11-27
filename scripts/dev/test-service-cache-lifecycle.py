#!/usr/bin/env python3
"""Real Linux process/flock regression for the service-list lifecycle.

Only paths and the manager are retargeted. Root and /proc are real; no board
or host services are used. The legacy extraction keeps the first regression
executable against the pre-repair helper for RED evidence.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import sys

ROOT = Path(__file__).resolve().parents[2]
CTL = ROOT / "etc/sa02m-web-service-ctl.sh"
MODULE = ROOT / "usr/local/lib/sa02m-service-cache.sh"


@unittest.skipUnless(os.geteuid() == 0 and Path("/proc/self/fd").exists(), "real Linux root required")
class CacheLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="service-cache-")
        self.root = Path(self.tmp.name)
        self.result = self.root / "result"
        self.result.mkdir(mode=0o755)
        self.epoch = self.result / 'owned-v1'
        self.epoch.write_bytes(Path('/proc/sys/kernel/random/boot_id').read_bytes())
        self.epoch.chmod(0o644)
        self.cache = self.result / "list.json"
        self.gen = self.result / "list.gen"
        self.lock = self.result / "list.lock"
        self.jobs = self.root / 'jobs'
        self.jobs.write_text('inactive')
        self.manager = self.root / 'systemctl'
        self.manager.write_text('#!/usr/bin/python3\nimport sys\nfrom pathlib import Path\n'
                                f'state=Path({str(self.jobs)!r}).read_text().strip()\n'
                                f'with Path({str(self.root / "queries")!r}).open("a") as log: log.write("query\\n")\n'
                                'if state == "unreadable": sys.exit(1)\n'
                                'for u in sys.argv[3:]:\n'
                                ' print("Id="+u+"\\nNames="+u+"\\nActiveState="+state+"\\nJob="+("123" if state == "activating" else "")+"\\n")\n')
        self.manager.chmod(0o755)
        self.library = self.root / "library.sh"
        source = MODULE.read_text() if MODULE.exists() else CTL.read_text().split('SVC_LIST_CACHE=', 1)[1].split('emit_result() {', 1)[0]
        if not MODULE.exists():
            source = 'SVC_LIST_CACHE=' + source
        self.library.write_text(f'RESULT_DIR={self.result}\nSC={self.manager}\nLOG={self.root}/log\n' + source)
        self.children = []

    def tearDown(self):
        for child in self.children:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=3)
        self.tmp.cleanup()

    def run_shell(self, body, **kwargs):
        return subprocess.run(["/bin/sh", "-c", '. "$1"; ' + body, "_", str(self.library)],
                              capture_output=True, text=True, timeout=12, **kwargs)

    def operation(self, name='first', units='mosquitto.service', body='sleep 30'):
        ready = self.root / (name + '.ready')
        child = subprocess.Popen(['/bin/sh', '-c', '. "$1"; svc_cache_begin "$2" || exit; '
                                  'echo "$SA02M_CACHE_LEASE" >"$3"; ' + body,
                                  '_', str(self.library), units, str(ready)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.children.append(child)
        end = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(ready.exists(), f'registration failed: {child.poll()}')
        return child, ready.read_text().strip()

    def publish(self):
        result = self.run_shell("cmd_list() { echo '{\"fresh\":true}'; }; svc_list_publish")
        self.assertEqual(result.stdout, '{"fresh":true}\n', result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def kill_operation(self, child):
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=3)

    def test_held_global_lock_never_publishes_or_invalidates(self):
        self.cache.write_text('{"old":true}\n')
        self.gen.write_text('7\n')
        self.lock.touch(mode=0o600)
        with self.lock.open('r+') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.run_shell("cmd_list() { echo '{\"fresh\":true}'; }; svc_list_publish")
            self.assertEqual(result.stdout, '{"fresh":true}\n', result.stderr)
            self.assertEqual(self.cache.read_text(), '{"old":true}\n', 'publication without lock')
            result = self.run_shell('svc_list_cache_invalidate')
            self.assertNotEqual(result.returncode, 0, 'invalidation without lock reported success')
            self.assertEqual(self.gen.read_text(), '7\n')
            self.assertTrue(self.cache.exists())

    def test_unknown_boot_epoch_never_registers_or_publishes(self):
        for value in (None, '00000000-0000-0000-0000-000000000000\n'):
            with self.subTest(value=value):
                self.epoch.unlink(missing_ok=True)
                if value:
                    self.epoch.write_text(value)
                self.publish()
                self.assertFalse(self.cache.exists())
                result = self.run_shell('svc_cache_begin nginx.service && touch "$1/effect"')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.result / 'operations').exists())

    def test_same_and_different_service_operations_are_independent(self):
        self.publish()
        first, a = self.operation()
        second, b = self.operation('second')
        third, c = self.operation('third', 'nginx.service')
        self.assertEqual(len({a, b, c}), 3)
        self.assertEqual(self.gen.read_text(), '3\n')
        self.publish()
        self.assertFalse(self.cache.exists())
        for child in (first, second):
            self.kill_operation(child)
        self.publish()
        self.assertFalse(self.cache.exists())
        self.assertEqual(len(list((self.result / 'operations').iterdir())), 1)
        self.kill_operation(third)
        self.publish()
        self.assertTrue(self.cache.exists())
        self.assertEqual(self.gen.read_text(), '5\n')

    def test_client_death_does_not_cancel_pid1_job(self):
        child, lease = self.operation()
        self.jobs.write_text('activating')
        self.kill_operation(child)
        self.publish()
        self.assertFalse(self.cache.exists())
        self.assertTrue((self.result / 'operations' / lease).exists())
        self.jobs.write_text('unreadable')
        self.publish()
        self.assertFalse(self.cache.exists())
        self.jobs.write_text('inactive')
        self.publish()
        self.assertTrue(self.cache.exists())
        self.assertFalse((self.result / 'operations' / lease).exists())

    def test_sigkill_parent_keeps_live_child_lease(self):
        child, lease = self.operation(body=f'sleep 30 & echo $! > {self.root}/child; wait')
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=3)
        self.publish()
        self.assertFalse(self.cache.exists())
        self.assertTrue((self.result / 'operations' / lease).exists())
        os.killpg(child.pid, signal.SIGKILL)
        time.sleep(0.05)
        self.publish()
        self.assertTrue(self.cache.exists())

    def test_completion_retires_only_owner_and_exact_generations(self):
        self.publish()
        commands = ['svc_cache_begin mosquitto.service && svc_cache_finish'] * 4
        children = [subprocess.Popen(['/bin/sh', '-c', '. "$1"; ' + command, '_', str(self.library)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for command in commands]
        for child in children:
            out, err = child.communicate(timeout=12)
            self.assertEqual(child.returncode, 0, (out, err))
        self.assertEqual(self.gen.read_text(), '8\n')
        self.assertFalse(self.cache.exists())
        self.publish()
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.result / 'operations').stat().st_mode & 0o777, 0o700)

    def test_lists_spanning_a_mutation_cannot_publish(self):
        result = self.run_shell('cmd_list() { svc_cache_begin mosquitto.service && svc_cache_finish; '
                                'echo \'{"fresh":true}\'; }; svc_list_publish')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.cache.exists())
        self.publish()
        self.assertTrue(self.cache.exists())

    def test_missing_flock_refuses_registration_without_side_effect(self):
        bindir = self.root / 'bin'
        bindir.mkdir()
        for name in ('python3', 'mktemp', 'rm'):
            (bindir / name).symlink_to(shutil.which(name))
        result = self.run_shell(f'svc_cache_begin mosquitto.service && echo bad >{self.root}/effect',
                                env={**os.environ, 'PATH': str(bindir)})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'effect').exists())

    def test_descriptor_collision_and_counterfeit_environment_refused(self):
        result = self.run_shell('exec 8</dev/null; svc_cache_begin mosquitto.service')
        self.assertNotEqual(result.returncode, 0)
        result = self.run_shell('svc_cache_begin mosquitto.service',
                                env={**os.environ, 'SA02M_CACHE_FD': '8', 'SA02M_CACHE_LEASE': 'lease.' + 'a'*16})
        self.assertNotEqual(result.returncode, 0)

    def test_join_cannot_retire_parent_and_foreign_open_is_rejected(self):
        body = ('svc_cache_begin mosquitto.service || exit; '
                'SA02M_CACHE_JOIN=1 /bin/sh -c \' . "$1"; svc_cache_begin mosquitto.service && svc_cache_finish\' _ "$1" || exit; '
                'test -f "$RESULT_DIR/operations/$SA02M_CACHE_LEASE" || exit; '
                'svc_cache_finish')
        result = self.run_shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        child, lease = self.operation()
        path = self.result / 'operations' / lease
        result = self.run_shell(f'exec 8<>"{path}"; SA02M_CACHE_LEASE={lease}; SA02M_CACHE_FD=8; svc_cache_state verify')
        self.assertNotEqual(result.returncode, 0, 'foreign OFD accepted')
        self.assertEqual(self.gen.read_text(), '3\n')

    def test_unsafe_records_and_malformed_generation_fail_closed(self):
        self.publish()
        records = self.result / 'operations'
        for kind in ('symlink', 'hardlink', 'owner', 'size', 'unknown'):
            with self.subTest(kind=kind):
                path = records / ('lease.' + 'a'*16)
                if kind == 'symlink':
                    path.symlink_to(self.gen)
                elif kind == 'hardlink':
                    os.link(self.gen, path)
                else:
                    path.write_text('x'*4097 if kind == 'size' else '{}')
                    path.chmod(0o600)
                    if kind == 'owner':
                        os.chown(path, 65534, 65534)
                    if kind == 'unknown':
                        path = path.rename(records / 'unexpected')
                result = self.run_shell('svc_cache_begin mosquitto.service')
                self.assertNotEqual(result.returncode, 0, kind)
                path.unlink()
        self.gen.write_text('bad\n')
        result = self.run_shell('svc_cache_begin mosquitto.service')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.gen.read_text(), 'bad\n')

    def test_final_bookkeeping_failure_retains_operation(self):
        result = self.run_shell('svc_cache_begin mosquitto.service || exit; '
                                'mkdir "$RESULT_DIR/list.gen.block"; mv "$RESULT_DIR/list.gen" "$RESULT_DIR/list.gen.saved"; '
                                'mv "$RESULT_DIR/list.gen.block" "$RESULT_DIR/list.gen"; svc_cache_finish')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(list((self.result / 'operations').iterdir())), 1)
        self.publish()
        self.assertFalse(self.cache.exists())

    def test_no_manager_queries_for_empty_warm_registry(self):
        self.publish()
        self.publish()
        self.assertFalse((self.root / 'queries').exists())

    def test_registry_bound_and_one_batched_stale_query(self):
        self.publish()
        records = self.result / 'operations'
        for index in range(64):
            path = records / ('lease.' + f'{index:016x}')
            path.write_text(json.dumps({'version': 1, 'owner': os.getpid(), 'units': ['mosquitto.service']}))
            path.chmod(0o600)
        result = self.run_shell('svc_cache_begin mosquitto.service')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.gen.read_text(), '0\n')
        extra = records / ('lease.' + 'f'*16)
        extra.write_text(next(records.iterdir()).read_text())
        extra.chmod(0o600)
        self.assertNotEqual(self.run_shell('svc_cache_begin mosquitto.service').returncode, 0)
        extra.unlink()
        self.publish()
        self.assertEqual((self.root / 'queries').read_text().splitlines(), ['query'])
        self.assertEqual(list(records.iterdir()), [])
        self.assertEqual(self.gen.read_text(), '1\n', 'owner PID liveness must not substitute for kernel lease')

    def test_nonroot_real_identity_cannot_register(self):
        self.root.chmod(0o755)
        result = self.run_shell('svc_cache_begin mosquitto.service', preexec_fn=lambda: os.setuid(65534))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.result / 'operations').exists())

    def test_failed_publication_never_replaces_old_cache(self):
        self.publish()
        self.cache.write_text('{"old":true}\n')
        text = self.library.read_text()
        needle = '        os.replace(name, path)'
        self.assertEqual(text.count(needle), 1)
        self.library.write_text(text.replace(needle, '        if path.name == "list.json": raise OSError("injected rename failure")\n' + needle))
        result = self.publish()
        self.assertIn('injected rename failure', result.stderr)
        self.assertEqual(self.cache.read_text(), '{"old":true}\n')
        self.assertEqual(list(self.result.glob('.list.json.*')), [])

    def test_failed_registration_has_zero_mutation_side_effects(self):
        text = self.library.read_text()
        needle = '            os.rename(path, records / path.name[1:])'
        self.assertEqual(text.count(needle), 1)
        self.library.write_text(text.replace(needle, '            raise OSError("injected registration failure")'))
        result = self.run_shell(f'svc_cache_begin mosquitto.service && echo bad >{self.root}/effect')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'effect').exists())
        self.assertEqual(list((self.result / 'operations').iterdir()), [])
        self.assertFalse(self.cache.exists())

    def legacy_retire(self):
        text = self.library.read_text().replace('RESULT_DIR=/var/run/sa02m-svcctl', 'RESULT_DIR=' + str(self.result))
        self.library.write_text(text)
        return subprocess.run(['/bin/sh', str(self.library), '--retire-legacy-cache'],
                              capture_output=True, text=True, timeout=5)

    def test_legacy_first_upgrade_retires_only_public_cache(self):
        self.cache.write_text('{"old":true}\n')
        self.cache.chmod(0o644)
        self.lock.touch(mode=0o600)
        inode = self.lock.stat().st_ino
        result = self.legacy_retire()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.gen.exists())
        self.assertFalse((self.result / 'operations').exists())
        self.assertEqual(self.lock.stat().st_ino, inode)
        self.publish()
        self.assertTrue(self.cache.exists())
        before = self.cache.read_bytes()
        self.assertEqual(self.legacy_retire().returncode, 0)
        self.assertEqual(self.cache.read_bytes(), before, 'new protocol must be untouched')

    def test_legacy_migration_preserves_generation_and_rejects_unsafe_state(self):
        self.cache.write_text('{"old":true}\n')
        self.cache.chmod(0o644)
        self.gen.write_text('17\n')
        self.gen.chmod(0o600)
        self.assertEqual(self.legacy_retire().returncode, 0)
        self.assertEqual(self.gen.read_text(), '17\n')
        self.cache.write_text('{"old":true}\n')
        self.gen.write_text('broken\n')
        self.assertNotEqual(self.legacy_retire().returncode, 0)
        self.assertTrue(self.cache.exists())
        self.gen.write_text('17\n')
        self.cache.unlink()
        self.cache.symlink_to(self.gen)
        self.assertNotEqual(self.legacy_retire().returncode, 0)
        self.assertTrue(self.cache.is_symlink())
        self.assertEqual(self.gen.read_text(), '17\n')

    def test_legacy_migration_does_not_remove_private_lease(self):
        self.publish()
        self.operation()
        records = list((self.result / 'operations').iterdir())
        generation = self.gen.read_text()
        self.assertEqual(self.legacy_retire().returncode, 0)
        self.assertEqual(list((self.result / 'operations').iterdir()), records)
        self.assertEqual(self.gen.read_text(), generation)


if __name__ == '__main__':
    if os.geteuid() != 0 or not Path('/proc/self/fd').exists():
        print('PARTIAL: real Linux root is required for the cache lifecycle suite')
        sys.exit(77)
    unittest.main(verbosity=2)
