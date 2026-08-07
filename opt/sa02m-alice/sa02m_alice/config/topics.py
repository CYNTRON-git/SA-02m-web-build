"""MQTT topic inventory for the Alice device picker (offline-capable)."""

from __future__ import annotations

import json
import os
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
    # Always include onboard SA-02m examples so the picker is never empty on a fresh board
    builtins = [
        "/devices/sa02m-SA-02/controls/do",
        "/devices/sa02m-SA-02/controls/beeper",
        "/devices/sa02m-SA-02/controls/alarm_led",
        "/devices/sa02m-SA-02/controls/temp_c",
    ]
    merged = sorted(set(topics) | set(builtins))
    return {
        "ok": True,
        "source": source,
        "topics": merged,
        "count": len(merged),
    }
