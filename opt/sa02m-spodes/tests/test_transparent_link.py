"""HDLC frames across a TCP socket, the way a transparent gateway copies them."""

import socket
import threading
import time
import unittest

from sa02m_spodes.link import TransparentLink

_FRAME = bytes.fromhex("7ea00f02232110f2e2e6e600c0013db87e")


def _serve(sock, payload):
    conn, _addr = sock.accept()
    try:
        buf = b""
        while _FRAME not in buf and len(buf) < 256:
            chunk = conn.recv(64)
            if not chunk:
                return
            buf += chunk
        conn.sendall(payload)
    finally:
        conn.close()


class TransparentLinkTest(unittest.TestCase):
    def test_exchange_returns_one_hdlc_frame(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        threading.Thread(target=_serve, args=(srv, b"\x00" + _FRAME), daemon=True).start()
        link = TransparentLink("127.0.0.1", port, timeout_s=2.0)
        try:
            got = link.exchange(_FRAME, 2.0)
        finally:
            link.close()
            srv.close()
        self.assertEqual(got, _FRAME)

    def test_silence_is_a_timeout(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        def _hold(sock):
            conn, _addr = sock.accept()
            try:
                time.sleep(2)
            finally:
                conn.close()

        threading.Thread(target=_hold, args=(srv,), daemon=True).start()
        link = TransparentLink("127.0.0.1", port, timeout_s=2.0)
        try:
            with self.assertRaises(TimeoutError):
                link.exchange(_FRAME, 0.3)
        finally:
            link.close()
            srv.close()


if __name__ == "__main__":
    unittest.main()
