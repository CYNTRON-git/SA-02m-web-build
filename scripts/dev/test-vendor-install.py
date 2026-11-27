#!/usr/bin/env python3
# comment-mutation-proof-exempt: imports and executes the shipped verifier and real filesystem swaps; archive attacks, process limits and fault injection must fail behaviorally, independent of source comments.
"""Isolated archive/transport/transaction primitives; no live service actions."""
import hashlib
import contextlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import threading
import unittest
import sys
import urllib.request
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
UNIT_TEMPLATE = b'[Service]\nExecStart=/usr/bin/node /usr/lib/node_modules/node-red/red.js --userDir /home/nodered/.node-red\n'
spec = importlib.util.spec_from_file_location('vendor_install', ROOT / 'etc/sa02m-vendor-install.py')
vendor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vendor)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = self.root / 'payload.tar.gz'
        self.dest = self.root / 'out'

    def tearDown(self):
        self.temp.cleanup()

    def make(self, entries):
        with tarfile.open(self.archive, 'w:gz') as tf:
            for name, kind, value in entries:
                info = tarfile.TarInfo(name)
                if kind == 'file':
                    info.size = len(value)
                    info.mode = 0o644
                    tf.addfile(info, io.BytesIO(value))
                elif kind == 'dir':
                    info.type = tarfile.DIRTYPE
                    tf.addfile(info)
                else:
                    info.type = {'link': tarfile.SYMTYPE, 'hard': tarfile.LNKTYPE, 'fifo': tarfile.FIFOTYPE}[kind]
                    info.linkname = value
                    tf.addfile(info)

    def unpack(self):
        return vendor.safe_unpack(self.archive, self.dest, 'node-red', time.monotonic() + 5)

    def rejects(self, entries):
        self.make(entries)
        with self.assertRaises(vendor.InstallError):
            self.unpack()
        self.assertFalse(self.dest.exists(), 'Index rejection must precede any extraction')

    def test_internal_links_and_bytes(self):
        self.make([('node-red/red.js', 'file', b'hello'), ('node-red/bin/nr', 'link', '../red.js')])
        result = self.unpack()
        self.assertEqual((result / 'bin/nr').read_bytes(), b'hello')
        self.assertEqual(os.readlink(result / 'bin/nr'), '../red.js')

    def test_nested_traversal(self):
        self.rejects([('node-red/../../escape', 'file', b'evil')])

    def test_absolute_member(self):
        self.rejects([('/node-red/red.js', 'file', b'evil')])

    def test_duplicate(self):
        self.rejects([('node-red/red.js', 'file', b'a'), ('node-red/red.js', 'file', b'b')])

    def test_escaping_link(self):
        self.rejects([('node-red/escape', 'link', '../../escape')])

    def test_link_ancestor(self):
        self.rejects([('node-red/dir', 'link', 'other'), ('node-red/dir/file', 'file', b'evil')])

    def test_hard_link_and_fifo(self):
        for kind in ('hard', 'fifo'):
            with self.subTest(kind=kind):
                self.rejects([('node-red/red.js', kind, 'node-red/target')])

    def test_link_cycle(self):
        self.make([('node-red/a', 'link', 'b'), ('node-red/b', 'link', 'a')])
        with self.assertRaises(vendor.InstallError):
            self.unpack()

    def test_raw_expanded_limit(self):
        self.make([('node-red/red.js', 'file', b'x' * 100)])
        with mock.patch.object(vendor, 'MAX_EXPANDED', 100), self.assertRaises(vendor.InstallError):
            self.unpack()
        self.assertFalse(self.dest.exists())

    def test_member_limit(self):
        self.make([('node-red/a', 'file', b'a'), ('node-red/b', 'file', b'b')])
        with mock.patch.object(vendor, 'MAX_MEMBERS', 1), self.assertRaises(vendor.InstallError):
            self.unpack()
        self.assertFalse(self.dest.exists())

    def test_expired_deadline(self):
        self.make([('node-red/red.js', 'file', b'hello')])
        with self.assertRaisesRegex(vendor.InstallError, 'payload_timeout'):
            vendor.safe_unpack(self.archive, self.dest, 'node-red', time.monotonic() - 1)

    def entry(self, data):
        return {'filename': 'bundle.tgz', 'root': 'node-red', 'size': len(data),
                'sha256': hashlib.sha256(data).hexdigest(), 'url': 'https://github.com/example/release/bundle.tgz'}

    def test_online_offline_exact_same_bytes(self):
        data = b'verified immutable release'
        entry = self.entry(data)
        local = self.root / entry['filename']
        local.write_bytes(data)
        self.assertEqual(vendor.verified_archive(entry, self.root, self.root / 'offline', time.monotonic() + 5), 'offline')
        local.unlink()
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        calls = []
        class LoopbackFixtureTransport:
            def open(self, url, timeout):
                calls.append(url)
                return urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/fixture', timeout=timeout)
        try:
            self.assertEqual(vendor.verified_archive(entry, self.root, self.root / 'online', time.monotonic() + 5, LoopbackFixtureTransport()), 'online')
            self.assertEqual((self.root / 'offline').read_bytes(), (self.root / 'online').read_bytes())
            self.assertEqual(calls, [entry['url']])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_wrong_hash_size_missing_and_retry(self):
        entry = self.entry(b'good')
        local = self.root / entry['filename']
        for data in (b'evil', b'shorter'):
            local.write_bytes(data)
            with self.assertRaises(vendor.InstallError):
                vendor.verified_archive(entry, self.root, self.dest, time.monotonic() + 5)
            self.assertFalse(self.dest.exists())
        local.unlink()
        entry['url'] = None
        with self.assertRaisesRegex(vendor.InstallError, 'payload_unavailable'):
            vendor.verified_archive(entry, self.root, self.dest, time.monotonic() + 5)
        local.write_bytes(b'good')
        vendor.verified_archive(entry, self.root, self.dest, time.monotonic() + 5)
        self.assertEqual(self.dest.read_bytes(), b'good')

    def test_symlink_source_rejected(self):
        entry = self.entry(b'good')
        (self.root / 'target').write_bytes(b'good')
        (self.root / entry['filename']).symlink_to('target')
        with self.assertRaises(vendor.InstallError):
            vendor.verified_archive(entry, self.root, self.dest, time.monotonic() + 5)

    def test_untrusted_urls(self):
        for url in ('http://github.com/file', 'https://evil.example/file', 'https://user@github.com/file', 'https://github.com:444/file'):
            with self.subTest(url=url), self.assertRaises(vendor.InstallError):
                vendor.validate_url(url)

    def test_swap_restore(self):
        target, staged = self.root / 'runtime', self.root / 'candidate'
        target.mkdir(); staged.mkdir()
        (target / 'version').write_text('old')
        (staged / 'version').write_text('new')
        swap = vendor.Swap(target)
        swap.replace(staged)
        self.assertEqual((target / 'version').read_text(), 'new')
        swap.rollback()
        self.assertEqual((target / 'version').read_text(), 'old')

    def test_manifest_types(self):
        for data in ({'schema': 1, 'packages': []}, {'schema': 1, 'packages': {'node-red': None}}):
            with mock.patch.object(vendor, 'trusted_file', return_value=json.dumps(data).encode()), self.assertRaises(vendor.InstallError):
                vendor.load_manifest()

    def test_process_output_and_time_bounds(self):
        system = vendor.System(time.monotonic() + 10)
        with self.assertRaisesRegex(vendor.InstallError, 'operation_output_limit'):
            system.run([sys.executable, '-c', 'print("x" * 3000000)'])
        started = time.monotonic()
        with self.assertRaisesRegex(vendor.InstallError, 'operation_timeout'):
            system.run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=.1)
        self.assertLess(time.monotonic() - started, 2)

    def test_wrong_arch_never_executes(self):
        binary = self.root / 'node'
        binary.write_bytes(b'not an ARM executable')
        system = mock.Mock()
        with self.assertRaisesRegex(vendor.InstallError, 'payload_arch_mismatch'):
            vendor.verify_node(binary, self.root, {'node_version': '22.23.5'}, system, True)
        system.run.assert_not_called()

    def test_engine_metadata_including_real_legacy_packages(self):
        self.assertEqual(vendor.node_engine_ranges({'engines': ['node >= 6.0']}), ['>= 6.0'])
        self.assertEqual(vendor.node_engine_ranges({'engines': ['node >= 0.2.0']}), ['>= 0.2.0'])
        self.assertEqual(vendor.node_engine_ranges({'engines': {'node': '^20.17.0 || >=22.9.0'}}), ['^20.17.0 || >=22.9.0'])
        self.assertEqual(vendor.node_engine_ranges({}), [])
        for engines in ('invalid', {'node': 22}, [22], ['node']):
            with self.subTest(engines=engines), self.assertRaises(vendor.InstallError):
                vendor.node_engine_ranges({'engines': engines})

    def test_bad_payload_has_zero_install_mutations(self):
        self.make([('node-red/../../escape', 'file', b'evil')])
        raw = self.archive.read_bytes()
        entry = self.entry(raw)
        (self.root / entry['filename']).write_bytes(raw)
        target = self.root / 'installed/node-red'
        target.parent.mkdir()
        system = mock.Mock(deadline=time.monotonic() + 540)
        system.unit_identity.return_value = {'Id': 'node-red.service', 'Names': 'node-red.service', 'LoadState': 'not-found', 'FragmentPath': ''}
        system.terminal.return_value = {'LoadState': 'loaded', 'UnitFileState': 'disabled', 'ActiveState': 'inactive'}
        package = {'version': '4.1.13', 'runtime': entry, 'unit': {'sha256': hashlib.sha256(UNIT_TEMPLATE).hexdigest()}}
        with mock.patch.object(vendor, 'TARGET', target), mock.patch.object(vendor, 'trusted_file', return_value=UNIT_TEMPLATE), mock.patch.object(vendor, 'trusted_directory'), mock.patch.object(vendor, 'account_info'), mock.patch.object(vendor, 'ensure_account') as account:
            with self.assertRaisesRegex(vendor.InstallError, 'payload_unsafe'):
                vendor.install(package, False, 'nodered', 1880, system, self.root)
            account.assert_not_called()
            system.service.assert_not_called()
            system.command.assert_not_called()
        self.assertFalse(target.exists())

    def test_masked_refusal_precedes_artifact_or_state_changes(self):
        system = mock.Mock(deadline=time.monotonic() + 540)
        system.unit_identity.return_value = {'Id': 'node-red.service', 'Names': 'node-red.service', 'LoadState': 'not-found', 'FragmentPath': ''}
        system.terminal.return_value = {'LoadState': 'masked', 'UnitFileState': 'masked', 'ActiveState': 'inactive'}
        for activate in (False, True):
            with mock.patch.object(vendor, 'verified_archive') as fetch:
                with self.assertRaisesRegex(vendor.InstallError, 'service_masked'):
                    vendor.install({}, activate, 'nodered', 1880, system)
                fetch.assert_not_called()
                system.command.assert_not_called()

    def test_unit_identity_requires_complete_manager_result(self):
        system = vendor.System(time.monotonic() + 10)
        complete = 'Id=node-red.service\nNames=node-red.service\nLoadState=not-found\nFragmentPath=\n'
        for code, output in [(0, complete), (1, complete), (0, 'LoadState=not-found\n'), (0, complete.replace('not-found', 'error'))]:
            with self.subTest(code=code, output=output), mock.patch.object(system, 'run', return_value=SimpleNamespace(returncode=code, stdout=output)):
                if code == 0 and output == complete:
                    self.assertEqual(system.unit_identity('node-red.service')['LoadState'], 'not-found')
                else:
                    with self.assertRaisesRegex(vendor.InstallError, 'manager_unknown'):
                        system.unit_identity('node-red.service')

    def test_manager_state_requires_invocation_and_directory_properties(self):
        values = {'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running',
                  'UnitFileState': 'enabled', 'Job': '0', 'MainPID': '123',
                  'ControlGroup': '/system.slice/nodered.service',
                  'WorkingDirectory': '/home/nodered/.node-red', 'InvocationID': 'a' * 32}
        system = vendor.System(time.monotonic() + 10)
        for missing, code in ((None, 0), ('InvocationID', 0), ('WorkingDirectory', 0), (None, 1)):
            output = '\n'.join(key + '=' + value for key, value in values.items() if key != missing)
            with self.subTest(missing=missing, code=code), \
                 mock.patch.object(system, 'run', return_value=SimpleNamespace(returncode=code, stdout=output)) as run:
                if missing or code:
                    with self.assertRaisesRegex(vendor.InstallError, 'manager_unknown'):
                        system.state()
                else:
                    self.assertEqual(system.state(), values)
                    requested = run.call_args.args[0][-1].removeprefix('--property=').split(',')
                    self.assertEqual(set(requested), set(values))

    def test_custom_user_validation_and_dropin(self):
        account = mock.Mock(pw_name='fixture-user', pw_uid=1234, pw_gid=4321, pw_dir='/home/fixture-user')
        with mock.patch.object(vendor.pwd, 'getpwnam', return_value=account):
            self.assertIs(vendor.account_info('fixture-user'), account)
            with self.assertRaisesRegex(vendor.InstallError, 'user_invalid'):
                vendor.account_info('bad\nUser=root')
        data = vendor.user_dropin(account, Path(account.pw_dir) / '.node-red', UNIT_TEMPLATE).decode()
        self.assertIn('User=fixture-user\nGroup=4321\n', data)
        self.assertIn('--userDir /home/fixture-user/.node-red', data)

    def test_manager_unknown_does_not_queue_another_action(self):
        system = vendor.System(time.monotonic() + 540)
        with mock.patch.object(system, 'run', side_effect=vendor.InstallError('operation_timeout')), mock.patch.object(system, 'terminal', side_effect=vendor.InstallError('manager_unknown')) as terminal:
            with self.assertRaisesRegex(vendor.InstallError, 'manager_unknown'):
                system.service('start', 'active')
            terminal.assert_called_once()

    @unittest.skipUnless(os.geteuid() == 0, 'root-owned lock checks require a disposable root environment')
    def test_concurrent_lock_and_unsafe_link(self):
        lock = self.root / 'install.lock'
        with vendor.install_lock(time.monotonic() + 1, lock):
            with self.assertRaisesRegex(vendor.InstallError, 'install_busy'):
                with vendor.install_lock(time.monotonic() + 1, lock):
                    self.fail('concurrent install admitted')
        lock.unlink()
        lock.symlink_to('outside')
        with self.assertRaisesRegex(vendor.InstallError, 'install_lock_unsafe'):
            with vendor.install_lock(time.monotonic() + 1, lock):
                self.fail('symlink lock admitted')

    @unittest.skipUnless(os.geteuid() == 0, 'real account ownership checks require disposable root')
    def test_account_keeps_credentials_and_existing_port_without_recursive_chown(self):
        home = self.root / 'home'
        home.mkdir()
        os.chown(home, 65534, 65534)
        directory = home / '.node-red'
        directory.mkdir()
        os.chown(directory, 65534, 65534)
        environment = directory / 'environment'
        environment.write_text('PORT=2444\nCUSTOM=keep\n')
        os.chown(environment, 65534, 65534)
        for name in ('flows.json', 'flows_cred.json', 'settings.js'):
            (directory / name).write_text('preserve ' + name)
        unrelated = home / 'unrelated'
        unrelated.write_text('root-owned unrelated file')
        account = SimpleNamespace(pw_name='fixture-user', pw_uid=65534, pw_gid=65534, pw_dir=str(home))
        with mock.patch.object(vendor, 'account_info', return_value=account), mock.patch.object(vendor, 'trusted_directory'):
            vendor.ensure_account('fixture-user', 1880, mock.Mock())
        self.assertIn('PORT=2444\nCUSTOM=keep\n', environment.read_text())
        self.assertEqual(environment.read_text().count('PORT='), 1)
        self.assertIn('NODE_OPTIONS=', environment.read_text())
        self.assertEqual(environment.stat().st_uid, 65534)
        for name in ('flows.json', 'flows_cred.json', 'settings.js'):
            self.assertEqual((directory / name).read_text(), 'preserve ' + name)
        self.assertEqual(unrelated.stat().st_uid, 0)

    @unittest.skipUnless(os.geteuid() == 0, 'real ownership checks require disposable root')
    def test_account_environment_link_attacks_do_not_write_outside(self):
        home = self.root / 'home'
        directory = home / '.node-red'
        directory.mkdir(parents=True)
        os.chown(home, 65534, 65534)
        os.chown(directory, 65534, 65534)
        outside = self.root / 'outside'
        outside.write_text('outside is unchanged')
        environment = directory / 'environment'
        account = SimpleNamespace(pw_name='fixture-user', pw_uid=65534, pw_gid=65534, pw_dir=str(home))
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    environment.symlink_to(outside)
                else:
                    os.link(outside, environment)
                with mock.patch.object(vendor, 'account_info', return_value=account), mock.patch.object(vendor, 'trusted_directory'):
                    with self.assertRaises((vendor.InstallError, OSError)):
                        vendor.ensure_account('fixture-user', 1880, mock.Mock())
                self.assertEqual(outside.read_text(), 'outside is unchanged')
                environment.unlink()


class ProductionHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.node = self.root / 'node'
        self.node.write_text('node fixture')
        self.user_dir = self.root / 'userdir'
        self.user_dir.mkdir()
        self.proc = self.root / 'proc'
        self.process = self.proc / '123'
        self.process.mkdir(parents=True)
        (self.process / 'exe').symlink_to(self.node)
        (self.process / 'cwd').symlink_to(self.user_dir)
        self.write_start_ticks('100')
        self.command = [str(self.node).encode(), str(vendor.TARGET / 'red.js').encode(), b'--userDir', str(self.user_dir).encode()]
        (self.process / 'cmdline').write_bytes(b'\0'.join(self.command) + b'\0')
        (self.process / 'cgroup').write_text('0::/system.slice/nodered.service\n')
        self.state = {'ActiveState': 'active', 'MainPID': '123', 'ControlGroup': '/system.slice/nodered.service',
                      'WorkingDirectory': str(self.user_dir), 'InvocationID': 'a' * 32}

    def write_start_ticks(self, ticks):
        # Field 22 is stable across process-title changes and changes on PID reuse.
        fields = ['S'] + ['0'] * 18 + [ticks] + ['0'] * 8
        (self.process / 'stat').write_text('123 (node fixture) ' + ' '.join(fields))

    def tearDown(self):
        self.temp.cleanup()

    def healthy(self, journal, code=0, records=None, states=None, after_journal=None):
        system = vendor.System(100)
        def path(value):
            return self.proc if str(value) == '/proc' else Path(value)
        def query(argv):
            output = journal
            if records is not None:
                filters = dict(item.split('=', 1) for item in argv if item.startswith('_') and '=' in item)
                output = '\n'.join(row['MESSAGE'] for row in records
                                   if all(row.get(key) == value for key, value in filters.items()))
            if after_journal:
                after_journal()
            return SimpleNamespace(returncode=code, stdout=output)
        with mock.patch.object(vendor, 'Path', side_effect=path), \
             mock.patch.object(system, 'terminal', **({'side_effect': states} if states else {'return_value': self.state})), \
             mock.patch.object(system, 'run', side_effect=query) as command, \
             mock.patch.object(vendor.time, 'monotonic', side_effect=[0, 0, 46]), mock.patch.object(vendor.time, 'sleep'):
            result = system.healthy(self.node, self.user_dir, 123.0)
            self.assertIn('@123.0', command.call_args.args[0])
            return result

    def test_latest_start_in_both_locales_is_required(self):
        for text in ('Started flows', 'Запущены потоки', 'Stopped flows\nStarted flows', 'Остановлены потоки\nЗапущены потоки'):
            with self.subTest(text=text):
                self.assertEqual(self.healthy(text), {'main_pid': 123, 'control_group': '/system.slice/nodered.service'})
        for text in ('', 'Server now running', 'Ожидание запуска', 'Stopped flows', 'Остановлены потоки'):
            with self.subTest(text=text), self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
                self.healthy(text)

    def test_latest_failures_in_both_locales_are_never_green(self):
        failures = ['Stopped flows', 'Остановлены потоки', 'Error loading credentials', 'Ошибка при загрузке учетных данных',
                    'Flows stopped in safe mode', 'Потоки остановлены в безопасном режиме',
                    'Waiting for missing types to be registered', 'Ожидание регистрации отсутствующих типов',
                    'Failed to load external modules', 'Unsupported version of', 'Неподдерживаемая версия']
        for failure in failures:
            with self.subTest(failure=failure), self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
                self.healthy('Started flows\nЗапущены потоки\n' + failure)

    def test_journal_error_and_wrong_pid_proof_refuse(self):
        with self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
            self.healthy('Started flows', code=1)
        for field, value in [('ActiveState', 'inactive'), ('MainPID', '0'), ('ControlGroup', '/system.slice/other.service')]:
            old = self.state[field]
            self.state[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
                self.healthy('Started flows')
            self.state[field] = old

    def test_real_proc_fixture_identity_mismatch_refuses(self):
        for kind in ('exe', 'cmdline', 'cgroup', 'cgroup_suffix'):
            with self.subTest(kind=kind):
                if kind == 'exe':
                    (self.process / 'exe').unlink()
                    (self.process / 'exe').symlink_to(self.root / 'other-node')
                elif kind == 'cmdline':
                    (self.process / 'cmdline').write_bytes(b'node\0some-other-app.js\0')
                else:
                    (self.process / 'cgroup').write_text('0::/system.slice/' + ('other.service' if kind == 'cgroup' else 'nodered.service-foreign') + '\n')
                with self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
                    self.healthy('Started flows')
                (self.process / 'exe').unlink()
                (self.process / 'exe').symlink_to(self.node)
                (self.process / 'cmdline').write_bytes(b'\0'.join(self.command) + b'\0')
                (self.process / 'cgroup').write_text('0::/system.slice/nodered.service\n')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'real /proc title evidence requires Linux')
    def test_real_node_process_title_retains_health_identity(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node must be installed for the process-title regression')
        script = '''const fs = require('fs');
const before = fs.readFileSync('/proc/self/cmdline').toString('base64');
process.title = 'node-red';
console.log(JSON.stringify({before, after: fs.readFileSync('/proc/self/cmdline').toString('base64'), cwd: process.cwd()}));'''
        import base64
        evidence = json.loads(subprocess.check_output([node, '-e', script], cwd=self.user_dir, timeout=10))
        before, after = (base64.b64decode(evidence[key]) for key in ('before', 'after'))
        self.assertIn(b'-e\0', before)
        self.assertEqual(after.rstrip(b'\0'), b'node-red')
        self.assertEqual(evidence['cwd'], str(self.user_dir))
        (self.process / 'cmdline').write_bytes(after)
        self.assertEqual(self.healthy('Started flows'), {'main_pid': 123, 'control_group': '/system.slice/nodered.service'})

    def test_title_needs_exact_executable_directory_and_cgroup(self):
        (self.process / 'cmdline').write_bytes(b'node-red\0' + b'\0' * 80)
        for kind in ('exe', 'cwd', 'cgroup', 'cgroup_suffix', 'unit_directory', 'missing_process'):
            with self.subTest(kind=kind):
                if kind in ('exe', 'cwd'):
                    (self.process / kind).unlink()
                    (self.process / kind).symlink_to(self.root / 'foreign')
                elif kind.startswith('cgroup'):
                    suffix = 'other.service' if kind == 'cgroup' else 'nodered.service-foreign'
                    (self.process / 'cgroup').write_text('0::/system.slice/' + suffix + '\n')
                elif kind == 'unit_directory':
                    self.state['WorkingDirectory'] = str(self.root / 'foreign')
                else:
                    (self.process / 'stat').unlink()
                with self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
                    self.healthy('Started flows')
                for name, target in (('exe', self.node), ('cwd', self.user_dir)):
                    (self.process / name).unlink()
                    (self.process / name).symlink_to(target)
                (self.process / 'cgroup').write_text('0::/system.slice/nodered.service\n')
                self.state['WorkingDirectory'] = str(self.user_dir)
                self.write_start_ticks('100')
        for command in (b'fake-node-red\0', b'node-red --other\0', b'node-red\0foreign.js\0'):
            with self.subTest(command=command), self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
                (self.process / 'cmdline').write_bytes(command)
                self.healthy('Started flows')

    def test_readiness_is_bound_to_current_pid_and_invocation(self):
        row = {'_SYSTEMD_UNIT': vendor.UNIT, '_SYSTEMD_INVOCATION_ID': 'a' * 32,
               '_PID': '123', 'MESSAGE': 'Started flows'}
        self.assertEqual(self.healthy('', records=[row])['main_pid'], 123)
        for key, value in (('_SYSTEMD_UNIT', 'foreign.service'), ('_SYSTEMD_INVOCATION_ID', 'b' * 32), ('_PID', '124')):
            with self.subTest(key=key), self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
                self.healthy('', records=[{**row, key: value}])
        for invocation in ('', 'unknown', '0' * 31, '0' * 32, 'A' * 32):
            with self.subTest(invocation=invocation), self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
                self.state['InvocationID'] = invocation
                self.healthy('Started flows')
        self.state['InvocationID'] = 'a' * 32

    def test_identity_is_rechecked_after_journal_read(self):
        for field, value in (('MainPID', '124'), ('InvocationID', 'b' * 32), ('ActiveState', 'inactive'),
                             ('ControlGroup', '/system.slice/foreign.service')):
            with self.subTest(field=field), self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
                self.healthy('Started flows', states=[self.state, {**self.state, field: value}])
        with self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
            self.healthy('Started flows', after_journal=lambda: self.write_start_ticks('101'))
        with self.assertRaisesRegex(vendor.InstallError, 'manager_unknown'):
            self.healthy('Started flows', states=[self.state, vendor.InstallError('manager_unknown')])


class FakeManager:
    def __init__(self, active=False, enabled='disabled', failure=None):
        self.deadline = time.monotonic() + 540
        self.values = {'LoadState': 'loaded', 'ActiveState': 'active' if active else 'inactive', 'UnitFileState': enabled}
        self.calls, self.failure, self.unknown = [], failure, False

    def terminal(self, desired=None):
        if self.unknown:
            raise vendor.InstallError('manager_unknown')
        if desired and self.values['ActiveState'] != desired:
            raise vendor.InstallError('service_state_failed')
        return dict(self.values)

    state = terminal

    def unit_identity(self, name):
        if name == 'node-red.service':
            return {'Id': name, 'Names': name, 'LoadState': 'not-found', 'FragmentPath': ''}
        return {'Id': 'nodered.service', 'Names': 'nodered.service', 'LoadState': 'loaded',
                'FragmentPath': '/etc/systemd/system/nodered.service'}

    def run(self, *_args, **_kwargs):
        return SimpleNamespace(returncode=0)

    def service(self, verb, desired):
        self.calls.append(verb)
        self.values['ActiveState'] = desired
        return self.terminal(desired)

    def command(self, verb, *options):
        self.calls.append(' '.join([verb, *options]))
        if verb in ('enable', 'disable'):
            self.values['UnitFileState'] = ('enabled-runtime' if '--runtime' in options else 'enabled') if verb == 'enable' else 'disabled'

    def healthy(self, *_):
        if self.failure:
            failure = self.failure
            self.failure = None
            self.unknown = failure == 'manager_unknown'
            raise vendor.InstallError(failure)
        return {'main_pid': 123, 'control_group': '/system.slice/nodered.service'}


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.layout = vendor.Layout(self.root / 'rootfs')
        self.source = self.root / 'source'
        self.source.mkdir()
        runtime = self.source / 'node-red'
        runtime.mkdir()
        (runtime / 'package.json').write_text('{"version":"4.1.13"}')
        (runtime / 'red.js').write_text('new runtime')
        provenance = {'source_lock_sha256': 'a' * 64, 'source_manifest_sha256': 'b' * 64, 'node_engines': '>=22'}
        (runtime / '.sa02m-build.json').write_text(json.dumps(provenance))
        (runtime / '.sa02m-inventory.json').write_text(json.dumps(vendor.tree_inventory(runtime)))
        archive = self.source / 'bundle.tgz'
        with tarfile.open(archive, 'w:gz') as tf:
            tf.add(runtime, arcname='node-red')
        self.package = {'version': '4.1.13', 'node_version': '22.23.5', **provenance,
            'inventory_sha256': vendor.digest(runtime / '.sa02m-inventory.json'),
            'unit': {'sha256': hashlib.sha256(UNIT_TEMPLATE).hexdigest()},
            'runtime': {'filename': archive.name, 'size': archive.stat().st_size, 'sha256': vendor.digest(archive), 'root': 'node-red', 'url': None}}
        self.layout.target.mkdir(parents=True)
        (self.layout.target / 'package.json').write_text('{"version":"4.1.12"}')
        (self.layout.target / 'red.js').write_text('old runtime')
        self.layout.unit.parent.mkdir(parents=True)
        self.layout.unit.write_bytes(b'old unit')
        self.user_dir = self.root / 'user'
        self.user_dir.mkdir()
        (self.user_dir / 'flows.json').write_text('retained flows')
        (self.user_dir / 'flows_cred.json').write_text('retained credentials')
        self.node = self.root / 'node'
        self.node.write_text('existing node')
        account = SimpleNamespace(pw_name='fixture-user', pw_gid=1234, pw_uid=1234, pw_dir=str(self.user_dir.parent))
        self.patches = contextlib.ExitStack()
        for name, kw in [('trusted_file', {'return_value': UNIT_TEMPLATE}),
                         ('trusted_directory', {'side_effect': lambda p: p.mkdir(parents=True, exist_ok=True)}),
                         ('account_info', {'return_value': account}), ('ensure_account', {'return_value': (account, self.user_dir)}),
                         ('existing_node', {'return_value': self.node}), ('verify_node', {'return_value': '22.23.5'})]:
            self.patches.enter_context(mock.patch.object(vendor, name, **kw))

    def tearDown(self):
        self.patches.close()
        self.temp.cleanup()

    def install(self, manager, activate=False):
        return vendor.install(self.package, activate, 'fixture-user', 1880, manager, self.source, self.layout)

    def assert_preserved_data(self):
        self.assertEqual((self.user_dir / 'flows.json').read_text(), 'retained flows')
        self.assertEqual((self.user_dir / 'flows_cred.json').read_text(), 'retained credentials')
        self.assertEqual(self.node.read_text(), 'existing node')

    def test_preserve_disabled_inactive(self):
        manager = FakeManager()
        result = self.install(manager)
        self.assertTrue(result['ok'])
        self.assertEqual(manager.calls, ['daemon-reload'])
        self.assertEqual(manager.values['UnitFileState'], 'disabled')
        self.assertEqual((self.layout.target / 'red.js').read_text(), 'new runtime')
        self.assert_preserved_data()

    def test_separate_legacy_unit_refuses_before_install_mutation(self):
        for activate in (False, True):
            for load in ('loaded', 'masked'):
                with self.subTest(activate=activate, load=load):
                    manager = FakeManager(active=True)
                    original = manager.unit_identity
                    manager.unit_identity = lambda name: ({'Id': name, 'Names': name, 'LoadState': load,
                        'FragmentPath': '/etc/systemd/system/node-red.service'} if name == 'node-red.service' else original(name))
                    with mock.patch.object(vendor, 'verified_archive', wraps=vendor.verified_archive) as archive, mock.patch.object(vendor, 'ensure_account', wraps=vendor.ensure_account) as account:
                        with self.assertRaisesRegex(vendor.InstallError, 'service_identity_conflict'):
                            self.install(manager, activate)
                        archive.assert_not_called()
                        account.assert_not_called()
                    self.assertEqual(manager.calls, [])
                    self.assertEqual((self.layout.target / 'red.js').read_text(), 'old runtime')
                    self.assertEqual(self.layout.unit.read_bytes(), b'old unit')
                    self.assertFalse(self.layout.receipts.exists())

    def test_proven_canonical_alias_is_preserved(self):
        manager = FakeManager(active=True)
        manager.unit_identity = lambda name: {'Id': 'nodered.service', 'Names': 'nodered.service node-red.service',
            'LoadState': 'loaded', 'FragmentPath': '/etc/systemd/system/nodered.service'}
        self.install(manager)
        self.assertEqual(manager.calls, ['stop', 'daemon-reload', 'start'])

    def test_ambiguous_alias_identity_refuses(self):
        for field, value in [('Id', 'node-red.service'), ('Names', 'node-red.service'),
                             ('FragmentPath', ''), ('LoadState', 'error')]:
            manager = FakeManager()
            identity = {'Id': 'nodered.service', 'Names': 'nodered.service node-red.service',
                        'LoadState': 'loaded', 'FragmentPath': '/etc/systemd/system/nodered.service'}
            def result(name):
                values = dict(identity)
                if name == 'node-red.service':
                    values[field] = value
                return values
            manager.unit_identity = result
            with self.subTest(field=field), self.assertRaises(vendor.InstallError):
                self.install(manager)
            self.assertEqual(manager.calls, [])

    def test_preserve_running_restarts_exactly_once_without_enable(self):
        manager = FakeManager(active=True)
        self.install(manager)
        self.assertEqual(manager.calls, ['stop', 'daemon-reload', 'start'])
        self.assertEqual(manager.values['UnitFileState'], 'disabled')

    def test_activate_enables_and_starts_once(self):
        manager = FakeManager()
        self.install(manager, True)
        self.assertEqual(manager.calls, ['daemon-reload', 'enable', 'start'])
        receipt = json.loads((self.layout.receipts / 'node-red.json').read_text())
        self.assertTrue(receipt['ok'])
        self.assertEqual(receipt['runtime_sha256'], self.package['runtime']['sha256'])

    def test_health_failure_restores_runtime_unit_state_and_receipt(self):
        self.layout.receipts.mkdir(parents=True)
        (self.layout.receipts / 'node-red.json').write_text('{"previous":true}')
        manager = FakeManager(failure='runtime_health_failed')
        with self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
            self.install(manager, True)
        self.assertEqual((self.layout.target / 'red.js').read_text(), 'old runtime')
        self.assertEqual(self.layout.unit.read_bytes(), b'old unit')
        self.assertEqual(manager.values['ActiveState'], 'inactive')
        self.assertEqual(manager.values['UnitFileState'], 'disabled')
        self.assertEqual((self.layout.receipts / 'node-red.json').read_text(), '{"previous":true}')
        self.assert_preserved_data()

    def test_failed_swap_restores_preceding_unit_writes(self):
        original = vendor.Swap.replace
        def fail_runtime(swap, staged):
            if swap.path == self.layout.target:
                raise OSError('synthetic rename failure')
            return original(swap, staged)
        with mock.patch.object(vendor.Swap, 'replace', fail_runtime), self.assertRaises(OSError):
            self.install(FakeManager(active=True))
        self.assertEqual(self.layout.unit.read_bytes(), b'old unit')
        self.assertEqual((self.layout.target / 'red.js').read_text(), 'old runtime')
        self.assert_preserved_data()

    def test_unknown_manager_preserves_backup_and_never_claims_success(self):
        manager = FakeManager(failure='manager_unknown')
        with self.assertRaisesRegex(vendor.InstallError, 'rollback_failed'):
            self.install(manager, True)
        self.assertEqual(manager.calls, ['daemon-reload', 'enable', 'start'])
        self.assertTrue(list(self.layout.target.parent.glob('node-red.sa02m-backup-*')))
        self.assertFalse((self.layout.receipts / 'node-red.json').exists())

    def test_failed_activation_restores_runtime_only_enable(self):
        manager = FakeManager(enabled='enabled-runtime', failure='runtime_health_failed')
        with self.assertRaisesRegex(vendor.InstallError, 'runtime_health_failed'):
            self.install(manager, True)
        self.assertEqual(manager.values['UnitFileState'], 'enabled-runtime')
        self.assertIn('enable --runtime', manager.calls)

    def test_fresh_node_uses_verified_versioned_directory(self):
        root_name = 'node-v22.23.5-linux-armv7l'
        directory = self.source / root_name
        (directory / 'bin').mkdir(parents=True)
        (directory / 'bin/node').write_bytes(b'verified node fixture')
        archive = self.source / 'node.tar.gz'
        with tarfile.open(archive, 'w:gz') as tf:
            tf.add(directory, arcname=root_name)
        self.package['node'] = {'filename': archive.name, 'root': root_name, 'size': archive.stat().st_size, 'sha256': vendor.digest(archive), 'url': None}
        with mock.patch.object(vendor, 'existing_node', return_value=None), mock.patch.object(vendor, 'verify_node', return_value='22.23.5') as validation:
            result = self.install(FakeManager())
        self.assertEqual(result['node_action'], 'installed')
        self.assertTrue(validation.call_args.args[-1])
        self.assertEqual(self.layout.node_link.resolve(), self.layout.nodes / '22.23.5/bin/node')
        self.assertEqual(self.layout.node_link.read_bytes(), b'verified node fixture')


if __name__ == '__main__':
    unittest.main()
