"""The HomeKit card's dispatch — run by `sa02m_homekit_api.cgi` as www-data.

Standard library only, on the SYSTEM python3 with `PYTHONPATH=/opt/sa02m-homekit`:
it never imports the venv (pyhap, segno) nor `sa02m_alice`, so a board whose
venv is broken still gets an honest card. It reads the conf and the three
/run files the daemon writes, writes the conf, and names the privileged verb
the CGI may nudge through sudo — it never runs a privileged command itself.

CLI contract (the CGI's only way in): `python3 -m sa02m_homekit.api <METHOD>`
with the request body on stdin; stdout is exactly two lines — the response
JSON, then the verb (`enable|disable|restart|reset-pairing`) or an empty line.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

from . import __version__
from . import config as conf_mod
from . import constants as C
from . import netif

_CODE_RE = re.compile(C.SETUP_CODE_RE)
_SETUP_ID_RE = re.compile(r"^[0-9A-Z]{4}$")
_QR_ROW_RE = re.compile(r"^[01]{21,177}$")
_URI_RE = re.compile(r"^X-HM://[0-9A-Z]{13}$")

Response = Dict[str, Any]


def footprint_installed() -> bool:
    """The install.sh footprint (D9): unit, venv interpreter, package. A board
    that got this package by OTA alone lacks the venv/unit ⇒ `not_installed`."""
    return (
        os.path.isfile(C.UNIT_FILE)
        and os.path.isfile(C.VENV_PYTHON)
        and os.path.isdir(C.PACKAGE_DIR)
    )


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


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def interfaces() -> List[Dict[str, Any]]:
    """The picker's options: the two wired ports, present or not."""
    return [
        {
            "name": name,
            "present": netif.interface_present(name),
            "address": netif.ipv4_address(name),
        }
        for name in C.INTERFACES
    ]


def merged_status(now: Optional[float] = None) -> Response:
    """Conf + status.json + projection.json → the card's view."""
    now = time.time() if now is None else now
    conf = conf_mod.load()
    installed = footprint_installed()
    st = _read_json(C.STATUS_FILE) or {}
    proj = _read_json(C.PROJECTION_FILE) or {}
    reason = _str(st.get("reason"))
    if reason not in C.REASONS:
        reason = ""
    if not installed:
        state, reason = C.STATE_NOT_INSTALLED, ""
    elif not conf.enabled:
        state, reason = C.STATE_DISABLED, ""
    elif not st:
        state = C.STATE_STARTING
    else:
        state = _str(st.get("state"))
        if state not in C.STATES or state == C.STATE_NOT_INSTALLED:
            state = C.STATE_ERROR
        elif state == C.STATE_DISABLED:
            # Enabled a moment ago; the unit has not written its first status.
            state = C.STATE_STARTING
        if state in C.HEARTBEAT_STATES:
            ts = st.get("ts")
            fresh = (
                isinstance(ts, (int, float)) and not isinstance(ts, bool)
                and -C.STATUS_STALE_S <= now - ts <= C.STATUS_STALE_S
            )
            if not fresh:
                state, reason = C.STATE_ERROR, C.REASON_STATUS_STALE
    live = state in (C.STATE_RUNNING, C.STATE_NO_INTERFACE, C.STATE_PORT_IN_USE, C.STATE_STARTING)
    paired = st.get("paired") if state == C.STATE_RUNNING else None
    if not isinstance(paired, bool):
        paired = None
    skipped = proj.get("skipped") if live else None
    accessory_list = proj.get("accessories") if live else None
    return {
        "ok": True,
        "state": state,
        "reason": reason,
        "message": _str(st.get("message"))[:200] if installed and conf.enabled else "",
        "enabled": conf.enabled,
        "interface": conf.interface,
        "address": _str(st.get("address")) if state == C.STATE_RUNNING else "",
        "port": conf.port,
        "bridge_name": _str(st.get("bridge_name")),
        "paired": paired,
        "pairings": _int(st.get("pairings")) if state == C.STATE_RUNNING else 0,
        "accessories": _int(st.get("accessories")) if state == C.STATE_RUNNING else 0,
        "skipped_total": _int(proj.get("skipped_total")) if live else 0,
        "skipped": skipped[: C.SKIPPED_CAP] if isinstance(skipped, list) else [],
        "accessory_list": accessory_list if isinstance(accessory_list, list) else [],
        "pair_setup_locked": bool(st.get("pair_setup_locked")) if state == C.STATE_RUNNING else False,
        "setup_available": (
            state == C.STATE_RUNNING and paired is False and os.path.exists(C.SETUP_FILE)
        ),
        "interfaces": interfaces(),
        "port_default": C.DEFAULT_PORT,
        "port_min": C.PORT_MIN,
        "port_max": C.PORT_MAX,
        "forbidden_ports": sorted(C.FORBIDDEN_PORTS),
        "version": __version__,
        "ts": int(now),
    }


def setup_view() -> Response:
    """The setup code + QR — only while running ∧ unpaired, only from the
    daemon's 0640 file. Anything malformed is `not_available`, never echoed."""
    status = merged_status()
    if status["state"] != C.STATE_RUNNING or status["paired"] is not False:
        return {"ok": False, "error": "not_available"}
    data = _read_json(C.SETUP_FILE)
    if data is None:
        return {"ok": False, "error": "not_available"}
    code = _str(data.get("code"))
    uri = _str(data.get("setup_uri"))
    setup_id = _str(data.get("setup_id"))
    qr = data.get("qr")
    rows_ok = (
        isinstance(qr, list) and len(qr) >= 21
        and all(isinstance(r, str) and _QR_ROW_RE.match(r) and len(r) == len(qr) for r in qr)
    )
    if not (_CODE_RE.match(code) and _URI_RE.match(uri) and _SETUP_ID_RE.match(setup_id)
            and uri.endswith(setup_id) and rows_ok):
        return {"ok": False, "error": "not_available"}
    return {"ok": True, "available": True, "code": code, "setup_uri": uri,
            "setup_id": setup_id, "qr": qr}


def _save(conf: conf_mod.BridgeConfig) -> Optional[Response]:
    try:
        conf_mod.save(conf)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "conf_write_failed", "message": str(exc)[:200]}
    return None


def _mutated(action: str) -> Response:
    return {"ok": True, "action": action, "status": merged_status()}


def dispatch(method: str, body: Any) -> Tuple[Response, Optional[str]]:
    """(response, verb-to-nudge or None). The action set is an allow-list."""
    method = (method or "GET").upper()
    if method == "GET":
        return merged_status(), None
    if method != "POST":
        return {"ok": False, "error": "method_not_allowed"}, None
    if not isinstance(body, dict):
        return {"ok": False, "error": "invalid_json"}, None
    action = body.get("action")
    if action in (None, "", "status"):
        return merged_status(), None
    if action == "setup":
        return setup_view(), None
    if action not in ("enable", "disable", "set_interface", "set_port", "reset_pairing"):
        return {"ok": False, "error": "not_found"}, None
    if not footprint_installed():
        return {"ok": False, "error": "not_installed"}, None
    if action == "reset_pairing":
        return _mutated(action), "reset-pairing"
    conf = conf_mod.load()
    verb: Optional[str]
    if action in ("enable", "disable"):
        conf.enabled = action == "enable"
        verb = action
    elif action == "set_interface":
        iface = body.get("interface")
        if not conf_mod.valid_interface(iface):
            return {"ok": False, "error": "invalid_interface"}, None
        conf.interface = iface
        verb = "restart" if conf.enabled else None
    else:
        port = conf_mod.parse_port(body.get("port"))
        if port is None:
            return {"ok": False, "error": "invalid_port"}, None
        conf.port = port
        verb = "restart" if conf.enabled else None
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
