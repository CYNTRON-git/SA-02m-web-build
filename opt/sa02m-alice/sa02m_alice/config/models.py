"""Validation helpers for Alice rooms/devices JSON."""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_NAME_RE = re.compile(r"^[\w \-./+]{1,64}$", re.UNICODE)
_MQTT_RE = re.compile(r"^/devices/[A-Za-z0-9_./+-]+$")

# Float-property instance allowlist (docs/contracts/alice-mqtt-mapping.md).
# Wider than the UI's six kinds on purpose: hand-edited configs may bind
# pressure/co2/battery; anything else is rejected before it reaches Yandex.
FLOAT_INSTANCES = {
    "temperature",
    "humidity",
    "voltage",
    "amperage",
    "power",
    "pressure",
    "co2_level",
    "battery_level",
}
# Keeps the unit string forwarded to Yandex shell/JSON-safe by construction.
_UNIT_RE = re.compile(r"^unit\.[a-z_.]{1,32}$")


def new_id() -> str:
    return str(uuid.uuid4())


def validate_room(room: Dict[str, Any], *, partial: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(room, dict):
        return None, "room must be an object"
    out = dict(room)
    if not partial or "id" in out:
        rid = str(out.get("id") or new_id())
        if not _ID_RE.match(rid):
            return None, "invalid room id"
        out["id"] = rid
    if not partial or "name" in out:
        name = str(out.get("name") or "").strip()
        if not name or not _NAME_RE.match(name):
            return None, "invalid room name"
        out["name"] = name
    if "devices" in out and out["devices"] is not None:
        if not isinstance(out["devices"], list):
            return None, "room.devices must be a list"
        cleaned = []
        for d in out["devices"]:
            s = str(d)
            if not _ID_RE.match(s):
                return None, "invalid device id in room"
            cleaned.append(s)
        out["devices"] = cleaned
    else:
        out.setdefault("devices", [])
    return out, None


def _validate_mqtt_item(item: Dict[str, Any], kind: str) -> Optional[str]:
    if not isinstance(item, dict):
        return "%s must be object" % kind
    t = str(item.get("type") or "")
    # A capability typed as a property (or vice versa) would be silently
    # discovered wrong — pin the namespace per kind.
    prefix = "devices.capabilities." if kind == "capability" else "devices.properties."
    if not t.startswith(prefix):
        return "invalid %s type" % kind
    mqtt = str(item.get("mqtt") or "").strip()
    if not mqtt or not _MQTT_RE.match(mqtt):
        return "invalid mqtt topic"
    if t == "devices.properties.float":
        params = item.get("parameters")
        if not isinstance(params, dict):
            return "float property requires parameters"
        if str(params.get("instance") or "") not in FLOAT_INSTANCES:
            return "invalid float property instance"
        unit = params.get("unit")
        if not isinstance(unit, str) or not _UNIT_RE.match(unit):
            return "invalid float property unit"
    return None


def validate_device(dev: Dict[str, Any], *, partial: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(dev, dict):
        return None, "device must be an object"
    out = dict(dev)
    if not partial or "id" in out:
        did = str(out.get("id") or new_id())
        if not _ID_RE.match(did):
            return None, "invalid device id"
        out["id"] = did
    if not partial or "name" in out:
        name = str(out.get("name") or "").strip()
        if not name or not _NAME_RE.match(name):
            return None, "invalid device name"
        out["name"] = name
    if "type" in out or not partial:
        dtype = str(out.get("type") or "devices.types.other")
        if not dtype.startswith("devices.types."):
            return None, "invalid device type"
        out["type"] = dtype
    if "room_id" in out and out["room_id"] not in (None, ""):
        if not _ID_RE.match(str(out["room_id"])):
            return None, "invalid room_id"
        out["room_id"] = str(out["room_id"])
    for key in ("capabilities", "properties"):
        if key in out and out[key] is not None:
            if not isinstance(out[key], list):
                return None, "%s must be a list" % key
            kind = "capability" if key == "capabilities" else "property"
            for item in out[key]:
                err = _validate_mqtt_item(item, kind)
                if err:
                    return None, err
        else:
            out.setdefault(key, [])
    return out, None
