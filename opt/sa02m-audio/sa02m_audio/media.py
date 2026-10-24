"""Is a medium usable for a clip? (contract §Носители, §Обнаружение)

A removable medium is PRESENT only when all three hold:

1. its fixed mount point is an exact mount point in /proc/mounts (octal
   escapes decoded; the last entry for a path wins, as the kernel stacks it);
2. that entry's source is a `/dev/…` block device;
3. `st_dev` of the mount point differs from `st_dev` of `/`.

Judging by the mount table, never by "the directory exists", is what
matters: storage-mount.sh creates the mount points with `mkdir -p` +
`chmod 777` and they stay on the rootfs after an unmount (bench 1.135: the
phantom `/media/usb` is 0777 on mmcblk2p2 and a hardened unit could write
into it). Check 1 rejects that phantom; checks 2 and 3 reject a mount-table
entry with no medium behind it (a tmpfs or a bind of a rootfs dir over the
path) — without them such an entry would put clips on eMMC unnoticed.

WRITABLE = the mount options lack `ro` AND (on a start) a probe file can be
created and unlinked in the store's `tmp/`. For eMMC the options are those of
the longest mount that covers the store (`/` when the unit has not split it
off). The periodic check never writes:
it judges presence, `ro` and free space only, so an idle recorder does not
touch the flash every 10 s.

Unknown is unusable: a /proc/mounts or stat that cannot be read reads as
absent, a statvfs that fails reads as full.
"""

from __future__ import annotations

import os
import re
import secrets
from collections import namedtuple
from typing import Callable, Dict, Optional, Tuple

from . import constants as C
from .fsutil import open_dir, open_subdir, unlink_quietly

Mount = namedtuple("Mount", "source mount_point fstype options")
Verdict = namedtuple("Verdict", "name state free_bytes dev reason")

_OCTAL = re.compile(r"\\([0-7]{3})")
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def unescape(field: str) -> str:
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mounts(text: str) -> Dict[str, Mount]:
    """mount point -> its LAST entry (a later mount over the same path hides
    the earlier one)."""
    out: Dict[str, Mount] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mount = Mount(unescape(parts[0]), unescape(parts[1]), parts[2],
                      tuple(parts[3].split(",")))
        out[mount.mount_point] = mount
    return out


def _covering_mount(mounts: Dict[str, Mount], store: str) -> str:
    """Longest mount point that contains `store`.

    `ProtectSystem=strict` leaves `/` read-only and bind-mounts the store
    read-write (bench 1.135, 2026-10-15). The options that decide eMMC are
    that bind, which is `/` only when the sandbox has not split the store
    off the root. A path that is not under any listed point still names `/`,
    so a truly read-only root stays read-only.
    """
    covered = "/"
    for point in mounts:
        if point == "/":
            ok = store.startswith("/")
        else:
            base = point.rstrip("/")
            ok = store == base or store.startswith(base + "/")
        if ok and len(point) > len(covered):
            covered = point
    return covered


def _default_dev_of(path: str) -> int:
    return os.stat(path).st_dev


def store_dirs(medium: C.Medium, *, create: bool,
               subdirs: Tuple[str, ...] = (C.REC_SUBDIR, C.TMP_SUBDIR)) -> Tuple[int, ...]:
    """(store fd, one fd per `subdirs`), every component opened O_NOFOLLOW.
    The caller closes them all. `create` makes missing dirs (eMMC: rec 2750,
    tmp 0700 — the access helper re-asserts them; removable: the FS has no
    modes)."""
    if medium.mount_point is None:
        store = open_dir(medium.store_dir)
    else:
        mount_fd = open_dir(medium.mount_point)
        try:
            store = open_subdir(mount_fd, C.REMOVABLE_STORE_NAME, create=create, mode=0o755)
        finally:
            os.close(mount_fd)
    fds = [store]
    try:
        for sub in subdirs:
            fds.append(open_subdir(store, sub, create=create,
                                   mode=0o700 if sub == C.TMP_SUBDIR else 0o2750))
    except BaseException:
        close_all(*fds)
        raise
    return tuple(fds)


def close_all(*fds: int) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


class MediaProbe:
    def __init__(
        self,
        media: Tuple[C.Medium, ...] = C.MEDIA,
        *,
        mounts_file: str = C.MOUNTS_FILE,
        root: str = "/",
        dev_of: Optional[Callable[[str], int]] = None,
    ) -> None:
        self.media = {m.name: m for m in media}
        self.mounts_file = mounts_file
        self.root = root
        self.dev_of = dev_of or _default_dev_of

    def read_mounts(self) -> Optional[Dict[str, Mount]]:
        try:
            with open(self.mounts_file, encoding="utf-8", errors="surrogateescape") as fh:
                return parse_mounts(fh.read(1 << 20))
        except OSError:
            return None

    def presence(self, medium: C.Medium,
                 mounts: Optional[Dict[str, Mount]]) -> Tuple[bool, Optional[int], str, Optional[Mount]]:
        """(present, st_dev, reason, mount entry)."""
        if medium.mount_point is None:
            try:
                st = os.lstat(medium.store_dir)
            except OSError:
                return False, None, "store dir missing", None
            if not os.path.isdir(medium.store_dir) or os.path.islink(medium.store_dir):
                return False, None, "store dir not a directory", None
            return True, st.st_dev, "", None
        if mounts is None:
            return False, None, "mounts unreadable", None
        entry = mounts.get(medium.mount_point)
        if entry is None:
            return False, None, "not a mount point", None
        if not entry.source.startswith("/dev/"):
            return False, None, "source %s is not a block device" % entry.source[:40], entry
        try:
            dev = self.dev_of(medium.mount_point)
            root_dev = self.dev_of(self.root)
        except OSError:
            return False, None, "stat failed", entry
        if dev == root_dev:
            return False, None, "mount point is on the rootfs", entry
        return True, dev, "", entry

    def verdict(self, name: str, *, probe: bool,
                mounts: Optional[Dict[str, Mount]] = None,
                read: bool = True) -> Verdict:
        medium = self.media[name]
        if mounts is None and read:
            mounts = self.read_mounts()
        present, dev, reason, entry = self.presence(medium, mounts)
        if not present:
            return Verdict(name, C.MEDIUM_ABSENT, 0, None, reason)
        if entry is not None and "ro" in entry.options:
            return Verdict(name, C.MEDIUM_READONLY, 0, dev, "mounted ro")
        if medium.mount_point is None and mounts is not None:
            # The mount that covers the store, not the root line. See
            # _covering_mount: a strict unit shows `/` ro and the store rw.
            root = mounts.get(_covering_mount(mounts, medium.store_dir))
            if root is not None and "ro" in root.options:
                return Verdict(name, C.MEDIUM_READONLY, 0, dev, "rootfs mounted ro")
        fds: Tuple[int, ...] = ()
        try:
            if probe:
                fds = store_dirs(medium, create=True)
                probe_name = C.PROBE_PREFIX + secrets.token_hex(4)
                fd = os.open(probe_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
                             | _O_CLOEXEC, 0o600, dir_fd=fds[2])
                os.close(fd)
                if not unlink_quietly(fds[2], probe_name):
                    raise OSError("probe unlink failed")
                st = os.fstatvfs(fds[0])
            else:
                st = os.statvfs(medium.mount_point or medium.store_dir)
        except OSError as exc:
            return Verdict(name, C.MEDIUM_READONLY, 0, dev,
                           "not writable: %s" % (exc.strerror or type(exc).__name__))
        finally:
            close_all(*fds)
        free = int(st.f_bavail) * int(st.f_frsize)
        if free < medium.floor_bytes:
            return Verdict(name, C.MEDIUM_FULL, free, dev, "free below floor")
        return Verdict(name, C.MEDIUM_OK, free, dev, "")

    def still_there(self, name: str, dev: Optional[int]) -> bool:
        """The finalize re-check: present now, and the same filesystem as at
        start (a stick pulled and another inserted is not «the same»)."""
        medium = self.media[name]
        mounts = self.read_mounts() if medium.mount_point is not None else None
        present, now_dev, _reason, _entry = self.presence(medium, mounts)
        return present and dev is not None and now_dev == dev
