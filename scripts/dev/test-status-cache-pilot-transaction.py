#!/usr/bin/env python3
# comment-mutation-proof-exempt: Behavioral filesystem and archive tests exercise real descriptors and compare resulting bytes/inodes; no source-text needle can satisfy them.
"""Adversarial fixed-scope transactions in a fixture tree, without services or hardware."""
import copy
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('pilot_transaction', ROOT / 'scripts/status-cache-pilot-transaction.py')
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


def package_bytes(manifest, payload, extra=None):
    result = io.BytesIO()
    entries = {**payload, 'manifest.json': json.dumps(manifest).encode()}
    with tarfile.open(fileobj=result, mode='w:gz') as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if extra:
            archive.addfile(extra, io.BytesIO(b'x' * extra.size))
    return result.getvalue()


def fixture_manifest():
    payload, rows = {}, []
    for index, (source, destination, mode, group) in enumerate(pilot.FILES):
        data = ('new payload ' + str(index)).encode()
        source = 'payload/' + source
        payload[source] = data
        rows.append({'source': source, 'destination': destination, 'mode': mode, 'owner': 'root', 'group': group,
                     'sha256': pilot.digest(data), 'expected_before_sha256': None})
    payload.update({'recipes/' + name: b'recipe' for name in pilot.RECIPES})
    return {'schema': 1, 'kind': 'status-cache-pilot', 'commit': 'a' * 40, 'base': 'b' * 40,
            'runtime': rows, 'files': {name: pilot.digest(data) for name, data in payload.items()}}, payload


class PackageTests(unittest.TestCase):
    def packager_fixture(self):
        spec = importlib.util.spec_from_file_location('pilot_packager', ROOT / 'scripts/pack-status-cache-pilot.py')
        packager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packager)
        temp = tempfile.TemporaryDirectory(prefix='sa02m-pilot-ref-')
        self.addCleanup(temp.cleanup)
        tree = Path(temp.name)/'repo'
        tree.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(tree), '-c', 'user.name=Fixture',
                                           '-c', 'user.email=fixture@example.invalid', *args], stderr=subprocess.DEVNULL)
        git('init', '-q')
        for source in [row[0] for row in packager.FILES] + list(pilot.RECIPES):
            path = tree/source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT/source).read_bytes())
        outside = tree/'www/network_config/static/js/app/hw.js'
        outside.write_text('// historical producer\n')
        git('add', '.')
        git('commit', '-qm', 'base')
        base = git('rev-parse', 'HEAD').decode().strip()
        consumer = tree/'www/network_config/static/js/app/status.js'
        consumer.write_bytes(consumer.read_bytes()+b'\n// demand-only fixture\n')
        git('add', '.')
        git('commit', '-qm', 'demand')
        packager.ROOT = tree
        return packager, tree, outside, base, git

    def test_packager_accepts_complete_demand_only_ref(self):
        packager, tree, _outside, base, _git = self.packager_fixture()
        output = tree.parent/'output'
        with mock.patch.object(sys, 'argv', ['pack', '--base', base, '--output', str(output)]):
            packager.main()
        manifest, _payload = pilot.load_package((output/'status-cache-pilot.tar.gz').read_bytes())
        self.assertEqual(len(manifest['runtime']), 8)

    def test_packager_rejects_paired_producer_and_other_www_changes_before_output(self):
        for change in ('modify', 'delete', 'rename'):
            with self.subTest(change=change):
                packager, tree, outside, base, git = self.packager_fixture()
                if change == 'modify': outside.write_text('// new producer with a paired consumer\n')
                elif change == 'delete': outside.unlink()
                else: outside.rename(outside.with_name('outside\nproducer.js'))
                git('add', '-A')
                git('commit', '-qm', 'outside whitelist')
                output = tree.parent/'refused'
                with mock.patch.object(sys, 'argv', ['pack', '--base', base, '--output', str(output)]):
                    with self.assertRaisesRegex(SystemExit, 'outside.*whitelist'):
                        packager.main()
                self.assertFalse(output.exists(), 'a rejected ref cannot leave a partial release artifact')

    def test_packager_and_transaction_policy_match(self):
        spec = importlib.util.spec_from_file_location('pilot_packager', ROOT / 'scripts/pack-status-cache-pilot.py')
        packager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packager)
        self.assertEqual(packager.FILES, pilot.FILES)

    def test_exact_package_accepted(self):
        manifest, payload = fixture_manifest()
        self.assertEqual(pilot.load_package(package_bytes(manifest, payload)), (manifest, {**payload, 'manifest.json': json.dumps(manifest).encode()}))

    def test_destination_source_mode_group_and_order_injections(self):
        for key, value in [('destination', '/etc/shadow'), ('source', 'recipes/scripts/status-cache-pilot-transaction.py'),
                           ('mode', '04755'), ('owner', 'www-data'), ('group', 'other')]:
            with self.subTest(key=key):
                manifest, payload = fixture_manifest()
                manifest['runtime'][0][key] = value
                with self.assertRaises(RuntimeError):
                    pilot.load_package(package_bytes(manifest, payload))
        manifest, payload = fixture_manifest()
        manifest['runtime'].reverse()
        with self.assertRaises(RuntimeError):
            pilot.load_package(package_bytes(manifest, payload))

    def test_archive_path_duplicate_symlink_and_oversize_rejected(self):
        for name, kind, size in [('../../etc/shadow', tarfile.REGTYPE, 1),
                                 ('manifest.json', tarfile.REGTYPE, 1),
                                 ('manifest.json', tarfile.SYMTYPE, 0),
                                 ('outside', tarfile.REGTYPE, pilot.LIMIT + 1)]:
            with self.subTest(name=name, kind=kind):
                manifest, payload = fixture_manifest()
                entry = tarfile.TarInfo(name)
                entry.type, entry.size, entry.linkname = kind, size, '/etc/shadow'
                with self.assertRaises(RuntimeError):
                    pilot.load_package(package_bytes(manifest, payload, entry))

    def test_hash_and_extra_manifest_member_rejected(self):
        manifest, payload = fixture_manifest()
        payload[next(iter(payload))] = b'corrupt'
        with self.assertRaises(RuntimeError):
            pilot.load_package(package_bytes(manifest, payload))
        manifest, payload = fixture_manifest()
        manifest['files']['../../escape'] = 'f' * 64
        with self.assertRaises(RuntimeError):
            pilot.load_package(package_bytes(manifest, payload))

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(RuntimeError):
            pilot.parse_json(b'{"destination":"safe","destination":"escape"}')

    def test_expanded_archive_bound_applies_before_tar_header_parsing(self):
        data = gzip.compress(b'x' * 2048)
        with mock.patch.object(pilot, 'PACKAGE_LIMIT', 1024):
            with self.assertRaisesRegex(RuntimeError, 'Expanded package too large'):
                pilot.load_package(data)

    def test_rollback_refuses_failed_stop_and_targets_only_collector(self):
        states = [{'active': 'active', 'enabled': 'enabled'}, {'active': 'active', 'enabled': 'enabled'}]
        with mock.patch.object(pilot, 'unit_state', side_effect=states), mock.patch.object(pilot, 'systemctl') as ctl:
            with self.assertRaisesRegex(RuntimeError, 'did not stop'):
                pilot.stop_pilot()
            ctl.assert_called_once_with('stop', pilot.UNIT, required=True)
        states[1] = {'active': 'inactive', 'enabled': 'enabled'}
        with mock.patch.object(pilot, 'unit_state', side_effect=states), mock.patch.object(pilot, 'systemctl') as ctl:
            pilot.stop_pilot()
            self.assertEqual(ctl.call_args_list, [mock.call('stop', pilot.UNIT, required=True), mock.call('disable', pilot.UNIT, required=True)])


@unittest.skipUnless(sys.platform == 'linux', 'Linux descriptor and renameat2 fixture tests')
class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sa02m-pilot-test-')
        self.root = Path(self.temp.name) / 'tree'
        self.root.mkdir(mode=0o700)
        self.stage = Path(self.temp.name) / 'stage'
        self.stage.mkdir(mode=0o700)
        self.outside = Path(self.temp.name) / 'outside'
        self.outside.mkdir()
        self.victim = self.outside / 'victim'
        self.victim.write_bytes(b'untouched')
        self.manifest, self.payload = fixture_manifest()
        self.original = {}
        for index, row in enumerate(self.manifest['runtime']):
            path = self.path(row['destination'])
            path.parent.mkdir(parents=True, exist_ok=True)
            if index in (0, 4, 5, 6):
                path.write_bytes(('original-' + str(index)).encode())
                path.chmod(0o750 if index == 0 else 0o640)
                row['expected_before_sha256'] = pilot.digest(path.read_bytes())
                if os.geteuid() == 0 and index == 4:
                    os.chown(path, 65534, 65534)
                self.original[row['destination']] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        self.root_fd = os.open(self.root, pilot.DIR_FLAGS)
        self.stage_fd = os.open(self.stage, pilot.DIR_FLAGS)
        self.store = pilot.Store(self.stage_fd)
        self.anchors = pilot.Anchors(self.root_fd, [row[1] for row in pilot.FILES])
        self.groups = {'root': os.getegid(), 'www-data': 33 if os.geteuid() == 0 else os.getegid()}
        self.tx = pilot.Transaction(self.anchors, self.store, self.manifest, self.payload, self.groups)
        self.state = self.tx.preflight({'active': 'inactive', 'enabled': 'not-found'})

    def tearDown(self):
        self.anchors.close()
        os.close(self.root_fd)
        os.close(self.stage_fd)
        self.temp.cleanup()

    def path(self, destination):
        return self.root / destination.lstrip('/')

    def assert_victim_safe(self):
        self.assertEqual(self.victim.read_bytes(), b'untouched')

    def test_successful_apply_and_idempotent_rollback_preserve_metadata(self):
        self.tx.prepare(self.state)
        self.tx.apply_files(self.state)
        for row in self.state['files']:
            actual, data = self.anchors.read(row['destination'])
            self.assertEqual(data, self.payload[row['source']])
            self.assertEqual((actual['uid'], actual['gid'], actual['mode']), (os.geteuid(), self.groups[row['group']], int(row['mode'], 8)))
        self.tx.rollback_files(self.state)
        self.tx.rollback_files(self.state)
        for row in self.state['files']:
            path = self.path(row['destination'])
            if row['destination'] in self.original:
                data, mode = self.original[row['destination']]
                self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), (data, mode))
                self.assertEqual((path.stat().st_uid, path.stat().st_gid), (row['before']['uid'], row['before']['gid']))
            else:
                self.assertFalse(path.exists())
        self.assertEqual(stat.S_IMODE((self.stage / 'state.json').stat().st_mode), 0o600)

    def test_parent_symlink_before_open_rejected(self):
        parent = self.path(pilot.FILES[0][1]).parent
        parent.rename(parent.with_name('original-cgi'))
        parent.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(OSError):
            pilot.Anchors(self.root_fd, [row[1] for row in pilot.FILES])
        self.assert_victim_safe()

    def test_parent_swap_since_preflight_rejected(self):
        parent = self.path(pilot.FILES[0][1]).parent
        parent.rename(parent.with_name('original-cgi'))
        parent.mkdir()
        with self.assertRaises(RuntimeError):
            pilot.Anchors(self.root_fd, [row[1] for row in pilot.FILES], self.state['parents'])
        with self.assertRaises(RuntimeError):
            self.tx.prepare(self.state)

    def test_parent_swap_during_write_never_follows_replacement(self):
        self.tx.prepare(self.state)
        real = pilot.new_temp
        parent = self.path(pilot.FILES[0][1]).parent
        def swap(*args):
            result = real(*args)
            if args[0] == self.anchors.parent(pilot.FILES[0][1])[0]:
                parent.rename(parent.with_name('original-cgi'))
                parent.symlink_to(self.outside, target_is_directory=True)
            return result
        with mock.patch.object(pilot, 'new_temp', side_effect=swap):
            with self.assertRaises(RuntimeError):
                self.tx.apply_files(self.state)
        self.assertEqual(list(self.outside.iterdir()), [self.victim])
        self.assert_victim_safe()

    def test_final_symlink_before_backup_and_apply_rejected(self):
        destination = self.path(pilot.FILES[0][1])
        destination.unlink()
        destination.symlink_to(self.victim)
        with self.assertRaises(OSError):
            self.tx.prepare(self.state)
        self.assert_victim_safe()

    def test_temp_symlink_cannot_be_opened_for_root_write(self):
        self.tx.prepare(self.state)
        parent = self.path(pilot.FILES[0][1]).parent
        (parent / ('.sa02m-pilot-' + 'a' * 48)).symlink_to(self.victim)
        with mock.patch.object(pilot.secrets, 'token_hex', return_value='a' * 48):
            with self.assertRaises(FileExistsError):
                self.tx.apply_files(self.state)
        self.assert_victim_safe()

    def test_temp_replaced_with_symlink_rejected(self):
        self.tx.prepare(self.state)
        real = pilot.new_temp
        parent_fd = self.anchors.parent(pilot.FILES[0][1])[0]
        def swap(*args):
            name, record = real(*args)
            if args[0] == parent_fd:
                os.unlink(name, dir_fd=parent_fd)
                os.symlink(str(self.victim), name, dir_fd=parent_fd)
            return name, record
        with mock.patch.object(pilot, 'new_temp', side_effect=swap):
            with self.assertRaises(OSError):
                self.tx.apply_files(self.state)
        self.assert_victim_safe()

    def test_final_symlink_during_temp_write_rejected(self):
        self.tx.prepare(self.state)
        real = pilot.new_temp
        destination = self.path(pilot.FILES[0][1])
        parent_fd = self.anchors.parent(pilot.FILES[0][1])[0]
        def swap(*args):
            result = real(*args)
            if args[0] == parent_fd:
                destination.unlink()
                destination.symlink_to(self.victim)
            return result
        with mock.patch.object(pilot, 'new_temp', side_effect=swap):
            with self.assertRaises(OSError):
                self.tx.apply_files(self.state)
        self.assert_victim_safe()

    def test_new_final_created_at_last_moment_is_not_overwritten(self):
        parent = self.path(pilot.FILES[1][1]).parent
        fd = os.open(parent, pilot.DIR_FLAGS)
        try:
            name, _ = pilot.new_temp(fd, b'new', os.geteuid(), os.getegid(), 0o644)
            destination = parent / 'sa02m-status-part'
            destination.symlink_to(self.victim)
            with self.assertRaises(FileExistsError):
                pilot.rename_new(fd, name, destination.name)
            self.assertTrue(destination.is_symlink())
            self.assert_victim_safe()
        finally:
            os.close(fd)

    def test_existing_final_last_moment_symlink_cannot_redirect_write(self):
        self.tx.prepare(self.state)
        real = pilot.os.replace
        parent_fd, name = self.anchors.parent(pilot.FILES[0][1])
        destination = self.path(pilot.FILES[0][1])
        def swap(source, target, **kwargs):
            if kwargs.get('dst_dir_fd') == parent_fd and target == name:
                destination.unlink()
                destination.symlink_to(self.victim)
            real(source, target, **kwargs)
        with mock.patch.object(pilot.os, 'replace', side_effect=swap):
            self.tx.apply_files(self.state)
        self.assertFalse(destination.is_symlink())
        self.assert_victim_safe()

    def test_concurrent_content_and_inode_drift_refuse_rollback(self):
        self.tx.prepare(self.state)
        self.tx.apply_files(self.state)
        destination = self.path(pilot.FILES[0][1])
        current = destination.read_bytes()
        destination.unlink()
        destination.write_bytes(current)
        destination.chmod(0o755)
        with self.assertRaises(RuntimeError):
            self.tx.rollback_files(self.state)
        self.assertEqual(destination.read_bytes(), current)
        destination.write_bytes(b'concurrent change')
        with self.assertRaises(RuntimeError):
            self.tx.rollback_files(self.state)
        self.assertEqual(destination.read_bytes(), b'concurrent change')

    def test_backup_corruption_refuses_all_rollback_writes(self):
        self.tx.prepare(self.state)
        self.tx.apply_files(self.state)
        (self.stage / 'backup-0').write_bytes(b'corrupt')
        with self.assertRaises(RuntimeError):
            self.tx.rollback_files(self.state)
        self.assertEqual(self.path(pilot.FILES[0][1]).read_bytes(), self.payload[self.state['files'][0]['source']])

    def test_parent_and_final_symlink_drift_refuse_rollback(self):
        self.tx.prepare(self.state)
        self.tx.apply_files(self.state)
        destination = self.path(pilot.FILES[0][1])
        destination.unlink()
        destination.symlink_to(self.victim)
        with self.assertRaises(OSError):
            self.tx.rollback_files(self.state)
        parent = destination.parent
        parent.rename(parent.with_name('original-cgi'))
        parent.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.tx.rollback_files(self.state)
        self.assert_victim_safe()

    def test_crash_after_rename_recovers_from_persisted_pending_inode(self):
        self.tx.prepare(self.state)
        real = pilot.os.replace
        parent_fd = self.anchors.parent(pilot.FILES[0][1])[0]
        def crash(source, destination, **kwargs):
            real(source, destination, **kwargs)
            if kwargs.get('dst_dir_fd') == parent_fd:
                raise InterruptedError('simulated process death after rename')
        with mock.patch.object(pilot.os, 'replace', side_effect=crash):
            with self.assertRaises(InterruptedError):
                self.tx.apply_files(self.state)
        durable = pilot.parse_json(self.store.read('state.json'))
        self.tx.rollback_files(durable)
        self.assertEqual(self.path(pilot.FILES[0][1]).read_bytes(), self.original[pilot.FILES[0][1]][0])

    def test_interrupted_rollback_resumes_from_journal(self):
        self.tx.prepare(self.state)
        self.tx.apply_files(self.state)
        real = pilot.os.replace
        parent_fd = self.anchors.parent(pilot.FILES[0][1])[0]
        def crash(source, destination, **kwargs):
            real(source, destination, **kwargs)
            if kwargs.get('dst_dir_fd') == parent_fd:
                raise InterruptedError('simulated process death during rollback')
        with mock.patch.object(pilot.os, 'replace', side_effect=crash):
            with self.assertRaises(InterruptedError):
                self.tx.rollback_files(self.state)
        self.tx.rollback_files(pilot.parse_json(self.store.read('state.json')))
        self.assertEqual(self.path(pilot.FILES[0][1]).read_bytes(), self.original[pilot.FILES[0][1]][0])

    def test_oversize_hardlink_fifo_and_world_writable_parent_rejected(self):
        path = self.path(pilot.FILES[0][1])
        path.write_bytes(b'x' * (pilot.LIMIT + 1))
        with self.assertRaises(RuntimeError):
            self.anchors.read(pilot.FILES[0][1])
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(RuntimeError):
            self.anchors.read(pilot.FILES[0][1])
        path.unlink()
        os.link(self.victim, path)
        with self.assertRaises(RuntimeError):
            self.anchors.read(pilot.FILES[0][1])
        path.parent.chmod(0o777)
        with self.assertRaises(RuntimeError):
            pilot.Anchors(self.root_fd, [row[1] for row in pilot.FILES])
        self.assert_victim_safe()

    def test_policy_injection_into_recovery_journal_rejected(self):
        self.tx.prepare(self.state)
        state = copy.deepcopy(self.state)
        state['files'][0]['destination'] = '/etc/shadow'
        with self.assertRaises(RuntimeError):
            self.tx.rollback_files(state)

    def test_production_rejects_nonroot_and_arbitrary_stage(self):
        with mock.patch.object(pilot.os, 'geteuid', return_value=1234), mock.patch.object(sys, 'argv', ['pilot', 'apply', '--stage', str(self.stage)]):
            with self.assertRaisesRegex(RuntimeError, 'requires root'):
                pilot.main()
        with self.assertRaises(RuntimeError):
            pilot.open_stage('/tmp/anything')


if __name__ == '__main__':
    unittest.main(verbosity=2)
