"""Descriptor-safe file operations for the recorder.

One home for "create beside, fsync, rename, fsync the directory" and for
walking into a store dir without following a symlink. A removable store dir is
writable by anyone holding the medium (vfat/exfat `umask=000`), so every name
under it is resolved relative to a descriptor opened `O_NOFOLLOW`: a planted
symlink at `sa02m-audio/`, `rec/`, `tmp/` or at a clip name is refused, never
followed out of the medium.

POSIX-only calls (`dir_fd`, `grp`) are imported or used at call time, so the
pure modules of the package still import on a dev host without them.
"""

from __future__ import annotations

import os
import secrets
import stat as stat_module
from typing import Optional, Union

from . import constants as C

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def group_gid(name: str = C.WEB_GROUP) -> Optional[int]:
    """gid of `name`, or None when the group does not exist (a dev host)."""
    try:
        import grp
    except ImportError:
        return None
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


def open_dir(path: str, dir_fd: Optional[int] = None) -> int:
    """A descriptor of the directory `path` itself — never of a symlink's target."""
    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
    if dir_fd is None:
        return os.open(path, flags)
    return os.open(path, flags, dir_fd=dir_fd)


def open_subdir(parent_fd: int, name: str, *, create: bool, mode: int) -> int:
    """`name` inside `parent_fd`, created with `mode` when absent and `create`."""
    try:
        return open_dir(name, parent_fd)
    except FileNotFoundError:
        if not create:
            raise
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return open_dir(name, parent_fd)


def fsync_dir(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        pass


def is_regular(fd: int, name: str) -> Optional[os.stat_result]:
    """lstat of `name` when it is a regular single-link file, else None."""
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except OSError:
        return None
    if not stat_module.S_ISREG(st.st_mode) or st.st_nlink != 1:
        return None
    return st


def unlink_quietly(fd: int, name: str) -> bool:
    """unlink `name` inside `fd` (a symlink itself, never its target)."""
    try:
        os.unlink(name, dir_fd=fd)
        return True
    except OSError:
        return False


def atomic_write(path: str, data: Union[str, bytes], *, mode: int = 0o640,
                 gid: Optional[int] = None, durable: bool = True) -> None:
    """Replace `path` with `data` atomically (temp `.au-*.tmp` beside it)."""
    directory = os.path.dirname(path) or "."
    base = os.path.basename(path)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    dfd = open_dir(directory)
    try:
        tmp = "%s%s%s" % (C.TMP_PREFIX, secrets.token_hex(6), C.TMP_SUFFIX)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                     0o600, dir_fd=dfd)
        try:
            try:
                os.fchmod(fd, mode)
                if gid is not None:
                    try:
                        os.fchown(fd, -1, gid)
                    except OSError:
                        pass
                view = memoryview(payload)
                while view:
                    view = view[os.write(fd, view):]
                if durable:
                    os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp, base, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            unlink_quietly(dfd, tmp)
            raise
        if durable:
            fsync_dir(dfd)
    finally:
        os.close(dfd)
