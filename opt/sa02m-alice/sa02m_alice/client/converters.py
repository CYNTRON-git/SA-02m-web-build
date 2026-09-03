"""MQTT ↔ Yandex Smart Home capability/property converters (clean-room)."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..common import constants as C


# Boolean-ish MQTT payload → the (false, true) event value of an instance.
# A `None` slot means "this instance has no event for that state" — vibration
# reports the event, never its absence.
BOOL_EVENT_VALUES = {
    "motion": ("not_detected", "detected"),
    "open": ("closed", "opened"),
    "water_leak": ("dry", "leak"),
    "smoke": ("not_detected", "detected"),
    "gas": ("not_detected", "detected"),
    "vibration": (None, "vibration"),
    # Cloud-only (a ventilation unit's alarm flag): the bridge publishes 0/1,
    # the cloud page reads the word. Never sent to Yandex — the item carrying
    # this instance is `cloud_only` and the Yandex profile drops it.
    "alarm": ("normal", "alarm"),
}

# Cloud-only event instances whose value is FREE TEXT, not a closed set: a PLC
# status line («Выключено по тревоге») has no Yandex equivalent to map onto, and
# the cloud renders it verbatim. Anything here is admitted only on a `cloud_only`
# item (config/models.py), so it cannot reach Yandex.
FREE_TEXT_EVENT_INSTANCES = ("unit_status",)


def _truthy_mqtt(raw: str) -> bool:
    """Numeric first, then the word set.

    The Modbus→MQTT bridge publishes scaled registers, so a DTV coil arrives as
    `"1.0"` / `"0.0"` — a word-set-only test read every one of them as false.
    """
    s = (raw or "").strip().lower()
    if not s:
        return False
    try:
        return float(s) != 0.0
    except ValueError:
        return s in ("1", "true", "on", "yes")


def apply_on_off_inversion(value: bool, inverted: bool) -> bool:
    """The ONE home for `inverted` (active-low outputs). Absent ⇒ False.

    The rule: **the bus side holds the electrical value, the Yandex/cloud side
    holds the logical one**, and `inverted` says they are opposites — bus 0 =
    logically on (the SA-02m `alarm_led` output sounds the buzzer at 0).

    It lives here once and only once because the transformation is its own
    inverse: `not` applied twice is identity, so the SAME call converts
    bus→logical (read) and logical→bus (write). The two boundary functions
    below are the only places an on_off value crosses that boundary, and they
    both call THIS function — a change to the rule cannot land on one path and
    miss the other.
    """
    return (not bool(value)) if inverted else bool(value)


def mqtt_to_on_off(
    raw: str, parameters: Optional[Dict[str, Any]] = None, *, inverted: bool = False
) -> Dict[str, Any]:
    """Bus payload → the logical on/off Yandex and the cloud page see."""
    _ = parameters
    return {
        "type": "devices.capabilities.on_off",
        "state": {"instance": "on", "value": apply_on_off_inversion(_truthy_mqtt(raw), inverted)},
    }


def yandex_to_on_off(
    state: Dict[str, Any], *, inverted: bool = False
) -> Tuple[Optional[str], Optional[str]]:
    """Logical on/off → the bus payload. Return (mqtt_payload, error_code)."""
    if not isinstance(state, dict):
        return None, C.ERR_INVALID_VALUE
    if state.get("instance", "on") != "on":
        return None, C.ERR_INVALID_ACTION
    value = state.get("value")
    if not isinstance(value, bool):
        return None, C.ERR_INVALID_VALUE
    return ("1" if apply_on_off_inversion(value, inverted) else "0"), None


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


def mqtt_to_float_property(
    raw: str, parameters: Optional[Dict[str, Any]] = None, scale: float = 1.0
) -> Optional[Dict[str, Any]]:
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
    try:
        factor = float(scale)
    except (TypeError, ValueError):
        factor = 1.0
    if factor != 1.0:
        # The ONE home for reading arithmetic. Rounded because a raw product
        # (101.32 × 7.50062) carries a 16-digit mantissa that is ugly in the
        # app and bloats every device_state payload.
        value = round(value * factor, 3)
    block: Dict[str, Any] = {
        "type": "devices.properties.float",
        "state": {"instance": instance, "value": value},
    }
    if unit:
        block["parameters"] = {"instance": instance, "unit": unit}
    return block


def mqtt_to_event_property(
    raw: str, parameters: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """MQTT payload → a Yandex event value the platform actually accepts.

    Yandex takes only the closed value set of the instance (`detected` /
    `not_detected` for motion, …), so a raw `"1.0"` forwarded verbatim is
    refused. A payload that maps to nothing yields no block at all — the same
    "omit rather than fabricate" rule the float converter follows.
    """
    params = parameters or {}
    instance = str(params.get("instance", "open"))
    s = (raw or "").strip()
    if instance in FREE_TEXT_EVENT_INSTANCES:
        if not s:
            return None
        return {
            "type": "devices.properties.event",
            "state": {"instance": instance, "value": s},
        }
    allowed = []
    for ev in params.get("events") or []:
        if isinstance(ev, dict) and isinstance(ev.get("value"), str):
            allowed.append(ev["value"])
    # A hand-edited binding whose topic already publishes the Yandex word
    # (`"opened"`) keeps working untouched.
    if s and (s in allowed or (not allowed and s in (BOOL_EVENT_VALUES.get(instance) or ()))):
        return {
            "type": "devices.properties.event",
            "state": {"instance": instance, "value": s},
        }
    pair = BOOL_EVENT_VALUES.get(instance)
    if not pair:
        return None
    if not s:
        return None
    try:
        on = float(s) != 0.0
    except ValueError:
        low = s.lower()
        if low in ("1", "true", "on", "yes"):
            on = True
        elif low in ("0", "false", "off", "no"):
            on = False
        else:
            return None
    value = pair[1] if on else pair[0]
    if not value:
        return None
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
    cap_type: str,
    raw: str,
    parameters: Optional[Dict[str, Any]] = None,
    inverted: bool = False,
) -> Optional[Dict[str, Any]]:
    if cap_type.endswith("on_off") or cap_type == "devices.capabilities.on_off":
        return mqtt_to_on_off(raw, parameters, inverted=inverted)
    if cap_type.endswith("range") or cap_type == "devices.capabilities.range":
        return mqtt_to_range(raw, parameters)
    if cap_type.endswith("color_setting") or cap_type == "devices.capabilities.color_setting":
        return mqtt_to_color_setting(raw, parameters)
    return None


def property_mqtt_to_yandex(
    prop_type: str, raw: str, parameters: Optional[Dict[str, Any]] = None, scale: float = 1.0
) -> Optional[Dict[str, Any]]:
    if prop_type.endswith("float") or prop_type == "devices.properties.float":
        return mqtt_to_float_property(raw, parameters, scale)
    if prop_type.endswith("event") or prop_type == "devices.properties.event":
        return mqtt_to_event_property(raw, parameters)
    return None


def capability_yandex_to_mqtt(
    cap_type: str,
    state: Dict[str, Any],
    *,
    current_raw: Optional[str] = None,
    parameters: Optional[Dict[str, Any]] = None,
    inverted: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    if cap_type.endswith("on_off") or cap_type == "devices.capabilities.on_off":
        return yandex_to_on_off(state, inverted=inverted)
    if cap_type.endswith("range") or cap_type == "devices.capabilities.range":
        return yandex_to_range(state, current_raw=current_raw, parameters=parameters)
    if cap_type.endswith("color_setting") or cap_type == "devices.capabilities.color_setting":
        return yandex_to_color_setting(state)
    return None, C.ERR_INVALID_ACTION
