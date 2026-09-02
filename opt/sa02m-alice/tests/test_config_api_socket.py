"""The Alice config API must be a root-only AF_UNIX socket, not an unauthenticated
TCP listener (audit 2026-08-28, M5).

`serve_unix` used to bind `ThreadingHTTPServer(("127.0.0.1", 8012))` — its name a
lie — and dispatch enable/disable/link/unlink/upsert_device/delete_device with NO
authentication to any local process (www-data, mosquitto, nodered, CODESYS), all as
root. The fix serves a real AF_UNIX socket at mode 0600, so only a root process
reaches it; the web UI never uses this socket (sa02m_alice_api.cgi dispatches
in-process behind session auth).

Proven RED (1.0.6.24): against the pre-fix module `_UnixConfigServer` does not exist
(AttributeError) and `serve_unix`'s source still carries "127.0.0.1" /
"ThreadingHTTPServer" — both source pins fail.
"""

from __future__ import annotations

import inspect
import os
import socket
import stat
import threading
import time
import unittest

from sa02m_alice.config import api


class ConfigApiSocketSourceTest(unittest.TestCase):
    """Runs everywhere — pins the transport shape by source, no socket needed."""

    def test_serve_unix_does_not_bind_tcp(self):
        # Code, not the historical docstring: the executable body (after the closing
        # triple quote) must carry no TCP bind. The module must not reference the TCP
        # server class at all.
        mod_src = inspect.getsource(api)
        self.assertNotIn("ThreadingHTTPServer", mod_src, "the TCP HTTP server class is still referenced")
        fn_src = inspect.getsource(api.serve_unix)
        body = fn_src.split('"""', 2)[-1] if fn_src.count('"""') >= 2 else fn_src
        self.assertNotIn('"127.0.0.1"', body, "serve_unix still binds a TCP address in code")
        self.assertNotIn("8012", body, "serve_unix still references the old TCP port in code")
        self.assertIn("_UnixConfigServer(", body, "serve_unix does not construct the unix-socket server")

    def test_module_uses_unix_stream_server(self):
        # The transport is AF_UNIX by construction: the module references the
        # UnixStreamServer base (guarded for non-POSIX import, where it is never run).
        src = inspect.getsource(api)
        self.assertIn("UnixStreamServer", src, "the config API no longer builds on a unix-socket server")


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "AF_UNIX unavailable (non-POSIX); CI is the authority")
class ConfigApiSocketLiveTest(unittest.TestCase):
    """Stands the server up on a temp socket and drives it over AF_UNIX."""

    def test_unix_server_class_exists(self):
        self.assertTrue(
            hasattr(api, "_UnixConfigServer"),
            "the AF_UNIX server class is absent on a POSIX box — the config API is not unix-socket based",
        )

    def test_binds_af_unix(self):
        import tempfile

        d = tempfile.mkdtemp()
        sp = os.path.join(d, "config.sock")
        srv = api._UnixConfigServer(sp, api._Handler)
        try:
            self.assertEqual(srv.socket.family, socket.AF_UNIX)
        finally:
            srv.server_close()
            try:
                os.unlink(sp)
            except FileNotFoundError:
                pass

    def test_socket_is_owner_only_and_speaks_http(self):
        import tempfile

        d = tempfile.mkdtemp()
        sp = os.path.join(d, "config.sock")
        t = threading.Thread(target=api.serve_unix, kwargs={"sock_path": sp}, daemon=True)
        t.start()
        for _ in range(100):
            if os.path.exists(sp):
                break
            time.sleep(0.02)
        self.assertTrue(os.path.exists(sp), "serve_unix never created the socket")

        mode = stat.S_IMODE(os.stat(sp).st_mode)
        self.assertEqual(mode & 0o077, 0, "the config socket is group/other-accessible (mode %o)" % mode)

        # Deterministic request that never touches device config: an invalid JSON
        # body takes the handler's 400 path (before dispatch), proving the socket
        # accepts and speaks HTTP without depending on a live gateway/config store.
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(3)
        try:
            c.connect(sp)
            c.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n\r\n{ x")
            data = c.recv(65536)
        finally:
            c.close()
        self.assertTrue(data.startswith(b"HTTP/"), "no HTTP response over the unix socket: %r" % data[:40])
        self.assertIn(b"400", data.split(b"\r\n", 1)[0], "expected the invalid_json 400 path: %r" % data[:40])


if __name__ == "__main__":
    unittest.main()
