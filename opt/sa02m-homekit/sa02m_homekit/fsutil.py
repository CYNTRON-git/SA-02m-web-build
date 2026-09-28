"""Durable atomic file writes for the HomeKit bridge.

One home for "write, fsync, rename, fsync the directory": the pairing store,
the AID map, the identity binding, the conf and the /run files all go
through here. The temp file carries the `.hk-` prefix (constants.TMP_PREFIX)
so the reset helper and the imaging sites can name a torn write's sidecar by
one glob (docs/contracts/image-identity-reset.md §2, the sidecar lesson).
"""

from __future__ import annotations

import grp
import os
import pwd
import stat as stat_module
import tempfile
from typing import Optional, Union

from . import constants as C

# Never carried over by `preserve` (see atomic_write).
_SPECIAL_BITS = stat_module.S_ISUID | stat_module.S_ISGID | stat_module.S_ISVTX


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
    file then keeps what the directory gave it — in a setgid conf dir that IS
    the daemon's group (contract §13), so the grant survives either way."""
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


def fsync_dir(directory: str) -> None:
    """fsync a directory so a rename inside it survives a power cut."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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
    written by root AND by the www-data CGI — without this a root write would
    lock the web layer out, the reason Alice's `_atomic_write` does the same).
    Only a regular, single-link file at the NAME is a source (lstat): the conf
    dir is www-data-writable and root writes here, so a planted symlink or a
    hard link to a foreign file must not lend its owner/mode to the new file;
    setuid/setgid/sticky are never carried over. Anything else falls back to
    `mode`/`uid`/`gid`, as for a new file.
    `uid`/`gid` set the owner/group of a NEW file (setup.json → group
    www-data; the conf → www-data:sa02m-homekit), best effort (`_own_new`).
    `durable` fsyncs the file and the directory; /run files may skip it.
    """
    directory = os.path.dirname(path) or "."
    payload = data.encode("utf-8") if isinstance(data, str) else data
    st = None
    if preserve:
        try:
            st = os.lstat(path)
        except OSError:
            st = None
        if st is not None and not (stat_module.S_ISREG(st.st_mode) and st.st_nlink == 1):
            st = None
    fd, tmp = tempfile.mkstemp(prefix=C.TMP_PREFIX, suffix=".tmp", dir=directory)
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
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if durable:
        fsync_dir(directory)


def remove_quietly(path: str) -> bool:
    """unlink `path`; True when a file was removed. Never raises."""
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
