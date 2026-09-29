# -*- coding: utf-8 -*-
"""sa02m-devices-api listens on an AF_UNIX socket (0660, group www-data) and keeps
the 127.0.0.1:8765 TCP listener only while the live nginx site file still
targets it — STAND_API_TCP_COMPAT=auto|0|1 (docs/threat-model.md §4 devices-api
row; docs/deployment.md «Чего OTA/офлайн-пакет не делает никогда»).

Three layers, so the claims are measured, not narrated:
  * source pins — run everywhere: the AF_UNIX server class, the 0660 mode, the
    one socket-path literal all three homes (nginx conf, unit, daemon) must agree on;
  * the TCP-compat DECISION as a pure function over fake site-file contents,
    including the hand-edited-file case (a `#`-commented socket line does NOT count);
  * a LIVE AF_UNIX server on a temp path (POSIX only — skipped elsewhere; the
    Orchestrator runs the suite under WSL): mode, HTTP over the socket, the 401
    without a cookie, and the two bind-failure cases that must NEVER raise (an
    exit fails the OTA health gate `units_active` and rolls the update back)
    and the socket-bind failure that must NOT open TCP when nginx names the
    socket (no TCP backup upstream — Operator decision 2026-09-29).

Proven RED on the 1.0.6.56 api.py: no `tcp_compat_decision`, no `build_listeners`,
no UnixStreamServer reference, no socket-path literal.
"""
from __future__ import annotations

import inspect
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path

from sa02m_devices import api

_SOCK = "/run/sa02m-devices/api.sock"
_MARK = "unix:" + _SOCK

_SITE_WITH_SOCKET = """
upstream sa02m_devices_api {
    server unix:/run/sa02m-devices/api.sock;
}
server { location /api/devices/ { proxy_pass http://sa02m_devices_api; } }
"""
_SITE_OLD = """
server { location /api/devices/ { proxy_pass http://127.0.0.1:8765; } }
"""
_SITE_HAND_EDITED = """
upstream sa02m_devices_api {
    # server unix:/run/sa02m-devices/api.sock;
    server 127.0.0.1:8765;
}
"""


class TestSourcePins(unittest.TestCase):
    def test_socket_path_literals_are_the_one_string(self) -> None:
        self.assertEqual(api.SOCKET_PATH_DEFAULT, _SOCK)
        self.assertEqual(api.SOCKET_UPSTREAM_MARK, _MARK)
        self.assertEqual(api.NGINX_SITE_FILE_DEFAULT, "/etc/nginx/sites-available/network_config")

    def test_module_builds_on_a_unix_stream_server(self) -> None:
        src = inspect.getsource(api)
        self.assertIn("UnixStreamServer", src, "the daemon no longer builds an AF_UNIX listener")

    def test_bind_asserts_0660(self) -> None:
        src = inspect.getsource(api.bind_unix_listener)
        self.assertIn("S_IRGRP", src)
        self.assertIn("S_IWGRP", src)
        self.assertNotIn("S_IROTH", src, "the socket must not be world-readable — the mode IS the boundary")

    def test_main_reads_the_switch_and_the_socket_env(self) -> None:
        src = inspect.getsource(api.main)
        self.assertIn("STAND_API_TCP_COMPAT", src)
        self.assertIn("STAND_API_SOCKET", src)
        self.assertIn("build_listeners(", src)


class TestTcpCompatDecision(unittest.TestCase):
    def test_auto_closes_tcp_only_when_nginx_names_the_socket(self) -> None:
        open_tcp, why = api.tcp_compat_decision("auto", _SITE_WITH_SOCKET)
        self.assertFalse(open_tcp, why)
        open_tcp, why = api.tcp_compat_decision("auto", _SITE_OLD)
        self.assertTrue(open_tcp, why)
        self.assertIn("127.0.0.1:8765", why)

    def test_auto_fails_to_availability_on_an_unreadable_file(self) -> None:
        open_tcp, why = api.tcp_compat_decision("auto", None)
        self.assertTrue(open_tcp, why)
        self.assertIn("unreadable", why)

    def test_auto_ignores_a_commented_out_socket_line(self) -> None:
        # The pre-mortem case: a hand-edited site file with the socket line disabled
        # still targets TCP — closing it would 502 the tab (plan §11).
        open_tcp, why = api.tcp_compat_decision("auto", _SITE_HAND_EDITED)
        self.assertTrue(open_tcp, why)

    def test_explicit_overrides(self) -> None:
        self.assertFalse(api.tcp_compat_decision("0", _SITE_OLD)[0])
        self.assertFalse(api.tcp_compat_decision("0", None)[0])
        self.assertTrue(api.tcp_compat_decision("1", _SITE_WITH_SOCKET)[0])

    def test_unknown_mode_and_none_are_auto(self) -> None:
        self.assertFalse(api.tcp_compat_decision("junk", _SITE_WITH_SOCKET)[0])
        self.assertTrue(api.tcp_compat_decision(None, _SITE_OLD)[0])
        self.assertTrue(api.tcp_compat_decision("", None)[0])

    def test_read_site_file_none_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(api.read_site_file(os.path.join(d, "nope")))
            p = Path(d) / "site"
            p.write_text(_SITE_WITH_SOCKET, encoding="utf-8")
            self.assertEqual(api.read_site_file(str(p)), _SITE_WITH_SOCKET)


def _http_over_unix(path: str, request: str) -> tuple[int, dict | None]:
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(5)
    try:
        c.connect(path)
        c.sendall(request.encode("ascii"))
        chunks = []
        while True:
            b = c.recv(65536)
            if not b:
                break
            chunks.append(b)
    finally:
        c.close()
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    try:
        return status, json.loads(body.decode("utf-8"))
    except ValueError:
        return status, None


class TestListenersEverywhere(unittest.TestCase):
    def test_tcp_bind_failure_never_raises(self) -> None:
        """203.0.113.1 (TEST-NET-3) is not a local address on any box → EADDRNOTAVAIL.
        The daemon must log and continue with whatever else it bound — never exit."""
        with tempfile.TemporaryDirectory() as d:
            servers, notes = api.build_listeners(
                socket_path=os.path.join(d, "api.sock"),
                host="203.0.113.1",
                port=0,
                compat_mode="1",
                site_text=None,
                session_dir=d,
            )
            try:
                self.assertTrue(any("TCP" in n and "not bound" in n for n in notes), notes)
                for s in servers:
                    self.assertEqual(s.socket.family, getattr(socket, "AF_UNIX", object()))
            finally:
                for s in servers:
                    s.server_close()

    def test_no_listener_at_all_still_returns(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bad_parent = Path(d) / "file"
            bad_parent.write_text("x", encoding="utf-8")
            servers, notes = api.build_listeners(
                socket_path=str(bad_parent / "api.sock"),   # parent is a regular file → cannot bind
                host="203.0.113.1",
                port=0,
                compat_mode="0",
                site_text=_SITE_WITH_SOCKET,
                session_dir=d,
            )
            self.assertEqual(servers, [])
            self.assertTrue(any("no listener" in n for n in notes), notes)


@unittest.skipUnless(getattr(api, "_HAVE_AF_UNIX", False), "AF_UNIX unavailable here (Windows dev box); run under WSL/CI")
class TestUnixSocketLive(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.sock = str(self.d / "api.sock")
        self.servers, self.notes = api.build_listeners(
            socket_path=self.sock, host="127.0.0.1", port=0, compat_mode="0",
            site_text=_SITE_WITH_SOCKET, session_dir=str(self.d),
        )
        import threading
        self.threads = []
        for s in self.servers:
            t = threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            t.start()
            self.threads.append(t)

    def tearDown(self) -> None:
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self._tmp.cleanup()

    def test_only_the_socket_is_bound_when_nginx_names_it(self) -> None:
        self.assertEqual(len(self.servers), 1, self.notes)
        self.assertEqual(self.servers[0].socket.family, socket.AF_UNIX)
        self.assertTrue(any("TCP compat: off" in n for n in self.notes), self.notes)

    def test_socket_mode_is_0660(self) -> None:
        st = os.stat(self.sock)
        self.assertTrue(stat.S_ISSOCK(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o660)

    def test_http_over_the_socket_health_then_401(self) -> None:
        status, j = _http_over_unix(self.sock, "GET /api/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        self.assertEqual((status, j and j.get("ok")), (200, True))
        status, j = _http_over_unix(self.sock, "GET /api/devices HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        self.assertEqual((status, j), (401, {"ok": False, "error": "unauthorized"}))

    def _unbindable(self) -> str:
        bad_parent = self.d / "file"
        bad_parent.write_text("x", encoding="utf-8")
        return str(bad_parent / "api.sock")   # parent is a regular file → bind raises

    def test_socket_bind_failure_does_not_open_tcp_when_nginx_names_the_socket(self) -> None:
        """Operator decision 2026-09-29 (no TCP backup upstream): with the site file
        naming the socket, nginx can reach nothing but the socket, so a failed socket
        bind must NOT open 127.0.0.1:8765 — that listener would serve nobody and
        would only hold a port the upstream never uses. The process still does not
        raise (an exit fails the OTA health gate)."""
        servers, notes = api.build_listeners(
            socket_path=self._unbindable(), host="127.0.0.1", port=0,
            compat_mode="auto", site_text=_SITE_WITH_SOCKET, session_dir=str(self.d),
        )
        try:
            self.assertEqual(servers, [], notes)
            self.assertTrue(any("not bound" in n for n in notes), notes)
            self.assertTrue(any("TCP compat: off" in n for n in notes), notes)
            self.assertTrue(any("no listener bound" in n for n in notes), notes)
        finally:
            for s in servers:
                s.server_close()

    def test_socket_bind_failure_keeps_tcp_for_an_old_site_file(self) -> None:
        """The OTA-only board: its site file still proxies to the literal port, so
        TCP compat is on by the decision alone — a failed socket changes nothing."""
        servers, notes = api.build_listeners(
            socket_path=self._unbindable(), host="127.0.0.1", port=0,
            compat_mode="auto", site_text=_SITE_OLD, session_dir=str(self.d),
        )
        try:
            self.assertEqual(len(servers), 1, notes)
            self.assertEqual(servers[0].socket.family, socket.AF_INET)
        finally:
            for s in servers:
                s.server_close()

    def test_stale_socket_file_is_replaced(self) -> None:
        # systemd wipes /run/sa02m-devices on stop, but a crash leaves the inode:
        # the next start must unlink and rebind, not fail with EADDRINUSE.
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.servers = []
        Path(self.sock).unlink(missing_ok=True)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(self.sock)
        stale.close()
        srv = api.bind_unix_listener(self.sock, api.DevicesAPIHandler)
        try:
            self.assertTrue(stat.S_ISSOCK(os.stat(self.sock).st_mode))
        finally:
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
