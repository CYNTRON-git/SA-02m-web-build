"""Alice config API handlers (stdlib; CGI or optional unix HTTP).

Never reports fake pairing/link success when the gateway is down.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import socketserver
import stat
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .. import __version__
from ..common import binding_core
from ..common import binding_sources
from ..common import constants as C
from ..common.config_store import (
    cert_paths_present,
    clear_unlink_marker,
    client_enabled,
    cloud_control_enabled,
    default_client_cfg,
    empty_devices,
    gateway_urls,
    devices_lock,
    load_devices,
    save_devices,
    set_client_enabled,
    set_cloud_control_enabled,
)
from . import models
from .inventory import build_mqtt_inventory
from .topics import list_mqtt_topics

log = logging.getLogger("sa02m_alice.config.api")

# The on-board scenario engine package (opt/sa02m-rules) is a sibling install,
# not a python dependency of sa02m_alice — resolve it lazily and treat its
# absence as «scenarios unsupported», never as an import-time crash.
RULES_DIR = os.environ.get("SA02M_RULES_DIR", "/opt/sa02m-rules")


def _rules_store():
    """sa02m_rules.store module, or None when the rules stack is absent."""
    try:
        from sa02m_rules import store as rules_store  # type: ignore
        return rules_store
    except ImportError:
        pass
    if RULES_DIR not in sys.path:
        sys.path.insert(0, RULES_DIR)
    try:
        from sa02m_rules import store as rules_store  # type: ignore
        return rules_store
    except ImportError:
        return None


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

    The state dir is www-data-owned 0700 (tmpfiles.d/sa02m-alice.conf), so the
    CGI (www-data) can enter it — but a caller running as another non-root user
    still cannot: there `os.path.isfile()` returns False for files that exist.
    So the client's status file (written by the client service, the actual cert
    user) is the first source; a local isfile() only when this process can
    actually enter the dir; otherwise an honest None — never a false False.
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


def read_cloud_status_file() -> Dict[str, Any]:
    try:
        with open(C.STATUS_FILE_CLOUD, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"state": "unknown"}


def _cloud_identity_files_present() -> Optional[bool]:
    """Do the cloud agent's identity files exist? True / False / None (unknown).

    Presence only — this process (www-data) must NEVER open the device secret
    (0600 root): `os.path.exists` stats it and reads nothing. `agent.conf`
    (0640) is parsed for `device_id` / `serial`; if even that is not readable
    the answer is None, not a false False.
    """
    if not os.path.exists(C.CLOUD_DEVICE_SECRET):
        return False
    cfg = configparser.ConfigParser()
    try:
        with open(C.CLOUD_AGENT_CONF, encoding="utf-8") as fh:
            cfg.read_file(fh)
    except FileNotFoundError:
        return False
    except (OSError, configparser.Error):
        return None
    device_id = cfg.get("cloud", "device_id", fallback="").strip()
    serial = cfg.get("device", "serial", fallback="").strip()
    return bool(device_id or serial)


def cloud_identity_presence(status: Dict[str, Any]) -> Tuple[Optional[bool], str]:
    """(cloud_enrolled, cloud_check) — the cloud twin of cert_presence().

    /etc/sa02m-cloud is 750 root:root on an installed board
    (scripts/05-cloud-agent.sh), so this process (www-data) normally cannot
    even enter it: the cloud-profile client (root) publishes
    `identity_present` in its status file on every write and that is the
    practical source. A local check runs only when the dir is traversable
    (a bench / a hand-widened install) and is presence-only — it never opens
    the secret (see _cloud_identity_files_present). Otherwise an honest None,
    never a false False (a false `false` would lock the «Управление из
    облака» button on an enrolled board).
    """
    val = status.get("identity_present")
    if isinstance(val, bool):
        return val, CERT_CHECK_CLIENT
    cloud_dir = os.path.dirname(C.CLOUD_DEVICE_SECRET) or "."
    try:
        os.stat(cloud_dir)
    except FileNotFoundError:
        return False, CERT_CHECK_LOCAL
    except OSError:
        return None, CERT_CHECK_UNREADABLE
    if not os.access(cloud_dir, os.X_OK):
        return None, CERT_CHECK_UNREADABLE
    present = _cloud_identity_files_present()
    if present is None:
        return None, CERT_CHECK_UNREADABLE
    return present, CERT_CHECK_LOCAL


def cloud_control_block(cfg=None) -> Dict[str, Any]:
    """`cloud_control` view for the UI: enable flag + the cloud client's
    status file + tri-state enrollment (identity files present)."""
    status = read_cloud_status_file()
    enabled = cloud_control_enabled(cfg)
    enrolled, check = cloud_identity_presence(status)
    # Flag off ⇒ `disabled`, whatever the file still says: the disable verb
    # restarts the unit and the file catches up, but the flag is the truth
    # the operator just set and the card must not show a stale `connected`.
    state = (status.get("state") or "unknown") if enabled else C.STATE_DISABLED
    return {
        "enabled": enabled,
        "state": state,
        "ts": status.get("ts"),
        "error": status.get("error"),
        "cloud_enrolled": enrolled,
        "cloud_check": check,
        "status_file": C.STATUS_FILE_CLOUD,
    }


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
    # Stored OAuth URL while a claim is pending (started, not yet issued):
    # popup blockers eat window.open and an F5 must not strand the operator
    # with no way back to the link page. The claim file outlives enrollment
    # marked issued=True — gate on that flag.
    _claim = _load_pending_claim()
    # Only claims WITH a stored deadline are servable: a pre-1.0.6.14 abandoned
    # claim (no expires_at, never issued) must NOT lock the UI into a dead
    # «Завершить привязку» forever — pressing «Привязать» writes a fresh claim
    # with the field and recovers.
    _claim_fresh = (
        bool(_claim.get("expires_at"))
        and time.time() < float(_claim.get("expires_at") or 0)
    )
    pending_reg_url = (
        _claim.get("registration_url")
        if _claim.get("claim_token") and not _claim.get("issued") and _claim_fresh
        else None
    )
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
        # Second unit (sa02m-cloud-control, `--profile cloud`): enable flag,
        # its own status file, tri-state enrollment like mtls.cert_present.
        "cloud_control": cloud_control_block(cfg),
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
            "registration_url": pending_reg_url,
            # Server truth for the UI's «ожидание завершения» state: True only
            # while a FRESH un-issued claim exists. An expired/abandoned claim
            # reads False, which returns the card to «Привязать» — the UI's
            # session-local pending mark alone must never hold that lock.
            "pending": pending_reg_url is not None,
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


def set_cloud_control(enabled: bool) -> Dict[str, Any]:
    """Flip `cloud_control_enabled` — gates sa02m-cloud-control.service exactly
    as client_enabled gates the Yandex unit. Refuses nothing here: an
    un-enrolled board simply lands in `missing_identity` standby, which the UI
    already names — the CGI's sudo trigger does the unit restart."""
    set_cloud_control_enabled(bool(enabled))
    return {
        "ok": True,
        "cloud_control_enabled": bool(enabled),
        "message": (
            "Cloud control enabled — start sa02m-cloud-control"
            if enabled
            else "Cloud control disabled — client exits 0 on next start"
        ),
        "restart_unit": "sa02m-cloud-control",
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
                # The gateway's claim lifetime — lets the status stop serving
                # a dead OAuth link once the claim has expired server-side.
                "expires_at": time.time() + float(data.get("expires_in") or 600),
            }
            if data.get("ca_pem"):
                _write_pem(C.CA_FILE, str(data["ca_pem"]), 0o644)
            _save_pending_claim(pending)
            # A board being bound again is no longer «отвязан в облаке».
            # Cleared here so the card is correct one step early; the
            # authoritative clear is in complete_link, which is the step that
            # actually produces certificates.
            clear_unlink_marker()
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
    # outlives enrollment (marked issued=True). The status/link view DOES read
    # this file since 1.0.6.14 (registration_url while pending), but it is
    # triple-gated — claim_token present, issued unset, expires_at fresh — so
    # this issued=True leftover can never flip the UI to «ожидание завершения».
    _save_pending_claim(
        {
            "claim_token": token,
            "controller_sn": sn,
            "issued": True,
        }
    )
    # Authoritative clear of the durable unlink marker: certificates now exist,
    # so a reboot must not resurrect «отвязано в облаке» on a bound board.
    clear_unlink_marker()
    return {
        "ok": True,
        "controller_sn": sn,
        "cert_present": cert_paths_present(),
        "message": "mTLS certs installed — enable client and restart sa02m-alice-client",
        "restart_unit": "sa02m-alice-client",
    }


def unlink_controller() -> Dict[str, Any]:
    """Ask the gateway to unlink, then erase the local binding on success.

    When the gateway is down — return an error and wipe NOTHING; the same
    "never erase on an outage" rule the gateway-driven path holds. Both refusals
    below (unreachable, HTTP >= 400) return BEFORE the wipe, and the HTTP call
    itself must stay ahead of it: the claim_token that authenticates
    /controller/unlink lives in pending_claim.json, which the wipe deletes.

    Since 1.0.6.32 this shares the binding-reset core with the gateway-driven
    path — same erase-list, same durable marker, same never-on-doubt rule.
    Leaving certificates behind after a CONFIRMED unlink is exactly what
    produced the false «привязан» card: the page treats a certificate on disk as
    proof of binding.

    NOTE the two namespaces one word apart: `unlink_failed` below is this API's
    ERROR TOKEN for a gateway refusal, distinct from the client status STATE of
    the same name (a wipe that could not complete).
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
    url = http.rstrip("/") + C.GATEWAY_UNLINK_PATH
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
        try:
            with urllib.request.urlopen(req, timeout=C.GATEWAY_PROBE_TIMEOUT_S) as resp:
                code = getattr(resp, "status", 200)
                raw = resp.read() if hasattr(resp, "read") else b""
        except urllib.error.HTTPError as exc:
            code = int(exc.code or 0)
            raw = exc.read() if exc.fp is not None else b""
        detail = _unlink_gateway_detail(raw)
        if _gateway_already_unlinked(code, _unlink_gateway_json_detail(raw), url):
            return _commit_local_unlink()
        if code >= 400:
            return {
                "ok": False,
                "error": "unlink_failed",
                "message": (
                    str(detail).strip()
                    or ("Gateway unlink HTTP %s" % code)
                ),
                "http_status": code,
                "url": url,
            }
        return _commit_local_unlink()
    except Exception as exc:
        return {
            "ok": False,
            "error": "unlink_failed",
            "message": str(exc),
            "url": url,
        }


def _unlink_gateway_json_detail(raw: Any) -> str:
    """The gateway's own `detail` — a str (or the first of a list) parsed
    from a JSON object body — else "". Never the raw text: an HTML 404 page
    or a proxy error that happens to contain the words is not the gateway
    speaking, and only the gateway's word may confirm an unlink."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw or "")
    try:
        data = json.loads(text.strip() or "null")
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    detail = data.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail and isinstance(detail[0], str):
        return detail[0]
    return ""


def _unlink_gateway_detail(raw: Any) -> str:
    """The text shown to the operator on a refusal: the JSON `detail` when
    there is one, else the first 240 chars of the body. Display only — the
    unlink DECISION reads `_unlink_gateway_json_detail`."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw or "")
    text = text.strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return text[:240]
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list) and detail:
            return str(detail[0])[:240]
    return text[:240]


def _gateway_already_unlinked(code: int, json_detail: str, url: str) -> bool:
    """The live gateway answers HTTP 404 `controller not linked` when the
    cloud record is already gone. That is a confirmed unlink, not a missing
    route — urllib raises HTTPError 404 and the card used to show
    `HTTP Error 404: Not Found` while leaving the mTLS files on disk.

    Honoured only over `https` (urlopen's default verifying TLS context is
    what makes the 404 the gateway's: `[gateway] http_url` is
    operator-settable, and a plain-http value would let a LAN peer answer
    the unlink POST and wipe the enrollment) and only on the parsed JSON
    `detail` (`_unlink_gateway_json_detail`), never the raw body."""
    if int(code or 0) != 404:
        return False
    if urlparse(str(url or "")).scheme.lower() != "https":
        return False
    text = (json_detail or "").lower()
    return "not linked" in text or "already unlink" in text


def _commit_local_unlink() -> Dict[str, Any]:
    spec = binding_sources.yandex_source()
    rc = binding_core.stand_down(
        spec, C.REFUSAL_CLASS_UNLINKED, binding_sources.SOURCE_LOCAL
    )
    if rc != "repair":
        # The gateway unlinked us but the files are still on disk. Say
        # so: never «отвязано» on a board that is not.
        #
        # This process cannot finish the job and must not pretend it
        # will: it exits the moment it answers, and /run/sa02m-alice is
        # root-only so it cannot even write the status file. What it CAN
        # do is hand the stand-down over durably — the core wrote it
        # into the client INI — and the running client adopts it on its
        # next watchdog tick and retries.
        #
        # So the answer distinguishes the two outcomes instead of
        # promising one of them: `wipe_failed` = handed over, the client
        # retries; `wipe_failed_not_recorded` = the hand-over failed too
        # (the same read-only filesystem, most likely), nothing is
        # retrying, and the operator has to press the button again. Read
        # BACK from disk rather than trusted — a claim about a write is
        # only worth what a read confirms.
        handed_over = bool(binding_sources.read_pending()[0])
        return {
            "ok": False,
            "error": "wipe_failed" if handed_over else "wipe_failed_not_recorded",
            "message": (
                "Gateway unlinked the controller, but the local binding "
                "could not be erased; the running client has taken the "
                "retry over."
                if handed_over else
                "Gateway unlinked the controller, but the local binding "
                "could not be erased and the retry could not be recorded; "
                "nothing is retrying — repeat the unlink."
            ),
            "client_enabled": client_enabled(),
        }
    # client_enabled stays ON: the client goes quiet because the binding
    # is gone, not because the flag was cleared — with no certificate
    # the loop routes into its soft wait on every transport
    # (client/main.py::_should_wait_for_cert). Switching the flag off
    # would HIDE the link row on the card (app/alice.js) — the very
    # «Привязать» button the next owner needs. The operator can still
    # disable the unit from the card or SSH after this.
    return {
        "ok": True,
        "message": "Unlinked; local cloud binding erased",
        "client_enabled": client_enabled(),
    }


def _listed_groups(doc: Dict[str, Any]) -> list:
    out = []
    for g in doc.get("groups") or []:
        if not isinstance(g, dict) or not g.get("id"):
            continue
        ids = []
        for item in g.get("device_ids") if isinstance(g.get("device_ids"), list) else []:
            if isinstance(item, str) and item:
                ids.append(item)
        row = {"id": str(g["id"]), "name": str(g.get("name") or ""), "device_ids": ids}
        if g.get("icon") in ("light", "ahu"):
            row["icon"] = g["icon"]
        out.append(row)
    return out


def apply_groups(body: Dict[str, Any]) -> Dict[str, Any]:
    """One atomic write: upsert or delete a lighting group.

    Membership lives only on the group (`device_ids`). Delete drops the
    group and never the devices. Unknown device ids are refused.
    """
    if not isinstance(body, dict):
        return {"ok": False, "error": "invalid_group", "message": "group must be an object"}
    delete = body.get("delete") is True
    gid = body.get("id")
    if gid is not None:
        if not isinstance(gid, str) or not models._ID_RE.match(gid.strip()):
            return {"ok": False, "error": "invalid_group", "message": "invalid group id"}
        gid = gid.strip()
    if delete:
        if not gid:
            return {"ok": False, "error": "not_found", "message": "group id required"}
        return delete_group(gid)
    name = body.get("name")
    if not isinstance(name, str):
        return {"ok": False, "error": "invalid_name", "message": "invalid group name"}
    name = name.strip()
    if not name or not models._NAME_RE.match(name):
        return {"ok": False, "error": "invalid_name", "message": "invalid group name"}
    if not gid:
        gid = models.new_id()
    devices = body.get("device_ids")
    if devices is None:
        devices = body.get("devices")
    bind = None
    if devices is not None:
        if not isinstance(devices, list):
            return {"ok": False, "error": "invalid_devices", "message": "device_ids must be a list"}
        bind = []
        for item in devices:
            if not isinstance(item, str) or not models._ID_RE.match(item.strip()):
                return {"ok": False, "error": "invalid_device", "message": "invalid device id"}
            bind.append(item.strip())
    with devices_lock():
        doc = load_devices()
        groups = doc.setdefault("groups", [])
        existing = None
        idx = None
        for i, g in enumerate(groups):
            if isinstance(g, dict) and g.get("id") == gid:
                existing = g
                idx = i
                break
        if body.get("id") and existing is None:
            return {"ok": False, "error": "not_found", "message": "group not found"}
        if existing is None and len(groups) >= C.COLLECTION_CAP:
            return {"ok": False, "error": "too_many",
                    "message": "too many groups (cap %d)" % C.COLLECTION_CAP}
        if bind is not None:
            known = {
                d.get("id") for d in (doc.get("devices") or []) if isinstance(d, dict) and d.get("id")
            }
            for did in bind:
                if did not in known:
                    return {"ok": False, "error": "not_found", "message": "device not found"}
        row = {"id": gid, "name": name, "device_ids": list(bind) if bind is not None else (
            list(existing.get("device_ids") or []) if existing else [])}
        # Group tile icon (`light`/`ahu`, docs/contracts/alice-gateway.md §groups):
        # set when the body carries a valid one, preserved from the stored group
        # when the body omits it — same upsert rule as `device_ids`.
        icon = body.get("icon")
        if icon in ("light", "ahu"):
            row["icon"] = icon
        elif existing and existing.get("icon") in ("light", "ahu"):
            row["icon"] = existing["icon"]
        if existing is None:
            groups.append(row)
        else:
            groups[idx] = row
        save_devices(doc)
    group_out = {"id": row["id"], "name": row["name"],
                 "device_ids": list(row["device_ids"])}
    if "icon" in row:
        group_out["icon"] = row["icon"]
    return {"ok": True, "group": group_out,
            "groups": _listed_groups(doc)}


def delete_group(group_id: str) -> Dict[str, Any]:
    with devices_lock():
        doc = load_devices()
        before = len(doc.get("groups") or [])
        doc["groups"] = [
            g for g in (doc.get("groups") or [])
            if not (isinstance(g, dict) and g.get("id") == group_id)
        ]
        if len(doc["groups"]) == before:
            return {"ok": False, "error": "not_found", "message": "group not found"}
        save_devices(doc)
    return {"ok": True, "groups": _listed_groups(doc)}


def _listed_rooms(doc: Dict[str, Any]) -> list:
    out = []
    for r in doc.get("rooms") or []:
        if isinstance(r, dict) and r.get("id"):
            out.append({"id": str(r["id"]), "name": str(r.get("name") or "")})
    return out


def _listed_room_devices(doc: Dict[str, Any]) -> list:
    names = {
        r.get("id"): str(r.get("name") or "")
        for r in (doc.get("rooms") or [])
        if isinstance(r, dict) and r.get("id")
    }
    out = []
    for d in doc.get("devices") or []:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        rid = d.get("room_id") if isinstance(d.get("room_id"), str) else ""
        out.append({"id": d["id"], "room_id": rid, "room": names.get(rid, "")})
    return out


def apply_rooms(body: Dict[str, Any]) -> Dict[str, Any]:
    """One atomic write: upsert or delete a room and bind listed devices.

    Checking a device sets its `room_id` to this room (a move, if it was
    elsewhere). Unchecking removes it from THIS room only — it becomes
    unassigned, never deleted. A room delete unassigns its devices; it
    never unlinks the controller or wipes the catalogue.
    """
    if not isinstance(body, dict):
        return {"ok": False, "error": "invalid_room", "message": "room must be an object"}
    delete = body.get("delete") is True
    rid = body.get("id")
    if rid is not None:
        if not isinstance(rid, str) or not models._ID_RE.match(rid.strip()):
            return {"ok": False, "error": "invalid_room", "message": "invalid room id"}
        rid = rid.strip()
    if delete:
        if not rid:
            return {"ok": False, "error": "not_found", "message": "room id required"}
        return delete_room(rid)
    name = body.get("name")
    if not isinstance(name, str):
        return {"ok": False, "error": "invalid_name", "message": "invalid room name"}
    room_in: Dict[str, Any] = {"name": name}
    if rid:
        room_in["id"] = rid
    cleaned, err = models.validate_room(room_in)
    if err:
        key = "invalid_name" if "name" in (err or "") else "invalid_room"
        return {"ok": False, "error": key, "message": err}
    devices = body.get("devices")
    bind = None
    if devices is not None:
        if not isinstance(devices, list):
            return {"ok": False, "error": "invalid_devices", "message": "devices must be a list"}
        bind = []
        for item in devices:
            if not isinstance(item, str) or not models._ID_RE.match(item.strip()):
                return {"ok": False, "error": "invalid_device", "message": "invalid device id"}
            bind.append(item.strip())
    with devices_lock():
        doc = load_devices()
        rooms = doc.setdefault("rooms", [])
        existing = None
        idx = None
        for i, r in enumerate(rooms):
            if isinstance(r, dict) and r.get("id") == cleaned["id"]:
                existing = r
                idx = i
                break
        if rid and existing is None:
            return {"ok": False, "error": "not_found", "message": "room not found"}
        if existing is None and len(rooms) >= C.COLLECTION_CAP:
            return {"ok": False, "error": "too_many",
                    "message": "too many rooms (cap %d)" % C.COLLECTION_CAP}
        if existing is None:
            rooms.append(cleaned)
            existing = cleaned
            idx = len(rooms) - 1
        else:
            existing["name"] = cleaned["name"]
            rooms[idx] = existing
        if bind is not None:
            known = {
                d.get("id") for d in (doc.get("devices") or []) if isinstance(d, dict) and d.get("id")
            }
            for did in bind:
                if did not in known:
                    return {"ok": False, "error": "not_found", "message": "device not found"}
            bind_set = set(bind)
            for d in doc.get("devices") or []:
                if not isinstance(d, dict):
                    continue
                did = d.get("id")
                if did in bind_set:
                    d["room_id"] = cleaned["id"]
                elif d.get("room_id") == cleaned["id"]:
                    # Unassigned devices DROP the key (an empty string would still
                    # serialise into the stored doc — docs/contracts/cloud-scenarios.md).
                    d.pop("room_id", None)
            for r in rooms:
                if not isinstance(r, dict):
                    continue
                if r.get("id") == cleaned["id"]:
                    r["devices"] = list(bind)
                elif isinstance(r.get("devices"), list):
                    r["devices"] = [x for x in r["devices"] if x not in bind_set]
        save_devices(doc)
    room_out = {"id": cleaned["id"], "name": cleaned["name"]}
    return {
        "ok": True,
        "room": room_out,
        "rooms": _listed_rooms(doc),
        "devices": _listed_room_devices(doc),
    }


def upsert_room(room: Dict[str, Any]) -> Dict[str, Any]:
    cleaned, err = models.validate_room(room)
    if err:
        return {"ok": False, "error": "invalid_room", "message": err}
    with devices_lock():
        doc = load_devices()
        rooms = doc.setdefault("rooms", [])
        for i, r in enumerate(rooms):
            if isinstance(r, dict) and r.get("id") == cleaned["id"]:
                rooms[i] = cleaned
                save_devices(doc)
                return {"ok": True, "room": cleaned}
        if len(rooms) >= C.COLLECTION_CAP:
            return {"ok": False, "error": "too_many",
                    "message": "too many rooms (cap %d)" % C.COLLECTION_CAP}
        rooms.append(cleaned)
        save_devices(doc)
        return {"ok": True, "room": cleaned}


def delete_room(room_id: str) -> Dict[str, Any]:
    with devices_lock():
        doc = load_devices()
        before = len(doc.get("rooms") or [])
        doc["rooms"] = [
            r for r in (doc.get("rooms") or [])
            if not (isinstance(r, dict) and r.get("id") == room_id)
        ]
        if len(doc["rooms"]) == before:
            return {"ok": False, "error": "not_found", "message": "room not found"}
        for d in doc.get("devices") or []:
            if isinstance(d, dict) and d.get("room_id") == room_id:
                d.pop("room_id", None)
        save_devices(doc)
    return {
        "ok": True,
        "rooms": _listed_rooms(doc),
        "devices": _listed_room_devices(doc),
    }


def upsert_device(dev: Dict[str, Any]) -> Dict[str, Any]:
    cleaned, err = models.validate_device(dev)
    if err:
        return {"ok": False, "error": "invalid_device", "message": err}
    with devices_lock():
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


def rename_device(device_id: str, name: Any) -> Dict[str, Any]:
    """Patch only the catalogue `name`. Bindings, type and icon stay as stored.

    The success payload is exactly {ok, name} — the hub cache patches itself
    from those keys (docs/contracts/cloud-scenarios.md §Channel).
    """
    did = str(device_id or "").strip()
    if not models._ID_RE.match(did):
        return {"ok": False, "error": "not_found", "message": "device not found"}
    if not isinstance(name, str):
        return {"ok": False, "error": "invalid_name", "message": "invalid device name"}
    cleaned = name.strip()
    if not cleaned or not models._NAME_RE.match(cleaned):
        return {"ok": False, "error": "invalid_name", "message": "invalid device name"}
    with devices_lock():
        doc = load_devices()
        for d in doc.get("devices") or []:
            if isinstance(d, dict) and d.get("id") == did:
                d["name"] = cleaned
                save_devices(doc)
                return {"ok": True, "name": cleaned}
    return {"ok": False, "error": "not_found", "message": "device not found"}


def delete_device(device_id: str) -> Dict[str, Any]:
    with devices_lock():
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
        for g in doc.get("groups") or []:
            if isinstance(g, dict) and isinstance(g.get("device_ids"), list):
                g["device_ids"] = [x for x in g["device_ids"] if x != device_id]
        save_devices(doc)
    return {"ok": True}


def reset_mappings() -> Dict[str, Any]:
    """Factory-reset helper: clear devices, keep certs, disable both clients
    (the cloud flag mirrors client_enabled — same conf, same policy row)."""
    with devices_lock():
        save_devices(empty_devices())
    set_client_enabled(False)
    set_cloud_control_enabled(False)
    return {
        "ok": True,
        "client_enabled": False,
        "cloud_control_enabled": False,
        "devices": empty_devices(),
    }


# ── Cloud scenario channel (alice_devices_scenarios) ────────────────────────
# Event payloads arrive with a hub-minted request_id; the handlers below only
# touch the local JSON documents and answer the dict the hub cache patches
# from (docs/contracts/alice-gateway.md, docs/contracts/cloud-scenarios.md).
# rename/rooms/groups are served by the same functions the local CGI uses
# (above) — one write path, one validation rule set.


def listed_scenarios() -> Optional[Dict[str, Any]]:
    """Scenario store snapshot for the cloud-profile list payload.

    None when the rules stack is not installed or has never written its store
    — the hub reads the ABSENT key as «scenarios unsupported» and the editor
    shows «контроллер не поддерживает сценарии» (cloud-scenarios.md).
    """
    store = _rules_store()
    if store is None:
        return None
    path = getattr(store, "DEFAULT_PATH", "")
    if not path or not os.path.exists(path):
        return None
    try:
        doc = store.load()
        return {
            "scenarios": store.listed(doc),
            "runs": list(doc.get("runs") or [])[-20:],
            "notify_queue": list(doc.get("notify_queue") or []),
            "library": doc.get("library") or "",
            "rules_engine": int(getattr(store, "RULES_ENGINE", 1)),
        }
    except Exception as exc:
        log.error("listed_scenarios failed: %s", exc)
        return None


def apply_scenarios(data: Dict[str, Any]) -> Dict[str, Any]:
    """`alice_devices_scenarios`: hand the document to sa02m_rules.store.

    The store answers the full ok payload (scenarios + runs + notify_queue +
    library); `rules_engine` rides along so the hub cache records the board
    engine version on every exchange, not only on the list fetch.
    """
    store = _rules_store()
    if store is None:
        return {"ok": False, "error": "rules_unavailable"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "bad json"}
    body = {k: v for k, v in data.items() if k != "request_id"}
    try:
        result = store.apply_command(body)
    except Exception as exc:
        log.error("apply_scenarios failed: %s", exc)
        return {"ok": False, "error": "internal"}
    if isinstance(result, dict) and result.get("ok"):
        result.setdefault("rules_engine", int(getattr(store, "RULES_ENGINE", 1)))
    return result if isinstance(result, dict) else {"ok": False, "error": "internal"}


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
    if path.endswith("/mqtt-inventory") and m == "GET":
        return 200, build_mqtt_inventory()
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
    if action == "cloud_control_enable":
        return 200, set_cloud_control(True)
    if action == "cloud_control_disable":
        return 200, set_cloud_control(False)
    if action == "link":
        return 200, start_link()
    if action == "complete_link":
        return 200, complete_link()
    if action == "unlink":
        return 200, unlink_controller()
    if action == "mqtt_topics":
        return 200, list_mqtt_topics()
    if action == "mqtt_inventory":
        return 200, build_mqtt_inventory()
    if action == "upsert_room":
        return 200, upsert_room(body.get("room") or body)
    if action == "delete_room":
        return 200, delete_room(str(body.get("id") or ""))
    if action == "upsert_device":
        return 200, upsert_device(body.get("device") or body)
    if action == "delete_device":
        return 200, delete_device(str(body.get("id") or ""))
    if action == "rename_device":
        return 200, rename_device(str(body.get("id") or body.get("device") or ""), body.get("name"))

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


# AF_UNIX (and socketserver.UnixStreamServer) exist only on POSIX; the target is
# Linux. Guard the class so this module still imports on a Windows dev box, where
# the server is never run. `_UnixConfigServer` is simply absent off-POSIX.
_HAVE_AF_UNIX = hasattr(socketserver, "UnixStreamServer")

if _HAVE_AF_UNIX:

    class _UnixConfigServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        """HTTP over AF_UNIX for the local config API. Filesystem permissions on the
        socket ARE the access control — see serve_unix(). Why not http.server: it is
        AF_INET only; the flasher daemon uses the same AF_UNIX shape for the same
        reason (opt/sa02m-flasher/sa02m_flasher/service.py)."""

        daemon_threads = True
        allow_reuse_address = True

        def get_request(self) -> Tuple[Any, Any]:
            sock, _ = self.socket.accept()
            return sock, ("unix", 0)


def serve_unix(sock_path: str = "/run/sa02m-alice/config.sock") -> None:
    """Local config API over a root-only AF_UNIX socket (sa02m-alice-config.service).

    The web UI does NOT talk to this socket — sa02m_alice_api.cgi dispatches
    in-process behind session auth. This endpoint is for root-local tooling / a
    future nginx proxy.

    It previously bound 127.0.0.1:8012 over TCP with NO authentication of any kind
    (the name "serve_unix" was itself a lie), so ANY local process — www-data
    (bypassing the session + CSRF the CGI enforces), mosquitto, nodered, CODESYS —
    could drive enable/disable/link/unlink/upsert_device/delete_device as root
    (audit 2026-08-28, M5). It now binds an AF_UNIX socket at mode 0600 owned by the
    service user (root), so the only reach is a process already running as root. A
    future caller that legitimately needs it gets an explicit group grant then;
    nothing today does.
    """
    if not _HAVE_AF_UNIX:
        raise RuntimeError("AF_UNIX sockets are unavailable on this platform")
    parent = os.path.dirname(sock_path) or "/run/sa02m-alice"
    os.makedirs(parent, exist_ok=True)
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    # An upgraded board can still carry the breadcrumb the TCP era wrote beside
    # the socket ("<sock>.tcp", holding the old loopback endpoint named in the
    # docstring above). Nothing reads it any more, but leaving it advertises a
    # listener that no longer exists. /run is tmpfs so a reboot would clear it
    # anyway — remove it here so the first start after the upgrade does
    # (security review 1.0.6.24, F6). The port literal is deliberately NOT
    # repeated: tests/test_config_api_socket.py asserts this function's body
    # names it nowhere, which is what keeps the retired endpoint from creeping
    # back in.
    try:
        os.unlink(sock_path + ".tcp")
    except OSError:
        pass
    # Create the socket owner-only from the outset (umask), then re-assert 0600 in
    # case a lax umask or a prior file survived — the socket is the whole boundary.
    old_umask = os.umask(0o177)
    try:
        httpd = _UnixConfigServer(sock_path, _Handler)
    finally:
        os.umask(old_umask)
    try:
        os.chmod(sock_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600, owner (root) only
    except OSError:
        pass
    httpd.serve_forever()


def main() -> int:
    serve_unix()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
