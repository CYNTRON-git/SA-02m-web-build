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
import stat as stat_module
import tempfile
from typing import Optional, Union

from . import constants as C


def group_gid(name: str = C.WEB_GROUP) -> Optional[int]:
    """gid of `name`, or None when the group does not exist (a dev host)."""
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


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
    gid: Optional[int] = None,
    preserve: bool = False,
    durable: bool = True,
) -> None:
    """Replace `path` with `data` atomically.

    `preserve=True` keeps an existing file's mode and owner (the conf is
    written by root AND by the www-data CGI — without this a root write would
    lock the web layer out, the reason Alice's `_atomic_write` does the same).
    `gid` sets the group of a NEW file (setup.json → www-data). `durable`
    fsyncs the file and the directory; /run files may skip it (tmpfs).
    """
    directory = os.path.dirname(path) or "."
    payload = data.encode("utf-8") if isinstance(data, str) else data
    st = None
    if preserve:
        try:
            st = os.stat(path)
        except OSError:
            st = None
    fd, tmp = tempfile.mkstemp(prefix=C.TMP_PREFIX, suffix=".tmp", dir=directory)
    try:
        try:
            if st is not None:
                os.fchmod(fd, stat_module.S_IMODE(st.st_mode))
                try:
                    os.fchown(fd, st.st_uid, st.st_gid)
                except OSError:
                    pass  # a non-root writer keeps its own uid; the group suffices
            else:
                os.fchmod(fd, mode)
                if gid is not None:
                    try:
                        os.fchown(fd, -1, gid)
                    except OSError:
                        pass  # not a member of the group: mode still applies
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
