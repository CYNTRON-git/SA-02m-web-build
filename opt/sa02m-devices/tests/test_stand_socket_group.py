# -*- coding: utf-8 -*-
"""Bench stand API must chgrp the devices socket after bind, not on a timer.

A 10s ExecStartPost missed the inode on 1.135 (socket at 19s, left root:root,
nginx 502, empty Devices tab). when_ready claims group www-data and mode 0660,
and a failure must not escape into gunicorn — that would take down :8765 too.
"""
from __future__ import annotations

import os
import socket
import unittest
from unittest import mock

from sa02m_devices import stand_socket_group as hook

_INET = getattr(socket, "AF_INET", 2)


class _Log:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):
        self.infos.append(msg % args if args else msg)


class _Sock:
    def __init__(self, family, name):
        self.family = family
        self._name = name

    def getsockname(self):
        return self._name


class _Listener:
    def __init__(self, family, name):
        self.sock = _Sock(family, name)


class _BoomSock:
    def getsockname(self):
        raise RuntimeError("getsockname failed")


class _Group:
    gr_gid = 33


def _server(*listeners):
    return type("S", (), {"LISTENERS": list(listeners), "log": _Log()})()


class StandSocketGroupTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("STAND_DEVICES_SOCKET", None)
        self.chown = mock.patch.object(hook.os, "chown", create=True)
        self.chmod = mock.patch.object(hook.os, "chmod")
        self.chown_mock = self.chown.start()
        self.chmod_mock = self.chmod.start()
        self.grp = mock.patch.object(hook, "grp")
        self.grp_mock = self.grp.start()
        self.grp_mock.getgrnam.return_value = _Group()
        if hook._AF_UNIX is None:
            self._af = mock.patch.object(hook, "_AF_UNIX", 99)
            self._af.start()
        else:
            self._af = None
        _BoomSock.family = hook._AF_UNIX

    def tearDown(self):
        if self._af is not None:
            self._af.stop()
        self.grp.stop()
        self.chmod.stop()
        self.chown.stop()
        self._env.stop()

    def test_claims_only_the_devices_socket(self):
        other = "/run/other/api.sock"
        server = _server(
            _Listener(_INET, ("127.0.0.1", 8765)),
            _Listener(hook._AF_UNIX, other),
            _Listener(hook._AF_UNIX, hook.SOCKET_PATH),
        )
        hook.claim_devices_socket(server)
        self.chown_mock.assert_called_once_with(hook.SOCKET_PATH, -1, 33)
        self.chmod_mock.assert_called_once_with(hook.SOCKET_PATH, 0o660)
        self.assertTrue(server.log.infos)
        self.grp_mock.getgrnam.assert_called_once_with("www-data")

    def test_fallback_when_listener_shape_is_unknown(self):
        server = _server()
        with mock.patch.object(hook, "_is_socket", return_value=True):
            hook.claim_devices_socket(server)
        self.chown_mock.assert_called_once_with(hook.SOCKET_PATH, -1, 33)

    def test_unbound_socket_does_not_chown(self):
        server = _server()
        with mock.patch.object(hook, "_is_socket", return_value=False):
            hook.claim_devices_socket(server)
        self.chown_mock.assert_not_called()
        self.chmod_mock.assert_not_called()
        self.assertTrue(server.log.warnings)

    def test_empty_env_disables_the_claim(self):
        os.environ["STAND_DEVICES_SOCKET"] = ""
        server = _server(_Listener(hook._AF_UNIX, hook.SOCKET_PATH))
        hook.claim_devices_socket(server)
        self.chown_mock.assert_not_called()

    def test_missing_group_does_not_raise(self):
        self.grp_mock.getgrnam.side_effect = KeyError("www-data")
        server = _server(_Listener(hook._AF_UNIX, hook.SOCKET_PATH))
        hook.claim_devices_socket(server)
        self.chown_mock.assert_not_called()
        self.assertTrue(server.log.warnings)

    def test_chown_oserror_does_not_raise(self):
        self.chown_mock.side_effect = OSError("read-only")
        server = _server(_Listener(hook._AF_UNIX, hook.SOCKET_PATH))
        hook.claim_devices_socket(server)
        self.chmod_mock.assert_not_called()
        self.assertTrue(server.log.warnings)

    def test_bad_listener_is_skipped(self):
        server = _server(type("L", (), {"sock": _BoomSock()})(), _Listener(hook._AF_UNIX, hook.SOCKET_PATH))
        hook.claim_devices_socket(server)
        self.chown_mock.assert_called_once_with(hook.SOCKET_PATH, -1, 33)

    def test_when_ready_swallows(self):
        server = _server()
        with mock.patch.object(hook, "claim_devices_socket", side_effect=RuntimeError("boom")):
            hook.when_ready(server)
        self.assertTrue(server.log.warnings)
        self.assertIn("boom", server.log.warnings[0])


if __name__ == "__main__":
    unittest.main()
