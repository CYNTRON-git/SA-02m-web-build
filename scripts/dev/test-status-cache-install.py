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
