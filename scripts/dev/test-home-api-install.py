#!/usr/bin/env python3
# comment-mutation-proof-exempt: runs shipped 06-alice and OTA apply functions in private filesystem fixtures; masks, journals, progress and installed bytes are observed, not source needles.
"""Home API unit installation policy; no host services or privileges."""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
UNIT = 'sa02m-home-api.service'


class HomeInstallTests(unittest.TestCase):
    def test_readlink_failure_refuses_before_unit_install(self):
        source = (ROOT / 'scripts/06-alice.sh').read_text()
        begin = source.index('home_unit=/etc/systemd/system/sa02m-home-api.service')
        block = source[begin:]
        if '# Never-started Yandex client' in block:
            block = block[:block.index('# Never-started Yandex client')]
        for policy in ('persistent', 'runtime', 'runtime-existing'):
            for output in ('', '/dev/null'):
                with self.subTest(policy=policy, output=output), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    persistent, runtime = root/'etc/systemd/system', root/'run/systemd/system'
                    persistent.mkdir(parents=True); runtime.mkdir(parents=True)
                    target = persistent/'sa02m-home-api.service'
                    mask = (persistent if policy == 'persistent' else runtime)/'sa02m-home-api.service'
                    mask.symlink_to('/dev/null')
                    if policy == 'runtime-existing':
                        target.write_bytes(b'previous unit')
                    inode = mask.lstat().st_ino
                    calls = root/'calls'; calls.write_text('')
                    callsite = block.replace('$source_root/etc/', '$source_root/SOURCE_ETC/')
                    callsite = callsite.replace('/etc/systemd/system', str(persistent)).replace('/run/systemd/system', str(runtime))
                    callsite = callsite.replace('$source_root/SOURCE_ETC/', '$source_root/etc/')
                    script = """set -euo pipefail
readlink() { printf '%s' "$LINK_OUTPUT"; return 1; }
log() { printf '%s\n' "$*" >&2; }
systemctl() { :; }
sa02m_systemctl() { :; }
sa02m_svc_apply() { :; }
sa02m_atomic_install() { printf 'install\n' >> "$CALLS"; }
"""
                    env = dict(os.environ, source_root=str(ROOT), UNIT_SRC=str(ROOT/'etc/systemd/system'),
                               CALLS=str(calls), LINK_OUTPUT=output)
                    result = subprocess.run(['bash', '-c', script + callsite], env=env, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0, 'an unreadable mask must fail even if readlink printed a target')
                    self.assertIn('Cannot inspect', result.stderr)
                    self.assertEqual(calls.read_text(), '', 'no unit installation after inspection failure')
                    self.assertTrue(mask.is_symlink())
                    self.assertEqual(mask.lstat().st_ino, inode)
                    self.assertEqual(os.readlink(mask), '/dev/null')
                    if policy == 'runtime':
                        self.assertFalse(target.exists())
                    elif policy == 'runtime-existing':
                        self.assertEqual(target.read_bytes(), b'previous unit')

    def test_real_callsite_preserves_persistent_and_runtime_masks(self):
        for policy in ('persistent', 'runtime'):
            with self.subTest(policy=policy):
                self.exercise(policy)

    def test_first_install_and_refresh_deliver_intended_unit(self):
        for policy in ('new', 'existing'):
            with self.subTest(policy=policy):
                self.exercise(policy)

    def exercise(self, policy):
        source = (ROOT/'scripts/06-alice.sh').read_text(encoding='utf-8')
        begin = source.index('sa02m_svc_capture sa02m-alice-config.service')
        end = source.index('# Never-started Yandex client', begin)
        block = source[begin:end]
        self.assertIn('sa02m-home-api.service', block, 'the real callsite must remain covered')
        lib = (ROOT/'scripts/lib.sh').read_text(encoding='utf-8')
        begin = lib.index('sa02m_atomic_install() {')
        atomic = lib[begin:lib.index('\n}\n', begin)+3]
        with tempfile.TemporaryDirectory(prefix='sa-home-install-') as tmp:
            work = Path(tmp)
            persistent, runtime = work/'etc/systemd/system', work/'run/systemd/system'
            persistent.mkdir(parents=True)
            runtime.mkdir(parents=True)
            target, mask = persistent/UNIT, (runtime if policy == 'runtime' else persistent)/UNIT
            if policy in ('persistent', 'runtime'):
                mask.symlink_to('/dev/null')
            elif policy == 'existing':
                target.write_bytes(b'old unit\n')
            block = block.replace('/etc/systemd/system', str(persistent)).replace('/run/systemd/system', str(runtime))
            script = '''set -euo pipefail
log() { :; }
sync() { :; }
systemctl() { printf 'systemctl %s\\n' "$*" >> "$CALLS"; }
sa02m_svc_capture() { printf 'capture %s\\n' "$*" >> "$CALLS"; }
sa02m_svc_apply() { printf 'apply %s\\n' "$*" >> "$CALLS"; }
# Preserve the real atomic rename, omitting only root ownership in this scratch.
install() {
    local -a args=()
    while [ "$#" -gt 0 ]; do
        case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
    done
    command install "${args[@]}"
}
'''+atomic+'\n'
            env = dict(os.environ, UNIT_SRC=str(ROOT/'etc/systemd/system'),
                       LOG_FILE=str(work/'log'), CALLS=str(work/'calls'))
            for _ in range(2):
                result = subprocess.run(['bash', '-c', script+block], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if policy in ('persistent', 'runtime'):
                    self.assertTrue(mask.is_symlink(), 'installer replaced the operator mask')
                    self.assertEqual(os.readlink(mask), '/dev/null')
                    if policy == 'runtime':
                        self.assertFalse(target.exists(), 'a new /etc fragment must not shadow the runtime mask')
                else:
                    self.assertEqual(target.read_bytes(), (ROOT/'etc/systemd/system'/UNIT).read_bytes())
            calls = (work/'calls').read_text()
            self.assertIn('apply sa02m-home-api.service app off', calls)
            self.assertNotIn('systemctl unmask', calls)


class HomeUpdateTests(unittest.TestCase):
    def test_real_apply_preserves_masks_without_blocking_other_files(self):
        for policy in ('persistent', 'runtime', 'runtime-existing'):
            with self.subTest(policy=policy):
                self.exercise(policy)

    def test_real_apply_installs_and_refreshes_unmasked_unit(self):
        for policy in ('new', 'existing'):
            with self.subTest(policy=policy):
                self.exercise(policy)

    def test_mask_inspection_failure_refuses_before_any_install(self):
        self.exercise('persistent', inspect_failure=True)

    def exercise(self, policy, inspect_failure=False):
        source = (ROOT/'etc/sa02m-update-runner.sh').read_text(encoding='utf-8')
        functions = []
        for name in ('apply_deploy_items', 'is_unchanged', 'atomic_install_file', 'journal_append'):
            start = source.index(name+'() {\n')
            functions.append(source[start:source.index('\n}\n', start)+3])
        with tempfile.TemporaryDirectory(prefix='sa-home-update-') as tmp:
            root = Path(tmp)
            stage = root/'state/staging/fixture'
            (stage/'overlay').mkdir(parents=True)
            (stage/'backups').mkdir()
            persistent, runtime = root/'etc/systemd/system', root/'run/systemd/system'
            persistent.mkdir(parents=True)
            runtime.mkdir(parents=True)
            target = persistent/UNIT
            mask = (runtime if policy.startswith('runtime') else persistent)/UNIT
            masked = policy in ('persistent', 'runtime', 'runtime-existing')
            if masked:
                mask.symlink_to('/dev/null')
                inode = mask.lstat().st_ino
            if policy in ('existing', 'runtime-existing'):
                target.write_bytes(b'old unit\n')
            unit_bytes = (ROOT/'etc/systemd/system'/UNIT).read_bytes()
            (stage/'overlay/unit').write_bytes(unit_bytes)
            neighbor = root/'other-file'
            neighbor.write_bytes(b'old neighbor\n')
            (stage/'overlay/neighbor').write_bytes(b'new neighbor\n')
            (stage/'manifest.json').write_text(json.dumps({'deploy': [
                {'src': 'unit', 'dst': str(target), 'mode': '0644', 'owner': ''},
                {'src': 'neighbor', 'dst': str(neighbor), 'mode': '0644', 'owner': ''},
            ]}), encoding='utf-8')
            code = '\n'.join(functions).replace('/etc/systemd/system', str(persistent)).replace('/run/systemd/system', str(runtime))
            script = '''set -euo pipefail
log() { printf '%s\\n' "$*"; }
txn_patch() { printf '%s\\n' "$*" >> "$PATCHES"; }
manifest_path() { printf '%s/staging/%s/manifest.json' "$STATEDIR" "$1"; }
'''+code+'\n'
            if inspect_failure:
                script += 'readlink() { return 1; }\n'
            script += 'if ! apply_deploy_items fixture; then exit 9; fi\n'
            env = dict(os.environ, STATEDIR=str(root/'state'), PATCHES=str(root/'patches'))
            for _ in range(2):
                result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
                if inspect_failure:
                    self.assertEqual(result.returncode, 9, result.stdout+result.stderr)
                    self.assertIn('cannot inspect Home API mask', result.stdout)
                    self.assertTrue(mask.is_symlink())
                    self.assertEqual(mask.lstat().st_ino, inode)
                    self.assertEqual(neighbor.read_bytes(), b'old neighbor\n')
                    self.assertFalse((stage/'journal.jsonl').exists())
                    return
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertEqual(neighbor.read_bytes(), b'new neighbor\n')
                if masked:
                    self.assertTrue(mask.is_symlink(), 'update destroyed mask')
                    self.assertEqual(os.readlink(mask), '/dev/null')
                    self.assertEqual(mask.lstat().st_ino, inode)
                    if policy == 'runtime':
                        self.assertFalse(target.exists(), 'update shadowed runtime mask')
                    elif policy == 'runtime-existing':
                        self.assertEqual(target.read_bytes(), b'old unit\n')
                    entries = [json.loads(line) for line in (stage/'journal.jsonl').read_text().splitlines()]
                    self.assertNotIn(str(target), [entry['dst'] for entry in entries], 'preserved mask must not enter rollback journal')
                else:
                    self.assertEqual(target.read_bytes(), unit_bytes)
                self.assertIn('files_total=2 files_done=0', (root/'patches').read_text())
                self.assertIn('files_done=2 progress_pct=100', (root/'patches').read_text())


if __name__ == '__main__':
    unittest.main()
