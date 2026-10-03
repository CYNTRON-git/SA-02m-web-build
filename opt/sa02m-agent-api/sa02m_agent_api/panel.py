# -*- coding: utf-8 -*-
"""What the panel CGI asks us to prepare. This process does not sudo.

stdout is `key=value` lines the shell can read. The secret is only in the
response file (mode 0600), never on this stdout.
"""

import json
import os
import re
import stat
import sys

from .tokens import ID_RE, TokenStore

SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _line(key, value):
    text = str(value)
    if any(ch in text for ch in "\r\n\x00 "):
        raise ValueError(key)
    if key in ("record", "response", "pass"):
        if len(text) > 180:
            raise ValueError(key)
    elif not SAFE_KEY.match(text):
        raise ValueError(key)
    sys.stdout.write("%s=%s\n" % (key, text))


def _mktemp(prefix):
    import secrets
    alph = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    base = os.environ.get("SA02M_AGENT_TMP") or "/tmp"
    name = os.path.join(base, "%s.%s" % (prefix, "".join(secrets.choice(alph) for _ in range(6))))
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    return fd, name


def status():
    active = "unknown"
    try:
        import subprocess
        proc = subprocess.run(
            ["systemctl", "is-active", "sa02m-agent-api.service"],
            capture_output=True, timeout=5,
        )
        active = proc.stdout.decode("utf-8", "replace").strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        active = "unknown"
    rows = []
    try:
        rows = TokenStore().public_rows()
    except OSError:
        rows = []
    # A board updated only over the web may run the daemon behind an nginx site
    # that predates the /api/v1 route; the card says so instead of a dead URL.
    site = os.environ.get("SA02M_NGINX_SITE") or "/etc/nginx/sites-available/network_config"
    routed = None
    try:
        with open(site, "r", encoding="utf-8", errors="replace") as fh:
            routed = "sa02m-agent-api/api.sock" in fh.read(262144)
    except OSError:
        routed = None
    sys.stdout.write(json.dumps({"ok": True, "active": active, "tokens": rows,
                                 "nginx_routed": routed}, ensure_ascii=False))
    sys.stdout.write("\n")


def prepare(body_path):
    with open(body_path, "r", encoding="utf-8") as fh:
        body = json.load(fh)
    if not isinstance(body, dict):
        raise ValueError("body")
    action = body.get("action") or ""
    if action in ("enable", "disable", "restart"):
        _line("verb", action)
        return
    if action == "revoke":
        ident = str(body.get("id") or "")
        if not ID_RE.match(ident):
            raise ValueError("id")
        _line("verb", "revoke")
        _line("id", ident)
        return
    if action != "create":
        raise ValueError("action")
    name = str(body.get("name") or "")
    scopes = body.get("scopes") or ["read"]
    days = body.get("days", 0)
    root = bool(body.get("root_capable"))
    password = body.get("root_password") or ""
    if root and not isinstance(password, str):
        raise ValueError("root_password")
    store = TokenStore()
    row, secret = store.build(name, scopes, days, root and bool(password))
    if root and not password:
        raise ValueError("root_password")
    fd, record = _mktemp("sa02m-agent-tok")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(row, fh)
    fd, response = _mktemp("sa02m-agent-rsp")
    payload = {"ok": True, "id": row["id"], "token": secret, "scopes": row["scopes"],
               "root_capable": row["root_capable"]}
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.chmod(response, stat.S_IRUSR | stat.S_IWUSR)
    pass_path = ""
    if root:
        fd, pass_path = _mktemp("sa02m-agent-pass")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(password)
    _line("verb", "create")
    _line("id", row["id"])
    _line("record", record)
    _line("response", response)
    if pass_path:
        _line("pass", pass_path)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "status":
            status()
            return 0
        if cmd == "prepare":
            prepare(argv[2])
            return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": "bad_request", "detail": type(exc).__name__}))
        sys.stdout.write("\n")
        return 2
    sys.stdout.write('{"ok":false,"error":"bad_action"}\n')
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
