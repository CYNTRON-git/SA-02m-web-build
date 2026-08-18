"""Alice config API handlers (stdlib; CGI or optional unix HTTP).

Never reports fake pairing/link success when the gateway is down.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from .. import __version__
from ..common import constants as C
from ..common.config_store import (
    cert_paths_present,
    client_enabled,
    default_client_cfg,
    empty_devices,
    gateway_urls,
    load_devices,
    save_devices,
    set_client_enabled,
)
from . import models
from .topics import list_mqtt_topics


def _controller_sn() -> str:
    for path in ("/etc/sa02m-cloud/agent.conf", "/etc/machine-id"):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
            if path.endswith("agent.conf"):
                for line in text.splitlines():
                    if line.strip().startswith("serial"):
                        val = line.split("=", 1)[-1].strip().strip("\"'")
                        if val:
                            return val
                continue
            if path.endswith("machine-id") and text:
                return text[:16]
        except OSError:
            continue
    return "sa02m"


def _save_pending_claim(data: Dict[str, Any]) -> None:
    os.makedirs(C.VAR_DIR, exist_ok=True)
    path = C.PENDING_CLAIM_FILE
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _load_pending_claim() -> Dict[str, Any]:
    try:
        with open(C.PENDING_CLAIM_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_pem(path: str, pem: str, mode: int = 0o600) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(pem if pem.endswith("\n") else pem + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_status_file() -> Dict[str, Any]:
    try:
        with open(C.STATUS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"state": "unknown"}


# `cert_check` values reported next to `mtls.cert_present`.
CERT_CHECK_CLIENT = "client"        # taken from the client's status file
CERT_CHECK_LOCAL = "local"          # this process could isfile() the cert dir
CERT_CHECK_UNREADABLE = "unreadable"  # cert dir not traversable → unknown


def cert_presence(status: Dict[str, Any]) -> Tuple[Optional[bool], str]:
    """(cert_present, cert_check) — True/False when known, None when unknowable.

    The web API usually runs as www-data, which cannot traverse the root-only
    cert dir: there `os.path.isfile()` returns False for files that exist. So
    the client's status file (written as root, the actual cert user) is the
    first source; a local isfile() only when this process can actually enter
    the dir; otherwise an honest None — never a false False.
    """
    val = status.get("cert_present")
    if isinstance(val, bool):
        return val, CERT_CHECK_CLIENT
    cert_dir = os.path.dirname(C.CERT_FILE) or "."
    try:
        os.stat(cert_dir)
    except FileNotFoundError:
        # No dir at all (never enrolled) — a definite absence, not "unknown".
        return False, CERT_CHECK_LOCAL
    except OSError:
        return None, CERT_CHECK_UNREADABLE
    if os.access(cert_dir, os.X_OK):
        return cert_paths_present(), CERT_CHECK_LOCAL
    return None, CERT_CHECK_UNREADABLE


def probe_gateway(http_url: Optional[str] = None, timeout: float = C.GATEWAY_PROBE_TIMEOUT_S) -> Dict[str, Any]:
    """HEAD/GET gateway /v1.0/ping. Clear error when unreachable (Phase 0 may be down)."""
    _, http, _ = gateway_urls()
    base = (http_url or http).rstrip("/")
    url = base + C.GATEWAY_PING_PATH
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", 200)
            if code == 200:
                return {"ok": True, "available": True, "url": url, "http_status": code}
            return {
                "ok": True,
                "available": False,
                "url": url,
                "http_status": code,
                "error": "gateway_bad_status",
                "message": "Gateway ping returned HTTP %s" % code,
            }
    except urllib.error.HTTPError as exc:
        # Some stacks reject HEAD — retry GET
        if exc.code in (405, 501):
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    code = getattr(resp, "status", 200)
                    return {
                        "ok": True,
                        "available": code == 200,
                        "url": url,
                        "http_status": code,
                    }
            except Exception as exc2:
                return {
                    "ok": True,
                    "available": False,
                    "url": url,
                    "error": "gateway_unreachable",
                    "message": str(exc2),
                }
        return {
            "ok": True,
            "available": False,
            "url": url,
            "http_status": exc.code,
            "error": "gateway_http_error",
            "message": str(exc),
        }
    except Exception as exc:
        return {
            "ok": True,
            "available": False,
            "url": url,
            "error": "gateway_unreachable",
            "message": str(exc),
        }


def full_config() -> Dict[str, Any]:
    cfg = default_client_cfg()
    _wss, http, _path = gateway_urls()
    devices = load_devices()
    status = read_status_file()
    probe = probe_gateway(http)
    enabled = client_enabled(cfg)
    cert_present, cert_check = cert_presence(status)
    connected = status.get("state") == C.STATE_CONNECTED
    return {
        "ok": True,
        "version": __version__,
        "client_enabled": enabled,
        "gateway": {
            "wss_url": _wss,
            "http_url": http,
            "available": bool(probe.get("available")),
            "probe": probe,
        },
        "mtls": {
            # True / False when known; None when this process cannot tell
            # (see cert_presence) — the UI renders None as «н/д», never «Нет».
            "cert_present": cert_present,
            "cert_check": cert_check,
            "cert_file": C.CERT_FILE,
            "key_file": C.KEY_FILE,
        },
        "status": status,
        "devices": devices,
        "link": {
            # Honest link state — never invent "linked" when gateway/cert missing.
            # A live mTLS session (state connected) is itself proof the cert is
            # present, so only an explicit False vetoes; unknown does not.
            "linked": bool(
                enabled
                and cert_present is not False
                and probe.get("available")
                and connected
            ),
            "state": status.get("state") or ("disabled" if not enabled else "unknown"),
        },
    }


def set_enable(enabled: bool) -> Dict[str, Any]:
    set_client_enabled(bool(enabled))
    # Best-effort restart hint for CGI/systemd (caller may systemctl restart)
    return {
        "ok": True,
        "client_enabled": bool(enabled),
        "message": (
            "Client enabled — start sa02m-alice-client"
            if enabled
            else "Client disabled — client exits 0 on next start"
        ),
        "restart_unit": "sa02m-alice-client",
    }


def start_link() -> Dict[str, Any]:
    """Begin pairing/enrollment. Fails clearly when Phase 0 gateway is down."""
    probe = probe_gateway()
    if not probe.get("available"):
        return {
            "ok": False,
            "error": "gateway_unavailable",
            "message": (
                "Alice gateway is not reachable (%s). "
                "Phase 0 (alice.cyntron.ru) must be deployed before pairing can succeed."
                % (probe.get("url") or C.DEFAULT_GATEWAY_HTTP)
            ),
            "probe": probe,
        }
    # Enrollment API — real call; do not fake success
    _, http, _ = gateway_urls()
    url = http.rstrip("/") + C.GATEWAY_ENROLL_PATH
    try:
        body = json.dumps({"action": "claim"}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=C.GATEWAY_PROBE_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {
                    "ok": False,
                    "error": "gateway_bad_response",
                    "message": "Enrollment endpoint returned non-JSON",
                    "http_status": getattr(resp, "status", None),
                }
            if not isinstance(data, dict) or not data.get("registration_url") and not data.get("ok"):
                return {
                    "ok": False,
                    "error": "enrollment_incomplete",
                    "message": "Gateway did not return a registration_url",
                    "gateway_response": data,
                }
            pending = {
                "claim_token": data.get("claim_token"),
                "registration_url": data.get("registration_url"),
                "controller_sn": _controller_sn(),
            }
            if data.get("ca_pem"):
                _write_pem(C.CA_FILE, str(data["ca_pem"]), 0o644)
            _save_pending_claim(pending)
            return {"ok": True, "enrollment": data, "pending": pending}
    except Exception as exc:
        return {
            "ok": False,
            "error": "enrollment_failed",
            "message": str(exc),
            "url": url,
        }


def complete_link() -> Dict[str, Any]:
    """After OAuth in browser: issue mTLS certs for this controller SN."""
    probe = probe_gateway()
    if not probe.get("available"):
        return {
            "ok": False,
            "error": "gateway_unavailable",
            "message": "Alice gateway is not reachable; cannot complete enrollment.",
            "probe": probe,
        }
    pending = _load_pending_claim()
    token = pending.get("claim_token")
    sn = pending.get("controller_sn") or _controller_sn()
    if not token:
        return {
            "ok": False,
            "error": "no_pending_claim",
            "message": "No pending claim — press «Привязать» first, then finish OAuth.",
        }
    _, http, _ = gateway_urls()
    url = http.rstrip("/") + C.GATEWAY_ENROLL_PATH
    try:
        body = json.dumps(
            {"action": "issue", "claim_token": token, "controller_sn": sn}
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=C.GATEWAY_PROBE_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
    except Exception as exc:
        return {
            "ok": False,
            "error": "issue_failed",
            "message": str(exc),
            "hint": "Finish OAuth account linking in the browser, then retry complete.",
        }
    if not isinstance(data, dict) or not data.get("ok"):
        return {
            "ok": False,
            "error": "issue_rejected",
            "message": "Gateway refused issue (OAuth bind missing or claim expired?)",
            "gateway_response": data,
        }
    for key, path, mode in (
        ("device.crt.pem", C.CERT_FILE, 0o644),
        ("device.key.pem", C.KEY_FILE, 0o600),
        ("ca.crt.pem", C.CA_FILE, 0o644),
    ):
        pem = data.get(key)
        if not pem:
            return {
                "ok": False,
                "error": "issue_incomplete",
                "message": "Missing %s in issue response" % key,
            }
        _write_pem(path, str(pem), mode)
    # Deliberately NOT cleared after a successful issue: the gateway requires the
    # claim_token for the authenticated /controller/unlink, so pending_claim.json
    # outlives enrollment (marked issued=True). Nothing in the status/link view
    # reads this file — a leftover can never flip the UI to «ожидание
    # завершения»; the UI's pending mark is session-local and yields to linked.
    _save_pending_claim(
        {
            "claim_token": token,
            "controller_sn": sn,
            "issued": True,
        }
    )
    return {
        "ok": True,
        "controller_sn": sn,
        "cert_present": cert_paths_present(),
        "message": "mTLS certs installed — enable client and restart sa02m-alice-client",
        "restart_unit": "sa02m-alice-client",
    }


def unlink_controller() -> Dict[str, Any]:
    """Ask gateway to unlink via controller API; clear local enable on success.

    When gateway is down — return error, do NOT claim unlink succeeded.
    """
    probe = probe_gateway()
    if not probe.get("available"):
        return {
            "ok": False,
            "error": "gateway_unavailable",
            "message": (
                "Cannot unlink via gateway — gateway is unreachable. "
                "You may disable the local client, but cloud unlink did not complete."
            ),
            "probe": probe,
            "local_disabled": False,
        }
    _, http, _ = gateway_urls()
    url = http.rstrip("/") + "/controller/unlink"
    pending = _load_pending_claim()
    payload = {
        "controller_sn": pending.get("controller_sn") or _controller_sn(),
    }
    if pending.get("claim_token"):
        payload["claim_token"] = pending["claim_token"]
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=C.GATEWAY_PROBE_TIMEOUT_S) as resp:
            code = getattr(resp, "status", 200)
            if code >= 400:
                return {
                    "ok": False,
                    "error": "unlink_failed",
                    "message": "Gateway unlink HTTP %s" % code,
                    "http_status": code,
                }
            set_client_enabled(False)
            return {
                "ok": True,
                "message": "Unlinked; local client_enabled set false",
                "client_enabled": False,
            }
    except Exception as exc:
        return {
            "ok": False,
            "error": "unlink_failed",
            "message": str(exc),
            "url": url,
        }


def upsert_room(room: Dict[str, Any]) -> Dict[str, Any]:
    cleaned, err = models.validate_room(room)
    if err:
        return {"ok": False, "error": "invalid_room", "message": err}
    doc = load_devices()
    rooms = doc.setdefault("rooms", [])
    for i, r in enumerate(rooms):
        if isinstance(r, dict) and r.get("id") == cleaned["id"]:
            rooms[i] = cleaned
            save_devices(doc)
            return {"ok": True, "room": cleaned}
    rooms.append(cleaned)
    save_devices(doc)
    return {"ok": True, "room": cleaned}


def delete_room(room_id: str) -> Dict[str, Any]:
    doc = load_devices()
    before = len(doc.get("rooms") or [])
    doc["rooms"] = [
        r for r in (doc.get("rooms") or [])
        if not (isinstance(r, dict) and r.get("id") == room_id)
    ]
    if len(doc["rooms"]) == before:
        return {"ok": False, "error": "not_found", "message": "room not found"}
    save_devices(doc)
    return {"ok": True}


def upsert_device(dev: Dict[str, Any]) -> Dict[str, Any]:
    cleaned, err = models.validate_device(dev)
    if err:
        return {"ok": False, "error": "invalid_device", "message": err}
    doc = load_devices()
    devices = doc.setdefault("devices", [])
    for i, d in enumerate(devices):
        if isinstance(d, dict) and d.get("id") == cleaned["id"]:
            devices[i] = cleaned
            save_devices(doc)
            return {"ok": True, "device": cleaned}
    devices.append(cleaned)
    # Attach to room.devices if room_id set
    rid = cleaned.get("room_id")
    if rid:
        for r in doc.get("rooms") or []:
            if isinstance(r, dict) and r.get("id") == rid:
                ids = list(r.get("devices") or [])
                if cleaned["id"] not in ids:
                    ids.append(cleaned["id"])
                r["devices"] = ids
    save_devices(doc)
    return {"ok": True, "device": cleaned}


def delete_device(device_id: str) -> Dict[str, Any]:
    doc = load_devices()
    before = len(doc.get("devices") or [])
    doc["devices"] = [
        d for d in (doc.get("devices") or [])
        if not (isinstance(d, dict) and d.get("id") == device_id)
    ]
    if len(doc["devices"]) == before:
        return {"ok": False, "error": "not_found", "message": "device not found"}
    for r in doc.get("rooms") or []:
        if isinstance(r, dict) and isinstance(r.get("devices"), list):
            r["devices"] = [x for x in r["devices"] if x != device_id]
    save_devices(doc)
    return {"ok": True}


def reset_mappings() -> Dict[str, Any]:
    """Factory-reset helper: clear devices, keep certs, disable client."""
    save_devices(empty_devices())
    set_client_enabled(False)
    return {"ok": True, "client_enabled": False, "devices": empty_devices()}


def dispatch(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
    """Route REST-like paths used by CGI/UI."""
    body = body or {}
    path = path.rstrip("/") or "/"
    m = method.upper()

    if path in ("/", "/integrations/alice", "/integrations/alice/") and m == "GET":
        return 200, full_config()
    if path.endswith("/available") and m == "GET":
        return 200, probe_gateway()
    if path.endswith("/enable_client"):
        if m == "GET":
            return 200, {"ok": True, "client_enabled": client_enabled()}
        if m == "POST":
            enabled = body.get("client_enabled", body.get("enabled", True))
            return 200, set_enable(bool(enabled))
    if path.endswith("/link") and m == "POST":
        return 200, start_link()
    if path.endswith("/unlink_controller") and m in ("PUT", "POST"):
        return 200, unlink_controller()
    if path.endswith("/mqtt-topics") and m == "GET":
        return 200, list_mqtt_topics()
    if path.endswith("/room"):
        if m in ("POST", "PUT"):
            return 200, upsert_room(body.get("room") or body)
        if m == "DELETE":
            return 200, delete_room(str(body.get("id") or ""))
    if path.endswith("/device"):
        if m in ("POST", "PUT"):
            return 200, upsert_device(body.get("device") or body)
        if m == "DELETE":
            return 200, delete_device(str(body.get("id") or ""))
    if path.endswith("/reset_mappings") and m == "POST":
        return 200, reset_mappings()

    # action-style (CGI JSON)
    action = str(body.get("action") or "")
    if action == "status" or (m == "GET" and path in ("", "/")):
        return 200, full_config()
    if action == "available":
        return 200, probe_gateway()
    if action == "enable":
        return 200, set_enable(True)
    if action == "disable":
        return 200, set_enable(False)
    if action == "link":
        return 200, start_link()
    if action == "complete_link":
        return 200, complete_link()
    if action == "unlink":
        return 200, unlink_controller()
    if action == "mqtt_topics":
        return 200, list_mqtt_topics()
    if action == "upsert_room":
        return 200, upsert_room(body.get("room") or body)
    if action == "delete_room":
        return 200, delete_room(str(body.get("id") or ""))
    if action == "upsert_device":
        return 200, upsert_device(body.get("device") or body)
    if action == "delete_device":
        return 200, delete_device(str(body.get("id") or ""))

    return 404, {"ok": False, "error": "not_found", "message": "unknown path %s" % path}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        return

    def _handle(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body: Dict[str, Any] = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":false,"error":"invalid_json"}')
                return
        code, result = dispatch(self.command, parsed.path, body)
        data = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _handle  # noqa: N815
    do_POST = _handle  # noqa: N815
    do_PUT = _handle  # noqa: N815
    do_DELETE = _handle  # noqa: N815


def serve_unix(sock_path: str = "/run/sa02m-alice/config.sock") -> None:
    """Optional long-running config API (sa02m-alice-config.service).

    Binds 127.0.0.1:8012. Web UI uses session-authenticated CGI; this process
    is for local tooling / future nginx proxy.
    """
    os.makedirs(os.path.dirname(sock_path) or "/run/sa02m-alice", exist_ok=True)
    host, port = "127.0.0.1", 8012
    httpd = ThreadingHTTPServer((host, port), _Handler)
    try:
        with open(sock_path + ".tcp", "w", encoding="utf-8") as fh:
            fh.write("%s:%d\n" % (host, port))
    except OSError:
        pass
    httpd.serve_forever()


def main() -> int:
    serve_unix()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
