#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tier-C hardware acceptance probes for SA-02m offline update / backup / factory reset.

Talks to the live web UI over HTTP (default http://192.168.1.136:9999, admin/cyntron)
and optionally uses tools/ssh/sa02m_remote.py for CSRF file reads / presence checks.

Does NOT flash eMMC/rootfs. Factory-reset wipe is opt-in (--factory-wipe); default is
status/CSRF probes only.

Full happy-path signed .sa02m apply requires scripts/pack-offline-update.py plus the
release signing key (private/sa02m-update-keys/) — not exercised here.

Examples:
  py -3 tools/update/hw_acceptance_update.py
  py -3 tools/update/hw_acceptance_update.py --base-url http://192.168.1.136:9999
  py -3 tools/update/hw_acceptance_update.py --factory-wipe   # destructive; lab only
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
REMOTE_PY = REPO_ROOT / "tools" / "ssh" / "sa02m_remote.py"

DEFAULT_BASE = os.environ.get(
    "SA02M_WEB_BASE",
    f"http://{os.environ.get('SA02M_WEB_HOST', os.environ.get('SA02M_HOST', '192.168.1.136'))}"
    f":{os.environ.get('SA02M_WEB_PORT', '9999')}",
)
DEFAULT_USER = os.environ.get("SA02M_WEB_USER", "admin")
DEFAULT_PASS = os.environ.get("SA02M_WEB_PASS", "cyntron")


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""


@dataclass
class Report:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(CheckResult(name, status, detail))
        mark = {"PASS": "+", "FAIL": "!", "SKIP": "-"}.get(status, "?")
        line = f"[{mark}] {status:4} {name}"
        if detail:
            line += f" — {detail}"
        print(line, flush=True)

    def failed(self) -> bool:
        return any(r.status == "FAIL" for r in self.results)

    def summary(self) -> str:
        counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
        for r in self.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        return (
            f"PASS={counts['PASS']} FAIL={counts['FAIL']} SKIP={counts['SKIP']} "
            f"total={len(self.results)}"
        )


class WebClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.session_token: str | None = None
        self.csrf: str | None = None

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base}/{path.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, dict[str, str], bytes]:
        hdrs = dict(headers or {})
        req = urllib.request.Request(self._url(path), data=data, method=method, headers=hdrs)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                body = resp.read()
                # http.client headers → plain dict
                rh = {k.lower(): v for k, v in resp.headers.items()}
                return int(resp.status), rh, body
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            rh = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
            return int(exc.code), rh, body
        except urllib.error.URLError as exc:
            raise ConnectionError(f"{method} {path}: {exc}") from exc

    def login(self) -> None:
        form = urllib.parse.urlencode(
            {"username": self.username, "password": self.password}
        ).encode("ascii")

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
                return None

        # Do not follow the 302 — capture Set-Cookie from the login response itself.
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            _NoRedirect,
        )
        req = urllib.request.Request(
            self._url("/cgi-bin/login.cgi"),
            data=form,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        code = 0
        headers: dict[str, str] = {}
        body = b""
        try:
            with opener.open(req, timeout=30.0) as resp:
                code = int(resp.status)
                headers = {k.lower(): v for k, v in resp.headers.items()}
                body = resp.read()
        except urllib.error.HTTPError as exc:
            # 302 Found is success for login.cgi
            code = int(exc.code)
            headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
            body = exc.read() if exc.fp else b""

        token = None
        for cookie in self.jar:
            if cookie.name == "session_token" and cookie.value:
                token = cookie.value
                break
        if not token:
            set_cookie = headers.get("set-cookie", "")
            if "session_token=" in set_cookie:
                part = set_cookie.split("session_token=", 1)[1]
                token = part.split(";", 1)[0].strip()
        if not token or code not in (200, 302, 303):
            snippet = body[:200].decode("utf-8", errors="replace")
            raise RuntimeError(f"login failed: HTTP {code}, no session_token ({snippet!r})")
        self.session_token = token

    def get_json(self, path: str, *, timeout: float = 60.0) -> tuple[int, Any, bytes]:
        code, _hdrs, body = self.request("GET", path, timeout=timeout)
        try:
            return code, json.loads(body.decode("utf-8")), body
        except (UnicodeDecodeError, json.JSONDecodeError):
            return code, None, body

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        csrf: str | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, Any, bytes]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-SA02M-CSRF"] = csrf
        code, _hdrs, body = self.request("POST", path, data=data, headers=headers, timeout=timeout)
        try:
            return code, json.loads(body.decode("utf-8")), body
        except (UnicodeDecodeError, json.JSONDecodeError):
            return code, None, body


def ssh_exec(command: str, *, timeout: float = 60.0) -> tuple[int, str, str]:
    if not REMOTE_PY.is_file():
        return 127, "", f"missing {REMOTE_PY}"
    proc = subprocess.run(
        [sys.executable, str(REMOTE_PY), "exec", command],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def fetch_csrf_via_ssh(session_token: str) -> str | None:
    """Plan §2.10: CSRF at /run/sa02m-web-sessions/<sha256(session)>.csrf."""
    digest = hashlib.sha256(session_token.encode("ascii")).hexdigest()
    path = f"/run/sa02m-web-sessions/{digest}.csrf"
    code, out, err = ssh_exec(f"cat {path} 2>/dev/null || true")
    if code != 0:
        return None
    token = (out or "").strip().splitlines()[0].strip() if out else ""
    return token or None


def _looks_undeployed(code: int, body: bytes) -> str | None:
    """Return a short reason if the response is nginx/CGI 'not deployed', else None."""
    text = body[:400].decode("utf-8", errors="replace")
    low = text.lower().strip()
    if code == 404:
        return "HTTP 404"
    # nginx default / deny for missing location often returns plain 403
    if code == 403 and (
        low in ("403 forbidden", "403 forbidden\r\n", "")
        or "403 forbidden" in low
        and "<html" not in low
        and "error_code" not in low
        and '"ok"' not in low
    ):
        return "HTTP 403 (endpoint not deployed / nginx deny)"
    if "no such file" in low:
        return "cgi missing"
    if code == 404 or ("not found" in low and "cgi" in low):
        return "cgi missing"
    return None


def cgi_exists(client: WebClient, path: str) -> tuple[bool, int, str]:
    """Probe endpoint; True if response looks like a real CGI, not nginx deny/404."""
    code, _hdrs, body = client.request("GET", path, timeout=30.0)
    reason = _looks_undeployed(code, body)
    if reason:
        return False, code, reason
    return True, code, f"HTTP {code}"


def check_login(report: Report, client: WebClient) -> None:
    try:
        client.login()
    except Exception as exc:  # noqa: BLE001 — surface as FAIL
        report.add("login", "FAIL", str(exc))
        return
    # Confirm session with a known authed CGI
    code, data, _ = client.get_json("/cgi-bin/web_update_check.cgi")
    if code == 200 and isinstance(data, dict) and data.get("error") != "unauthorized":
        report.add("login", "PASS", f"session_token len={len(client.session_token or '')}")
    elif code == 200 and isinstance(data, dict) and data.get("error") == "unauthorized":
        report.add("login", "FAIL", "cookie rejected by web_update_check.cgi")
    else:
        report.add("login", "FAIL", f"HTTP {code} after login")


def check_update_check(report: Report, client: WebClient) -> None:
    code, data, body = client.get_json("/cgi-bin/web_update_check.cgi")
    if code != 200:
        report.add("web_update_check.cgi", "FAIL", f"HTTP {code}")
        return
    if not isinstance(data, dict):
        report.add("web_update_check.cgi", "FAIL", f"non-JSON body ({body[:80]!r})")
        return
    if data.get("error") == "unauthorized":
        report.add("web_update_check.cgi", "FAIL", "unauthorized")
        return
    # Cache may be empty (no_cache_yet) — still a valid authenticated response
    report.add(
        "web_update_check.cgi",
        "PASS",
        f"keys={sorted(data.keys())[:8]} error={data.get('error')!r}",
    )


def _tar_has_member(blob: bytes, name: str) -> bool:
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            members = [m.name for m in tf.getmembers()]
    except tarfile.TarError:
        # Some streams may be plain tar
        try:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
                members = [m.name for m in tf.getmembers()]
        except tarfile.TarError:
            return False
    return any(m == name or m.endswith("/" + name) or m.endswith(name) for m in members)


def check_backup(report: Report, client: WebClient) -> None:
    exists, code0, why = cgi_exists(client, "/cgi-bin/web_backup.cgi")
    if not exists:
        report.add("web_backup.cgi", "SKIP", f"endpoint not deployed ({why})")
        return
    code, hdrs, body = client.request("GET", "/cgi-bin/web_backup.cgi", timeout=120.0)
    if code != 200:
        report.add("web_backup.cgi", "FAIL", f"HTTP {code}")
        return
    if len(body) <= 1024:
        report.add("web_backup.cgi", "FAIL", f"body too small ({len(body)} bytes)")
        return
    if not _tar_has_member(body, "backup-manifest.json"):
        ctype = hdrs.get("content-type", "")
        report.add(
            "web_backup.cgi",
            "FAIL",
            f"no backup-manifest.json member (size={len(body)}, content-type={ctype!r})",
        )
        return
    report.add("web_backup.cgi", "PASS", f"size={len(body)} has backup-manifest.json")


def _multipart_body(field: str, filename: str, content: bytes, boundary: str) -> bytes:
    crlf = b"\r\n"
    parts = [
        f"--{boundary}".encode("ascii"),
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"'.encode("ascii"),
        b"Content-Type: application/octet-stream",
        b"",
        content,
        f"--{boundary}--".encode("ascii"),
        b"",
    ]
    return crlf.join(parts)


def check_invalid_upload(report: Report, client: WebClient) -> None:
    exists, code0, why = cgi_exists(client, "/cgi-bin/web_update_upload.cgi")
    # GET may 404 when no package — treat script-missing vs empty-package carefully
    if not exists and code0 == 404:
        # Distinguish nginx 404 HTML from CGI "no package"
        code, _h, body = client.request("GET", "/cgi-bin/web_update_upload.cgi", timeout=30.0)
        text = body[:300].decode("utf-8", errors="replace")
        if "<html" in text.lower() or "404 Not Found" in text:
            report.add("web_update_upload.cgi invalid", "SKIP", "endpoint not deployed")
            return
    junk = b"not-a-valid-sa02m-package\x00\x01\x02" + os.urandom(32)
    boundary = "----sa02mHwAcceptBoundary7a3f"
    body = _multipart_body("file", "bad.sa02m", junk, boundary)
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if client.csrf:
        headers["X-SA02M-CSRF"] = client.csrf
    code, _hdrs, resp = client.request(
        "POST",
        "/cgi-bin/web_update_upload.cgi",
        data=body,
        headers=headers,
        timeout=60.0,
    )
    undeployed = _looks_undeployed(code, resp)
    if undeployed:
        report.add("web_update_upload.cgi invalid", "SKIP", undeployed)
        return
    if code >= 500:
        report.add(
            "web_update_upload.cgi invalid",
            "FAIL",
            f"server error HTTP {code}: {resp[:160]!r}",
        )
        return
    try:
        data = json.loads(resp.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        report.add(
            "web_update_upload.cgi invalid",
            "FAIL",
            f"expected JSON validation error, HTTP {code} body={resp[:120]!r}",
        )
        return
    # Accept any structured rejection (ok:false / error_code / error)
    ok_flag = data.get("ok")
    err = data.get("error_code") or data.get("error") or data.get("message")
    if ok_flag is False or err:
        report.add(
            "web_update_upload.cgi invalid",
            "PASS",
            f"HTTP {code} rejected: {err!r}",
        )
        return
    report.add(
        "web_update_upload.cgi invalid",
        "FAIL",
        f"expected validation error, got {data!r}",
    )


def check_cancel_csrf(report: Report, client: WebClient) -> None:
    # Probe cancel endpoint exists (POST without CSRF should fail closed)
    code, data, body = client.post_json("/cgi-bin/web_update_cancel.cgi", {})
    undeployed = _looks_undeployed(code, body)
    if undeployed:
        # Cancel without CSRF on a deployed CGI also returns 403 — distinguish
        # nginx plain body from JSON CSRF rejection.
        try:
            parsed = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if parsed is None and code in (403, 404):
            report.add("web_update_cancel.cgi CSRF", "SKIP", undeployed)
            return
    text = body[:200].decode("utf-8", errors="replace").lower()
    if "<html" in text and "not found" in text:
        report.add("web_update_cancel.cgi CSRF", "SKIP", "endpoint not deployed")
        return

    # Without CSRF: must not succeed as ok:true
    no_csrf_ok = isinstance(data, dict) and data.get("ok") is True
    if no_csrf_ok:
        report.add(
            "web_update_cancel.cgi CSRF",
            "FAIL",
            "POST without X-SA02M-CSRF returned ok:true",
        )
        return

    # With CSRF (if available via SSH): may cancel or return stage error — both OK
    if client.session_token and not client.csrf:
        client.csrf = fetch_csrf_via_ssh(client.session_token)

    if not client.csrf:
        report.add(
            "web_update_cancel.cgi CSRF",
            "PASS",
            f"without CSRF rejected (HTTP {code}); CSRF file not on device yet",
        )
        return

    code2, data2, _ = client.post_json(
        "/cgi-bin/web_update_cancel.cgi", {}, csrf=client.csrf
    )
    if code2 >= 500:
        report.add("web_update_cancel.cgi CSRF", "FAIL", f"with CSRF HTTP {code2}")
        return
    report.add(
        "web_update_cancel.cgi CSRF",
        "PASS",
        f"no-CSRF rejected; with CSRF HTTP {code2} body={data2!r}"[:180],
    )


def check_factory_reset_status(report: Report, client: WebClient) -> None:
    exists, code0, why = cgi_exists(client, "/cgi-bin/web_factory_reset.cgi")
    if not exists:
        report.add("web_factory_reset.cgi GET", "SKIP", f"endpoint not deployed ({why})")
        return
    code, data, body = client.get_json("/cgi-bin/web_factory_reset.cgi")
    if code != 200:
        report.add("web_factory_reset.cgi GET", "FAIL", f"HTTP {code}")
        return
    if not isinstance(data, dict):
        report.add("web_factory_reset.cgi GET", "FAIL", f"non-JSON {body[:80]!r}")
        return
    stage = data.get("stage") or data.get("status") or data.get("state")
    # Must not have started a wipe solely from GET
    if stage in ("wipe", "apply", "backing_up", "confirmed"):
        report.add(
            "web_factory_reset.cgi GET",
            "FAIL",
            f"GET must not start wipe; stage={stage!r}",
        )
        return
    report.add("web_factory_reset.cgi GET", "PASS", f"status={data!r}"[:180])


def check_factory_wipe_opt_in(report: Report, client: WebClient, enabled: bool) -> None:
    if not enabled:
        report.add(
            "factory_reset wipe",
            "SKIP",
            "opt-in flag --factory-wipe not set (default off; no wipe on live device)",
        )
        return
    exists, _code0, why = cgi_exists(client, "/cgi-bin/web_factory_reset.cgi")
    if not exists:
        report.add("factory_reset wipe", "SKIP", f"endpoint not deployed ({why})")
        return
    if client.session_token and not client.csrf:
        client.csrf = fetch_csrf_via_ssh(client.session_token)
    if not client.csrf:
        report.add("factory_reset wipe", "FAIL", "CSRF token unavailable for wipe POST")
        return
    code, data, body = client.post_json(
        "/cgi-bin/web_factory_reset.cgi",
        {"confirm": "SA02M-RESET", "backup_done": True},
        csrf=client.csrf,
        timeout=120.0,
    )
    if code >= 500:
        report.add("factory_reset wipe", "FAIL", f"HTTP {code} {body[:120]!r}")
        return
    report.add("factory_reset wipe", "PASS", f"HTTP {code} {data!r}"[:200])


def check_ssh_sanity(report: Report) -> None:
    code, out, err = ssh_exec("hostname; systemctl is-active nginx fcgiwrap 2>/dev/null | tr '\\n' ' '")
    if code != 0:
        report.add("ssh sa02m_remote", "FAIL", (err or out or f"exit {code}")[:160])
        return
    report.add("ssh sa02m_remote", "PASS", (out or "").strip()[:160])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="SA-02m offline-update / backup / factory-reset HW acceptance (Tier C)"
    )
    ap.add_argument("--base-url", default=DEFAULT_BASE, help="Web UI base URL")
    ap.add_argument("--user", default=DEFAULT_USER, help="Web login (default admin)")
    ap.add_argument("--password", default=DEFAULT_PASS, help="Web password (default cyntron)")
    ap.add_argument(
        "--factory-wipe",
        action="store_true",
        default=False,
        help="OPT-IN: POST factory reset wipe (destructive). Default: off.",
    )
    ap.add_argument(
        "--skip-ssh",
        action="store_true",
        help="Skip sa02m_remote.py SSH sanity / CSRF fetch",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = Report()
    print(f"HW acceptance against {args.base_url}", flush=True)
    print(
        "Note: full signed .sa02m apply needs pack-offline-update.py + signing key "
        "(not part of this smoke suite).",
        flush=True,
    )

    if not args.skip_ssh:
        check_ssh_sanity(report)
    else:
        report.add("ssh sa02m_remote", "SKIP", "--skip-ssh")

    client = WebClient(args.base_url, args.user, args.password)
    try:
        check_login(report, client)
    except ConnectionError as exc:
        report.add("login", "FAIL", str(exc))
        print(report.summary(), flush=True)
        return 1

    if not any(r.name == "login" and r.status == "PASS" for r in report.results):
        print(report.summary(), flush=True)
        return 1

    if not args.skip_ssh and client.session_token:
        client.csrf = fetch_csrf_via_ssh(client.session_token)

    check_update_check(report, client)
    check_backup(report, client)
    check_invalid_upload(report, client)
    check_cancel_csrf(report, client)
    check_factory_reset_status(report, client)
    check_factory_wipe_opt_in(report, client, enabled=bool(args.factory_wipe))

    print(report.summary(), flush=True)
    return 1 if report.failed() else 0


if __name__ == "__main__":
    raise SystemExit(main())
