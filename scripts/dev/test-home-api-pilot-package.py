#!/usr/bin/env python3
# comment-mutation-proof-exempt: builds the actual committed-byte pilot package, overlays it onto the actual historical baseline and executes cloud event registration in an isolated subprocess; removing the constants member is an explicit dependency mutation.
"""Verify pilot dependency closure over the declared oldest board baseline."""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BASE = '5e367530de3ab283e1060c32017091bdf75b25e3'
CONSTANTS = 'opt/sa02m-alice/sa02m_alice/common/constants.py'
TREES = ('opt/sa02m-alice/sa02m_alice', 'opt/sa02m-devices/sa02m_devices',
         'opt/sa02m-rules/sa02m_rules')
EXERCISE = r'''
from pathlib import Path
import sys
root = Path(sys.argv[1]).resolve()
for folder in ('sa02m-devices', 'sa02m-rules', 'sa02m-alice'):
    sys.path.insert(0, str(root/'opt'/folder))
from sa02m_alice.common import constants as C
from sa02m_alice.client.sio_connection import AliceSocketIO
from sa02m_alice.client.sio_handlers import SioHandlers
for module in ('sa02m_alice.common.constants', 'sa02m_alice.client.sio_connection', 'sa02m_alice.client.sio_handlers'):
    assert Path(sys.modules[module].__file__).resolve().is_relative_to(root)
class Network:
    def __init__(self): self.handlers = {}
    def on(self, event, handler): self.handlers[event] = handler
responses = []
handler = SioHandlers(None, publish_mqtt=lambda *_: (_ for _ in ()).throw(AssertionError('unexpected publish')),
                      emit_response=responses.append, profile=C.PROFILE_CLOUD)
cloud = AliceSocketIO(profile=C.PROFILE_CLOUD, on_event=handler.handle)
cloud._sio = Network()
for event in cloud._events(): cloud._register(event)
assert 'alice_devices_delete' in cloud._sio.handlers
assert 'home_api_v1' in cloud._sio.handlers
cloud._sio.handlers['alice_devices_delete']({'request_id': 'package-probe'})
assert responses[-1] == {'request_id':'package-probe', 'ok':False, 'error':'invalid_device'}
# Invalid route is rejected by the actual packaged bridge before Unix I/O.
cloud._sio.handlers['home_api_v1']({'request_id':'home-probe', 'method':'GET', 'path':'/exec'})
assert responses[-1]['payload']['status'] == 404
yandex = AliceSocketIO(profile=C.PROFILE_YANDEX)
yandex._sio = Network()
for event in yandex._events(): yandex._register(event)
assert 'alice_devices_delete' not in yandex._sio.handlers
assert 'home_api_v1' not in yandex._sio.handlers
print('assembled cloud registration and handler dispatch OK')
'''


def git(repo, *args):
    return subprocess.check_output(['git', '-c', 'safe.directory='+str(repo),
                                    '-c', 'core.hooksPath=/dev/null', '-C', str(repo), *args],
                                   stderr=subprocess.PIPE)


class PilotPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        git(ROOT, 'cat-file', '-e', BASE+'^{commit}')
        cls.tmp = tempfile.TemporaryDirectory(prefix='home-pilot-package-')
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.work = Path(cls.tmp.name)
        cls.baseline = cls.work/'baseline'
        cls.baseline.mkdir()
        archive = git(ROOT, 'archive', BASE, *TREES, 'etc/nginx/network_config.conf',
                      'etc/systemd/system/sa02m-rules.service')
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar:
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or '..' in relative.parts:
                    raise AssertionError('unsafe historical archive member')
                target = cls.baseline.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(tar.extractfile(member).read())
                else:
                    raise AssertionError('unexpected historical link')
        cls.old_constants = (cls.baseline/CONSTANTS).read_bytes()
        if b'EVT_DEVICES_DELETE' in cls.old_constants:
            raise AssertionError('historical fixture no longer discriminates the missing dependency')
        cls.repo = cls.work/'repo'
        shutil.copytree(cls.baseline, cls.repo)
        git(cls.repo, 'init', '-q', '--template=')
        git(cls.repo, 'config', 'core.autocrlf', 'false')
        git(cls.repo, 'add', '.')
        git(cls.repo, '-c', 'user.name=Pilot fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-qm', 'Historical baseline fixture')
        cls.base_commit = git(cls.repo, 'rev-parse', 'HEAD').decode().strip()
        # Candidate commit contains the full module sources. Only packager
        # runtime entries may subsequently reach the assembled board tree.
        for name in TREES:
            shutil.copytree(ROOT/name, cls.repo/name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        selected = ('scripts/pack-home-api-pilot.py', 'etc/nginx/network_config.conf',
                    'etc/systemd/system/sa02m-rules.service', 'etc/systemd/system/sa02m-home-api.service',
                    'docs/contracts/home-api-v1.md', 'docs/contracts/home-api-v1-wire.json',
                    'docs/contracts/home-api-v1-pilot.md')
        for name in selected:
            destination = cls.repo/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((ROOT/name).read_bytes())
        git(cls.repo, 'add', '.')
        git(cls.repo, '-c', 'user.name=Pilot fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-qm', 'Candidate fixture')
        cls.candidate_commit = git(cls.repo, 'rev-parse', 'HEAD').decode().strip()
        cls.package = cls.work/'package'
        command = [sys.executable, '-I', str(cls.repo/'scripts/pack-home-api-pilot.py'),
                   '--ref', cls.candidate_commit, '--base', cls.base_commit, '--output', str(cls.package)]
        subprocess.run(command, check=True, capture_output=True, timeout=30)
        cls.manifest = json.loads((cls.package/'manifest.json').read_text())
        cls.archive = next(cls.package.glob('home-api-*.tar.gz'))

    def assemble(self, name, *, omit_constants=False):
        target = self.work/name
        shutil.copytree(self.baseline, target)
        with tarfile.open(self.archive) as archive:
            members = {member.name: member for member in archive}
            for entry in self.manifest['runtime']:
                relative = PurePosixPath(entry['destination'].removeprefix('/'))
                self.assertFalse(relative.is_absolute())
                self.assertNotIn('..', relative.parts)
                self.assertEqual(entry['source'], 'payload/'+relative.as_posix())
                self.assertEqual((entry['mode'], entry['owner'], entry['group']), ('0644', 'root', 'root'))
                if omit_constants and relative.as_posix() == CONSTANTS:
                    continue
                member = members[entry['source']]
                self.assertTrue(member.isfile())
                data = archive.extractfile(member).read()
                metadata = self.manifest['files'][entry['source']]
                self.assertEqual(hashlib.sha256(data).hexdigest(), metadata['sha256'])
                self.assertEqual(len(data), metadata['bytes'])
                destination = target.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        return target

    def exercise(self, target):
        env = {key:value for key,value in os.environ.items() if key not in ('PYTHONPATH', 'PYTHONHOME')}
        return subprocess.run([sys.executable, '-I', '-c', EXERCISE, str(target)], cwd=target,
                              env=env, capture_output=True, text=True, timeout=15)

    def test_real_package_closes_old_baseline_and_preserves_committed_bytes(self):
        target = self.assemble('installed')
        result = self.exercise(target)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        payload = self.package/'payload'/CONSTANTS
        committed = git(self.repo, 'show', self.candidate_commit+':'+CONSTANTS)
        self.assertEqual(payload.read_bytes(), committed)
        self.assertEqual((target/CONSTANTS).read_bytes(), committed)
        sums = (self.package/'SHA256SUMS').read_text()
        self.assertIn(hashlib.sha256(committed).hexdigest()+'  payload/'+CONSTANTS+'\n', sums)
        # Runtime-manifest rollback restores the exact baseline dependency.
        (target/CONSTANTS).write_bytes(self.old_constants)
        self.assertEqual(hashlib.sha256((target/CONSTANTS).read_bytes()).digest(), hashlib.sha256(self.old_constants).digest())

    def test_omitted_constants_member_reproduces_import_failure(self):
        target = self.assemble('missing-dependency', omit_constants=True)
        result = self.exercise(target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('AttributeError', result.stderr)
        self.assertIn('EVT_DEVICES_DELETE', result.stderr)


if __name__ == '__main__':
    unittest.main()
