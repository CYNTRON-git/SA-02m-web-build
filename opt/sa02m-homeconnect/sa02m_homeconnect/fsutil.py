"""Durable, descriptor-safe file writes and reads for the Home Connect client.

One home for "create beside, fsync, rename, fsync the directory". Every name is
resolved relative to a descriptor of its directory, opened `O_NOFOLLOW`, and
the temp file is created `O_CREAT|O_EXCL|O_NOFOLLOW`: a symlink planted at the
temp name or at the final name is never followed, and no name is re-opened
between the checks and the rename (the rename replaces a planted link, not
its target). The temp name carries `.hc-` (constants.TMP_PREFIX) so the unlink
helper and the imaging sites name a torn write's sidecar by one glob.
"""

from __future__ import annotations

import grp
import os
import pwd
import secrets
import stat as stat_module
from typing import Optional, Tuple, Union

from . import constants as C

# Never carried over by `preserve`.
_SPECIAL_BITS = stat_module.S_ISUID | stat_module.S_ISGID | stat_module.S_ISVTX
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class UnsafeFile(OSError):
    """The name is not a regular single-link file this process may trust."""


def group_gid(name: str = C.WEB_GROUP) -> Optional[int]:
    """gid of `name`, or None when the group does not exist (a dev host)."""
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


def user_uid(name: str = C.WEB_USER) -> Optional[int]:
    """uid of `name`, or None when the account does not exist (a dev host)."""
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def _own_new(fd: int, uid: Optional[int], gid: Optional[int]) -> None:
    """Give a NEW file `uid`/`gid`, best effort. Root gets both. A non-root
    writer (the CGI) is refused a foreign owner and a group it is not in; the
    file then keeps what the directory gave it — in the setgid conf dir that IS
    the client's group (contract §11), so the grant survives either way."""
    try:
        os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
        return
    except OSError:
        pass
    if uid is not None and gid is not None:
        try:
            os.fchown(fd, -1, gid)
        except OSError:
            pass


def open_dir(directory: str) -> int:
    """A descriptor of `directory` itself — never of a symlink's target."""
    return os.open(directory or ".", os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)


def _tmp_name() -> str:
    return "%s%s%s" % (C.TMP_PREFIX, secrets.token_hex(6), C.TMP_SUFFIX)


def atomic_write(
    path: str,
    data: Union[str, bytes],
    *,
    mode: int = 0o600,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
    preserve: bool = False,
    durable: bool = True,
) -> None:
    """Replace `path` with `data` atomically.

    `preserve=True` keeps an existing file's mode and owner (the conf is
    written by root AND by the www-data CGI). Only a regular, single-link file
    at the NAME is a source (lstat through the directory descriptor), and
    setuid/setgid/sticky are never carried over; anything else falls back to
    `mode`/`uid`/`gid` as for a new file. `uid`/`gid` set the owner/group of
    a NEW file, best effort (`_own_new`).
    `durable` fsyncs the file and the directory; /run files may skip it.
    """
    directory = os.path.dirname(path) or "."
    base = os.path.basename(path)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    dfd = open_dir(directory)
    try:
        st = None
        if preserve:
            try:
                st = os.stat(base, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                st = None
            if st is not None and not (stat_module.S_ISREG(st.st_mode) and st.st_nlink == 1):
                st = None
        tmp = _tmp_name()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                     0o600, dir_fd=dfd)
        try:
            try:
                if st is not None:
                    os.fchmod(fd, stat_module.S_IMODE(st.st_mode) & ~_SPECIAL_BITS)
                    try:
                        os.fchown(fd, st.st_uid, st.st_gid)
                    except OSError:
                        pass  # a non-root writer keeps its own uid; the group suffices
                else:
                    os.fchmod(fd, mode)
                    if uid is not None or gid is not None:
                        _own_new(fd, uid, gid)
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                if durable:
                    os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp, base, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
        if durable:
            try:
                os.fsync(dfd)
            except OSError:
                pass
    finally:
        os.close(dfd)


def read_private(path: str, max_bytes: int, *, require_owner: bool = True) -> Tuple[bytes, os.stat_result]:
    """Read a file that must be ours alone.

    Raises FileNotFoundError when absent and UnsafeFile when the name is a
    symlink, not a regular single-link file, not owned by this euid, or
    readable/writable by group or other. Nothing is read from a refused file.
    """
    directory = os.path.dirname(path) or "."
    base = os.path.basename(path)
    dfd = open_dir(directory)
    try:
        try:
            fd = os.open(base, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=dfd)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            # ELOOP: the name is a symlink (O_NOFOLLOW refused it).
            raise UnsafeFile("refused %s: %s" % (base, exc.strerror or exc)) from None
        try:
            st = os.fstat(fd)
            if not stat_module.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise UnsafeFile("refused %s: not a regular single-link file" % base)
            if require_owner:
                if st.st_uid != os.geteuid():
                    raise UnsafeFile("refused %s: owner uid %d is not this process" % (base, st.st_uid))
                if stat_module.S_IMODE(st.st_mode) & 0o077:
                    raise UnsafeFile("refused %s: mode %o allows group/other" % (
                        base, stat_module.S_IMODE(st.st_mode)))
            chunks = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise UnsafeFile("refused %s: larger than %d bytes" % (base, max_bytes))
            return data, st
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def remove_quietly(path: str) -> bool:
    """unlink `path` (a symlink itself, never its target); True when removed."""
    try:
        os.unlink(path)
        return True
    except OSError:
        return False
