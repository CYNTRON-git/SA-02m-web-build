"""MQTT topic inventory for the Alice device picker (offline-capable)."""

from __future__ import annotations

import json
import os
import re
import socket
from typing import Any, Dict, List, Set

YAML_CANDIDATES = (
    "/etc/sa02m-modbus-mqtt.yaml",
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "opt",
        "sa02m-modbus-mqtt",
        "sa02m-modbus-mqtt.yaml",
    ),
)
ROSTER_CANDIDATES = (
    "/run/sa02m-rs485-roster.json",
    "/var/cache/sa02m-flasher/last_scan.json",
)

# Control names for bridge yaml devices that carry no controls/Channels key
# (type: dtv / type: ce02m3 entries describe polling, not channels).
# Conscious duplication: the topic canon is docs/MQTT_TOPICS.md (§cyntron-dtv,
# §CE-02m-3) — the alice package cannot import the bridge package at runtime.
# A stale name here costs only a missing picker entry (a bound topic stays
# selectable on edit regardless).
DTV_DEFAULT_CONTROLS = (
    # Mirrors /etc/sa02m-device-templates/dtv-sensor.json `sensors_present`.
    "temp_ds18b20",
    "temp_bme680",
    "humidity_bme680",
    "pressure_bme680_kpa",
    "iaq_bme680",
    "eco2_bme680",
    "tvoc_zmod",
    "presence",
    "moving_distance",
    "still_distance",
    "detect_distance",
)
DTV_ACTUATOR_CONTROLS = ("buzzer", "leds")

# MR-02m channel kinds the bridge publishes as `<kind>_<ch>` controls, plus the
# per-module diagnostics every module carries (docs/MQTT_TOPICS.md is the home
# of the naming). `ai` on a 12AI module carries live sensor readings — bench
# 1.135 has temperature probes on ai_7..ai_12.
MR02M_CHANNEL_KINDS = ("ai", "ao", "di", "do")
MR02M_DIAG_CONTROLS = ("mcu_temp",)
CE02M3_CONTROLS = (
    "voltage_a",
    "voltage_b",
    "voltage_c",
    "voltage_ab",
    "voltage_bc",
    "voltage_ca",
    "current_a",
    "current_b",
    "current_c",
    "current_n",
    "power_a",
    "power_b",
    "power_c",
    "power_total",
    "frequency",
    "pf_a",
    "pf_b",
    "pf_c",
    "pf_total",
    "asic_temp",
)


# The controller's own device id — the board name (docs/MQTT_TOPICS.md
# §Схема Device ID). Conscious duplication of sa02m_telemetry.py's resolution:
# the alice package cannot import the bridge package at runtime, the same
# constraint as DTV_DEFAULT_CONTROLS above. Unlike that list, a drift here is
# NOT cheap — a FROZEN literal here is what let «Пищалка контроллера» be bound
# to a topic no board ever served — so the two derivations are pinned equal by
# the `telemetry-device-id-contract` quality row, not by good intentions.
CONTROLLER_ID_ENV = "SA02M_TELEMETRY_DEVICE_ID"
CONTROLLER_ID_CONF = os.environ.get(
    "SA02M_TELEMETRY_CONF", "/etc/sa02m_telemetry.conf"
)
CONTROLLER_ID_FALLBACK = "SA-02m"
CONTROLLER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
# The board's own onboard controls: always present, so the picker is never
# empty on a fresh board before any bridge device is configured.
CONTROLLER_CONTROLS = ("do", "beeper", "alarm_led", "temp_c")


def _controller_conf_value(path: str, key: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    found = ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, raw = line.partition("=")
        if not sep or name.strip() != key:
            continue
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0]:
            raw = raw[1:-1]
        found = raw.strip()
    return found


def _controller_hostname() -> str:
    try:
        return (socket.gethostname() or "").strip()
    except Exception:
        return ""


def _controller_device_id() -> str:
    """env → conf → hostname → fallback, first VALID wins (fail closed)."""
    for raw in (
        os.environ.get(CONTROLLER_ID_ENV, ""),
        _controller_conf_value(CONTROLLER_ID_CONF, CONTROLLER_ID_ENV),
        _controller_hostname(),
    ):
        value = (raw or "").strip()
        if value and CONTROLLER_ID_RE.match(value):
            return value
    return CONTROLLER_ID_FALLBACK


def _load_yaml(path: str) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except OSError:
        return None


def _topics_from_yaml(doc: Any) -> List[str]:
    out: Set[str] = set()
    if not isinstance(doc, dict):
        return []
    devices = doc.get("devices") or doc.get("Devices") or []
    if isinstance(devices, dict):
        devices = [
            {"id": k, **(v if isinstance(v, dict) else {})} for k, v in devices.items()
        ]
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        did = str(dev.get("id") or dev.get("name") or "").strip()
        if not did:
            continue
        controls = dev.get("controls") or dev.get("Channels") or []
        if isinstance(controls, dict):
            for cname in controls.keys():
                out.add("/devices/%s/controls/%s" % (did, cname))
        elif isinstance(controls, list):
            for c in controls:
                if isinstance(c, str):
                    out.add("/devices/%s/controls/%s" % (did, c))
                elif isinstance(c, dict):
                    cname = c.get("name") or c.get("id")
                    if cname:
                        out.add("/devices/%s/controls/%s" % (did, cname))
        # dtv/ce02m3 yaml entries carry no controls key — expand by type.
        dtype = str(dev.get("type") or "").strip().lower()
        if dtype == "dtv":
            sensors = dev.get("sensors_present")
            if isinstance(sensors, list):
                names = [s for s in sensors if isinstance(s, str) and s]
            else:
                names = list(DTV_DEFAULT_CONTROLS)
            for cname in names + list(DTV_ACTUATOR_CONTROLS):
                out.add("/devices/%s/controls/%s" % (did, cname))
        elif dtype == "ce02m3":
            for cname in CE02M3_CONTROLS:
                out.add("/devices/%s/controls/%s" % (did, cname))
        elif dtype == "mr02m" and not controls:
            # MR-02m modules publish per-channel controls named <kind>_<ch>
            # (the bridge's own naming — docs/MQTT_TOPICS.md). The analog
            # inputs of a 12AI carry real sensor readings (temperature
            # probes on the bench), so the picker must offer them; digital
            # kinds are offered too — a DO channel is a valid switch binding.
            # Only when the entry has no explicit `controls` list: an explicit
            # list is the author's own inventory and is never second-guessed.
            channels = dev.get("channels")
            if isinstance(channels, dict):
                for kind, chans in channels.items():
                    kname = str(kind).strip().lower()
                    if kname not in MR02M_CHANNEL_KINDS or not isinstance(chans, list):
                        continue
                    for c in chans:
                        if isinstance(c, dict):
                            num = c.get("ch")
                            if c.get("enabled") is False or num is None:
                                continue
                            out.add("/devices/%s/controls/%s_%s" % (did, kname, num))
            for cname in MR02M_DIAG_CONTROLS:
                out.add("/devices/%s/controls/%s" % (did, cname))
        # Fallback: device id alone is not enough — skip
    return sorted(out)


def _topics_from_roster(path: str) -> List[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    out: Set[str] = set()
    modules = data.get("modules") or data.get("devices") or []
    if isinstance(modules, dict):
        modules = list(modules.values())
    for m in modules:
        if not isinstance(m, dict):
            continue
        # Prefer mqtt device id if present
        mid = m.get("mqtt_id") or m.get("device_id") or m.get("id")
        if not mid:
            port = m.get("port") or m.get("com") or "COM?"
            addr = m.get("address") or m.get("addr")
            family = m.get("family") or m.get("type") or "mod"
            if addr is not None:
                mid = "%s-%s-%s" % (family, port, addr)
        if not mid:
            continue
        for cname in m.get("controls") or []:
            if isinstance(cname, str):
                out.add("/devices/%s/controls/%s" % (mid, cname))
        # Common DO/DI guess when roster has channel counts
        for n in range(1, int(m.get("do_count") or 0) + 1):
            out.add("/devices/%s/controls/do_%d" % (mid, n))
        for n in range(1, int(m.get("di_count") or 0) + 1):
            out.add("/devices/%s/controls/di_%d" % (mid, n))
    return sorted(out)


def list_mqtt_topics() -> Dict[str, Any]:
    """Return inventory; works fully offline (no gateway)."""
    topics: List[str] = []
    source = None
    for path in YAML_CANDIDATES:
        ap = os.path.abspath(path)
        if os.path.isfile(ap):
            doc = _load_yaml(ap)
            topics = _topics_from_yaml(doc)
            if topics:
                source = ap
                break
    if not topics:
        for path in ROSTER_CANDIDATES:
            if os.path.isfile(path):
                topics = _topics_from_roster(path)
                if topics:
                    source = path
                    break
    # Always include the onboard controls so the picker is never empty on a
    # fresh board — DERIVED from the live id, so every offered topic is one the
    # board actually serves.
    controller = _controller_device_id()
    builtins = [
        "/devices/%s/controls/%s" % (controller, name)
        for name in CONTROLLER_CONTROLS
    ]
    merged = sorted(set(topics) | set(builtins))
    return {
        "ok": True,
        "source": source,
        "topics": merged,
        "count": len(merged),
    }
