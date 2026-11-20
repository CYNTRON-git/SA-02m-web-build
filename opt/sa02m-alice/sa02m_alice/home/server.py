"""HTTP over one Unix socket. Panel auth and root-only RPC are separate doors."""
from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from sa02m_devices.websession import check_csrf, check_session_store, csrf_error_body
from .contract import APIError, MAX_REQUEST, encoded


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Request paths and headers can contain private identifiers.

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400, "unsupported_encoding")
        values = self.headers.get_all("Content-Length", [])
        if len(values) > 1:
            raise APIError(400, "invalid_length")
        try:
            length = int(values[0]) if values else 0
        except ValueError as exc:
            raise APIError(400, "invalid_length") from exc
        if length < 0 or length > MAX_REQUEST:
            raise APIError(413, "request_too_large")
        if length and self.headers.get_content_type() != "application/json":
            raise APIError(400, "json_required")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            body = json.loads(raw) if raw else None
            if body is not None and not isinstance(body, dict):
                raise ValueError()
            return body
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise APIError(400, "invalid_json") from exc

    def _handle(self):
        dispatcher = self.server.dispatcher
        parsed = urlsplit(self.path)
        if parsed.path in ("/internal/home/v1", "/internal/home/v1/rules/claim", "/internal/home/v1/rules/finish"):
            if self.command != "POST" or not hasattr(socket, "SO_PEERCRED"):
                raise APIError(403, "forbidden")
            _pid, uid, _gid = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != 0:
                raise APIError(403, "forbidden")
            body = self._body() or {}
            if parsed.path != "/internal/home/v1":
                return dispatcher.rule_request(parsed.path.rsplit("/", 1)[1], body)
            if set(body) != {"method", "path", "query", "body"}:
                raise APIError(400, "invalid_request")
            return dispatcher.request(body["method"], body["path"], body["query"], body["body"])
        cookie = self.headers.get("Cookie")
        if not check_session_store(cookie, self.server.session_dir):
            raise APIError(401, "unauthorized")
        if self.command != "GET":
            allowed, reason = check_csrf(cookie, self.headers.get("X-SA02M-CSRF"), self.server.session_dir)
            if not allowed:
                return 403, csrf_error_body(reason)
        prefix = "/api/home/v1"
        if self.command == "GET" and parsed.path == prefix + "/meta":
            return 200, dispatcher.meta()
        if self.command == "GET" and parsed.path == prefix + "/controllers":
            return 200, dispatcher.envelope(controllers=[{"id": dispatcher.controller_id,
                "controller_id": dispatcher.controller_id, "name": "SA-02m",
                "online": bool(dispatcher.broker_available())}])
        start = prefix + "/controllers/" + dispatcher.controller_id
        if not parsed.path.startswith(start + "/"):
            raise APIError(404, "not_found")
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
        except ValueError as exc:
            raise APIError(400, "invalid_query") from exc
        return dispatcher.request(self.command, parsed.path[len(start):], query, self._body())

    def _serve(self):
        try:
            status, body = self._handle()
        except APIError as exc:
            status, body = exc.status, {"ok": False, "error": exc.code}
        except (OSError, ValueError):
            status, body = 503, {"ok": False, "error": "temporarily_unavailable"}
        except Exception:
            status, body = 500, {"ok": False, "error": "internal"}
        raw = encoded(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = _serve


class Server(socketserver.ThreadingMixIn, getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, path, dispatcher, session_dir="/run/sa02m-web-sessions"):
        if not hasattr(socketserver, "UnixStreamServer"):
            raise RuntimeError("production listener requires Unix sockets")
        self.dispatcher, self.session_dir = dispatcher, session_dir
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(path, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()
