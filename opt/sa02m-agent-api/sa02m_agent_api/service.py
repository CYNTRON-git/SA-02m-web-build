# -*- coding: utf-8 -*-
"""HTTP router. Session POSTs pass check_csrf before self._route; token calls do not."""

import json
import os
import re
import secrets
import subprocess
import threading

from . import mcp as mcp_mod
from . import openapi as openapi_mod
from .audit import record as audit_record
from .cgi_adapter import ServiceSession
from .events import Bus
from .guide import GUIDE
from .jobs import Jobs
from .ops import register_all
from .registry import OPS
from .tokens import ADMIN_PER_MIN, RateLimiter, TokenStore, allows
from .websession import check_csrf, check_session_store, csrf_error_body, session_token_from_cookie

register_all()

JOB_EVENTS_RE = re.compile(r"^/api/v1/jobs/([0-9a-f]{16})/events$")
JOB_RE = re.compile(r"^/api/v1/jobs/([0-9a-f]{16})$")
MAX_BODY = 8 * 1024 * 1024
_ALPH = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


class Principal:
    def __init__(self, via, token_id, scopes, root_capable=False):
        self.via = via
        self.token_id = token_id or ""
        self.scopes = list(scopes or [])
        self.root_capable = bool(root_capable)

    def allows(self, need):
        return allows(self.scopes, need)


def _header(headers, name):
    if not headers:
        return ""
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return value or ""
    return ""


def _token_from(headers):
    own = _header(headers, "x-sa02m-token").strip()
    if own:
        return own
    auth = _header(headers, "authorization").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def default_run(argv, env=None, input=None, timeout=30, cwd=None, binary=False):
    try:
        proc = subprocess.run(
            argv, input=input, capture_output=True, timeout=timeout,
            env=env, cwd=cwd if cwd and os.path.isdir(cwd) else None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"code": 1, "stdout": b"" if binary else "", "stderr": type(exc).__name__}
    if binary:
        err = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else (proc.stderr or "")
        return {"code": proc.returncode, "stdout": proc.stdout or b"", "stderr": err}
    out = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else (proc.stdout or "")
    err = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else (proc.stderr or "")
    return {"code": proc.returncode, "stdout": out, "stderr": err}


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _tmp_name(prefix):
    suffix = "".join(secrets.choice(_ALPH) for _ in range(6))
    base = os.environ.get("SA02M_AGENT_TMP") or "/tmp"
    return os.path.join(base, prefix + "." + suffix)


def _nodered_basic():
    """Basic auth from nodered.env when that file is regular and not world-writable."""
    import base64
    import stat
    path = os.environ.get("SA02M_NODERED_ENV") or "/etc/sa02m-agent-api/nodered.env"
    try:
        st = os.lstat(path)
    except OSError:
        return ""
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return ""
    if st.st_mode & stat.S_IWOTH:
        return ""
    user = ""
    password = ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("NODERED_USER="):
                    user = line.split("=", 1)[1]
                elif line.startswith("NODERED_PASS="):
                    password = line.split("=", 1)[1]
    except OSError:
        return ""
    if not user:
        return ""
    raw = base64.b64encode((user + ":" + password).encode("utf-8")).decode("ascii")
    return "Basic " + raw


class Ctx:
    def __init__(self, app, principal):
        self.app = app
        self.principal = principal
        self.session = app.session
        self.run = app.run
        self.sink = app.sink

    def side(self, name, fn):
        self.app.sides.append(name)
        if self.sink is not None:
            return self.sink(name)
        return fn()

    def rules_apply(self, body):
        path = _tmp_name("sa02m-rules-cmd")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        try:
            proc = self.run(["sudo", "-n", "/usr/local/sbin/sa02m-rules-store-apply.sh", path],
                            timeout=30)
        finally:
            _unlink_quiet(path)
        out = (proc.get("stdout") or "").strip()
        try:
            parsed = json.loads(out) if out.startswith("{") else None
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            parsed.setdefault("ok", proc.get("code") == 0)
            return 200, parsed
        return 200, {"ok": proc.get("code") == 0, "result": out[:8000],
                     "stderr": (proc.get("stderr") or "")[:1000]}

    def nodered(self, method, path, payload=None):
        import http.client
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", int(os.environ.get("SA02M_NODERED_PORT") or 1880), timeout=20)
        headers = {"Content-Type": "application/json"}
        auth = _nodered_basic()
        if auth:
            headers["Authorization"] = auth
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read(1 << 20)
            status = resp.status
        except OSError as exc:
            return 200, {"ok": False, "error": "nodered", "detail": type(exc).__name__}
        finally:
            conn.close()
        return 200, {"ok": status < 400, "status": status,
                     "body": data.decode("utf-8", "replace")[:8000]}

    def unix_request(self, sock, method, path, body=b""):
        import http.client
        import socket

        class Conn(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(20)
                self.sock.connect(sock)

        raw = body if isinstance(body, bytes) else (body or b"")
        headers = {"Content-Type": "application/json", "Content-Length": str(len(raw))}
        # The flasher and devices daemons check the panel session store
        # themselves (websession.check_session_store): mint ours first.
        try:
            sess = self.session.ensure()
        except (OSError, RuntimeError):
            sess = self.session
        if sess.token:
            headers["Cookie"] = "session_token=" + sess.token
            headers["X-SA02M-CSRF"] = sess.csrf or ""
        conn = Conn("localhost")
        try:
            conn.request(method, path, body=raw, headers=headers)
            resp = conn.getresponse()
            data = resp.read(1 << 20)
            return resp.status, data.decode("utf-8", "replace")
        except OSError as exc:
            return 200, json.dumps({"ok": False, "error": "socket", "detail": type(exc).__name__})
        finally:
            conn.close()

    def web_exec(self, cmd, timeout):
        cwd = os.environ.get("SA02M_USER_ROOT") or "/opt/sa02m-user"
        proc = self.run(["bash", "-c", cmd], timeout=timeout, cwd=cwd)
        return 200, {"ok": proc.get("code") == 0, "code": proc.get("code"),
                     "stdout": (proc.get("stdout") or "")[:8000],
                     "stderr": (proc.get("stderr") or "")[:2000]}

    def root_exec(self, ident, cmd, timeout):
        path = _tmp_name("sa02m-agent-cmd")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/bash\n" + cmd + "\n")
        try:
            proc = self.run(
                ["sudo", "-n", "/usr/local/sbin/sa02m-agent-root-exec.sh", ident, path],
                timeout=timeout + 5,
            )
        finally:
            _unlink_quiet(path)
        return 200, {"ok": proc.get("code") == 0, "code": proc.get("code"),
                     "stdout": (proc.get("stdout") or "")[:8000],
                     "stderr": (proc.get("stderr") or "")[:2000]}

    def root_read(self, ident, path):
        script = (
            "#!/bin/bash\npython3 - <<'PY'\n"
            "import os, stat, sys\n"
            "p = %s\n"
            "st = os.lstat(p)\n"
            "if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size > 262144:\n"
            "    sys.exit(2)\n"
            "sys.stdout.write(open(p, encoding='utf-8', errors='replace').read(262144))\n"
            "PY\n"
        ) % json.dumps(path)
        return self.root_exec(ident, script, 20)

    def root_write(self, ident, path, text):
        script = (
            "#!/bin/bash\npython3 - <<'PY'\n"
            "import os, stat, sys\n"
            "p = %s\n"
            "text = %s\n"
            "if os.path.lexists(p):\n"
            "    st = os.lstat(p)\n"
            "    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):\n"
            "        sys.exit(2)\n"
            "open(p, 'w', encoding='utf-8').write(text)\n"
            "PY\n"
        ) % (json.dumps(path), json.dumps(text))
        return self.root_exec(ident, script, 20)


class App:
    def __init__(self):
        self.ops = list(OPS)
        self.store = TokenStore()
        self.limiter = RateLimiter()
        self.bus = Bus()
        self.jobs = Jobs(self.bus)
        self.session = ServiceSession()
        self.run = default_run
        self.sink = None
        self.inline_jobs = False
        self.sides = []
        self.admin_per_min = ADMIN_PER_MIN
        self.audit_path = os.environ.get("SA02M_AGENT_AUDIT") or "/var/log/sa02m-agent-api/audit.jsonl"
        self._slots = threading.BoundedSemaphore(32)
        self._by_path = {op.rest_path: op for op in self.ops}

    def handle(self, method, path, headers=None, body=b""):
        self.sides = []
        if not self._slots.acquire(blocking=False):
            return 503, {"ok": False, "error": "busy"}
        bare, _, query = (path or "").partition("?")
        hdrs = dict(headers or {})
        if query:
            hdrs["x-sa02m-query"] = query
        try:
            return self.dispatch(method, bare, hdrs, body)
        finally:
            self._slots.release()

    def dispatch(self, method, path, headers, body):
        if path == "/health" and method == "GET":
            return self._health()
        principal = self._authenticate(headers, path)
        if principal is None:
            return 401, {"ok": False, "error": "unauthorized"}
        if method == "POST" and principal.via == "session":
            ok, reason = check_csrf(
                _header(headers, "cookie"),
                _header(headers, "x-sa02m-csrf"),
                self.session.session_dir,
            )
            if not ok:
                return 200, csrf_error_body(reason)
        return self._route(principal, method, path, headers, body)

    def _health(self):
        return 200, {"ok": True}

    def _authenticate(self, headers, path):
        raw = _token_from(headers)
        if raw:
            row = self.store.authenticate(raw)
            if not row:
                return None
            return Principal("token", row.get("id"), row.get("scopes") or [],
                             bool(row.get("root_capable")))
        if path.startswith("/api/v1/admin/"):
            cookie = _header(headers, "cookie")
            if check_session_store(cookie, self.session.session_dir):
                tok = session_token_from_cookie(cookie) or "session"
                return Principal("session", tok[:16], ["read", "control", "config", "admin"], False)
        return None

    def _route(self, principal, method, path, headers, body):
        limit = ADMIN_PER_MIN if path.startswith("/api/v1/admin/") else None
        if not self.limiter.allow(principal.token_id or principal.via, limit):
            return 429, {"ok": False, "error": "rate_limited"}
        if path == "/mcp":
            if method != "POST":
                return 405, {"ok": False, "error": "method"}
            return self._mcp(principal, body)
        if path == "/api/v1/openapi.json" and method == "GET":
            if not principal.allows("read"):
                return 403, {"ok": False, "error": "forbidden", "need": "read"}
            return 200, openapi_mod.build(self.ops)
        if path == "/api/v1/events" and method == "GET":
            if not principal.allows("read"):
                return 403, {"ok": False, "error": "forbidden", "need": "read"}
            q = self.bus.subscribe()
            if q is None:
                return 503, {"ok": False, "error": "busy"}
            return ("sse", q)
        m = JOB_EVENTS_RE.match(path)
        if m and method == "GET":
            job = self.jobs.get(m.group(1), principal.token_id)
            if not job:
                return 404, {"ok": False, "error": "not_found"}
            q = self.bus.subscribe()
            if q is None:
                return 503, {"ok": False, "error": "busy"}
            q.put_nowait({"event": "state", "job_id": job["id"], "status": job["status"],
                          "result": job["result"]})
            return ("sse", q)
        m = JOB_RE.match(path)
        if m and method == "GET":
            job = self.jobs.get(m.group(1), principal.token_id)
            if not job:
                return 404, {"ok": False, "error": "not_found"}
            return 200, {"ok": True, "job": job}
        if path.startswith("/api/v1/admin/"):
            if principal.via != "session":
                return 403, {"ok": False, "error": "token_cannot_mint"}
            return 403, {"ok": False, "error": "use_panel"}
        op = self._by_path.get(path)
        if op is None or method not in ("GET", "POST"):
            return 404, {"ok": False, "error": "not_found"}
        if method == "GET" and op.mutating:
            return 405, {"ok": False, "error": "method"}
        args = _args(method, headers, body)
        if args is None:
            return 400, {"ok": False, "error": "bad_request"}
        return self.invoke(principal, op, args)

    def invoke(self, principal, op, args):
        if not principal.allows(op.scope):
            self._audit(principal, op.name, False, args, "forbidden")
            return 403, {"ok": False, "error": "forbidden", "need": op.scope}
        if op.scope == "admin" and not self.limiter.allow(
                (principal.token_id or "") + ":admin", self.admin_per_min):
            return 429, {"ok": False, "error": "rate_limited"}
        if op.confirm and args.get("confirm") != op.confirm:
            self._audit(principal, op.name, False, args, "confirm_required")
            return 409, {"ok": False, "error": "confirm_required", "need": op.confirm}
        if op.mutating and args.get("dry_run") is True:
            self._audit(principal, op.name, True, args, "dry_run")
            return 200, {"ok": True, "dry_run": True, "op": op.name}
        ctx = Ctx(self, principal)

        def run():
            return op.handler(ctx, args)

        def run_job():
            # The job record keeps the same shape a direct answer has: the
            # payload dict, with the HTTP status folded in when it is an error.
            status, payload = _normalize(run())
            if status >= 400:
                payload = dict(payload, status=status)
            return payload

        if op.long and not self.inline_jobs and args.get("wait") is not True:
            jid = self.jobs.start(principal.token_id, run_job)
            self._audit(principal, op.name, True, args, "job")
            return 200, {"ok": True, "job_id": jid}
        try:
            result = run()
        except Exception as exc:  # noqa: BLE001
            self._audit(principal, op.name, False, args, type(exc).__name__)
            return 500, {"ok": False, "error": "internal"}
        status, payload = _normalize(result)
        self._audit(principal, op.name, status < 400 and payload.get("ok") is not False, args,
                    "" if status < 400 else str(payload.get("error") or ""))
        return status, payload

    def _mcp(self, principal, body):
        try:
            message = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body or "null")
        except ValueError:
            message = None
        payload, status = mcp_mod.handle(self, principal, message)
        if payload is None:
            return status, b""
        return status, payload

    def read_resource(self, principal, uri):
        if not principal.allows("read"):
            return None
        if uri == "sa02m://guide":
            return GUIDE
        if uri == "sa02m://openapi":
            return json.dumps(openapi_mod.build(self.ops))
        if uri == "sa02m://mqtt/topics":
            return "Wiren Board: /devices/<id>/controls/<name>, write .../on without retain."
        if uri == "sa02m://rules/scenarios":
            path = os.environ.get("SA02M_RULES_PATH") or "/etc/sa02m-rules/scenarios.json"
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read(65536)
            except OSError:
                return "{}"
        if uri == "sa02m://user/tree":
            root = os.environ.get("SA02M_USER_ROOT") or "/opt/sa02m-user"
            try:
                return "\n".join(sorted(os.listdir(root))[:200])
            except OSError:
                return ""
        return None

    def _audit(self, principal, op, ok, args, error):
        audit_record(self.audit_path, principal.token_id, op, ok, args, error)


def _args(method, headers, body):
    if method == "GET":
        import urllib.parse
        query = _header(headers, "x-sa02m-query")
        if not query:
            return {}
        return {k: v[-1] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
    if not body:
        return {}
    raw = body.decode("utf-8") if isinstance(body, bytes) else body
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _normalize(result):
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
        status, payload = result
        if not isinstance(payload, dict):
            payload = {"ok": True, "result": payload}
        return status, payload
    if isinstance(result, dict):
        return 200, result
    return 200, {"ok": True, "result": result}
