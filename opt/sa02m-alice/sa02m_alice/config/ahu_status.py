"""Append cloud-only AHU status + Carel extra readings on Carel bindings.

Live documents saved before SH_VENT_ROWS included `plant_state` /
`unit_status` / `alarm` have only `supply_temp`. The cloud tile then
falls back to «Вкл». This helper adds the missing rows; it does not
remint ids, wipe other devices, or invent a plant_state enum from the
PLC text. `unit_status` binds `unit_status_text` (free words), not the
numeric `unit_status` code.

Carel extras the tile/detail list (declare always; `present:false`
hides an unpublished MQTT control): return water, heat valve, fan
speed / step, outdoor, room, pump, alarm_text. Bind outdoor/room
even when the last MQTT value was 0.0 — the control existing is
enough; the cloud tile still hides `present:false`.

Idempotent: a second pass is a no-op. Other devices are untouched.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

_CAREL_PREFIX = "/devices/carel-"

# instance, MQTT control, events (None = free text, no Yandex value list)
_STATUS_ROWS: Tuple[Tuple[str, str, Optional[List[Dict[str, str]]]], ...] = (
    ("plant_state", "plant_state",
     [{"value": "run"}, {"value": "stop"}, {"value": "alarm"}]),
    ("unit_status", "unit_status_text", None),
    ("alarm", "alarm",
     [{"value": "alarm"}, {"value": "normal"}]),
    ("pump", "pump",
     [{"value": "on"}, {"value": "off"}]),
    ("alarm_text", "alarm_text", None),
)

# instance, MQTT control, unit
_FLOAT_ROWS: Tuple[Tuple[str, str, str], ...] = (
    ("return_water_temperature", "return_water_temp", "unit.temperature.celsius"),
    ("heat_valve", "heat_valve", "unit.percent"),
    ("fan_speed", "fan_supply", "unit.percent"),
    ("fan_step", "fan_step", "unit.step"),
    ("outdoor_temperature", "outdoor_temp", "unit.temperature.celsius"),
    ("room_temperature", "room_temp", "unit.temperature.celsius"),
)


def _carel_controls_prefix(dev: Dict[str, Any]) -> Optional[str]:
    """`/devices/carel-COM3-N/controls` from any binding, else None."""
    for key in ("capabilities", "properties"):
        items = dev.get(key) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            mqtt = str(item.get("mqtt") or "")
            if mqtt.startswith(_CAREL_PREFIX) and "/controls/" in mqtt:
                return mqtt.rsplit("/controls/", 1)[0] + "/controls"
    return None


def _instances(dev: Dict[str, Any]) -> set:
    out = set()
    for item in dev.get("properties") or []:
        if not isinstance(item, dict):
            continue
        params = item.get("parameters")
        if isinstance(params, dict) and params.get("instance"):
            out.add(str(params["instance"]))
    return out


def ahu_status_item(
    prefix: str,
    instance: str,
    control: str,
    events: Optional[List[Dict[str, str]]],
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"instance": instance}
    if events is not None:
        params["events"] = list(events)
    return {
        "type": "devices.properties.event",
        "mqtt": "%s/%s" % (prefix, control),
        "cloud_only": True,
        "retrievable": True,
        "reportable": True,
        "parameters": params,
    }


def ahu_float_item(prefix: str, instance: str, control: str, unit: str) -> Dict[str, Any]:
    return {
        "type": "devices.properties.float",
        "mqtt": "%s/%s" % (prefix, control),
        "cloud_only": True,
        "retrievable": True,
        "reportable": True,
        "parameters": {"instance": instance, "unit": unit},
    }


def ensure_ahu_cloud_status(doc: Dict[str, Any]) -> bool:
    """Append missing status events and Carel extras. True if `doc` changed."""
    if not isinstance(doc, dict):
        return False
    devices = doc.get("devices")
    if not isinstance(devices, list):
        return False
    changed = False
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        prefix = _carel_controls_prefix(dev)
        if not prefix:
            continue
        props = dev.get("properties")
        if not isinstance(props, list):
            props = []
            dev["properties"] = props
        have = _instances(dev)
        for instance, control, events in _STATUS_ROWS:
            if instance in have:
                continue
            props.append(ahu_status_item(prefix, instance, control, events))
            have.add(instance)
            changed = True
        for instance, control, unit in _FLOAT_ROWS:
            if instance in have:
                continue
            props.append(ahu_float_item(prefix, instance, control, unit))
            have.add(instance)
            changed = True
    return changed
