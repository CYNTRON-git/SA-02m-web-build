#!/usr/bin/env python3
# comment-mutation-proof-exempt: executes the released and proposed generators, a real Ed25519 package and released validator; asserts destinations, order, hashes and rejection before extraction.
"""Verify the old online bootstrap and the offline updater floor against released bytes."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'opt/sa02m-update/lib'))
from runtime_delivery import ACTIVATE, CONSUMER, MANAGER, REQUIRED, ordered

BLOBS = {'runner':'97b20dae3f7145a640866f56dc4bac9962eef9fd',
         'validate_package.py':'96f558df96c7de6a7d9aa5f30637327e0ca210ce',
         'semver.py':'5e9c66428d497e03765a9c1112a84788a07e289b',
         '__init__.py':'c4efdfa8c8c0eae50dea1f5c51737dad11496667'}


def released(name):
    sha = BLOBS[name]
    supplied = os.environ.get('SA02M_RELEASED_UPDATER_BLOBS')
    data = ((Path(supplied)/sha).read_bytes() if supplied else
            subprocess.check_output(['git','cat-file','blob',sha],cwd=ROOT))
    if hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest() != sha:
        raise ValueError('released updater fixture hash mismatch')
    return data


def online(folder, source, overlay):
    start = source.index('prepare_github_overlay() {')
    end = source.index('\n}\n\n# --- validate + extract',start)+2
    script = '''set -euo pipefail
txn_get() {
 case "$1" in
 overlay_path) printf '%s' "$PAYLOAD";;
 target_version) printf '1.0.8.3';;
 target_commit) printf '%040d' 1;;
 previous_version) printf '1.0.8.2';;
 *) return 2;; esac
}
die() { echo "$*" >&2; exit 1; }
log() { :; }
''' + source[start:end] + '\nprepare_github_overlay fixture\n'
    run = subprocess.run(['bash','-c',script],capture_output=True,text=True,timeout=40,
                         env={**os.environ,'STATEDIR':str(folder),'PAYLOAD':str(overlay),
                              'VALIDATE_PY':str(ROOT/'opt/sa02m-update/lib/validate_package.py')})
    return run,folder/'staging/fixture/meta/manifest.json'


def packer():
    spec = importlib.util.spec_from_file_location('runtime_delivery_packer',ROOT/'scripts/pack-offline-update.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RuntimeDelivery(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.overlay = self.base/'overlay'
        self.pack = packer()
        self.map = self.pack.load_deploy_map()
        manifest = json.loads((ROOT/MANAGER/'runtime.json').read_bytes())
        self.wheels = {MANAGER+'wheelhouse/'+manifest['wheelset']+'/'+w['file'] for w in manifest['packages'].values()}
        self.files = sorted(REQUIRED | self.wheels | self.pack.VENDOR_INSTALL_SOURCES |
                            {'opt/sa02m-update/lib/runtime_delivery.py','www/network_config/VERSION'})
        for rel in self.files:
            path = self.overlay / rel
            path.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/rel,path)

    def test_dirty_pack_selects_real_allowlisted_inputs_and_omits_removed_files(self):
        gone = 'etc/systemd/sa02m-retired.service'
        ignored = 'notes/unrelated'
        paths = [CONSUMER, ACTIVATE, 'www/network_config/VERSION', gone, ignored]
        answer = subprocess.CompletedProcess([], 0, ('\0'.join(paths)+'\0').encode())
        with patch.object(self.pack, 'run', return_value=answer) as ls:
            selected = self.pack.list_allowlisted_files(self.pack.load_allowlist_patterns(), dirty=True)
        ls.assert_called_once_with(['git','ls-files','-z','--cached','--others','--exclude-standard'])
        self.assertNotIn(ignored,selected)
        staged = self.base/'dirty'
        with patch.object(self.pack, 'ROOT', self.overlay):
            self.assertEqual(self.pack.overlay_workdir_files(selected, staged),3)
            self.assertEqual((staged/CONSUMER).read_bytes(),(ROOT/CONSUMER).read_bytes())
            self.assertFalse((staged/gone).exists())
            (self.overlay/CONSUMER).unlink()
            (self.overlay/CONSUMER).symlink_to(self.overlay/ACTIVATE)
            with self.assertRaises(SystemExit):
                self.pack.overlay_workdir_files(selected, staged)

    def test_first_and_current_online_match_the_installed_destinations(self):
        old = released('runner').decode()
        current = (ROOT/'etc/sa02m-update-runner.sh').read_text()
        self.assertIn('src=$(readlink -f "$0"',old)
        self.assertIn('install -m 755 "$src" "$dest"',old)
        for name,source,floor in [('first',old,'1.0.8.2'),('next',current,'1.0.8.3')]:
            with self.subTest(map=name):
                run,path = online(self.base/name,source,self.overlay)
                self.assertEqual(run.returncode,0,run.stderr)
                result = json.loads(path.read_bytes())
                self.assertEqual(result['min_updater'],floor)
                items = {e['src']:e for e in result['deploy']}
                self.assertEqual(set(items),set(self.files))
                for rel in (ACTIVATE,CONSUMER,'usr/local/sbin/sa02m-mqtt-scan','usr/local/lib/sa02m-root-auth.py',*self.wheels):
                    self.assertEqual(items[rel]['dst'],'/'+rel)
                    self.assertEqual(items[rel]['owner'],'root:root')
                    self.assertEqual(items[rel]['mode'],'0755' if rel in (ACTIVATE,CONSUMER,'usr/local/sbin/sa02m-mqtt-scan') else '0644')
                for rel in ('etc/systemd/sa02m-devices-api.service','etc/systemd/sa02m-devices-logger.service'):
                    self.assertIn('Wants=sa02m-runtime-activate.service',(self.overlay/rel).read_text())
                if name == 'next':
                    self.assertEqual(result['deploy'],ordered(result['deploy'],self.overlay))
        missing = self.overlay/next(iter(sorted(self.wheels)))
        missing.unlink()
        run,path = online(self.base/'partial',current,self.overlay)
        self.assertNotEqual(run.returncode,0)
        self.assertFalse(path.exists())

    def test_actual_pack_and_released_validator_refuse_before_live_deployment(self):
        pack = self.pack
        self.assertEqual(pack.resolve_min_updater(None),'1.0.8.3')
        with self.assertRaises(SystemExit):
            pack.build_deploy_list(self.files,self.map,'1.0.8.2')
        with self.assertRaises(SystemExit):
            pack.build_deploy_list([f for f in self.files if f != CONSUMER],self.map,'1.0.8.3')
        deploy = pack.build_deploy_list(self.files,self.map,'1.0.8.3')
        self.assertEqual({i['src'] for i in deploy},set(self.files))
        self.assertEqual(deploy,ordered(deploy,self.overlay))
        patterns=pack.load_allowlist_patterns()
        for rel in self.files:
            self.assertTrue(any(pack.path_matches(rel,p) for p in patterns),rel)
        payload, size = pack.make_payload_tar_gz(self.overlay,self.files)
        mf = pack.build_manifest(version='1.0.8.3',commit='1'*40,key_id='release-fixture',
                                 payload_gz=payload,uncompressed_size=size,deploy=deploy,min_updater='1.0.8.3')
        keys = self.base/'keys'
        keys.mkdir()
        private = self.base/'private'
        subprocess.run(['openssl','genpkey','-algorithm','Ed25519','-out',str(private)],check=True,capture_output=True)
        subprocess.run(['openssl','pkey','-in',str(private),'-pubout','-out',str(keys/'release-fixture.pem')],check=True,capture_output=True)
        package = self.base/'signed.sa02m'
        pack.write_outer_package(package,manifest=mf,sig_b64=pack.sign_manifest(mf,private,'openssl'),payload_gz=payload)
        old = self.base/'released'
        old.mkdir()
        for name in ('validate_package.py','semver.py','__init__.py'):
            (old/name).write_bytes(released(name))
        probe = """import sys; from pathlib import Path
sys.path.insert(0,sys.argv[1])
from validate_package import validate_package,PackageError
try:
    validate_package(Path(sys.argv[2]),trusted_keys_dir=Path(sys.argv[3]),installed_version='1.0.8.2',
                     runner_version='1.0.8.2',extract_to=Path(sys.argv[4]),check_compat=True)
except PackageError as error:
    print(error.code,str(error))
    raise SystemExit(0 if error.code=='E_COMPAT' and 'min_updater' in str(error) else 1)
raise SystemExit(1)"""
        dest = self.base/'extracted'
        run = subprocess.run([sys.executable,'-I','-B','-c',probe,str(old),str(package),str(keys),str(dest)],
                             capture_output=True,text=True,timeout=30)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        # The released validator extracts only into its private staging area
        # before returning E_COMPAT to the caller. The 8.3 validator rejects
        # the same package/runner pairing before creating the extraction root.
        self.assertEqual((dest/'overlay/www/network_config/VERSION').read_bytes(),
                         (self.overlay/'www/network_config/VERSION').read_bytes())
        fresh = self.base/'current-staging'
        current = ROOT/'opt/sa02m-update/lib'
        run = subprocess.run([sys.executable,'-I','-B','-c',probe,str(current),str(package),str(keys),str(fresh)],
                             capture_output=True,text=True,timeout=30)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        self.assertFalse(fresh.exists())

        from validate_package import PackageError, validate_package
        def inspect(candidate, folder=None):
            pack.write_outer_package(package, manifest=candidate, sig_b64=pack.sign_manifest(candidate,private,'openssl'), payload_gz=payload)
            return validate_package(package, trusted_keys_dir=keys, installed_version='1.0.8.2',
                                    runner_version='1.0.8.3', check_compat=True, extract_to=folder)

        self.assertIs(inspect(mf)['ok'], True)
        self.assertIs(inspect(mf, fresh)['ok'], True)
        for case, changes in (
            ('missing-launcher', {'deploy':[item for item in deploy if item['src'] != CONSUMER]}),
            ('duplicate-source', {'deploy':[deploy[0], *deploy]}),
            ('missing-payload', {'deploy':[dict(deploy[0], src='opt/missing.py'), *deploy[1:]]}),
            ('wrong-order', {'deploy':list(reversed(deploy))}),
            ('downlevel', {'min_updater':'1.0.8.2'}),
        ):
            with self.subTest(case=case), self.assertRaises(PackageError) as caught:
                inspect({**mf, **changes})
            self.assertEqual(caught.exception.code, 'E_MANIFEST')


if __name__ == '__main__':
    unittest.main(verbosity=2)
