"""Append cloud-only AHU status + Carel extra readings on Carel bindings.

Live documents saved before SH_VENT_ROWS included `plant_state` /
`unit_status` / `alarm` have only `supply_temp`. The cloud tile then
falls back to «Вкл». This helper adds the missing rows; it does not
remint ids, wipe other devices, or invent a plant_state enum from the
PLC text. `unit_status` binds `unit_status_text` (free words), not the
numeric `unit_status` code.

Carel extras the tile/detail list: return water, heat valve, fan
speed / step, pump, alarm_text — declare always (`present:false`
hides an unpublished MQTT control). Outdoor and room are optional
probes: bind them only when `live_controls` says that MQTT control
is configured and has no `/meta/error`. A retained `0.0` with
`error=r` is not a sensor — Alice would report it as a live °C.

Idempotent: a second pass is a no-op. Other devices are untouched.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

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

# instance, MQTT control, unit — always bind (unpublished → present:false)
_FLOAT_ROWS: Tuple[Tuple[str, str, str], ...] = (
    ("return_water_temperature", "return_water_temp", "unit.temperature.celsius"),
    ("heat_valve", "heat_valve", "unit.percent"),
    ("fan_speed", "fan_supply", "unit.percent"),
    ("fan_step", "fan_step", "unit.step"),
)

# Optional analogue probes. Bind only when live_controls names this MQTT
# control (configured, no /meta/error). A retained 0.0 + error=r is not one.
_OPTIONAL_FLOAT_ROWS: Tuple[Tuple[str, str, str], ...] = (
    ("outdoor_temperature", "outdoor_temp", "unit.temperature.celsius"),
    ("room_temperature", "room_temp", "unit.temperature.celsius"),
)
_OPTIONAL_INSTANCES = frozenset(row[0] for row in _OPTIONAL_FLOAT_ROWS)
_OPTIONAL_CONTROL = {row[0]: row[1] for row in _OPTIONAL_FLOAT_ROWS}


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


def _mqtt_id_from_prefix(prefix: str) -> str:
    """`/devices/carel-COM3-1/controls` → `carel-COM3-1`."""
    if not prefix.startswith("/devices/"):
        return ""
    return prefix[len("/devices/"):].split("/", 1)[0]


def _live_ok(
    live_controls: Optional[Dict[str, Set[str]]],
    mqtt_id: str,
    control: str,
) -> bool:
    if not live_controls or not mqtt_id:
        return False
    names = live_controls.get(mqtt_id)
    return bool(names) and control in names


def drop_unfitted_ahu_probes(
    doc: Dict[str, Any],
    live_controls: Dict[str, Set[str]],
) -> bool:
    """Drop outdoor/room rows whose MQTT control is dead or unconfigured."""
    if not isinstance(doc, dict) or not isinstance(live_controls, dict):
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
            continue
        mqtt_id = _mqtt_id_from_prefix(prefix)
        kept: List[Any] = []
        dropped = False
        for item in props:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            inst = str((item.get("parameters") or {}).get("instance") or "")
            if inst not in _OPTIONAL_INSTANCES:
                kept.append(item)
                continue
            if _live_ok(live_controls, mqtt_id, _OPTIONAL_CONTROL[inst]):
                kept.append(item)
                continue
            dropped = True
        if dropped:
            dev["properties"] = kept
            changed = True
    return changed


def ensure_ahu_cloud_status(
    doc: Dict[str, Any],
    live_controls: Optional[Dict[str, Set[str]]] = None,
) -> bool:
    """Append missing status events and Carel extras. True if `doc` changed.

    Outdoor / room are appended only when `live_controls[mqtt_id]` contains
    that MQTT control name. Absent `live_controls` is fail-closed: do not
    add the optional probes.
    """
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
        mqtt_id = _mqtt_id_from_prefix(prefix)
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
        for instance, control, unit in _OPTIONAL_FLOAT_ROWS:
            if instance in have:
                continue
            if not _live_ok(live_controls, mqtt_id, control):
                continue
            props.append(ahu_float_item(prefix, instance, control, unit))
            have.add(instance)
            changed = True
    return changed
