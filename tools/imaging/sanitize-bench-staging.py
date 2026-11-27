#!/usr/bin/env python3
"""Remove verified commissioning trees only from the mounted copy of a loop image."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

CATALOG = Path(__file__).with_name('bench-staging-sha256.json')
PARENTS = ('opt', 'root', 'var/tmp')
PRIVATE_TMP = re.compile(r'systemd-private-[0-9a-f]{32}-(?:chrony|ModemManager|polkit|systemd-logind)\.service-[A-Za-z0-9]{6}')
EMPTY_PRIVATE_TMP = '66fac2837a28112215df650febd03a33a8d8fe3c7a46b2dbc04b4676ebd3d75e'
PATH_FLAGS = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC


@contextlib.contextmanager
def opened(name, flags, parent=None):
    fd = os.open(name, flags, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def mount_id(fd):
    for line in Path('/proc/self/fdinfo', str(fd)).read_text().splitlines():
        if line.startswith('mnt_id:'):
            return int(line.split()[1])
    raise ValueError('missing Linux mount identity')


def check_image(root_fd, image, device):
    fs = os.fstat(root_fd)
    block = device.lstat()
    if (not stat.S_ISREG(image.lstat().st_mode) or not stat.S_ISBLK(block.st_mode)
            or block.st_rdev != fs.st_dev or fs.st_dev == os.stat('/').st_dev):
        raise ValueError('offline root and image device do not match')
    kernel = Path('/sys/dev/block', f'{os.major(fs.st_dev)}:{os.minor(fs.st_dev)}').resolve(strict=True)
    loop = kernel.parent if (kernel / 'partition').is_file() else kernel
    if not re.fullmatch(r'loop[0-9]+', loop.name):
        raise ValueError('offline image requires a loop device')
    backing = (loop / 'loop/backing_file').read_text().rstrip('\n')
    if not os.path.samefile(backing, image):
        raise ValueError('loop backing file differs from the selected image')
    mid = mount_id(root_fd)
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields, detail = line.split(' - ', 1)
        ids = fields.split()
        if int(ids[0]) == mid:
            mountpoint = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), ids[4])
            if (ids[3] == '/' and detail.split()[0] == 'ext4'
                    and os.readlink(f'/proc/self/fd/{root_fd}') == mountpoint):
                return mid
            break
    raise ValueError('selected root must be the ext4 mountpoint')


def require_mount(fd, expected):
    if mount_id(fd) != expected:
        raise ValueError('mount boundary inside bench sanitation scope')


def inspect_tree(parent, name, relative, expected, records, identities, *, remove=False):
    with opened(name, PATH_FLAGS, parent) as fd:
        item = os.fstat(fd)
        require_mount(fd, expected)
        identity = (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns)
        if remove:
            if identities.get(relative) != identity:
                raise ValueError('bench entry changed since inspection')
        else:
            identities[relative] = identity
        mode = stat.S_IFMT(item.st_mode)
        directory = stat.S_ISDIR(mode)
        if relative == '.' and (not directory or item.st_uid != 0):
            raise ValueError('unexpected type or owner for a bench directory')
        digest = hashlib.sha256()
        if stat.S_ISREG(mode):
            if not remove:
                with open(f'/proc/self/fd/{fd}', 'rb') as contents:
                    for block in iter(lambda: contents.read(1 << 20), b''):
                        digest.update(block)
        elif stat.S_ISLNK(mode):
            digest.update(os.fsencode(os.readlink('', dir_fd=fd)))
        elif not directory:
            raise ValueError('special file in a bench directory')
        if not remove:
            records.append([relative, mode, stat.S_IMODE(item.st_mode), digest.hexdigest()])
        if directory:
            with opened('.', os.O_RDONLY | os.O_DIRECTORY, fd) as children:
                for child in sorted(os.listdir(children)):
                    child_path = child if relative == '.' else relative + '/' + child
                    inspect_tree(children, child, child_path, expected, records, identities, remove=remove)
        if remove:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not os.path.samestat(item, current):
                raise ValueError('bench entry changed during sanitation')
            (os.rmdir if directory else os.unlink)(name, dir_fd=parent)


def fingerprint(parent, name, mid):
    records = []
    identities = {}
    inspect_tree(parent, name, '.', mid, records, identities)
    digest = hashlib.sha256(json.dumps(records, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    return digest, identities


def sanitize(root, device, image, apply):
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('absolute offline root required')
    catalog = json.loads(CATALOG.read_text())
    with contextlib.ExitStack() as stack:
        def enter(name, parent=None):
            return stack.enter_context(opened(name, PATH_FLAGS | os.O_DIRECTORY, parent))

        fd = enter('/')
        for part in root.parts[1:]:
            fd = enter(part, fd)
        mid = check_image(fd, image, device)
        candidates = []
        for scope in PARENTS:
            parent = fd
            try:
                for part in scope.split('/'):
                    parent = enter(part, parent)
                    require_mount(parent, mid)
            except FileNotFoundError:
                continue
            listing = stack.enter_context(opened('.', os.O_RDONLY | os.O_DIRECTORY, parent))
            for name in sorted(os.listdir(listing)):
                relative = scope + '/' + name
                wanted = catalog.get(relative)
                if scope == 'var/tmp' and PRIVATE_TMP.fullmatch(name):
                    wanted = EMPTY_PRIVATE_TMP
                if wanted is None:
                    continue
                if not apply:
                    raise ValueError('commissioning residue at ' + relative)
                digest, identities = fingerprint(parent, name, mid)
                if digest != wanted:
                    raise ValueError('unreviewed contents at ' + relative)
                candidates.append((parent, name, identities))
        for parent, name, identities in candidates:
            inspect_tree(parent, name, '.', mid, [], identities, remove=True)
        return len(candidates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('root', 'device', 'image'):
        parser.add_argument('--' + option, required=True, type=Path)
    choices = parser.add_mutually_exclusive_group(required=True)
    choices.add_argument('--apply', action='store_true')
    choices.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        count = sanitize(args.root, args.device, args.image, args.apply)
    except (OSError, ValueError) as exc:
        print('offline bench sanitation failed:', exc, file=sys.stderr)
        return 1
    print(f'offline bench sanitation complete, removed {count} verified directories')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
