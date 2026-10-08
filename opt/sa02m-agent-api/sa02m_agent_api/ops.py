# -*- coding: utf-8 -*-
"""Every operation. A handler's only side effect goes through ctx.side."""

import base64
import json
import os
import re
import time
import urllib.parse

from .cgi_adapter import PART_RE, run_cgi
from .fence import (
    PIP_RE, UNIT_RE, USER_ROOT, FenceError, delete_path, list_dir, mkdir,
    read_text, write_text,
)
from .registry import Op, add
from . import __version__

ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
FORM_KEYS = (
    "net_iface", "ip", "netmask", "gateway", "dns",
    "ip_eth1", "netmask_eth1", "gateway_eth1", "dns_eth1",
    "eth0_enable", "eth1_enable", "skip_network", "timezone", "datetime",
)
# Same list as unit_ok() in usr/local/sbin/sa02m-agent-journal.sh.
JOURNAL_UNITS = (
    "sa02m-agent-api", "sa02m-rules", "sa02m-modbus-mqtt", "mosquitto",
    "nodered", "mplc4", "codesyscontrol", "sa02m-alice-client", "sa02m-alice-config",
    "sa02m-cloud-control", "sa02m-homekit", "sa02m-homeconnect", "sa02m-flasher",
    "sa02m-devices-api", "sa02m-devices-logger", "sa02m-telemetry",
    "sa02m-serial-gateway", "nginx", "fcgiwrap",
)
# Copies of the devices daemon's RANGES (history_ranges.py) and HISTORY_GROUPS
# (history_metrics.py) keys: separate trees and deploys, so no import. The daemon
# turns an unknown range into 1h silently; refusing it here keeps the schema
# honest. tests/test_devices_ops.py pins both tuples to their homes.
HISTORY_RANGES = ("1h", "6h", "24h", "7d", "30d", "mtd", "month")
HISTORY_GROUPS = ("climate", "energy", "ahu", "mtd")


def _err(status, error, **extra):
    body = {"ok": False, "error": error}
    body.update(extra)
    return status, body


def _ok(payload):
    if isinstance(payload, dict) and "ok" in payload:
        return 200, payload
    return 200, {"ok": True, "result": payload}


def _cgi(ctx, script, method, query="", body=b"", content_type="application/json"):
    status, parsed = run_cgi(ctx, script, method, query, body, content_type)
    if isinstance(parsed, dict) and parsed.get("ok") is False and status < 400:
        return 200, parsed
    return (status if status >= 400 else 200), parsed


def _form(args, keys):
    parts = []
    for key in keys:
        if key in args and args[key] is not None:
            parts.append(urllib.parse.urlencode({key: str(args[key])}))
    return "&".join(parts).encode("utf-8")


def _live_dir():
    return os.environ.get("SA02M_MQTT_RUN") or "/run/sa02m-modbus-mqtt"


def _read_live():
    root = _live_dir()
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    now = time.time()
    for name in sorted(names):
        if not name.endswith(".json"):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.islink(path):
                continue
            st = os.lstat(path)
            if now - st.st_mtime > 10:
                continue
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append({"device": name[:-5], "data": data})
    return out


def _rules_doc():
    path = os.environ.get("SA02M_RULES_PATH") or "/etc/sa02m-rules/scenarios.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"scenarios": []}
    return data if isinstance(data, dict) else {"scenarios": []}


def _rules_runs():
    path = os.environ.get("SA02M_RULES_RUNS") or "/etc/sa02m-rules/runs.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return data


def _sudo(ctx, helper, argv):
    return ctx.run(["sudo", "-n", "/usr/local/sbin/" + helper, *argv], timeout=130)


def _proc(proc, **extra):
    """A helper's exit status as the answer: ok only when it exited 0."""
    body = {"ok": proc.get("code") == 0, "code": proc.get("code"),
            "stdout": (proc.get("stdout") or "")[:8000],
            "stderr": (proc.get("stderr") or "")[:2000]}
    body.update(extra)
    return 200, body


def _fenced(name, fn):
    """Run a sandbox file op; a fence refusal or an I/O error is a 400, not a 500."""
    try:
        return _ok(fn())
    except FenceError as exc:
        return _err(400, "bad_request", reason=str(exc))
    except OSError as exc:
        return _err(400, "io_error", reason=type(exc).__name__)


# Keys the dispatcher consumes; a CGI never sees them.
_CONTROL_KEYS = ("confirm", "dry_run", "wait")


def _payload(args):
    """The caller's arguments without the dispatcher's control keys."""
    return {k: v for k, v in (args or {}).items() if k not in _CONTROL_KEYS}


def _unix(ctx, sock, method, path, body=b""):
    return ctx.unix_request(sock, method, path, body)


# devices-api query grammar: key → (JSON schema, accepts(text)). The daemon's
# handle_history / handle_summary (opt/sa02m-devices/sa02m_devices/api.py) is
# the home of what each key means; this only shapes what may travel.
_DEV_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_DEVICE_KEYS = {
    "device_id": ({"type": "string", "description": "e.g. ce02m3-COM1-5, carel-COM3-2"},
                  _DEV_ID.match),
    "kind": ({"type": "string", "description": "carel | mr (own series paths); other kinds use metric/group"},
             re.compile(r"^[a-z][a-z0-9_]{0,31}$").match),
    "metric": ({"type": "string", "description": "one metric id (devices-mr-history.md)"},
               re.compile(r"^[A-Za-z0-9_]{1,64}$").match),
    "group": ({"type": "string", "enum": list(HISTORY_GROUPS)},
              lambda v: v in HISTORY_GROUPS),
    "range": ({"type": "string", "enum": list(HISTORY_RANGES)},
              lambda v: v in HISTORY_RANGES),
    "window_s": ({"type": "integer", "description": "seconds, overrides range; daemon clamps to 60..2592000"},
                 re.compile(r"^[0-9]{1,8}$").match),
    "channel": ({"type": "string", "description": "kind=mr: AI channel, 5 or ai_5"},
                re.compile(r"^(ai_)?[0-9]{1,3}$").match),
    "kwh_rub": ({"type": "number", "description": "tariff, RUB per kWh"},
                re.compile(r"^[0-9]{1,6}([.,][0-9]{1,6})?$").match),
}
_HISTORY_KEYS = ("device_id", "kind", "metric", "group", "range", "window_s", "channel")
_SUMMARY_KEYS = ("range", "device_id", "kwh_rub")


def _devices_schema(keys, legacy_device=False):
    props = {k: _DEVICE_KEYS[k][0] for k in keys}
    if legacy_device:
        props["device"] = {"type": "string", "deprecated": True,
                           "description": "legacy alias of device_id"}
    return {"type": "object", "properties": props}


def _devices_query(args, keys):
    """(query, None), or (None, key) for the first refused value. A key that is
    absent, null or empty does not travel; `device` stands in for device_id."""
    pairs = []
    for key in keys:
        val = args.get(key)
        if key == "device_id" and val in (None, ""):
            val = args.get("device")
        if val is None or val == "":
            continue
        if isinstance(val, bool) or not isinstance(val, (str, int, float)):
            return None, key
        text = str(val)
        if not _DEVICE_KEYS[key][1](text):
            return None, key
        pairs.append((key, text))
    return urllib.parse.urlencode(pairs), None


def _devices_get(ctx, name, path, args, keys):
    query, bad = _devices_query(args, keys)
    if bad:
        return _err(400, "bad_request", reason=bad)

    def work():
        # unix_request carries the agent's own panel service session (Cookie),
        # which the daemon checks on every listener since 1.0.6.65.
        sock = os.environ.get("SA02M_DEVICES_SOCK") or "/run/sa02m-devices/api.sock"
        status, raw = _unix(ctx, sock, "GET", path + ("?" + query if query else ""))
        # The whole body or an explicit refusal, never a cut string: unix_request
        # answers 502 response_too_large past its read limit (agent-api.md).
        if status == 502 and isinstance(raw, str):
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("error") == "response_too_large":
                body["hint"] = "narrow range or window_s"
                return status, body
        return status, {"ok": status < 400, "body": raw}
    return ctx.side(name, work)


def register_all():
    if register_all.done:
        return
    register_all.done = True
    _register()


register_all.done = False


def _register():
    def status(ctx, args):
        part = str(args.get("part") or "all")
        if not PART_RE.match(part):
            return _err(400, "bad_request")

        def work():
            return _cgi(ctx, "status.cgi", "GET", "part=" + part)
        return ctx.side("system.status", work)

    add(Op("system.status", "read", "Dashboard status.cgi part", status,
           {"type": "object", "properties": {"part": {"type": "string"}}}))

    def info(ctx, args):
        def work():
            ver = ""
            vpath = os.environ.get("SA02M_VERSION_FILE") or "/var/www/network_config/VERSION"
            try:
                with open(vpath, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            ver = line
                            break
            except OSError:
                ver = ""
            return _ok({"version": ver, "api": "sa02m-agent-api", "api_version": __version__})
        return ctx.side("system.info", work)

    add(Op("system.info", "read", "Version and API identity", info))

    def services_list(ctx, args):
        return ctx.side("services.list", lambda: _cgi(ctx, "services_ctrl.cgi", "GET"))

    add(Op("services.list", "read", "Managed services", services_list))

    def network_get(ctx, args):
        return ctx.side("network.get", lambda: _cgi(ctx, "config.cgi", "GET"))

    add(Op("network.get", "read", "Network and time config", network_get))

    def mqtt_get(ctx, args):
        return ctx.side("mqtt.config.get", lambda: _cgi(ctx, "mqtt_config.cgi", "GET"))

    add(Op("mqtt.config.get", "read", "MQTT bridge config", mqtt_get))

    def live(ctx, args):
        return ctx.side("mqtt.devices.live", lambda: _ok(_read_live()))

    add(Op("mqtt.devices.live", "read", "Live MQTT cache, files younger than 10s", live))

    def snap(ctx, args):
        def work():
            rows = _read_live()
            return _ok([{"device": r["device"], "keys": list((r.get("data") or {}))} for r in rows])
        return ctx.side("mqtt.topics.snapshot", work)

    add(Op("mqtt.topics.snapshot", "read", "Keys present in the live cache", snap))

    def gw_get(ctx, args):
        return ctx.side("gateway.config.get", lambda: _cgi(ctx, "gateway_config.cgi", "GET"))

    add(Op("gateway.config.get", "read", "Serial gateway config", gw_get))

    def gw_st(ctx, args):
        return ctx.side("gateway.status", lambda: _cgi(ctx, "gateway_status.cgi", "GET"))

    add(Op("gateway.status", "read", "Serial gateway status", gw_st))

    def rules_list(ctx, args):
        return ctx.side("rules.list", lambda: _ok(_rules_doc()))

    add(Op("rules.list", "read", "Scenario document", rules_list))

    def rules_get(ctx, args):
        sid = str(args.get("id") or "")
        if not ID_RE.match(sid):
            return _err(400, "bad_request")

        def work():
            doc = _rules_doc()
            rows = doc.get("scenarios") or []
            for row in rows:
                if isinstance(row, dict) and row.get("id") == sid:
                    return _ok(row)
            return _err(404, "not_found")
        return ctx.side("rules.get", work)

    add(Op("rules.get", "read", "One scenario by id", rules_get))

    def rules_runs(ctx, args):
        return ctx.side("rules.runs", lambda: _ok(_rules_runs()))

    add(Op("rules.runs", "read", "Scenario run journal", rules_runs))

    def history(ctx, args):
        return _devices_get(ctx, "devices.history", "/api/devices/history", args, _HISTORY_KEYS)

    add(Op("devices.history", "read",
           "Device archive series (sa02m-devices-api /api/devices/history; body = daemon JSON)",
           history, _devices_schema(_HISTORY_KEYS, legacy_device=True)))

    def summary(ctx, args):
        return _devices_get(ctx, "devices.summary", "/api/devices/history/summary", args,
                            _SUMMARY_KEYS)

    add(Op("devices.summary", "read",
           "CE-02m-3 period summary: mean phase power, energy delta, cost "
           "(/api/devices/history/summary)",
           summary, _devices_schema(_SUMMARY_KEYS)))

    def journal(ctx, args):
        unit = str(args.get("unit") or "")
        base = unit[:-8] if unit.endswith(".service") else unit
        inst = re.match(r"^sa02m-user@[a-z0-9][a-z0-9-]{0,30}$", base)
        if base not in JOURNAL_UNITS and not inst:
            return _err(400, "bad_request")
        try:
            lines = int(args.get("lines") or 50)
        except (TypeError, ValueError):
            return _err(400, "bad_request")
        if lines < 1 or lines > 200:
            return _err(400, "bad_request")

        def work():
            proc = _sudo(ctx, "sa02m-agent-journal.sh", [base, str(lines)])
            return _ok({"code": proc.get("code"), "text": (proc.get("stdout") or "")[:8000]})
        return ctx.side("logs.journal", work)

    add(Op("logs.journal", "read", "journalctl for an allow-listed unit", journal))

    def install_log(ctx, args):
        return ctx.side("logs.install", lambda: _cgi(ctx, "log.cgi", "GET"))

    add(Op("logs.install", "read", "Installer log tail", install_log))

    def u_list(ctx, args):
        rel = str(args.get("path") or "")
        return ctx.side("user.files.list", lambda: _fenced("list", lambda: list_dir(rel)))

    add(Op("user.files.list", "read", "List /opt/sa02m-user", u_list,
           {"type": "object", "properties": {"path": {"type": "string"}}}))

    def u_read(ctx, args):
        rel = str(args.get("path") or "")
        return ctx.side("user.files.read", lambda: _fenced("read", lambda: {"text": read_text(rel)}))

    add(Op("user.files.read", "read", "Read a file under /opt/sa02m-user", u_read,
           {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}))

    def u_status(ctx, args):
        name = str(args.get("name") or "")
        if not UNIT_RE.match(name):
            return _err(400, "bad_request")
        def work():
            proc = _sudo(ctx, "sa02m-user-unit.sh", ["status", name])
            return _ok({"code": proc.get("code"), "active": (proc.get("stdout") or "").strip(),
                        "text": proc.get("stdout") or ""})
        return ctx.side("user.units.status", work)

    add(Op("user.units.status", "read", "Status of a user unit", u_status,
           {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}))

    def u_logs(ctx, args):
        name = str(args.get("name") or "")
        if not UNIT_RE.match(name):
            return _err(400, "bad_request")
        def work():
            proc = _sudo(ctx, "sa02m-agent-journal.sh", ["sa02m-user@" + name, "80"])
            return _ok({"text": (proc.get("stdout") or "")[:8000]})
        return ctx.side("user.units.logs", work)

    add(Op("user.units.logs", "read", "Journal of a user unit", u_logs))

    def nr_get(ctx, args):
        return ctx.side("nodered.flows.get", lambda: ctx.nodered("GET", "/flows"))

    add(Op("nodered.flows.get", "read", "Node-RED flows", nr_get))

    def mplc_info(ctx, args):
        def work():
            path = os.environ.get("SA02M_MPLC_DIR") or "/opt/mplc4"
            return _ok({"present": os.path.isdir(path)})
        return ctx.side("mplc.project.info", work)

    add(Op("mplc.project.info", "read", "Whether the MPLC runtime directory exists", mplc_info))

    def _flash_get(name, path):
        def handler(ctx, args):
            def work():
                sock = os.environ.get("SA02M_FLASHER_SOCK") or "/run/sa02m-flasher/flasher.sock"
                status, raw = _unix(ctx, sock, "GET", path)
                return status, {"ok": status < 400, "body": raw}
            return ctx.side(name, work)
        return handler

    add(Op("flasher.ports", "read", "Flasher COM ports", _flash_get("flasher.ports", "/ports")))
    add(Op("flasher.firmware", "read", "Flasher firmware catalog", _flash_get("flasher.firmware", "/firmware")))
    add(Op("flasher.jobs", "read", "Flasher jobs", _flash_get("flasher.jobs", "/jobs")))

    def publish(ctx, args):
        for key in ("device", "control", "value"):
            if not isinstance(args.get(key), str) or not args[key] or len(args[key]) > 128:
                return _err(400, "bad_request")
        body = _form(args, ("device", "control", "value"))
        return ctx.side("mqtt.publish", lambda: _cgi(
            ctx, "mqtt_set.cgi", "POST", "", body, "application/x-www-form-urlencoded"))

    add(Op("mqtt.publish", "control", "Publish a control value (mqtt-set grammar)", publish,
           mutating=True))

    def hw(ctx, args):
        ch = str(args.get("channel") or "")
        val = str(args.get("value") if args.get("value") is not None else "")
        if not re.match(r"^[A-Za-z0-9_-]{1,32}$", ch) or not re.match(r"^-?[0-9]{1,6}$", val):
            return _err(400, "bad_request")
        body = _form({"channel": ch, "value": val}, ("channel", "value"))
        return ctx.side("hw.set", lambda: _cgi(
            ctx, "hw_set.cgi", "POST", "", body, "application/x-www-form-urlencoded"))

    add(Op("hw.set", "control", "Set a board output", hw, mutating=True))

    def rules_run(ctx, args):
        sid = str(args.get("id") or "")
        if not ID_RE.match(sid):
            return _err(400, "bad_request")
        return ctx.side("rules.run", lambda: ctx.rules_apply({"run_now": True, "id": sid}))

    add(Op("rules.run", "control", "Run a scenario now", rules_run, mutating=True))

    def rules_off(ctx, args):
        sid = str(args.get("id") or "")
        if not ID_RE.match(sid):
            return _err(400, "bad_request")
        topic = "/devices/sa02m-rules-%s/controls/run/on" % sid
        def work():
            return _proc(ctx.run([
                "mosquitto_pub", "-h", "127.0.0.1", "-p", "1883",
                "-t", topic, "-m", "0",
            ], timeout=5), topic=topic)
        return ctx.side("rules.off", work)

    add(Op("rules.off", "control", "Turn a scenario's outputs off", rules_off, mutating=True))

    def net_apply(ctx, args):
        body = _form(args, FORM_KEYS)
        return ctx.side("network.apply", lambda: _cgi(
            ctx, "apply.cgi", "POST", "", body, "application/x-www-form-urlencoded"))

    add(Op("network.apply", "config", "Apply network form fields", net_apply, mutating=True))

    def mqtt_set(ctx, args):
        raw = json.dumps(args.get("config") if isinstance(args.get("config"), dict)
                         else _payload(args)).encode()
        return ctx.side("mqtt.config.set", lambda: _cgi(ctx, "mqtt_config.cgi", "POST", "", raw))

    add(Op("mqtt.config.set", "config", "Save the MQTT bridge config", mqtt_set, mutating=True))

    def mqtt_scan(ctx, args):
        port = str(args.get("port") or "")
        if not re.fullmatch(r"/dev/[A-Za-z0-9_-]+", port):
            return _err(400, "bad_request", reason="port")
        raw = json.dumps({k: args.get(k) for k in ("port", "baudrate", "max_addr")
                          if args.get(k) not in (None, "")}).encode()
        return ctx.side("mqtt.scan", lambda: _cgi(ctx, "mqtt_scan.cgi", "POST", "", raw))

    add(Op("mqtt.scan", "config", "Scan the Modbus bus on a serial port", mqtt_scan,
           {"type": "object", "required": ["port"], "properties": {
               "port": {"type": "string", "description": "/dev/ttyXXX"},
               "baudrate": {"type": "integer"}, "max_addr": {"type": "integer"}}},
           mutating=True, long=True))

    def probe(ctx, args):
        raw = json.dumps(_payload(args)).encode()
        return ctx.side("mqtt.tcp_probe", lambda: _cgi(ctx, "mqtt_tcp_probe.cgi", "POST", "", raw))

    add(Op("mqtt.tcp_probe", "config", "One Modbus TCP probe", probe, mutating=True))

    def gw_set(ctx, args):
        raw = json.dumps(_payload(args)).encode()
        return ctx.side("gateway.config.set", lambda: _cgi(ctx, "gateway_config.cgi", "POST", "", raw))

    add(Op("gateway.config.set", "config", "Save the serial gateway config", gw_set, mutating=True))

    def gw_ctrl(ctx, args):
        action = str(args.get("action") or "")
        if action not in ("start", "stop", "restart", "reload"):
            return _err(400, "bad_request", reason="action")
        raw = json.dumps({"action": action}).encode()
        return ctx.side("gateway.ctrl", lambda: _cgi(ctx, "gateway_ctrl.cgi", "POST", "", raw))

    add(Op("gateway.ctrl", "config", "Start or stop the serial gateway", gw_ctrl, mutating=True))

    def _svc(action):
        name = "services." + action
        def handler(ctx, args):
            sid = str(args.get("id") or "")
            if not re.match(r"^[a-z0-9-]{1,32}$", sid):
                return _err(400, "bad_request")
            raw = json.dumps({"id": sid, "action": action}).encode()
            return ctx.side(name, lambda: _cgi(ctx, "services_ctrl.cgi", "POST", "", raw))
        add(Op(name, "config", "Service " + action, handler, mutating=True))

    for act in ("start", "stop", "install", "uninstall"):
        _svc(act)

    # sa02m_rules.store.apply_command reads the verb from a flag key, not a
    # "verb" field: upsert=[rows] | replace=true+scenarios | delete=true+id |
    # library=<text>. One body shape per op, built here.
    def rules_upsert(ctx, args):
        rows = args.get("scenarios")
        if rows is None:
            rows = args.get("upsert")
        if rows is None:
            rows = [_payload(args)]
        if not isinstance(rows, list) or not rows or len(rows) > 16 \
                or not all(isinstance(r, dict) for r in rows):
            return _err(400, "bad_request", reason="scenarios")
        return ctx.side("rules.upsert", lambda: ctx.rules_apply({"upsert": rows}))

    add(Op("rules.upsert", "config", "Create or update scenarios (one row or scenarios[])",
           rules_upsert, {"type": "object", "properties": {
               "scenarios": {"type": "array", "items": {"type": "object"}}},
               "additionalProperties": True}, mutating=True))

    def rules_replace(ctx, args):
        rows = args.get("scenarios")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            return _err(400, "bad_request", reason="scenarios")
        return ctx.side("rules.replace", lambda: ctx.rules_apply(
            {"replace": True, "scenarios": rows}))

    add(Op("rules.replace", "config", "Replace the whole scenario list", rules_replace,
           {"type": "object", "required": ["scenarios"], "properties": {
               "scenarios": {"type": "array", "items": {"type": "object"}}}},
           mutating=True))

    def rules_delete(ctx, args):
        sid = str(args.get("id") or "")
        if not ID_RE.match(sid):
            return _err(400, "bad_request", reason="id")
        return ctx.side("rules.delete", lambda: ctx.rules_apply({"delete": True, "id": sid}))

    add(Op("rules.delete", "config", "Delete a scenario by id", rules_delete,
           {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
           mutating=True))

    def rules_library(ctx, args):
        text = args.get("library")
        if text is None:
            text = args.get("text")
        if not isinstance(text, str) or len(text) > 16000:
            return _err(400, "bad_request", reason="library")
        return ctx.side("rules.library", lambda: ctx.rules_apply({"library": text}))

    add(Op("rules.library", "config", "Store the shared JS library of the scenarios",
           rules_library, {"type": "object", "required": ["library"], "properties": {
               "library": {"type": "string"}}}, mutating=True))

    def nr_deploy(ctx, args):
        return ctx.side("nodered.flows.deploy", lambda: ctx.nodered("POST", "/flows", args.get("flows")))

    add(Op("nodered.flows.deploy", "config", "Deploy Node-RED flows", nr_deploy, mutating=True))

    def nr_nodes(ctx, args):
        module = str(args.get("module") or "")
        if not PIP_RE.match(module):
            return _err(400, "bad_request")
        return ctx.side("nodered.nodes.install", lambda: ctx.nodered(
            "POST", "/nodes", {"module": module}))

    add(Op("nodered.nodes.install", "config", "Install a Node-RED node module", nr_nodes,
           mutating=True, long=True))

    def mplc_deploy(ctx, args):
        raw_b64 = args.get("zip_b64")
        if not isinstance(raw_b64, str) or len(raw_b64) > 8 * 1024 * 1024:
            return _err(400, "bad_request")
        def work():
            try:
                blob = base64.b64decode(raw_b64, validate=True)
            except ValueError:
                return _err(400, "bad_request")
            if len(blob) > 5 * 1024 * 1024:
                return _err(400, "bad_request")
            boundary = "sa02mMplc"
            body = (
                ("--" + boundary + "\r\n"
                 "Content-Disposition: form-data; name=\"file\"; filename=\"project.zip\"\r\n"
                 "Content-Type: application/zip\r\n\r\n").encode()
                + blob + ("\r\n--" + boundary + "--\r\n").encode()
            )
            return _cgi(ctx, "mplc_project_deploy.cgi", "POST", "", body,
                        "multipart/form-data; boundary=" + boundary)
        return ctx.side("mplc.project.deploy", work)

    add(Op("mplc.project.deploy", "config", "Deploy an MPLC project zip", mplc_deploy,
           mutating=True, long=True))

    _PATH_SCHEMA = {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}

    def u_write(ctx, args):
        rel = str(args.get("path") or "")
        text = args.get("text")
        if not isinstance(text, str):
            return _err(400, "bad_request")

        def work():
            write_text(rel, text)
            return {"path": rel}
        return ctx.side("user.files.write", lambda: _fenced("write", work))

    add(Op("user.files.write", "config", "Write a file under /opt/sa02m-user", u_write,
           {"type": "object", "required": ["path", "text"], "properties": {
               "path": {"type": "string"}, "text": {"type": "string"}}}, mutating=True))

    def u_del(ctx, args):
        rel = str(args.get("path") or "")

        def work():
            return {"path": rel, "existed": delete_path(rel)}
        return ctx.side("user.files.delete", lambda: _fenced("delete", work))

    add(Op("user.files.delete", "config", "Delete a file under /opt/sa02m-user", u_del,
           _PATH_SCHEMA, mutating=True))

    def u_mkdir(ctx, args):
        rel = str(args.get("path") or "")

        def work():
            mkdir(rel)
            return {"path": rel}
        return ctx.side("user.files.mkdir", lambda: _fenced("mkdir", work))

    add(Op("user.files.mkdir", "config", "Create a directory under /opt/sa02m-user", u_mkdir,
           _PATH_SCHEMA, mutating=True))

    def _uunit(action):
        name = "user.units." + action
        def handler(ctx, args):
            unit = str(args.get("name") or "")
            if not UNIT_RE.match(unit):
                return _err(400, "bad_request")
            return ctx.side(name, lambda: _proc(_sudo(ctx, "sa02m-user-unit.sh", [action, unit])))
        add(Op(name, "config", "User unit " + action + " (sa02m-user@<name>, entry point <name>/start.sh)",
               handler, {"type": "object", "required": ["name"],
                         "properties": {"name": {"type": "string"}}}, mutating=True))

    for act in ("install", "start", "stop", "remove"):
        _uunit(act)

    def pip(ctx, args):
        pkg = str(args.get("package") or "")
        if not PIP_RE.match(pkg):
            return _err(400, "bad_request")
        def work():
            venv = os.path.join(USER_ROOT, ".venv")
            py = os.path.join(venv, "bin", "python")
            if not os.path.isfile(py):
                # ensurepip on the A40i takes minutes, not seconds.
                made = ctx.run([os.environ.get("PYTHON") or "python3", "-m", "venv", venv], timeout=600)
                if made.get("code") != 0:
                    return _err(200, "venv", detail=(made.get("stderr") or "")[:500])
            proc = ctx.run([py, "-m", "pip", "install", "--disable-pip-version-check", pkg], timeout=900)
            if proc.get("code") != 0:
                return _ok({"ok": False, "error": "pip", "detail": (proc.get("stderr") or "")[:800]})
            return _ok({"package": pkg})
        return ctx.side("user.pip", work)

    add(Op("user.pip", "config", "pip install into /opt/sa02m-user/.venv", pip, mutating=True, long=True))

    def shell(ctx, args):
        cmd = args.get("cmd")
        if not isinstance(cmd, str) or not cmd or len(cmd) > 4000:
            return _err(400, "bad_request")
        try:
            timeout = int(args.get("timeout") or 15)
        except (TypeError, ValueError):
            return _err(400, "bad_request")
        if timeout < 1 or timeout > 120:
            return _err(400, "bad_request")
        mode = args.get("mode") or "web"
        if mode not in ("web", "root"):
            return _err(400, "bad_request")
        if mode == "root" and not ctx.principal.root_capable:
            return _err(403, "forbidden", need="root_capable")
        def work():
            if mode == "root":
                return ctx.root_exec(ctx.principal.token_id, cmd, timeout)
            return ctx.web_exec(cmd, timeout)
        return ctx.side("shell.exec", work)

    add(Op("shell.exec", "admin", "Shell as www-data, or root when root_capable", shell,
           mutating=True, long=True))

    def files_read(ctx, args):
        if not ctx.principal.root_capable:
            return _err(403, "forbidden", need="root_capable")
        path = args.get("path")
        if not isinstance(path, str) or len(path) > 512 or "\x00" in path:
            return _err(400, "bad_request")
        return ctx.side("files.read", lambda: ctx.root_read(ctx.principal.token_id, path))

    add(Op("files.read", "admin", "Read a regular file outside the sandbox", files_read))

    def files_write(ctx, args):
        if not ctx.principal.root_capable:
            return _err(403, "forbidden", need="root_capable")
        path = args.get("path")
        text = args.get("text")
        if not isinstance(path, str) or not isinstance(text, str):
            return _err(400, "bad_request")
        if len(text.encode("utf-8")) > 256 * 1024:
            return _err(400, "bad_request")
        return ctx.side("files.write", lambda: ctx.root_write(ctx.principal.token_id, path, text))

    add(Op("files.write", "admin", "Write a regular file outside the sandbox", files_write, mutating=True))

    def wu_check(ctx, args):
        return ctx.side("web_update.check", lambda: _cgi(ctx, "web_update_check.cgi", "GET"))

    add(Op("web_update.check", "admin", "Check for a web update", wu_check))

    def wu_apply(ctx, args):
        # confirm_version → file-package transaction; absent → GitHub OTA (legacy).
        ver = args.get("confirm_version")
        if ver is not None and not re.fullmatch(r"[0-9]+(\.[0-9]+){1,3}", str(ver)):
            return _err(400, "bad_request", reason="confirm_version")
        raw = json.dumps({"confirm_version": str(ver)} if ver is not None else {}).encode()
        return ctx.side("web_update.apply", lambda: _cgi(ctx, "web_update_apply.cgi", "POST", "", raw))

    add(Op("web_update.apply", "admin", "Apply a web update", wu_apply,
           mutating=True, confirm="web_update.apply", long=True))

    def wu_up(ctx, args):
        raw_b64 = args.get("file_b64")
        if not isinstance(raw_b64, str):
            return _err(400, "bad_request")
        def work():
            try:
                blob = base64.b64decode(raw_b64, validate=True)
            except ValueError:
                return _err(400, "bad_request")
            if len(blob) > 8 * 1024 * 1024:
                return _err(400, "bad_request")
            boundary = "sa02mUp"
            body = (
                ("--" + boundary + "\r\n"
                 "Content-Disposition: form-data; name=\"file\"; filename=\"package.sa02m\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n").encode()
                + blob + ("\r\n--" + boundary + "--\r\n").encode()
            )
            return _cgi(ctx, "web_update_upload.cgi", "POST", "", body,
                        "multipart/form-data; boundary=" + boundary)
        return ctx.side("web_update.upload", work)

    add(Op("web_update.upload", "admin", "Upload an offline package", wu_up,
           mutating=True, confirm="web_update.upload", long=True))

    def kernel_set(ctx, args):
        profile = str(args.get("profile") or "")
        if profile not in ("smp", "rt"):
            return _err(400, "bad_request")
        raw = json.dumps({"profile": profile}).encode()
        return ctx.side("kernel.set", lambda: _cgi(ctx, "kernel_ctrl.cgi", "POST", "", raw))

    add(Op("kernel.set", "admin", "Select the SMP or RT kernel", kernel_set,
           mutating=True, confirm="kernel.set", long=True))

    def kernel_refresh(ctx, args):
        raw = json.dumps({"action": "refresh"}).encode()
        return ctx.side("kernel.refresh", lambda: _cgi(ctx, "kernel_ctrl.cgi", "POST", "", raw))

    add(Op("kernel.refresh", "admin", "Reinstall the running kernel profile", kernel_refresh,
           mutating=True, confirm="kernel.refresh", long=True))

    def cpu(ctx, args):
        profile = str(args.get("profile") or "")
        if profile not in ("performance", "high", "medium", "low", "adaptive"):
            return _err(400, "bad_request")
        raw = json.dumps({"profile": profile}).encode()
        return ctx.side("cpu.profile", lambda: _cgi(ctx, "cpu_profile.cgi", "POST", "", raw))

    add(Op("cpu.profile", "admin", "Set the CPU profile", cpu, mutating=True))

    def reboot(ctx, args):
        return ctx.side("system.reboot", lambda: _cgi(ctx, "reboot.cgi", "POST", "", b""))

    add(Op("system.reboot", "admin", "Reboot the board", reboot,
           mutating=True, confirm="reboot", long=True))

    def restart(ctx, args):
        return ctx.side("system.restart_services", lambda: _cgi(ctx, "restart.cgi", "POST", "", b""))

    add(Op("system.restart_services", "admin", "Restart panel services", restart, mutating=True))

    def creds(ctx, args):
        body = _form(args, ("current_password", "new_username", "new_password", "new_password_confirm"))
        return ctx.side("web_creds.set", lambda: _cgi(
            ctx, "web_creds.cgi", "POST", "", body, "application/x-www-form-urlencoded"))

    add(Op("web_creds.set", "admin", "Change the panel user and password", creds,
           mutating=True, confirm="web_creds.set"))

    def backup(ctx, args):
        def work():
            # web_backup.cgi streams a tar.gz: binary read, body as base64.
            status, parsed = run_cgi(ctx, "web_backup.cgi", "GET", binary=True)
            return (status if status >= 400 else 200), parsed
        return ctx.side("backup.download", work)

    add(Op("backup.download", "admin", "Download the web backup (tar.gz as body_b64)", backup,
           long=True))

    def factory(ctx, args):
        raw = json.dumps({"confirm_phrase": "SA02M-RESET", "backup_ok": True}).encode()
        return ctx.side("factory_reset", lambda: _cgi(ctx, "web_factory_reset.cgi", "POST", "", raw))

    add(Op("factory_reset", "admin", "Factory-reset configuration", factory,
           mutating=True, confirm="factory_reset", long=True))

    def flash(ctx, args):
        def work():
            sock = os.environ.get("SA02M_FLASHER_SOCK") or "/run/sa02m-flasher/flasher.sock"
            body = json.dumps(args).encode()
            status, raw = _unix(ctx, sock, "POST", "/flash", body)
            return status, {"ok": status < 400, "body": raw}
        return ctx.side("flasher.flash", work)

    add(Op("flasher.flash", "admin", "Flash one module", flash,
           mutating=True, confirm="flasher.flash", long=True))

    def scan(ctx, args):
        def work():
            sock = os.environ.get("SA02M_FLASHER_SOCK") or "/run/sa02m-flasher/flasher.sock"
            body = json.dumps(args).encode()
            status, raw = _unix(ctx, sock, "POST", "/scan", body)
            return status, {"ok": status < 400, "body": raw}
        return ctx.side("flasher.scan", work)

    add(Op("flasher.scan", "admin", "Scan a COM port", scan, mutating=True, long=True))


register_all()
