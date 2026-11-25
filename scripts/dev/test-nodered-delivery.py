#!/usr/bin/env python3
# comment-mutation-proof-exempt: Executes both real deployment generators and the real fullrefresh atomic-install block. Missing inputs, old updater tiers and incomplete boundaries refuse; output hashes, modes and installed destinations are asserted.
"""Pinned vendor payload delivery must be complete before its caller is used."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PINS = {
    'etc/sa02m-vendor-install.py': ('/usr/local/lib/sa02m-vendor-install.py', '0755'),
    'etc/vendor-payloads.json': ('/usr/local/share/sa02m/vendor-payloads.json', '0644'),
    'etc/systemd/system/nodered.service': ('/usr/local/share/sa02m/nodered.service', '0644'),
}


class DeliveryTests(unittest.TestCase):
    def test_actual_online_and_offline_generators(self):
        spec = importlib.util.spec_from_file_location('vendor_pack_test', ROOT/'scripts/pack-offline-update.py')
        pack = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = pack
        spec.loader.exec_module(pack)
        mapping = json.loads((ROOT/'scripts/offline-update-deploy-map.json').read_text())
        with self.assertRaises(SystemExit):
            pack.build_deploy_list(list(PINS), mapping, '1.0.8.1')
        with self.assertRaises(SystemExit):
            pack.build_deploy_list(list(PINS)[:-1], mapping, '1.0.8.2')
        offline = pack.build_deploy_list(list(PINS), mapping, '1.0.8.2')
        source = (ROOT/'etc/sa02m-update-runner.sh').read_text()
        begin = source.index('prepare_github_overlay() {')
        end = source.index('\n}\n\n# --- validate + extract', begin) + 3
        function = source[begin:end]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            overlay = base/'overlay'
            for rel in PINS:
                target = overlay/rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT/rel).read_bytes())
            script = '''set -euo pipefail
txn_get() {
 case "$1" in
 overlay_path) printf '%s' "$SOURCE_OVERLAY";;
 target_version) printf '1.0.8.2';;
 target_commit) printf '1111111111111111111111111111111111111111';;
 previous_version) printf '1.0.8.1';;
 *) exit 2;; esac
}
die() { printf '%s\\n' "$*" >&2; exit 1; }
log() { :; }
''' + function + '\nprepare_github_overlay fixture\n'
            result = subprocess.run(['bash','-c',script], env={**os.environ, 'STATEDIR':str(base/'state'), 'SOURCE_OVERLAY':str(overlay)}, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((base/'state/staging/fixture/meta/manifest.json').read_text())
            self.assertEqual(manifest['min_updater'], '1.0.8.2')
            online = manifest['deploy']
            (overlay/'etc/vendor-payloads.json').unlink()
            result = subprocess.run(['bash','-c',script], env={**os.environ, 'STATEDIR':str(base/'partial-state'), 'SOURCE_OVERLAY':str(overlay)}, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((base/'partial-state/staging/fixture/meta/manifest.json').exists())
        expected = {src:(dst,mode,'root:root') for src,(dst,mode) in PINS.items()}
        for entries in (online, offline):
            self.assertEqual({e['src']:(e['dst'],e['mode'],e['owner']) for e in entries}, expected)

    def test_real_fullrefresh_atomic_install_block(self):
        source = (ROOT/'scripts/03-webserver.sh').read_text()
        begin = source.index('# Pinned vendor payload metadata must precede')
        end = source.index('# ── Политика сторонних стеков', begin)
        self.assertLess(end, source.index('"$SCRIPT_DIR/../etc/sa02m-web-service-ctl.sh"'))
        block = source[begin:end]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            block = block.replace('/usr/local/', str(target)+'/usr/local/')
            script = '. "$SOURCE_LIB"\nlog() { :; }\n' + block
            env = {**os.environ, 'SOURCE_LIB':str(ROOT/'scripts/lib.sh'), 'ETC_DIR':str(ROOT/'etc'), 'LOG_FILE':str(target/'install.log')}
            result = subprocess.run(['bash','-c',script], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            for src,(dst,mode) in PINS.items():
                installed = target/dst.lstrip('/')
                self.assertEqual(hashlib.sha256(installed.read_bytes()).digest(), hashlib.sha256((ROOT/src).read_bytes()).digest())
                self.assertEqual(installed.stat().st_mode & 0o777, int(mode,8))
            # A missing trust input must refuse before another installed file changes.
            hashes = {p:p.read_bytes() for p in target.rglob('*') if p.is_file()}
            env['ETC_DIR'] = str(target/'missing')
            result = subprocess.run(['bash','-c',script], env=env, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(all(p.read_bytes()==data for p,data in hashes.items()))


if __name__ == '__main__':
    unittest.main()
