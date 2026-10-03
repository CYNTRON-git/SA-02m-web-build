# -*- coding: utf-8 -*-
"""Run an existing CGI with a service session the daemon minted.

The CGI contract is unchanged. apply.cgi answers 302, not JSON: that status
and Location are returned as fields, not parsed as a document.
"""

import json
import os
import re
import subprocess

PART_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class ServiceSession:
    """Mint via lib_web_auth.sh. Tests set token and csrf directly."""

    def __init__(self, lib_path=None, runner=None):
        self.lib_path = lib_path or os.environ.get("SA02M_WEB_AUTH_LIB") or \
            "/var/www/network_config/cgi-bin/lib_web_auth.sh"
        self.runner = runner
        self.token = ""
        self.csrf = ""
        self.session_dir = os.environ.get("SA02M_SESSION_DIR") or "/run/sa02m-web-sessions"

    def ensure(self):
        if self.token and self.csrf and self._alive():
            return self
        if self.runner is None:
            self._mint()
        return self

    def _alive(self):
        try:
            import hashlib
            digest = hashlib.sha256(self.token.encode("utf-8")).hexdigest()
            path = os.path.join(self.session_dir, digest)
            with open(path, "r", encoding="utf-8") as fh:
                exp = int((fh.readline() or "0").split()[0])
            return exp > __import__("time").time()
        except (OSError, ValueError, IndexError):
            return False

    SERVICE_USER = "agent-api"

    def _drop_stale(self):
        """Remove sessions a previous daemon instance minted (user agent-api).

        The token lives only in this process: after a restart the old file
        would sit in /run until its 10-day expiry. Panel sessions (other
        users) are never touched. Best effort: any read error skips the file.
        """
        try:
            names = os.listdir(self.session_dir)
        except OSError:
            return
        for name in names:
            if not re.fullmatch(r"[0-9a-f]{64}", name):
                continue
            path = os.path.join(self.session_dir, name)
            try:
                if os.path.islink(path):
                    continue
                with open(path, "r", encoding="utf-8") as fh:
                    parts = (fh.readline() or "").split()
                if len(parts) >= 2 and parts[1] == self.SERVICE_USER:
                    os.unlink(path)
                    try:
                        os.unlink(path + ".csrf")
                    except OSError:
                        pass
            except (OSError, ValueError):
                continue

    def _mint(self):
        self._drop_stale()
        script = (
            '. "$1"\n'
            'tok=$(web_session_create "$2") || exit 1\n'
            'printf "%s" "$tok"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script, "bash", self.lib_path, self.SERVICE_USER],
            capture_output=True, timeout=10,
        )
        tok = proc.stdout.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not tok:
            raise RuntimeError("service session was not minted")
        import hashlib
        digest = hashlib.sha256(tok.encode("utf-8")).hexdigest()
        csrf_path = os.path.join(self.session_dir, digest + ".csrf")
        with open(csrf_path, "r", encoding="utf-8") as fh:
            csrf = fh.read().strip()
        self.token = tok
        self.csrf = csrf


def split_cgi(raw, returncode):
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    head, _, body = text.partition("\n\n")
    if "\r\n\r\n" in text and "\n\n" not in text.split("\r\n\r\n", 1)[0]:
        head, _, body = text.partition("\r\n\r\n")
    status = 200
    headers = {}
    for line in head.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    if "status" in headers:
        try:
            status = int(headers["status"].split()[0])
        except ValueError:
            status = 200
    parsed = None
    stripped = body.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
    if status in (301, 302, 303) and parsed is None:
        parsed = {"ok": True, "redirect": headers.get("location", "")}
    if parsed is None:
        parsed = {"ok": returncode == 0, "raw": stripped[:4000]}
    return status, parsed


def split_cgi_binary(raw, returncode):
    """Headers as text, the body as bytes. For a CGI that streams a file."""
    data = bytes(raw or b"")
    sep = b"\r\n\r\n" if b"\r\n\r\n" in data else b"\n\n"
    head, _, body = data.partition(sep)
    headers = {}
    for line in head.decode("iso-8859-1", "replace").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    ctype = headers.get("content-type", "")
    if ctype.startswith("application/json") or body.lstrip().startswith(b"{"):
        return split_cgi(data, returncode)
    import base64
    return 200, {"ok": returncode == 0, "content_type": ctype,
                 "filename": headers.get("content-disposition", ""),
                 "size": len(body), "body_b64": base64.b64encode(body).decode("ascii")}


def run_cgi(ctx, script, method, query="", body=b"", content_type="application/json",
            binary=False):
    if not re.match(r"^[A-Za-z0-9_.-]+\.cgi$", script or ""):
        return 400, {"ok": False, "error": "bad_cgi"}
    sess = ctx.session.ensure()
    if isinstance(body, str):
        body = body.encode("utf-8")
    root = os.environ.get("SA02M_CGI_DIR") or "/var/www/network_config/cgi-bin"
    path = os.path.join(root, script)
    env = {
        "REQUEST_METHOD": method,
        "QUERY_STRING": query or "",
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(body)),
        "HTTP_COOKIE": "session_token=" + (sess.token or ""),
        "HTTP_X_SA02M_CSRF": sess.csrf or "",
        "SCRIPT_NAME": "/cgi-bin/" + script,
        "GATEWAY_INTERFACE": "CGI/1.1",
        "SERVER_NAME": "127.0.0.1",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": "127.0.0.1",
        "PATH": os.environ.get("PATH") or "/usr/bin:/bin",
        "SA02M_SESSION_DIR": sess.session_dir,
    }
    if binary:
        proc = ctx.run([os.environ.get("BASH") or "bash", path], env=env, input=body,
                       timeout=120, binary=True)
        return split_cgi_binary(proc.get("stdout") or b"", proc.get("code", 1))
    proc = ctx.run([os.environ.get("BASH") or "bash", path], env=env, input=body, timeout=60)
    return split_cgi(proc.get("stdout") or b"", proc.get("code", 1))
