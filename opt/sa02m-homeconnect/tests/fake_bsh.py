"""A local fake of the BSH Home Connect cloud (OAuth device flow, REST, SSE).

Bound to 127.0.0.1 on an ephemeral port. Behaviour is scripted per test:

* `device_auth` — the device_authorization answer (dict) or (status, dict);
* `poll_script` — answers to successive device_code token polls: an OAuth
  error string ("authorization_pending", "slow_down", "expired_token",
  "access_denied", "invalid_client") or "ok" (issue tokens);
* `refresh_script` — answers to refresh_token grants ("ok" or an error);
* `appliances`, `status`, `programs` — the REST data;
* `overrides[path]` — a list of (status, headers, body-dict) consumed before
  the normal handler (429 with Retry-After, 5xx, 403, redirects…);
* `sse_scripts` — one entry per stream connection: a list of raw text
  chunks, then the connection closes; a chunk "HANG" keeps it open silently
  until the test ends or `release` is set; a chunk "WAIT:<name>" pauses until
  the test sets `gate(<name>)`.

Every request is recorded (method, path, headers, form) for assertions.
Issued tokens are `AT-<n>` / `RT-<n>`; /api/ requests must carry the
current access token or get 401.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple


class FakeBSH:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: List[Dict[str, Any]] = []
        self.device_auth: Any = {
            "device_code": "DEVICE-CODE-SECRET-123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://api.home-connect.com/security/oauth/device_verify",
            "verification_uri_complete":
                "https://api.home-connect.com/security/oauth/device_verify?user_code=ABCD-1234",
            "expires_in": 300,
            "interval": 5,
        }
        self.poll_script: List[str] = ["ok"]
        self.refresh_script: List[str] = []
        self.expires_in = 86400
        self.issued = 0
        self.access: Optional[str] = None
        self.appliances: List[Dict[str, Any]] = []
        self.status: Dict[str, List[Dict[str, Any]]] = {}
        self.programs: Dict[str, Optional[Dict[str, Any]]] = {}
        self.overrides: Dict[str, List[Tuple[int, Dict[str, str], Any]]] = {}
        self.sse_scripts: List[List[str]] = []
        self.sse_connections = 0
        self.release = threading.Event()
        self.gates: Dict[str, threading.Event] = {}
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args: Any) -> None:  # keep test output clean
                pass

            def do_GET(self) -> None:
                fake._handle(self, "GET")

            def do_POST(self) -> None:
                fake._handle(self, "POST")

            def do_PUT(self) -> None:
                fake._handle(self, "PUT")

            def do_DELETE(self) -> None:
                fake._handle(self, "DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02},
                                        daemon=True)
        self._thread.start()
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def stop(self) -> None:
        self.release.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # ── helpers ──────────────────────────────────────────────────────────
    def calls(self, path: Optional[str] = None, method: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests
                    if (path is None or r["path"] == path) and (method is None or r["method"] == method)]

    def gate(self, name: str) -> threading.Event:
        with self.lock:
            return self.gates.setdefault(name, threading.Event())

    def grants(self, grant_type: str) -> List[Dict[str, Any]]:
        return [r for r in self.calls("/security/oauth/token")
                if r["form"].get("grant_type") == grant_type]

    def _issue(self) -> Dict[str, Any]:
        self.issued += 1
        self.access = "AT-%d" % self.issued
        return {"access_token": self.access, "refresh_token": "RT-%d" % self.issued,
                "expires_in": self.expires_in, "token_type": "Bearer", "scope": "IdentifyAppliance Monitor"}

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, body: Any,
              headers: Optional[Dict[str, str]] = None) -> None:
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)

    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urllib.parse.urlsplit(h.path)
        path = parsed.path
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("ascii", "replace")).items()}
        with self.lock:
            self.requests.append({"method": method, "path": path,
                                  "headers": {k.lower(): v for k, v in h.headers.items()},
                                  "form": form})
            queue = self.overrides.get(path)
            override = queue.pop(0) if queue else None
        if override is not None:
            status, headers, body = override
            self._send(h, status, body, headers)
            return
        if path == "/security/oauth/device_authorization" and method == "POST":
            if isinstance(self.device_auth, tuple):
                self._send(h, self.device_auth[0], self.device_auth[1])
            else:
                self._send(h, 200, self.device_auth)
            return
        if path == "/security/oauth/token" and method == "POST":
            grant = form.get("grant_type", "")
            with self.lock:
                if grant.endswith("device_code"):
                    step = self.poll_script.pop(0) if self.poll_script else "authorization_pending"
                elif grant == "refresh_token":
                    step = self.refresh_script.pop(0) if self.refresh_script else "ok"
                else:
                    step = "unsupported_grant_type"
                if step == "ok":
                    body = self._issue()
            if step == "ok":
                self._send(h, 200, body)
            else:
                self._send(h, 400, {"error": step, "error_description": "scripted"})
            return
        if path.startswith("/api/"):
            with self.lock:
                authorised = self.access is not None and \
                    h.headers.get("Authorization") == "Bearer " + self.access
            if not authorised:
                self._send(h, 401, {"error": {"key": "invalid_token"}})
                return
            if method != "GET":
                self._send(h, 405, {"error": {"key": "SDK.Error.MethodNotAllowed"}})
                return
            if path == "/api/homeappliances/events":
                self._stream(h)
                return
            if path == "/api/homeappliances":
                self._send(h, 200, {"data": {"homeappliances": self.appliances}})
                return
            parts = path.split("/")
            # /api/homeappliances/<haId>/<rest...>
            if len(parts) >= 5:
                ha = urllib.parse.unquote(parts[3])
                rest = "/".join(parts[4:])
                if rest == "status" and ha in self.status:
                    self._send(h, 200, {"data": {"status": self.status[ha]}})
                    return
                if rest == "programs/active":
                    prog = self.programs.get(ha)
                    if prog is None:
                        self._send(h, 404, {"error": {"key": "SDK.Error.NoProgramActive"}})
                    else:
                        self._send(h, 200, {"data": prog})
                    return
            self._send(h, 404, {"error": {"key": "SDK.Error.NotFound"}})
            return
        self._send(h, 404, {"error": "not_found"})

    def _stream(self, h: BaseHTTPRequestHandler) -> None:
        with self.lock:
            script = self.sse_scripts.pop(0) if self.sse_scripts else ["HANG"]
            self.sse_connections += 1
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.end_headers()
        try:
            for chunk in script:
                if chunk == "HANG":
                    self.release.wait(30)
                    return
                if chunk.startswith("WAIT:"):
                    self.gate(chunk[5:]).wait(10)
                    continue
                h.wfile.write(chunk.encode("utf-8"))
                h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def sse_event(event_type: str, data: Any = None, event_id: Optional[str] = None) -> str:
    lines = ["event: %s" % event_type]
    if data is not None:
        lines.append("data: %s" % json.dumps(data))
    else:
        lines.append("data: ")
    if event_id is not None:
        lines.append("id: %s" % event_id)
    return "\n".join(lines) + "\n\n"
