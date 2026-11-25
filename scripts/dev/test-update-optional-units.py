#!/usr/bin/env python3
# comment-mutation-proof-exempt: executes the shipped GitHub manifest builder, offline selector and full installers with real filesystem targets and a stateful systemctl adapter; no host service or network calls.
"""Web update followed by full refresh must preserve optional-service policy."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
UNITS = ('sa02m-status-cache.service', 'sa02m-phase-energy.service')
UNIT_PATHS = tuple('etc/systemd/system/' + name for name in UNITS)
CONTROLS = ('www/network_config/VERSION', 'etc/systemd/system/sa02m-rules.service',
            'etc/systemd/system/sa02m-home-api.service', 'usr/local/sbin/sa02m-dtb-patch.sh')
PAYLOADS = ('etc/sa02m-status-cache.py', 'etc/sa02m-status-part.sh',
            'etc/sa02m-phase-energy.json', 'opt/sa02m-phase-energy/energy_state.py',
            'opt/sa02m-phase-energy/phase_energy.py', 'opt/sa02m-phase-energy/mqtt_service.py')


def shell_function(source, name):
    begin = source.index(name + '() {')
    # The embedded Python manifest also has an unindented closing brace.
    end = source.index('\n}\n\n# --- validate + extract', begin) + 3
    return source[begin:end]


class OptionalUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.overlay = self.base/'overlay'
        for rel in (*UNIT_PATHS, *CONTROLS, *PAYLOADS):
            target = self.overlay/rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT/rel).read_bytes())
        self.state = self.base/'update'
        self.target = self.base/'target'
        for rel in ('etc/systemd/system', 'run/systemd/system', 'usr/local/libexec', 'var/lib/sa02m-phase-energy'):
            (self.target/rel).mkdir(parents=True, exist_ok=True)
        (self.target/'manager.json').write_text('{}')
        self.bin = self.base/'bin'; self.bin.mkdir()
        shutil.copyfile(ROOT/'scripts/dev/fixtures/optional-units-systemctl.py', self.bin/'systemctl')
        (self.bin/'systemctl').chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'],
                        TARGET_ROOT=str(self.target), LOG_FILE=str(self.target/'installer.log'),
                        SA02M_INSTALL_MODE='refresh')
        self.env.pop('SA02M_ROOTFS_BUILD', None)

    def manifest(self):
        function = shell_function((ROOT/'etc/sa02m-update-runner.sh').read_text(), 'prepare_github_overlay')
        script = '''set -euo pipefail
txn_get() {
  case "$1" in
    overlay_path) printf '%s' "$SOURCE_OVERLAY" ;;
    target_version) printf '9.9.9.9' ;;
    target_commit) printf '1111111111111111111111111111111111111111' ;;
    previous_version) printf '1.0.7.36' ;;
    *) exit 2 ;;
  esac
}
die() { printf '%s\\n' "$*" >&2; exit 1; }
log() { :; }
''' + function + '\nprepare_github_overlay fixture\n'
        env = dict(self.env, STATEDIR=str(self.state), SOURCE_OVERLAY=str(self.overlay))
        result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.state/'staging/fixture/meta/manifest.json').read_text())

    def apply_online(self):
        manifest = self.manifest()
        for item in manifest['deploy']:
            dest = self.target/item['dst'].lstrip('/')
            self.assertTrue(dest.resolve().is_relative_to(self.target.resolve()), 'fixture destination escaped target')
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.state/'staging/fixture/overlay'/item['src'], dest)
        return manifest

    def full_refresh(self):
        source = self.base/'installer-source'
        (source/'scripts').mkdir(parents=True, exist_ok=True)
        for rel in (*UNIT_PATHS, *PAYLOADS, 'etc/sa02m-stacks-policy.sh',
                    'etc/sa02m-install-profile-lib.sh', 'etc/sa02m-web-port-lib.sh',
                    'etc/sa02m-web-log-scrub-lib.sh'):
            dest = source/rel; dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((ROOT/rel).read_bytes())
        # The real shared policy and atomic install run against this target.
        # Retarget only literal host filesystem roots; transport drops owner
        # flags so the same composition also runs under an unprivileged UID.
        original_lib = (ROOT/'scripts/lib.sh').read_text()
        lib = re.sub(r'/(?:usr/lib|lib|etc|run)/systemd',
                     lambda match: str(self.target)+match[0], original_lib)
        self.assertEqual(lib.replace(str(self.target), ''), original_lib,
                         'only absolute systemd roots may be retargeted')
        lib += '''
check_root() { :; }
install() {
    local -a args=()
    while [ "$#" -gt 0 ]; do
        case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
    done
    command install "${args[@]}"
}
'''
        (source/'scripts/lib.sh').write_text(lib)
        for name in ('install-status-cache.sh', 'install-phase-energy.sh'):
            script = (ROOT/'scripts'/name).read_text()
            script = script.replace('$source_root/etc/', '$source_root/SOURCE_ETC/')
            for prefix in ('/usr/local/libexec', '/etc/systemd/system', '/run/systemd/system', '/etc/sa02m-phase-energy.json'):
                script = script.replace(prefix, str(self.target)+prefix)
            script = script.replace('$source_root/SOURCE_ETC/', '$source_root/etc/')
            target = source/'scripts'/name; target.write_text(script)
            result = subprocess.run(['bash', str(target)], env=self.env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, name+'\n'+result.stdout+result.stderr)

    def test_real_online_and_offline_manifests_exclude_only_optional_units(self):
        online = self.manifest()
        destinations = {item['src']: item['dst'] for item in online['deploy']}
        self.assertTrue(all((self.overlay/p).is_file() for p in UNIT_PATHS))
        for rel in UNIT_PATHS:
            self.assertNotIn(rel, destinations)
        for rel in CONTROLS:
            self.assertIn(rel, destinations)
        services = online['services']
        for name in ('enable', 'restart', 'restart_if_active', 'stop_before_apply'):
            self.assertFalse({u.removesuffix('.service') for u in UNITS}
                             & {u.removesuffix('.service') for u in services[name]})
        spec = importlib.util.spec_from_file_location('offline_optional_unit_pack', ROOT/'scripts/pack-offline-update.py')
        pack = importlib.util.module_from_spec(spec); spec.loader.exec_module(pack)
        inventory = subprocess.CompletedProcess([], 0, ('\0'.join((*UNIT_PATHS, *CONTROLS))+'\0').encode())
        with patch.object(pack, 'run', return_value=inventory):
            selected = pack.list_allowlisted_files(pack.load_allowlist_patterns())
        self.assertTrue(set(CONTROLS) <= set(selected))
        self.assertFalse(set(UNIT_PATHS) & set(selected))

    def test_online_then_refresh_preserves_real_policy_decisions(self):
        for policy in ('absent', 'active', 'disabled', 'persistent-mask', 'runtime-mask', 'old-orphan'):
            with self.subTest(policy=policy):
                # Each subcase has an independent target and manager state.
                with OptionalUnitTests('runTest') as case:
                    case.setUp()
                    self.addCleanup(case.doCleanups)
                    case.exercise_policy(policy)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.doCleanups()

    def exercise_policy(self, policy):
        state = {}
        masks = {}
        for unit in UNITS:
            persistent = self.target/'etc/systemd/system'/unit
            runtime = self.target/'run/systemd/system'/unit
            if policy != 'absent':
                persistent.write_text('previous operator fragment\n')
                state[unit] = {'enabled': 'enabled' if policy == 'active' else 'disabled',
                               'active': 'active' if policy == 'active' else 'inactive', 'timestamp': 1}
            if policy in ('persistent-mask', 'runtime-mask'):
                mask = persistent if policy == 'persistent-mask' else runtime
                if mask.exists(): mask.unlink()
                mask.symlink_to('/dev/null'); masks[unit] = (mask, mask.lstat().st_ino)
        if policy not in ('absent', 'old-orphan'):
            for name in ('sa02m-status-cache.py', 'sa02m-status-part',
                         'sa02m-phase-energy/energy_state.py', 'sa02m-phase-energy/phase_energy.py',
                         'sa02m-phase-energy/mqtt_service.py'):
                old_runtime = self.target/'usr/local/libexec'/name
                old_runtime.parent.mkdir(parents=True, exist_ok=True)
                old_runtime.write_bytes(b'previous installed runtime\n')
        (self.target/'manager.json').write_text(json.dumps(state))
        config = self.target/'etc/sa02m-phase-energy.json'
        config.write_bytes(b'{"enabled":false,"operator_marker":"preserve"}\n')
        data = self.target/'var/lib/sa02m-phase-energy/state.json'
        data.write_bytes(b'{"operator_state":"preserve"}\n')
        for attempt in range(2):
            before = {unit: (self.target/'etc/systemd/system'/unit).read_bytes()
                      for unit in UNITS if (self.target/'etc/systemd/system'/unit).is_file()
                      and not (self.target/'etc/systemd/system'/unit).is_symlink()}
            self.apply_online()
            if policy == 'absent' and attempt == 0:
                self.assertFalse(any((self.target/rel).exists() for rel in UNIT_PATHS), 'OTA created an orphan unit')
                self.assertEqual(json.loads((self.target/'manager.json').read_text()), {})
            for unit, old in before.items():
                self.assertEqual((self.target/'etc/systemd/system'/unit).read_bytes(), old, 'OTA replaced optional fragment')
            self.full_refresh()
            observed = json.loads((self.target/'manager.json').read_text())
            for unit in UNITS:
                expected_on = policy == 'active' or (policy == 'absent' and unit == UNITS[0])
                self.assertEqual(observed[unit]['enabled'], 'enabled' if expected_on else 'disabled')
                self.assertEqual(observed[unit]['active'], 'active' if expected_on else 'inactive')
                if unit in masks:
                    mask, inode = masks[unit]
                    self.assertTrue(mask.is_symlink()); self.assertEqual(os.readlink(mask), '/dev/null')
                    self.assertEqual(mask.lstat().st_ino, inode)
                    if policy == 'runtime-mask':
                        self.assertEqual((self.target/'etc/systemd/system'/unit).read_bytes(), before[unit])
            self.assertTrue((self.target/'usr/local/libexec/sa02m-status-cache.py').is_file())
            self.assertTrue((self.target/'usr/local/libexec/sa02m-phase-energy/mqtt_service.py').is_file())
            self.assertEqual(config.read_bytes(), b'{"enabled":false,"operator_marker":"preserve"}\n')
            self.assertEqual(data.read_bytes(), b'{"operator_state":"preserve"}\n')
        calls = [json.loads(line) for line in (self.target/'manager-calls').read_text().splitlines()]
        self.assertFalse(any(call[0] == 'unmask' for call in calls))
        if policy in ('disabled', 'old-orphan', 'persistent-mask', 'runtime-mask'):
            self.assertFalse(any(call[0] in ('start', 'restart', 'enable') for call in calls))


if __name__ == '__main__':
    unittest.main()
