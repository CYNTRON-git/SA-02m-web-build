#!/usr/bin/env python3
# comment-mutation-proof-exempt: runs the real installer against filesystem fixtures and the real offline packer selection against a tracked-file inventory.
"""Collector installation policy; no host systemd or physical board is touched."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class InstallTests(unittest.TestCase):
    def test_readlink_failure_refuses_before_unit_install(self):
        source = (ROOT / 'scripts/install-status-cache.sh').read_text()
        begin = source.index('unit=/etc/systemd/system/sa02m-status-cache.service')
        block = source[begin:]
        if '# Never-started Yandex client' in block:
            block = block[:block.index('# Never-started Yandex client')]
        for policy in ('persistent', 'runtime', 'runtime-existing'):
            for output in ('', '/dev/null'):
                with self.subTest(policy=policy, output=output), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    persistent, runtime = root/'etc/systemd/system', root/'run/systemd/system'
                    persistent.mkdir(parents=True); runtime.mkdir(parents=True)
                    target = persistent/'sa02m-status-cache.service'
                    mask = (persistent if policy == 'persistent' else runtime)/'sa02m-status-cache.service'
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

    def test_mask_before_first_install_and_repeat_remains_masked(self):
        self.exercise(masked=True)

    def test_normal_install_delivers_complete_runtime(self):
        self.exercise(masked=False)

    def exercise(self, masked):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            src = base/'source'
            scripts = src/'scripts'
            scripts.mkdir(parents=True)
            dest = base/'target'
            unit = dest/'etc/systemd/system/sa02m-status-cache.service'
            unit.parent.mkdir(parents=True)
            if masked:
                unit.symlink_to('/dev/null')
            for name in ('etc/sa02m-status-part.sh', 'etc/sa02m-status-cache.py', 'etc/systemd/system/sa02m-status-cache.service'):
                target = src/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT/name).read_bytes())
            script = (ROOT/'scripts/install-status-cache.sh').read_text()
            script = script.replace('$source_root/etc/', '$source_root/SOURCE_ETC/')
            script = script.replace('/usr/local/libexec', str(dest/'usr/local/libexec')).replace('/etc/systemd/system', str(dest/'etc/systemd/system'))
            script = script.replace('$source_root/SOURCE_ETC/', '$source_root/etc/')
            (scripts/'install-status-cache.sh').write_text(script)
            # Keep installer decisions real. Replace only host-root/systemctl
            # and the atomic copy transport, confined to this temp target.
            (scripts/'lib.sh').write_text('''check_root() { :; }
log() { :; }
sa02m_svc_capture() { :; }
sa02m_svc_apply() { :; }
sa02m_systemctl() { :; }
sa02m_atomic_install() {
    local source=${@: -2:1} target=${@: -1}
    cp "$source" "$target.tmp"
    mv -f "$target.tmp" "$target"
}
''')
            for _ in range(2):
                result = subprocess.run(['bash', str(scripts/'install-status-cache.sh')], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if masked:
                    self.assertTrue(unit.is_symlink(), 'installer replaced an operator mask')
                    self.assertEqual(os.readlink(unit), '/dev/null')
                else:
                    self.assertEqual(unit.read_bytes(), (ROOT/'etc/systemd/system/sa02m-status-cache.service').read_bytes())
                self.assertEqual((dest/'usr/local/libexec/sa02m-status-part').read_bytes(), (ROOT/'etc/sa02m-status-part.sh').read_bytes())
                self.assertEqual((dest/'usr/local/libexec/sa02m-status-cache.py').read_bytes(), (ROOT/'etc/sa02m-status-cache.py').read_bytes())

    def test_runtime_mask_is_not_shadowed_by_first_install_or_refresh(self):
        source = (ROOT / 'scripts/install-status-cache.sh').read_text()
        block = source[source.index('unit=/etc/systemd/system/sa02m-status-cache.service'):]
        lib = (ROOT / 'scripts/lib.sh').read_text()
        begin = lib.index('sa02m_atomic_install() {')
        atomic = lib[begin:lib.index('\n}\n', begin) + 3]
        for old_unit in (None, b'previous operator unit\n'):
            with self.subTest(existing=old_unit is not None), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                persistent, runtime = root/'etc/systemd/system', root/'run/systemd/system'
                persistent.mkdir(parents=True); runtime.mkdir(parents=True)
                target = persistent/'sa02m-status-cache.service'
                mask = runtime/target.name
                mask.symlink_to('/dev/null')
                if old_unit is not None:
                    target.write_bytes(old_unit)
                inode = mask.lstat().st_ino
                callsite = block.replace('$source_root/etc/', '$source_root/SOURCE_ETC/')
                callsite = callsite.replace('/etc/systemd/system', str(persistent)).replace('/run/systemd/system', str(runtime))
                callsite = callsite.replace('$source_root/SOURCE_ETC/', '$source_root/etc/')
                script = """set -euo pipefail
log() { :; }
sync() { :; }
sa02m_systemctl() { printf 'systemctl %s\\n' "$*" >> "$CALLS"; }
sa02m_svc_apply() { printf 'apply %s\\n' "$*" >> "$CALLS"; }
install() {
    local -a args=()
    while [ "$#" -gt 0 ]; do
        case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
    done
    command install "${args[@]}"
}
""" + atomic + '\n'
                env = dict(os.environ, source_root=str(ROOT), LOG_FILE=str(root/'log'), CALLS=str(root/'calls'))
                for _ in range(2):
                    result = subprocess.run(['bash', '-c', script + callsite], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(mask.is_symlink())
                    self.assertEqual(os.readlink(mask), '/dev/null')
                    self.assertEqual(mask.lstat().st_ino, inode)
                    if old_unit is None:
                        self.assertFalse(target.exists(), 'an installed /etc unit must not shadow the runtime mask')
                    else:
                        self.assertEqual(target.read_bytes(), old_unit, 'runtime mask also preserves the prior /etc fragment')
                self.assertNotIn('unmask', (root/'calls').read_text())
                self.assertEqual((root/'calls').read_text().splitlines(),
                                 ['systemctl daemon-reload', 'apply sa02m-status-cache.service app on'] * 2)

    def test_web_only_offline_payload_does_not_install_orphan_collector_unit(self):
        spec = importlib.util.spec_from_file_location('offline_pack', ROOT/'scripts/pack-offline-update.py')
        pack = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pack)
        files = ['www/network_config/VERSION', 'www/network_config/cgi-bin/status_snapshot.cgi',
                 'etc/systemd/system/sa02m-status-cache.service', 'etc/sa02m-status-cache.py',
                 'etc/sa02m-status-part.sh', 'etc/systemd/system/sa02m-rules.service']
        inventory = subprocess.CompletedProcess([], 0, '\0'.join(files).encode()+b'\0')
        with patch.object(pack, 'run', return_value=inventory):
            selected = pack.list_allowlisted_files(pack.load_allowlist_patterns())
        self.assertIn('etc/systemd/system/sa02m-rules.service', selected)
        self.assertIn('www/network_config/cgi-bin/status_snapshot.cgi', selected)
        self.assertFalse(any('sa02m-status-cache' in name or 'sa02m-status-part' in name for name in selected))


if __name__ == '__main__':
    unittest.main()
