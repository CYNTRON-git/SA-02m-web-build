"""The Home Connect card's dispatch — run by `sa02m_homeconnect_api.cgi` as www-data.

Standard library only, on the system python3 with
`PYTHONPATH=/opt/sa02m-homeconnect`. It reads the conf and the three /run
files the daemon writes, writes the conf, and names the privileged verb the
CGI may nudge through sudo — it never runs a privileged command itself and
never opens the token file (0600, daemon user: www-data cannot read it, and
nothing here tries). No response carries a token or the device code.

CLI contract (the CGI's only way in): `python3 -m sa02m_homeconnect.api <METHOD>`
with the request body on stdin; stdout is exactly two lines — the response
JSON, then the verb (`enable|disable|restart|unlink`) or an empty line.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import sys
import time
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

from . import __version__
from . import config as conf_mod
from . import constants as C

Response = Dict[str, Any]

_USER_CODE_RE = re.compile(r"^[A-Za-z0-9-]{4,32}$")
_DEVICE_ID_RE = re.compile(r"^hc-[a-z0-9-]{1,61}$")
_CONTROL_RE = re.compile(r"^[a-z_]{1,32}$")
# States a LIVE daemon re-writes every heartbeat.
_LIVE = frozenset(s for s in C.STATES if s not in C.EXIT_ONCE_STATES and s != C.STATE_NOT_INSTALLED)


def footprint_installed() -> bool:
    """The install.sh footprint (D9): unit, package, daemon user. A board that
    got the package by OTA alone lacks the user ⇒ `not_installed`."""
    if not (os.path.isfile(C.UNIT_FILE) and os.path.isdir(C.PACKAGE_DIR)):
        return False
    try:
        pwd.getpwnam(C.DAEMON_USER)
    except KeyError:
        return False
    return True


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _str(value: Any, max_len: int = 200) -> str:
    return value[:max_len] if isinstance(value, str) else ""


def _conf_mtime() -> Optional[float]:
    try:
        return os.stat(C.CONF_FILE).st_mtime
    except OSError:
        return None


def _link_view(now: float) -> Optional[Dict[str, Any]]:
    """The user code + URIs from the daemon's 0640 file, only if well-formed
    and unexpired. Anything else is None, never echoed."""
    data = _read_json(C.LINK_FILE)
    if data is None:
        return None
    code = _str(data.get("user_code"), 64)
    uri = _str(data.get("verification_uri"), 512)
    complete = _str(data.get("verification_uri_complete"), 512)
    expires = _int(data.get("expires_at"))
    if not (_USER_CODE_RE.match(code) and uri.startswith("https://")
            and (not complete or complete.startswith("https://")) and expires > now):
        return None
    return {"user_code": code, "verification_uri": uri,
            "verification_uri_complete": complete, "expires_at": expires}


def _appliance_list() -> List[Dict[str, Any]]:
    data = _read_json(C.INVENTORY_FILE) or {}
    items = data.get("appliances")
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not _DEVICE_ID_RE.match(_str(item.get("device_id"), 64)):
            continue
        controls = item.get("controls")
        out.append({
            "device_id": item["device_id"],
            "name": _str(item.get("name"), C.NAME_MAX),
            "type": _str(item.get("type"), 32),
            "brand": _str(item.get("brand"), 32),
            "connected": item.get("connected") is True,
            "controls": [c for c in controls if isinstance(c, str) and _CONTROL_RE.match(c)]
            if isinstance(controls, list) else [],
        })
        if len(out) >= C.MAX_APPLIANCES:
            break
    return out


def merged_status(now: Optional[float] = None) -> Response:
    """Conf + status.json (+ link.json, inventory.json) → the card's view."""
    now = time.time() if now is None else now
    conf = conf_mod.load()
    installed = footprint_installed()
    st = _read_json(C.STATUS_FILE) or {}
    reason = _str(st.get("reason"))
    if reason not in C.REASONS:
        reason = ""
    if not installed:
        state, reason = C.STATE_NOT_INSTALLED, ""
    elif not conf.enabled:
        state, reason = C.STATE_DISABLED, ""
    elif not conf.effective_client_id:
        # The conf is the truth; the daemon follows within CONF_POLL_S.
        state, reason = C.STATE_MISSING_CLIENT_ID, ""
    elif not st:
        state = C.STATE_CONNECTING
    else:
        state = _str(st.get("state"))
        if state not in C.STATES or state == C.STATE_NOT_INSTALLED:
            state, reason = C.STATE_ERROR, ""
        elif state in (C.STATE_DISABLED, C.STATE_MISSING_CLIENT_ID):
            # Enabled / client id set a moment ago; the daemon has not caught
            # up. "A moment" is measured from the conf write, so a unit that
            # never starts still turns into status_stale.
            state, reason = C.STATE_CONNECTING, ""
            st = dict(st, ts=_conf_mtime())
        if state in _LIVE:
            ts = st.get("ts")
            fresh = (isinstance(ts, (int, float)) and not isinstance(ts, bool)
                     and -C.STATUS_STALE_S <= now - ts <= C.STATUS_STALE_S)
            if not fresh:
                state, reason = C.STATE_ERROR, C.REASON_STATUS_STALE
    daemon_view = installed and conf.enabled and state not in (
        C.STATE_MISSING_CLIENT_ID, C.STATE_NOT_INSTALLED, C.STATE_DISABLED)
    linked = st.get("linked") is True and daemon_view and state != C.STATE_ERROR
    link = _link_view(now) if state == C.STATE_AWAITING_USER else None
    return {
        "ok": True,
        "state": state,
        "reason": reason,
        "message": _str(st.get("message")) if daemon_view else "",
        "enabled": conf.enabled,
        "linked": linked,
        "host": conf.host,
        "client_id": conf.client_id,
        "client_id_source": conf.client_id_source,
        "vendor_preset": bool(conf.vendor_client_id),
        "appliances": _int(st.get("appliances")) if linked else 0,
        "appliances_connected": _int(st.get("appliances_connected")) if linked else 0,
        "appliance_list": _appliance_list() if linked else [],
        "stream": _str(st.get("stream"), 8) if daemon_view else "off",
        "budget": {
            "day": _str(st.get("budget_day"), 10),
            "used": _int(st.get("budget_used")),
            "limit": C.DAILY_LIMIT,
            "local": C.LOCAL_BUDGET,
            "remaining": _int(st.get("budget_remaining"), C.DAILY_LIMIT),
        },
        "rate_limited_until": _int(st.get("rate_limited_until")) if state == C.STATE_RATE_LIMITED else 0,
        "last_event_ts": _int(st.get("last_event_ts")) if linked else 0,
        "link": link,
        "read_only": True,
        "version": __version__,
        "ts": int(now),
    }


def _save(conf: conf_mod.ClientConfig) -> Optional[Response]:
    try:
        conf_mod.save(conf)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "conf_write_failed", "message": str(exc)[:200]}
    return None


def _mutated(action: str) -> Response:
    return {"ok": True, "action": action, "status": merged_status()}


MUTATIONS = ("enable", "disable", "set_client_id", "link", "unlink")


def dispatch(method: str, body: Any, now: Optional[float] = None) -> Tuple[Response, Optional[str]]:
    """(response, verb-to-nudge or None). The action set is an allow-list."""
    method = (method or "GET").upper()
    if method == "GET":
        return merged_status(now), None
    if method != "POST":
        return {"ok": False, "error": "method_not_allowed"}, None
    if not isinstance(body, dict):
        return {"ok": False, "error": "invalid_json"}, None
    action = body.get("action")
    if action in (None, "", "status"):
        return merged_status(now), None
    if action not in MUTATIONS:
        return {"ok": False, "error": "not_found"}, None
    if not footprint_installed():
        return {"ok": False, "error": "not_installed"}, None
    conf = conf_mod.load()
    verb: Optional[str]
    if action in ("enable", "disable"):
        conf.enabled = action == "enable"
        verb = action
    elif action == "set_client_id":
        raw = body.get("client_id")
        value = raw.strip() if isinstance(raw, str) else None
        if value is None or (value and not conf_mod.valid_client_id(value)):
            return {"ok": False, "error": "invalid_client_id"}, None
        if value == conf.client_id:
            return _mutated(action), None
        conf.client_id = value
        # A new application invalidates the pending «Подключить».
        conf.link_requested_at = 0
        verb = "restart" if conf.enabled else None
    elif action == "link":
        if not conf.enabled:
            return {"ok": False, "error": "not_enabled"}, None
        if not conf.effective_client_id:
            return {"ok": False, "error": "missing_client_id"}, None
        current = merged_status(now)
        if current["linked"]:
            return {"ok": False, "error": "already_linked"}, None
        conf.link_requested_at = int(time.time() if now is None else now)
        verb = "restart"
    else:  # unlink
        conf.link_requested_at = 0
        verb = "unlink"
    failed = _save(conf)
    if failed is not None:
        return failed, None
    return _mutated(action), verb


def cli(argv: List[str], stdin: BinaryIO, stdout: BinaryIO) -> int:
    method = argv[0] if argv else "GET"
    raw = stdin.read(C.MAX_BODY_BYTES + 1)
    verb: Optional[str] = None
    if len(raw) > C.MAX_BODY_BYTES:
        response: Response = {"ok": False, "error": "payload_too_large"}
    else:
        body: Any = {}
        text = raw.decode("utf-8", "replace").strip()
        if text:
            try:
                body = json.loads(text)
            except ValueError:
                body = None
        if body is None and method.upper() == "POST":
            response = {"ok": False, "error": "invalid_json"}
        else:
            response, verb = dispatch(method, body if body is not None else {})
    if verb not in C.TRIGGER_VERBS:
        verb = None
    line = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    stdout.write((line + "\n" + (verb or "") + "\n").encode("utf-8"))
    stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:], sys.stdin.buffer, sys.stdout.buffer))
