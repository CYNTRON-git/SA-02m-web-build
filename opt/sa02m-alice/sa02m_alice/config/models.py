"""Validation helpers for Alice rooms/devices JSON."""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ..common import constants as C

# Tile icon allow-list (docs/contracts/alice-mqtt-mapping.md §Device document).
DEVICE_ICONS = frozenset(C.DEVICE_ICONS)

# Widen these deliberately: nothing downstream re-checks them (why —
# docs/contracts/alice-mqtt-mapping.md, §Device document).
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_NAME_RE = re.compile(r"^[\w \-./+]{1,64}$", re.UNICODE)
_MQTT_RE = re.compile(r"^/devices/[A-Za-z0-9_./+-]+$")

# Float-property instance allowlist (docs/contracts/alice-mqtt-mapping.md).
# Wider than the UI's kind list on purpose: hand-edited configs may bind
# illumination/battery/water level or an energy meter; anything else is
# rejected before it reaches Yandex.
FLOAT_INSTANCES = {
    "temperature",
    "humidity",
    "voltage",
    "amperage",
    "power",
    "pressure",
    "co2_level",
    "tvoc",
    "illumination",
    "battery_level",
    "water_level",
    "electricity_meter",
}

# Event-property instances → the values Yandex accepts for each
# (docs/contracts/alice-mqtt-mapping.md). The UI offers only `motion`; the
# rest are hand-edit surface, same as the wider float allowlist.
EVENT_INSTANCES = {
    "motion": frozenset(("detected", "not_detected")),
    "open": frozenset(("opened", "closed")),
    "button": frozenset(("click", "double_click", "long_press")),
    "vibration": frozenset(("tilt", "fall", "vibration")),
    "smoke": frozenset(("detected", "not_detected", "high")),
    "gas": frozenset(("detected", "not_detected", "high")),
    "water_leak": frozenset(("dry", "leak")),
    "battery_level": frozenset(("low", "normal")),
    "food_level": frozenset(("empty", "low", "normal")),
    "water_level": frozenset(("empty", "low", "normal")),
}

# Keeps the unit string forwarded to Yandex shell/JSON-safe by construction.
# The digit class is load-bearing: without it `unit.density.mcg_m3` (tvoc,
# PM densities) was rejected.
_UNIT_RE = re.compile(r"^unit\.[a-z0-9_.]{1,32}$")

# A scale outside this range is a typo, not a unit conversion (the real ones
# in use are 1000 mg→µg and 7.50062 kPa→mmHg). Zero would erase the reading.
_SCALE_MAX = 1e6


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


def _validate_event_item(item: Dict[str, Any]) -> Optional[str]:
    """Event property: instance + a non-empty `events` array of allowed values.

    Yandex requires `parameters.events` for discovery — a device advertising an
    event property without it is refused by the platform, not by us.
    """
    params = item.get("parameters")
    if not isinstance(params, dict):
        return "event property requires parameters"
    instance = str(params.get("instance") or "")
    allowed = EVENT_INSTANCES.get(instance)
    if allowed is None:
        return "invalid event property instance"
    events = params.get("events")
    if not isinstance(events, list) or not events:
        return "invalid event property events"
    seen = set()
    for ev in events:
        if not isinstance(ev, dict):
            return "invalid event property events"
        value = ev.get("value")
        if not isinstance(value, str) or value not in allowed or value in seen:
            return "invalid event property events"
        seen.add(value)
    return None


def _validate_scale(item: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """Optional item-level unit conversion factor. Absent ⇒ None (identity).

    Item level, NOT inside `parameters`: discovery copies `parameters` verbatim
    into the Yandex payload, so a `scale` key there would leak a non-Yandex
    field to the platform.
    """
    if "scale" not in item:
        return None, None
    scale = item.get("scale")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        return None, "invalid property scale"
    value = float(scale)
    if value != value or value in (float("inf"), float("-inf")):
        return None, "invalid property scale"
    if value == 0.0 or abs(value) > _SCALE_MAX:
        return None, "invalid property scale"
    return value, None


def _item_instance(item: Dict[str, Any]) -> str:
    params = item.get("parameters")
    if not isinstance(params, dict):
        return ""
    return str(params.get("instance") or "")


def _validate_mqtt_item(item: Dict[str, Any], kind: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (normalised item, error). The item is copied, never mutated."""
    if not isinstance(item, dict):
        return None, "%s must be object" % kind
    t = str(item.get("type") or "")
    # A capability typed as a property (or vice versa) would be silently
    # discovered wrong — pin the namespace per kind.
    prefix = "devices.capabilities." if kind == "capability" else "devices.properties."
    if not t.startswith(prefix):
        return None, "invalid %s type" % kind
    mqtt = str(item.get("mqtt") or "").strip()
    if not mqtt or not _MQTT_RE.match(mqtt):
        return None, "invalid mqtt topic"
    if t == "devices.properties.float":
        params = item.get("parameters")
        if not isinstance(params, dict):
            return None, "float property requires parameters"
        if str(params.get("instance") or "") not in FLOAT_INSTANCES:
            return None, "invalid float property instance"
        unit = params.get("unit")
        if not isinstance(unit, str) or not _UNIT_RE.match(unit):
            return None, "invalid float property unit"
    elif t == "devices.properties.event":
        err = _validate_event_item(item)
        if err:
            return None, err
    scale, err = _validate_scale(item)
    if err:
        return None, err
    if scale is None:
        return item, None
    out = dict(item)
    out["scale"] = scale
    return out, None


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
    # `alice_visible`: absent ⇒ true (every pre-existing document is unchanged
    # and stays visible). Only the Yandex discovery list reads it — a strict
    # bool so a stray "false" string can never hide a device by accident.
    if "alice_visible" in out:
        if not isinstance(out["alice_visible"], bool):
            return None, "invalid alice_visible"
    # `icon`: optional tile icon for the cloud control page. Empty/None is
    # "unset" and the key is dropped; anything else must be in the allow-list.
    if "icon" in out:
        icon = out["icon"]
        if icon in (None, ""):
            del out["icon"]
        elif not isinstance(icon, str) or icon not in DEVICE_ICONS:
            return None, "invalid icon"
    for key in ("capabilities", "properties"):
        if key in out and out[key] is not None:
            if not isinstance(out[key], list):
                return None, "%s must be a list" % key
            kind = "capability" if key == "capabilities" else "property"
            cleaned = []
            # A Yandex property/capability is addressed by (type, instance) and
            # nothing else — the state wire format carries no per-item id. Two
            # items sharing a pair means the second one is invisible in the app,
            # so refuse the document rather than store a reading nobody sees.
            seen = set()
            for item in out[key]:
                normalised, err = _validate_mqtt_item(item, kind)
                if err:
                    return None, err
                pair = (str(normalised.get("type") or ""), _item_instance(normalised))
                if pair in seen:
                    return None, "duplicate %s instance: %s" % (kind, pair[1])
                seen.add(pair)
                cleaned.append(normalised)
            out[key] = cleaned
        else:
            out.setdefault(key, [])
    return out, None
