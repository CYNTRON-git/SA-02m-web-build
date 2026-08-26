"""MQTT ↔ Yandex Smart Home capability/property converters (clean-room)."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..common import constants as C


def _truthy_mqtt(raw: str) -> bool:
    s = (raw or "").strip().lower()
    return s in ("1", "true", "on", "yes")


def mqtt_to_on_off(raw: str, parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    _ = parameters
    return {
        "type": "devices.capabilities.on_off",
        "state": {"instance": "on", "value": _truthy_mqtt(raw)},
    }


def yandex_to_on_off(state: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Return (mqtt_payload, error_code)."""
    if not isinstance(state, dict):
        return None, C.ERR_INVALID_VALUE
    if state.get("instance", "on") != "on":
        return None, C.ERR_INVALID_ACTION
    value = state.get("value")
    if not isinstance(value, bool):
        return None, C.ERR_INVALID_VALUE
    return ("1" if value else "0"), None


def mqtt_to_range(raw: str, parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = parameters or {}
    instance = params.get("instance", "brightness")
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        value = 0.0
    return {
        "type": "devices.capabilities.range",
        "state": {"instance": instance, "value": value},
    }


def yandex_to_range(
    state: Dict[str, Any],
    current_raw: Optional[str] = None,
    parameters: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Absolute or relative range → MQTT string."""
    if not isinstance(state, dict):
        return None, C.ERR_INVALID_VALUE
    params = parameters or {}
    instance = params.get("instance", "brightness")
    if state.get("instance", instance) != instance:
        return None, C.ERR_INVALID_ACTION
    value = state.get("value")
    relative = bool(state.get("relative"))
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None, C.ERR_INVALID_VALUE
    if relative:
        try:
            base = float(str(current_raw).strip()) if current_raw is not None else 0.0
        except (TypeError, ValueError):
            base = 0.0
        num = base + num
    lo = params.get("range", {}).get("min") if isinstance(params.get("range"), dict) else params.get("min")
    hi = params.get("range", {}).get("max") if isinstance(params.get("range"), dict) else params.get("max")
    if lo is not None:
        try:
            num = max(float(lo), num)
        except (TypeError, ValueError):
            pass
    if hi is not None:
        try:
            num = min(float(hi), num)
        except (TypeError, ValueError):
            pass
    # Prefer int when whole
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num))), None
    return ("%g" % num), None


def mqtt_to_float_property(raw: str, parameters: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    params = parameters or {}
    instance = params.get("instance", "temperature")
    unit = params.get("unit")
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        # An unparseable payload must not fabricate a 0.0 reading (Alice would
        # show 0 °C / 0 W as real). Callers skip falsy blocks — the property
        # is simply omitted from query/state.
        return None
    block: Dict[str, Any] = {
        "type": "devices.properties.float",
        "state": {"instance": instance, "value": value},
    }
    if unit:
        block["parameters"] = {"instance": instance, "unit": unit}
    return block


def mqtt_to_event_property(raw: str, parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = parameters or {}
    instance = params.get("instance", "open")
    value = (raw or "").strip() or params.get("value", "detected")
    return {
        "type": "devices.properties.event",
        "state": {"instance": instance, "value": value},
    }


def mqtt_to_color_setting(raw: str, parameters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = parameters or {}
    instance = params.get("instance", "rgb")
    s = (raw or "").strip()
    if instance == "temperature_k":
        try:
            value: Any = int(float(s))
        except (TypeError, ValueError):
            value = 0
    else:
        # Accept decimal int or #RRGGBB
        if s.startswith("#") and len(s) == 7:
            value = int(s[1:], 16)
        else:
            try:
                value = int(float(s))
            except (TypeError, ValueError):
                value = 0
    return {
        "type": "devices.capabilities.color_setting",
        "state": {"instance": instance, "value": value},
    }


def yandex_to_color_setting(state: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(state, dict):
        return None, C.ERR_INVALID_VALUE
    instance = state.get("instance", "rgb")
    value = state.get("value")
    if instance == "temperature_k":
        try:
            return str(int(value)), None
        except (TypeError, ValueError):
            return None, C.ERR_INVALID_VALUE
    if instance == "rgb":
        try:
            return str(int(value)), None
        except (TypeError, ValueError):
            return None, C.ERR_INVALID_VALUE
    return None, C.ERR_INVALID_ACTION


def capability_mqtt_to_yandex(
    cap_type: str, raw: str, parameters: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    if cap_type.endswith("on_off") or cap_type == "devices.capabilities.on_off":
        return mqtt_to_on_off(raw, parameters)
    if cap_type.endswith("range") or cap_type == "devices.capabilities.range":
        return mqtt_to_range(raw, parameters)
    if cap_type.endswith("color_setting") or cap_type == "devices.capabilities.color_setting":
        return mqtt_to_color_setting(raw, parameters)
    return None


def property_mqtt_to_yandex(
    prop_type: str, raw: str, parameters: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    if prop_type.endswith("float") or prop_type == "devices.properties.float":
        return mqtt_to_float_property(raw, parameters)
    if prop_type.endswith("event") or prop_type == "devices.properties.event":
        return mqtt_to_event_property(raw, parameters)
    return None


def capability_yandex_to_mqtt(
    cap_type: str,
    state: Dict[str, Any],
    *,
    current_raw: Optional[str] = None,
    parameters: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    if cap_type.endswith("on_off") or cap_type == "devices.capabilities.on_off":
        return yandex_to_on_off(state)
    if cap_type.endswith("range") or cap_type == "devices.capabilities.range":
        return yandex_to_range(state, current_raw=current_raw, parameters=parameters)
    if cap_type.endswith("color_setting") or cap_type == "devices.capabilities.color_setting":
        return yandex_to_color_setting(state)
    return None, C.ERR_INVALID_ACTION
