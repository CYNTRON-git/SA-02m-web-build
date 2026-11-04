# -*- coding: utf-8 -*-
"""Gunicorn config for the bench stand API (sa02m-stand-api).

The stand binds unix:/run/sa02m-devices/api.sock with --umask 0117, so the
inode is 0660 and owned by the process group (root). nginx workers run as
www-data. An ExecStartPost that polls for about 10s misses a cold import —
on 1.135 the socket appeared 19s after the process started (2026-10-15
14:42:39 → 14:42:58) and stayed root:root, so every /api/devices* was 502
and the Devices tab looked empty.

when_ready runs in the master after "Listening at", when the inode exists.
It re-asserts the same boundary sa02m-devices-api uses (bind_unix_listener):
group www-data, mode 0660. A failure is logged and swallowed: port 8765
still serves the bench UI, and a raised hook would kill that too.

Loaded only on a board that has the stand unit, via
GUNICORN_CMD_ARGS=--config (scripts/11-devices.sh). Do not import
sa02m_devices.api from here — this file is executed as a gunicorn config
with the stand's PYTHONPATH, not ours.
"""
from __future__ import annotations

import os
import socket
import stat

try:
    import grp
except ImportError:  # Windows (the unit tests); the board has grp
    grp = None  # type: ignore[assignment]

SOCKET_PATH = "/run/sa02m-devices/api.sock"
SOCKET_GROUP = "www-data"
# None where the platform has no AF_UNIX (Windows). Tests patch this.
_AF_UNIX = getattr(socket, "AF_UNIX", None)


def _warn(server, msg, *args):
    log = getattr(server, "log", None)
    if log is not None:
        log.warning(msg, *args)


def devices_socket_path():
    """Stand script: empty STAND_DEVICES_SOCKET means "do not bind". Unset
    means the contracted path."""
    raw = os.environ.get("STAND_DEVICES_SOCKET")
    if raw is None:
        return SOCKET_PATH
    return raw.strip()


def _unix_listener_paths(server):
    paths = []
    if _AF_UNIX is None:
        return paths
    for listener in getattr(server, "LISTENERS", None) or ():
        sock = getattr(listener, "sock", None)
        if sock is None:
            continue
        try:
            if sock.family != _AF_UNIX:
                continue
            name = sock.getsockname()
        except Exception:
            continue
        if isinstance(name, bytes):
            name = name.decode("utf-8", "surrogateescape")
        if isinstance(name, str) and name and not name.startswith("\0"):
            paths.append(name)
    return paths


def _is_socket(path):
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode)


def claim_devices_socket(server):
    """chgrp + chmod the devices socket. Raises nothing the caller must not
    already expect; when_ready still wraps it."""
    path = devices_socket_path()
    if not path:
        return
    if grp is None:
        _warn(server, "grp unavailable; devices socket %s keeps the process group", path)
        return
    try:
        gid = grp.getgrnam(SOCKET_GROUP).gr_gid
    except KeyError:
        _warn(server, "group %s not found; devices socket %s keeps the process group", SOCKET_GROUP, path)
        return
    bound = _unix_listener_paths(server)
    if path not in bound and not _is_socket(path):
        _warn(server, "devices socket %s is not bound", path)
        return
    try:
        os.chown(path, -1, gid)
        os.chmod(path, 0o660)
    except OSError as exc:
        _warn(server, "cannot chgrp %s to %s: %s", path, SOCKET_GROUP, exc)
        return
    log = getattr(server, "log", None)
    if log is not None:
        log.info("devices socket %s group %s mode 0660", path, SOCKET_GROUP)


def when_ready(server):
    try:
        claim_devices_socket(server)
    except Exception as exc:
        _warn(server, "devices socket group not applied: %s", exc)
