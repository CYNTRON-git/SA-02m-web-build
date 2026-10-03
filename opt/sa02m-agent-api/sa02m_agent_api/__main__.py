# -*- coding: utf-8 -*-
"""Unix socket server for sa02m-agent-api. Run: python3 -m sa02m_agent_api."""

import json
import os
import socketserver
import stat

from .service import App


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        sock, _ = self.socket.accept()
        return sock, ("unix", 0)


class Request(socketserver.StreamRequestHandler):
    def handle(self):
        raw = b""
        while b"\r\n\r\n" not in raw and len(raw) < 65536:
            chunk = self.rfile.read(1)
            if not chunk:
                return
            raw += chunk
        head, _, _rest = raw.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1", "replace").split("\r\n")
        if not lines or " " not in lines[0]:
            return
        method, target, _proto = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
        try:
            length = int(headers.get("Content-Length") or headers.get("content-length") or "0")
        except ValueError:
            length = 0
        if length < 0 or length > 8 * 1024 * 1024:
            self._send(413, {"ok": False, "error": "too_large"})
            return
        body = self.rfile.read(length) if length else b""
        app = self.server.app
        result = app.handle(method, target, headers, body)
        if isinstance(result, tuple) and result and result[0] == "sse":
            self._sse(result[1])
            return
        status, payload = result
        self._send(status, payload)

    def _send(self, status, payload):
        if payload == b"":
            data = b""
            ctype = "application/json"
        elif isinstance(payload, (bytes, bytearray)):
            data = bytes(payload)
            ctype = "application/json"
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        reason = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
                  403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
                  409: "Conflict", 413: "Payload Too Large", 429: "Too Many Requests",
                  500: "Internal Server Error", 503: "Service Unavailable"}.get(status, "OK")
        header = (
            "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
            % (status, reason, ctype, len(data))
        ).encode("ascii")
        self.wfile.write(header + data)

    def _sse(self, q):
        import queue
        self.wfile.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Cache-Control: no-store\r\nX-Accel-Buffering: no\r\nConnection: close\r\n\r\n"
            b": connected\n\n"
        )
        self.wfile.flush()
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b":\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + json.dumps(ev).encode("utf-8") + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self.server.app.bus.unsubscribe(q)


def serve(path=None):
    sock = path or os.environ.get("SA02M_AGENT_API_SOCK") or "/run/sa02m-agent-api/api.sock"
    parent = os.path.dirname(sock)
    os.makedirs(parent, exist_ok=True)
    try:
        os.unlink(sock)
    except FileNotFoundError:
        pass
    app = App()
    httpd = UnixHTTPServer(sock, Request)
    httpd.app = app
    try:
        os.chmod(sock, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
    except OSError:
        pass
    httpd.serve_forever()


def main():
    serve()


if __name__ == "__main__":
    main()
