"""Root-owned permanent lock inodes; clients receive descriptors, never paths."""

import fcntl
import os
from pathlib import Path
import stat


def secure_directory(directory):
    path = Path(directory)
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0:
            raise PermissionError("Lock directory must belong to root")
        os.fchmod(fd, 0o755)
    finally:
        os.close(fd)


def _check_directory(directory):
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise PermissionError("Lock directory is not root-controlled")


def _check_inode(fd, path, *, strict_mode=True):
    info = os.fstat(fd)
    named = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
            or (strict_mode and info.st_mode & 0o077)):
        raise PermissionError("Unsafe or replaced lock inode")


def provision(path):
    """Broker only: create/narrow a regular root inode without replacing it."""
    path = Path(path)
    _check_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW |
                 os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    try:
        _check_inode(fd, path, strict_mode=False)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def open_locked(path, shared=False):
    path = Path(path)
    _check_directory(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        _check_inode(fd, path)
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        _check_inode(fd, path)
        return fd
    except BaseException:
        os.close(fd)
        raise


def verify_descriptor(fd, path):
    """A root helper may only reuse an inherited, still-locked guard inode."""
    _check_directory(Path(path).parent)
    _check_inode(fd, path)
    probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        raise PermissionError("Inherited guard is not locked")
    finally:
        os.close(probe)
