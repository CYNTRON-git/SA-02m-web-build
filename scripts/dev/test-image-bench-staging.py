#!/usr/bin/env python3
"""Exercise offline image cleanup on Linux directory descriptors and the image producer."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('bench_staging', ROOT / 'tools/imaging/sanitize-bench-staging.py')
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


class ImageBenchStaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.anchor = Path(self.tmp.name)
        self.stage = self.anchor / 'stage'
        self.stage.mkdir(mode=0o700)
        self.fd = os.open(self.anchor, os.O_DIRECTORY | os.O_RDONLY)
        self.addCleanup(os.close, self.fd)
        self.mid = bench.mount_id(self.fd)

    def test_manifest_and_systemd_shape(self):
        catalog = json.loads(bench.CATALOG.read_text())
        self.assertGreater(len(catalog), 0)
        for name, digest in catalog.items():
            path = Path(name)
            self.assertIn(str(path.parent), bench.PARENTS)
            self.assertNotIn('..', path.parts)
            self.assertRegex(digest, '^[0-9a-f]{64}$')
        (self.stage/'tmp').mkdir()
        (self.stage/'tmp').chmod(0o1777)
        digest, _ = bench.fingerprint(self.fd, 'stage', self.mid)
        self.assertEqual(digest, bench.EMPTY_PRIVATE_TMP)
        (self.stage/'tmp'/'operator-file').touch()
        self.assertNotEqual(bench.fingerprint(self.fd, 'stage', self.mid)[0], digest)

    def test_symlink_data_and_replacement(self):
        outside = self.anchor/'outside'
        outside.write_bytes(b'outside')
        (self.stage/'link').symlink_to(outside)
        (self.stage/'data').write_bytes(b'fixture')
        before, plan = bench.fingerprint(self.fd, 'stage', self.mid)
        outside.write_bytes(b'changed outside')
        self.assertEqual(bench.fingerprint(self.fd, 'stage', self.mid)[0], before)
        # Same pathname and equal data still represent another inode.
        (self.stage/'data').rename(self.anchor/'saved')
        (self.stage/'data').write_bytes(b'fixture')
        with self.assertRaisesRegex(ValueError, 'changed since inspection'):
            bench.inspect_tree(self.fd, 'stage', '.', self.mid, [], plan, remove=True)
        self.assertTrue((self.stage/'data').exists())
        _, fresh = bench.fingerprint(self.fd, 'stage', self.mid)
        bench.inspect_tree(self.fd, 'stage', '.', self.mid, [], fresh, remove=True)
        self.assertEqual(outside.read_bytes(), b'changed outside')
        self.assertFalse(self.stage.exists())

    def test_top_symlink_special_and_mount_guard(self):
        (self.anchor/'link').symlink_to(self.stage)
        with self.assertRaisesRegex(ValueError, 'type or owner'):
            bench.fingerprint(self.fd, 'link', self.mid)
        os.mkfifo(self.stage/'pipe')
        with self.assertRaisesRegex(ValueError, 'special file'):
            bench.fingerprint(self.fd, 'stage', self.mid)
        with self.assertRaisesRegex(ValueError, 'mount boundary'):
            bench.fingerprint(self.fd, 'stage', self.mid+1)
        self.assertTrue((self.stage/'pipe').is_fifo())

    def test_image_entrypoints(self):
        patch=(ROOT/'tools/imaging/patch-firstboot-image.sh').read_text()
        apply='python3 -I -B "$SCRIPT_DIR/sanitize-bench-staging.py" --root "$mnt" --device "$rootpart" --image "$IMG"'
        self.assertEqual(patch.count(apply),2)
        self.assertLess(patch.index('mount "$rootpart" "$mnt"'),patch.index(apply+' --apply'))
        self.assertLess(patch.index(apply+' --check'),patch.index('install -d "$mnt/etc/systemd/system"'))
        self.assertIn(apply+' --apply \\\n    || die',patch)
        self.assertIn(apply+' --check \\\n    || die',patch)
        make=(ROOT/'tools/imaging/make-image.sh').read_text()
        self.assertLess(make.index('run_firstboot_patch "$RAW_IMG" ||'),make.index('bash "$SEAL_SCRIPT"'))


if __name__ == '__main__':
    unittest.main()
