#!/usr/bin/env python3
"""Create a verified copy of a sealed MBR image with free ext4 p2 blocks zeroed."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile

CHUNK = 8 * 1024**2


def partition_2(stream):
    sector = stream.read(512)
    if len(sector) != 512 or sector[-2:] != b'\x55\xaa':
        raise ValueError('missing MBR signature')
    entries = [struct.unpack_from('<4xB3xII', sector, 446 + 16 * i) for i in range(4)]
    boot, root = entries[:2]
    if (
        boot[0] not in (11, 12)
        or root[0] != 131
        or any(e != (0, 0, 0) for e in entries[2:])
        or boot[1] < 1
        or boot[2] < 1
        or root[1] < boot[1] + boot[2]
        or root[2] < 1
        or (root[1] + root[2]) * 512 > os.fstat(stream.fileno()).st_size
    ):
        raise ValueError('expected non-overlapping FAT p1 and Linux p2 within the raw image')
    return root[1] * 512, root[2] * 512


def free_blocks(stream, offset, length):
    # e2fsprogs opens an ordinary file at a byte offset. Passing an open fd
    # avoids mounting, executing image code, or interpreting '?' in a pathname.
    fd = stream.fileno()
    path = f'/proc/self/fd/{fd}'
    options = dict(env=dict(os.environ, LC_ALL='C'), pass_fds=(fd,), text=True)
    kind = subprocess.check_output(
        ['blkid', '-p', '-O', str(offset), '-S', str(length), '-s', 'TYPE', '-o', 'value', path], **options
    )
    if kind.strip() != 'ext4':
        raise ValueError('p2 must contain ext4')
    ext4 = f'{path}?offset={offset}'
    subprocess.run(['e2fsck', '-fn', ext4], stdout=subprocess.DEVNULL, check=True, **options)
    info = subprocess.check_output(['dumpe2fs', ext4], **options)
    features = re.search(r'^Filesystem features:\s*(.*)$', info, re.M)[1].split()
    state = re.search(r'^Filesystem state:\s*(.*)$', info, re.M)[1].strip()
    if (
        state != 'clean'
        or {'needs_recovery', 'bigalloc', 'journal_dev'} & set(features)
        or '\nJournal device:' in info
    ):
        raise ValueError('clean ext4 with a local block allocation map required')
    numbers = {
        k: int(v) for k, v in re.findall(r'^(Block size|Block count|Free blocks):\s*([0-9]+)$', info, re.M)
    }
    size, count = numbers['Block size'], numbers['Block count']
    if size < 1024 or size > 65536 or size & (size - 1) or size * count > length:
        raise ValueError('ext4 geometry exceeds p2')
    spans = []
    for line in info.splitlines():
        if not line.startswith('  Free blocks:'):
            continue
        for interval in line.split(':', 1)[1].split(','):
            if not interval.strip():
                continue
            first, _, last = interval.strip().partition('-')
            lo, hi = int(first), int(last or first)
            if lo < 0 or hi < lo or hi >= count or (spans and lo <= spans[-1][1]):
                raise ValueError('unordered or invalid free block interval')
            spans.append((lo, hi))
    if sum(hi - lo + 1 for lo, hi in spans) != numbers['Free blocks']:
        raise ValueError('incomplete free block inventory')
    return size, spans


def clean_copy(source, destination):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as original:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or os.path.lexists(destination):
            raise ValueError('expected regular source and new destination')
        offset, length = partition_2(original)
        block_size, spans = free_blocks(original, offset, length)
        original.seek(0)
        zeros = bytes(CHUNK)
        changed = 0
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.zero-ext4-') as copy:
            shutil.copyfileobj(original, copy, CHUNK)
            for lo, hi in spans:
                pos = offset + lo * block_size
                end = offset + (hi + 1) * block_size
                copy.seek(pos)
                while pos < end:
                    n = min(CHUNK, end - pos)
                    data = copy.read(n)
                    if len(data) != n:
                        raise ValueError('short filesystem copy')
                    if data != zeros[:n]:
                        changed += sum(
                            data[i : i + block_size] != zeros[:block_size] for i in range(0, n, block_size)
                        )
                        copy.seek(pos)
                        copy.write(zeros[:n])
                    pos += n
            copy.flush()
            os.fsync(copy.fileno())
            if free_blocks(copy, offset, length) != (block_size, spans):
                raise ValueError('free block map changed')
            if os.fstat(copy.fileno()).st_size != before.st_size:
                raise ValueError('disk size changed')
            # Independently compare every byte to the source or zero as selected
            # by the verified bitmap. In particular the journal remains allocated.
            original.seek(0)
            copy.seek(0)
            src_sha, output_sha = hashlib.sha256(), hashlib.sha256()

            def compare_to(end, free=False):
                while original.tell() < end:
                    n = min(CHUNK, end - original.tell())
                    prior, actual = original.read(n), copy.read(n)
                    if len(prior) != n or actual != (zeros[:n] if free else prior):
                        raise ValueError('readback differs from source or free-block projection')
                    src_sha.update(prior)
                    output_sha.update(actual)

            for lo, hi in spans:
                compare_to(offset + lo * block_size)
                compare_to(offset + (hi + 1) * block_size, free=True)
            compare_to(before.st_size)
            after = os.fstat(fd)
            if (before.st_size, before.st_ctime_ns, before.st_mtime_ns) != (
                after.st_size,
                after.st_ctime_ns,
                after.st_mtime_ns,
            ):
                raise ValueError('source changed during copy')
            os.link(copy.name, destination)
        return dict(
            source_sha256=src_sha.hexdigest(),
            copy_sha256=output_sha.hexdigest(),
            image_bytes=before.st_size,
            p2_offset=offset,
            p2_bytes=length,
            block_size=block_size,
            cleared_blocks=changed,
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(json.dumps(clean_copy(args.source, args.destination), sort_keys=True))
