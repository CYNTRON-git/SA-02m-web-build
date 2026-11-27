#!/usr/bin/env python3
# comment-mutation-proof-exempt: Uses real ext4 allocation, a retired key and a journal to compare bytes of the shipped final disk writer.
"""Run the production cleaner on a real filesystem in ordinary temporary files."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HELPER = Path(__file__).resolve().parents[2] / 'tools/imaging/zero-free-ext4.py'
spec = importlib.util.spec_from_file_location('zero_ext4', HELPER)
zero_ext4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zero_ext4)


class FinalFreeBlocks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fs = self.root / 'root.ext4'
        self.image = self.root / 'source.img'
        self.output = self.root / 'output.img'
        with self.fs.open('wb') as stream:
            stream.truncate(32 * 1024**2)
        subprocess.run(
            [
                'mke2fs',
                '-q',
                '-t',
                'ext4',
                '-b',
                '4096',
                '-g',
                '1024',
                '-E',
                'lazy_itable_init=0,lazy_journal_init=0',
                '-F',
                self.fs,
            ],
            check=True,
        )
        self.key = b'ephemeral retired SSH key fixture\n' + os.urandom(256)
        self.live = b'preserved allocated configuration\n'
        self.save_file('host-key', self.key)
        self.save_file('live-config', self.live)
        self.debug('rm /host-key')
        self.offset = 6144 * 512
        header = bytearray(512)
        header[-2:] = b'\x55\xaa'
        for index, (kind, first, length) in enumerate(
            [(12, 2048, 4096), (131, 6144, self.fs.stat().st_size // 512)]
        ):
            struct.pack_into('<4xB3xII', header, 446 + 16 * index, kind, first, length)
        with self.image.open('wb') as stream:
            stream.write(header)
            stream.seek(2048 * 512)
            stream.write(b'boot partition must survive')
            stream.seek(self.offset)
            stream.write(self.fs.read_bytes())
            stream.write(b'post-partition bytes'.ljust(512, b'\0'))

    def debug(self, operation):
        return subprocess.check_output(['debugfs', '-w', '-R', operation, self.fs], stderr=subprocess.DEVNULL)

    def save_file(self, name, data):
        src = self.root / name
        src.write_bytes(data)
        self.debug(f'write {src} /{name}')

    def test_real_copy_zeros_retired_file_and_preserves_allocated_bytes_and_journal(self):
        original = self.image.read_bytes()
        self.assertEqual(original.count(self.key), 1)
        journal = self.root / 'journal-before'
        self.debug(f'dump <8> {journal}')
        self.assertTrue(journal.stat().st_size)
        result = json.loads(subprocess.check_output([sys.executable, HELPER, self.image, self.output]))
        data = self.output.read_bytes()
        self.assertEqual(data.count(self.key), 0)
        self.assertEqual(self.image.read_bytes(), original)
        self.assertEqual(len(data), len(original))
        self.assertEqual(data[: self.offset], original[: self.offset])
        self.assertEqual(data[-512:], original[-512:])
        self.assertGreater(result['cleared_blocks'], 0)
        self.assertEqual(result['copy_sha256'], hashlib.sha256(data).hexdigest())
        self.fs.write_bytes(data[self.offset : -512])
        self.assertEqual(self.debug('cat /live-config'), self.live)
        after = self.root / 'journal-after'
        self.debug(f'dump <8> {after}')
        self.assertEqual(after.read_bytes(), journal.read_bytes())
        repeat = zero_ext4.clean_copy(self.output, self.root / 'repeat.img')
        self.assertEqual(repeat['cleared_blocks'], 0)
        self.assertEqual(repeat['copy_sha256'], result['copy_sha256'])

    def test_existing_output_and_malformed_geometry_preserve_ordinary_inputs(self):
        src_hash = hashlib.sha256(self.image.read_bytes()).digest()
        with self.assertRaises(ValueError):
            zero_ext4.clean_copy(self.image, self.image)
        self.output.symlink_to(self.image)
        with self.assertRaises(ValueError):
            zero_ext4.clean_copy(self.image, self.output)
        self.assertEqual(hashlib.sha256(self.image.read_bytes()).digest(), src_hash)
        self.output.unlink()
        with self.image.open('r+b') as stream:
            stream.seek(446 + 16 + 12)
            stream.write(struct.pack('<I', 0xFFFFFFFF))
        with self.assertRaises(ValueError):
            zero_ext4.clean_copy(self.image, self.output)
        self.assertFalse(self.output.exists())

    def test_corrupt_filesystem_is_refused_before_publication(self):
        with self.image.open('r+b') as stream:
            stream.seek(self.offset + 1024 + 56)
            stream.write(b'XX')
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            zero_ext4.clean_copy(self.image, self.output)
        self.assertFalse(self.output.exists())

    def test_allocated_write_after_projection_is_rejected_before_publication(self):
        original = self.image.read_bytes()
        target = original.index(self.live)
        verify = zero_ext4.free_blocks

        def corrupt_copy(stream, offset, length):
            if stream.writable():
                stream.seek(target)
                stream.write(b'X')
                stream.flush()
            return verify(stream, offset, length)

        with patch.object(zero_ext4, 'free_blocks', side_effect=corrupt_copy):
            with self.assertRaisesRegex(ValueError, 'readback'):
                zero_ext4.clean_copy(self.image, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.image.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
