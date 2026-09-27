"""Device document → HAP accessory specs. Pure: no pyhap, no I/O.

The v1 mapping table lives here once (`MAPPING`) and once in
docs/contracts/homekit-bridge.md §Таблица соответствия (the contract's copy of
the table, the skip reasons and the states). Everything the table does not cover
is SKIPPED WITH A NAMED REASON (`SKIP_REASONS`) that the card shows — never
silently dropped (promise P2).

Value conversion across the seam happens in the Alice converters (scale,
inversion, event words — alice-mqtt-mapping.md: "no other code path converts
an on_off value across the seam"). This module only re-expresses the
already-converted Yandex-shaped state in HAP units (bool ↔ 0/1, K → °C,
a range ↔ 0..100) and back.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sa02m_alice.client import converters

from . import constants as C

CAP_ON_OFF = "devices.capabilities.on_off"
CAP_RANGE = "devices.capabilities.range"
PROP_FLOAT = "devices.properties.float"
PROP_EVENT = "devices.properties.event"

UNIT_CELSIUS = "unit.temperature.celsius"
UNIT_KELVIN = "unit.temperature.kelvin"


@dataclass(frozen=True)
class MappingRow:
    """One row of the contract's mapping table (strings are the contract's)."""

    row_id: str
    source: str
    condition: str
    service: str
    characteristics: Tuple[str, ...]


MAPPING: Tuple[MappingRow, ...] = (
    MappingRow("M01", "capabilities.on_off", "type=devices.types.light*", "Lightbulb", ("On",)),
    MappingRow("M02", "capabilities.range:brightness", "with=M01", "Lightbulb", ("Brightness",)),
    MappingRow("M03", "capabilities.on_off", "type=devices.types.socket", "Outlet", ("On", "OutletInUse")),
    MappingRow("M04", "capabilities.on_off", "type=devices.types.openable.valve", "Valve",
               ("Active", "InUse", "ValveType")),
    MappingRow("M05", "capabilities.on_off", "type=devices.types.ventilation.fan", "Fanv2", ("Active",)),
    MappingRow("M06", "capabilities.on_off", "writable=false", "ContactSensor", ("ContactSensorState",)),
    MappingRow("M07", "capabilities.on_off", "type=*", "Switch", ("On",)),
    MappingRow("M08", "properties.float:temperature", "unit=%s|%s" % (UNIT_CELSIUS, UNIT_KELVIN),
               "TemperatureSensor", ("CurrentTemperature",)),
    MappingRow("M09", "properties.float:humidity", "-", "HumiditySensor", ("CurrentRelativeHumidity",)),
    MappingRow("M10", "properties.float:illumination", "-", "LightSensor", ("CurrentAmbientLightLevel",)),
    MappingRow("M11", "properties.event:motion", "-", "MotionSensor", ("MotionDetected",)),
    MappingRow("M12", "properties.event:open", "-", "ContactSensor", ("ContactSensorState",)),
    MappingRow("M13", "properties.event:water_leak", "-", "LeakSensor", ("LeakDetected",)),
    MappingRow("M14", "properties.event:smoke", "-", "SmokeSensor", ("SmokeDetected",)),
)
ROWS: Dict[str, MappingRow] = {row.row_id: row for row in MAPPING}

# Skip reasons (docs/contracts/homekit-bridge.md §Причины пропуска).
SKIP_RANGE_UNSUPPORTED = "range_unsupported"
SKIP_CAPABILITY_UNSUPPORTED = "capability_unsupported"
SKIP_NEEDS_THRESHOLD = "needs_threshold"
SKIP_NO_HOMEKIT_TYPE = "no_homekit_type"
SKIP_UNIT_UNSUPPORTED = "unit_unsupported"
SKIP_NOTHING_MAPPABLE = "nothing_mappable"
SKIP_HIDDEN = "hidden"
SKIP_BRIDGE_FULL = "bridge_full"
SKIP_REASONS = (
    SKIP_RANGE_UNSUPPORTED,
    SKIP_CAPABILITY_UNSUPPORTED,
    SKIP_NEEDS_THRESHOLD,
    SKIP_NO_HOMEKIT_TYPE,
    SKIP_UNIT_UNSUPPORTED,
    SKIP_NOTHING_MAPPABLE,
    SKIP_HIDDEN,
    SKIP_BRIDGE_FULL,
)

# Float instances HomeKit has a sensor for, and the one that would need an
# invented threshold (CarbonDioxideSensor's mandatory "detected" flag).
_FLOAT_ROWS = {"temperature": "M08", "humidity": "M09", "illumination": "M10"}
_FLOAT_NEEDS_THRESHOLD = frozenset(("co2_level",))
_EVENT_ROWS = {"motion": "M11", "open": "M12", "water_leak": "M13", "smoke": "M14"}

# HAP value rules per characteristic binding (docs/contracts §Правило значения).
RULE_BOOL = "bool"
RULE_BOOL_DERIVED = "bool_derived"
RULE_ACTIVE = "active"
RULE_ACTIVE_DERIVED = "active_derived"
RULE_CONST_ZERO = "const_zero"
RULE_CONTACT_FROM_ON_OFF = "contact_from_on_off"
RULE_BRIGHTNESS = "brightness"
RULE_TEMPERATURE = "temperature"
RULE_HUMIDITY = "humidity"
RULE_ILLUMINATION = "illumination"
RULE_EVENT_BOOL = "event_bool"
RULE_EVENT_INT = "event_int"

# HAP minimum for CurrentAmbientLightLevel — 0 lux is not a legal value.
LUX_MIN = 0.0001
TEMPERATURE_MIN = -100.0
TEMPERATURE_MAX = 200.0


@dataclass(frozen=True)
class CharBinding:
    """One HAP characteristic and the document item it reads (and writes)."""

    char: str
    rule: str
    source_type: str
    instance: str
    writable: bool = False
    # Range bounds for RULE_BRIGHTNESS: (min, max, precision); else ().
    bounds: Tuple[float, ...] = ()
    # Document unit for RULE_TEMPERATURE (celsius or kelvin); else "".
    unit: str = ""


@dataclass(frozen=True)
class ServiceSpec:
    row_ids: Tuple[str, ...]
    service: str
    bindings: Tuple[CharBinding, ...]


@dataclass
class AccessorySpec:
    device_id: str
    name: str
    model: str
    services: List[ServiceSpec]
    aid: Optional[int] = None

    def signature(self) -> Tuple[Any, ...]:
        """What iOS caches: aid, name, model, services and characteristics."""
        return (self.aid, self.name, self.model, tuple(self.services))


@dataclass
class Projection:
    accessories: List[AccessorySpec] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)

    def signature(self) -> Tuple[Any, ...]:
        return tuple(acc.signature() for acc in self.accessories)


def sanitize_name(name: Any, fallback: str) -> str:
    """HAP-safe accessory name: no control/format chars, no emoji/symbols, no
    leading/trailing punctuation, single spaces, ≤ NAME_MAX. Cyrillic kept.
    Empty result ⇒ `fallback` (the device id)."""
    text = name if isinstance(name, str) else ""
    kept = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn", "So", "Sk", "Mn", "Me"):
            continue
        if ch.isspace():
            kept.append(" ")
            continue
        kept.append(ch)
    out = " ".join("".join(kept).split())

    def _edge(ch: str) -> bool:
        return unicodedata.category(ch)[0] in ("P", "S") or ch.isspace()

    while out and _edge(out[0]):
        out = out[1:]
    while out and _edge(out[-1]):
        out = out[:-1]
    out = out[: C.NAME_MAX].rstrip()
    while out and _edge(out[-1]):
        out = out[:-1]
    if out:
        return out
    fb = fallback[: C.NAME_MAX] if isinstance(fallback, str) else ""
    return fb or "device"


def _instance(item: Mapping[str, Any], default: str = "") -> str:
    params = item.get("parameters")
    if isinstance(params, dict):
        return str(params.get("instance") or default)
    return default


def _params(item: Mapping[str, Any]) -> Dict[str, Any]:
    params = item.get("parameters")
    return params if isinstance(params, dict) else {}


def _range_bounds(item: Mapping[str, Any]) -> Optional[Tuple[float, float, float]]:
    rng = _params(item).get("range")
    if not isinstance(rng, dict):
        return None
    try:
        lo = float(rng["min"])
        hi = float(rng["max"])
        precision = float(rng.get("precision", 1))
    except (KeyError, TypeError, ValueError):
        return None
    if isinstance(rng.get("min"), bool) or isinstance(rng.get("max"), bool):
        return None
    if hi <= lo or precision <= 0:
        return None
    return (lo, hi, precision)


def _skip(dev_id: str, name: str, item: Optional[str], reason: str) -> Dict[str, Any]:
    return {"device_id": dev_id, "name": name, "item": item, "reason": reason}


def _item_label(item: Mapping[str, Any]) -> str:
    t = str(item.get("type") or "?")
    short = t.replace("devices.", "", 1)
    inst = _instance(item)
    return "%s:%s" % (short, inst) if inst else short


def _on_off_service(device_type: str, item: Mapping[str, Any]) -> ServiceSpec:
    inst = _instance(item, "on") or "on"
    if item.get("writable") is False:
        # A latching DI / report-only output: a sensor, never a switch
        # (apply_actions would refuse the write anyway).
        return ServiceSpec(("M06",), "ContactSensor", (
            CharBinding("ContactSensorState", RULE_CONTACT_FROM_ON_OFF, CAP_ON_OFF, inst),))
    if device_type.startswith("devices.types.light"):
        return ServiceSpec(("M01",), "Lightbulb", (
            CharBinding("On", RULE_BOOL, CAP_ON_OFF, inst, writable=True),))
    if device_type == "devices.types.socket":
        return ServiceSpec(("M03",), "Outlet", (
            CharBinding("On", RULE_BOOL, CAP_ON_OFF, inst, writable=True),
            CharBinding("OutletInUse", RULE_BOOL_DERIVED, CAP_ON_OFF, inst),
        ))
    if device_type == "devices.types.openable.valve":
        return ServiceSpec(("M04",), "Valve", (
            CharBinding("Active", RULE_ACTIVE, CAP_ON_OFF, inst, writable=True),
            CharBinding("InUse", RULE_ACTIVE_DERIVED, CAP_ON_OFF, inst),
            CharBinding("ValveType", RULE_CONST_ZERO, CAP_ON_OFF, inst),
        ))
    if device_type == "devices.types.ventilation.fan":
        return ServiceSpec(("M05",), "Fanv2", (
            CharBinding("Active", RULE_ACTIVE, CAP_ON_OFF, inst, writable=True),))
    return ServiceSpec(("M07",), "Switch", (
        CharBinding("On", RULE_BOOL, CAP_ON_OFF, inst, writable=True),))


def _float_service(item: Mapping[str, Any]) -> Tuple[Optional[ServiceSpec], Optional[str]]:
    inst = _instance(item)
    row = _FLOAT_ROWS.get(inst)
    if row is None:
        if inst in _FLOAT_NEEDS_THRESHOLD:
            return None, SKIP_NEEDS_THRESHOLD
        return None, SKIP_NO_HOMEKIT_TYPE
    if row == "M08":
        unit = str(_params(item).get("unit") or "")
        if unit not in (UNIT_CELSIUS, UNIT_KELVIN):
            return None, SKIP_UNIT_UNSUPPORTED
        return ServiceSpec((row,), "TemperatureSensor", (
            CharBinding("CurrentTemperature", RULE_TEMPERATURE, PROP_FLOAT, inst, unit=unit),)), None
    if row == "M09":
        return ServiceSpec((row,), "HumiditySensor", (
            CharBinding("CurrentRelativeHumidity", RULE_HUMIDITY, PROP_FLOAT, inst),)), None
    return ServiceSpec((row,), "LightSensor", (
        CharBinding("CurrentAmbientLightLevel", RULE_ILLUMINATION, PROP_FLOAT, inst),)), None


def _event_service(item: Mapping[str, Any]) -> Tuple[Optional[ServiceSpec], Optional[str]]:
    inst = _instance(item)
    row = _EVENT_ROWS.get(inst)
    if row is None:
        return None, SKIP_NO_HOMEKIT_TYPE
    spec = ROWS[row]
    rule = RULE_EVENT_BOOL if spec.service == "MotionSensor" else RULE_EVENT_INT
    return ServiceSpec((row,), spec.service, (
        CharBinding(spec.characteristics[0], rule, PROP_EVENT, inst),)), None


def device_services(
    device: Mapping[str, Any],
    caps: Sequence[Mapping[str, Any]],
    props: Sequence[Mapping[str, Any]],
) -> Tuple[List[ServiceSpec], List[Tuple[str, str]]]:
    """(services, [(item_label, skip_reason)]) for one device."""
    device_type = str(device.get("type") or "devices.types.other")
    services: List[ServiceSpec] = []
    skipped: List[Tuple[str, str]] = []
    bulb_index: Optional[int] = None
    brightness: List[Mapping[str, Any]] = []
    for item in caps:
        ctype = str(item.get("type") or "")
        if ctype == CAP_ON_OFF:
            spec = _on_off_service(device_type, item)
            if spec.service == "Lightbulb":
                bulb_index = len(services)
            services.append(spec)
        elif ctype == CAP_RANGE:
            if _instance(item) == "brightness":
                brightness.append(item)
            else:
                skipped.append((_item_label(item), SKIP_RANGE_UNSUPPORTED))
        else:
            skipped.append((_item_label(item), SKIP_CAPABILITY_UNSUPPORTED))
    for item in brightness:
        bounds = _range_bounds(item)
        if bulb_index is None or bounds is None:
            skipped.append((_item_label(item), SKIP_RANGE_UNSUPPORTED))
            continue
        bulb = services[bulb_index]
        services[bulb_index] = ServiceSpec(
            bulb.row_ids + ("M02",),
            bulb.service,
            bulb.bindings + (CharBinding("Brightness", RULE_BRIGHTNESS, CAP_RANGE, "brightness",
                                         writable=True, bounds=bounds),),
        )
    for item in props:
        ptype = str(item.get("type") or "")
        if ptype == PROP_FLOAT:
            spec, reason = _float_service(item)
        elif ptype == PROP_EVENT:
            spec, reason = _event_service(item)
        else:
            spec, reason = None, SKIP_NO_HOMEKIT_TYPE
        if spec is None:
            skipped.append((_item_label(item), reason or SKIP_NO_HOMEKIT_TYPE))
        else:
            services.append(spec)
    return services, skipped


def project(
    catalogue: Iterable[Tuple[str, Mapping[str, Any], Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]],
    aids: Mapping[str, int],
) -> Projection:
    """The accessory set for `catalogue` (DeviceRegistry.catalogue_items()).

    Exposure is opt-in (Q-D): only `homekit_visible: true` is exposed — an
    absent or non-true flag hides the device, so a stale writer that drops
    the field can only hide, never expose. Admission when more than
    MAX_BRIDGED qualify: devices already holding an aid first (by aid), then
    new ones by device id; the rest are `bridge_full`. New accessories carry
    `aid=None` — the caller allocates from the aid store.
    """
    out = Projection()
    candidates: List[AccessorySpec] = []
    for device_id, device, caps, props in catalogue:
        did = str(device_id)
        name = sanitize_name(device.get("name"), did)
        if device.get("homekit_visible") is not True:
            out.skipped.append(_skip(did, name, None, SKIP_HIDDEN))
            continue
        services, item_skips = device_services(device, caps, props)
        for label, reason in item_skips:
            out.skipped.append(_skip(did, name, label, reason))
        if not services:
            out.skipped.append(_skip(did, name, None, SKIP_NOTHING_MAPPABLE))
            continue
        candidates.append(AccessorySpec(
            device_id=did,
            name=name,
            model=str(device.get("type") or "devices.types.other"),
            services=services,
            aid=aids.get(did) if isinstance(aids.get(did), int) else None,
        ))
    known = sorted((c for c in candidates if c.aid is not None), key=lambda c: c.aid)
    new = sorted((c for c in candidates if c.aid is None), key=lambda c: c.device_id)
    admitted = (known + new)[: C.MAX_BRIDGED]
    for spec in (known + new)[C.MAX_BRIDGED:]:
        out.skipped.append(_skip(spec.device_id, spec.name, None, SKIP_BRIDGE_FULL))
    out.accessories = admitted
    return out


# ── Values: Yandex-shaped state ↔ HAP ──────────────────────────────────────

def state_index(entry: Mapping[str, Any]) -> Dict[Tuple[str, str], Any]:
    """`query_devices()` entry → {(type, instance): value}."""
    out: Dict[Tuple[str, str], Any] = {}
    for key in ("capabilities", "properties"):
        for block in entry.get(key) or []:
            if not isinstance(block, dict):
                continue
            state = block.get("state")
            if not isinstance(state, dict):
                continue
            out[(str(block.get("type") or ""), str(state.get("instance") or ""))] = state.get("value")
    return out


def _event_active(instance: str, value: Any) -> Optional[bool]:
    """An event word → active? The pair comes from the Alice converters
    (BOOL_EVENT_VALUES), never retyped: inactive is its first slot, every
    other accepted word (smoke `high`) is active."""
    pair = converters.BOOL_EVENT_VALUES.get(instance)
    if not pair or not isinstance(value, str) or not value:
        return None
    return value != pair[0]


def hap_value(binding: CharBinding, value: Any) -> Any:
    """Yandex-normalised value → the HAP characteristic value, or None when
    there is nothing honest to show (the characteristic keeps its value and
    the accessory's availability says the rest)."""
    rule = binding.rule
    if rule == RULE_CONST_ZERO:
        return 0
    if value is None:
        return None
    if rule in (RULE_BOOL, RULE_BOOL_DERIVED):
        return bool(value) if isinstance(value, bool) else None
    if rule in (RULE_ACTIVE, RULE_ACTIVE_DERIVED):
        return (1 if value else 0) if isinstance(value, bool) else None
    if rule == RULE_CONTACT_FROM_ON_OFF:
        # on (contact closed / input active) → CONTACT_DETECTED (0)
        return (0 if value else 1) if isinstance(value, bool) else None
    if isinstance(value, bool):
        return None
    if rule == RULE_BRIGHTNESS:
        if not isinstance(value, (int, float)) or len(binding.bounds) < 2:
            return None
        lo, hi = binding.bounds[0], binding.bounds[1]
        pct = (float(value) - lo) * 100.0 / (hi - lo)
        return int(round(min(100.0, max(0.0, pct))))
    if rule == RULE_TEMPERATURE:
        if not isinstance(value, (int, float)):
            return None
        celsius = float(value) - 273.15 if binding.unit == UNIT_KELVIN else float(value)
        return round(min(TEMPERATURE_MAX, max(TEMPERATURE_MIN, celsius)), 1)
    if rule == RULE_HUMIDITY:
        return float(value) if isinstance(value, (int, float)) else None
    if rule == RULE_ILLUMINATION:
        if not isinstance(value, (int, float)):
            return None
        return max(LUX_MIN, float(value))
    if rule in (RULE_EVENT_BOOL, RULE_EVENT_INT):
        active = _event_active(binding.instance, value)
        if active is None:
            return None
        if rule == RULE_EVENT_BOOL:
            return active
        return 1 if active else 0
    return None


def yandex_capability(binding: CharBinding, value: Any) -> Optional[Dict[str, Any]]:
    """HAP write → one Yandex capability for DeviceRegistry.apply_actions,
    or None when the characteristic is not writable / the value is unusable."""
    if not binding.writable:
        return None
    if binding.rule == RULE_BOOL:
        if not isinstance(value, (bool, int)):
            return None
        return {"type": CAP_ON_OFF, "state": {"instance": binding.instance, "value": bool(value)}}
    if binding.rule == RULE_ACTIVE:
        if not isinstance(value, (bool, int)):
            return None
        return {"type": CAP_ON_OFF, "state": {"instance": binding.instance, "value": bool(value)}}
    if binding.rule == RULE_BRIGHTNESS:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or len(binding.bounds) < 3:
            return None
        lo, hi, precision = binding.bounds[0], binding.bounds[1], binding.bounds[2]
        pct = min(100.0, max(0.0, float(value)))
        raw = lo + pct * (hi - lo) / 100.0
        steps = round((raw - lo) / precision)
        num = min(hi, max(lo, lo + steps * precision))
        num = round(num, 6)
        if abs(num - round(num)) < 1e-9:
            num = int(round(num))
        return {"type": CAP_RANGE, "state": {"instance": binding.instance, "value": num}}
    return None


def firmware_revision(version: str) -> str:
    """HAP FirmwareRevision is `x[.y[.z]]`: the first three numeric parts of
    the web version (1.0.6.55 → 1.0.6); anything unusable → 1.0.0."""
    parts = re.findall(r"\d+", version or "")[:3]
    return ".".join(parts) if parts else "1.0.0"


def summary(projection: Projection) -> Dict[str, Any]:
    """The card's view of a projection (projection.json body, capped)."""
    return {
        "accessories": [
            {
                "device_id": acc.device_id,
                "name": acc.name,
                "aid": acc.aid,
                "services": [svc.service for svc in acc.services],
            }
            for acc in projection.accessories
        ],
        "skipped": projection.skipped[: C.SKIPPED_CAP],
        "skipped_total": len(projection.skipped),
    }
