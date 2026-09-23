"""Cloud-only AHU status + Carel extra readings on Carel bindings.

Caller: `client/device_registry.py` — `DeviceRegistry.__init__` / `reload`
run `prepare_catalogue_doc` on every catalogue build, IN MEMORY ONLY. The
stored document (`/etc/sa02m-alice/sa02m-alice-devices.conf`) is never
rewritten by this module: what the operator saved stays what the operator
saved; what the cloud sees is the saved rows plus the ones below.

Live documents saved before SH_VENT_ROWS included `plant_state` /
`unit_status` / `alarm` have only `supply_temp`. The cloud tile then
falls back to «Вкл». This helper adds the missing rows; it does not
remint ids, wipe other devices, or invent a plant_state enum from the
PLC text. `unit_status` binds `unit_status_text` (free words), not the
numeric `unit_status` code.

Carel extras the tile/detail list: return water, heat valve, pump,
alarm_text — declared for every unit (`present:false` hides an
unpublished MQTT control). THE FAN ROW FOLLOWS THE FAMILY: a
c.pCOmini declares `fan_speed` (percent, HR53), a uAria declares
`fan_step` (steps 1..10, HR197), and neither declares the other's —
a device cannot report a register its controller does not have.
Beside it goes ONE `devices.capabilities.mode` / `fan_speed` with the
four shared names, the only item here that reaches Yandex rather than
the cloud page. The names and the values behind them are NOT restated
here — they come from `sa02m_carel.carel_fan`, whose ladder is uneven
on purpose; customer-facing home of both: docs/contracts/carel-ahu.md
§6.

Outdoor and room are optional probes: bind them only when
`live_controls` says that MQTT control is configured and has no
`/meta/error`. A retained `0.0` with `error=r` is not a sensor — Alice
would report it as a live °C. `live_controls` comes from the bridge's
live cache (`/run/sa02m-modbus-mqtt/<mqtt_id>.json`: `controls` minus
the names in `errors`) — the same file the «Устройства» tab reads, so
the two answer the same about one input (docs/contracts/carel-ahu.md
§5). A device with NO cache file is unknown, not dead: nothing optional
is added, nothing hand-bound is dropped, BOTH fan rows stay (today's
declaration, bit for bit) and the fan control is withheld — the bridge
may simply not be up yet at boot, and only the live control must never
be wrong.

Idempotent: a second pass is a no-op. Other devices are untouched.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..common import carel_import
from . import inventory

_CAREL_PREFIX = "/devices/carel-"

# The two family words. They are `sa02m_carel.controls.FAMILY_*`, restated
# here because the Alice tree must keep narrowing the rows even on a board
# where that package is not installed (the import below is fail-soft). The
# parity that matters — which control decides the family — is pinned against
# the devices tree in tests/test_ahu_status.py.
FAMILY_CRST = "crst"
FAMILY_UARIA = "uaria"

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

# instance, MQTT control, unit — every family has these (unpublished →
# present:false)
_COMMON_FLOAT_ROWS: Tuple[Tuple[str, str, str], ...] = (
    ("return_water_temperature", "return_water_temp", "unit.temperature.celsius"),
    ("heat_valve", "heat_valve", "unit.percent"),
)

# The fan reading, one row per family. Both keep `cloud_only: True`, and that
# flag does not move: neither `fan_speed` nor `fan_step` is a valid Yandex
# float instance, so sending either would put an unknown instance into a
# Discovery response. What that costs, and how well that is sourced, has one
# home: docs/contracts/carel-ahu.md §6. The flag is free either way.
_FAN_ROW_BY_FAMILY: Dict[str, Tuple[str, str, str]] = {
    FAMILY_CRST: ("fan_speed", "fan_supply", "unit.percent"),
    FAMILY_UARIA: ("fan_step", "fan_step", "unit.step"),
}

# Unknown family ⇒ both, in the order they have always been appended.
_ALL_FAN_ROWS: Tuple[Tuple[str, str, str], ...] = (
    _FAN_ROW_BY_FAMILY[FAMILY_CRST],
    _FAN_ROW_BY_FAMILY[FAMILY_UARIA],
)

# The cloud fan control. `mode`, not `range`: a few named positions on both
# families, no slider, and the same words on each. How many and which is
# `carel_fan`'s to say, never this module's.
_FAN_MODE_INSTANCE = "fan_speed"

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


def _capability_instances(dev: Dict[str, Any]) -> set:
    """Instances already declared on the CAPABILITIES side.

    `_instances` reads properties only — that is its job, the float/event
    rows live there. Without this second scan a re-run of the catalogue build
    would append a duplicate mode capability, and `validate_device` refuses a
    duplicate (type, instance) pair: the whole document would stop loading.
    """
    out = set()
    for item in dev.get("capabilities") or []:
        if not isinstance(item, dict):
            continue
        params = item.get("parameters")
        if isinstance(params, dict) and params.get("instance"):
            out.add(str(params["instance"]))
    return out


def fan_mode_item(prefix: str, family: str, control: str, modes) -> Dict[str, Any]:
    """The Yandex fan-speed control for one Carel family.

    No `cloud_only`: unlike every other row this module adds, this one MUST
    reach Yandex — it is the control, not a reading. `carel_family` sits at
    item level beside `mqtt`, never inside `parameters`: discovery copies
    `parameters` verbatim to Yandex, so a local field there would leak (the
    `scale` / `inverted` precedent, client/device_registry.py).
    """
    return {
        "type": "devices.capabilities.mode",
        "mqtt": "%s/%s" % (prefix, control),
        "carel_family": family,
        "retrievable": True,
        "reportable": True,
        "parameters": {
            "instance": _FAN_MODE_INSTANCE,
            # Order preserved from `carel_fan.MODES` on purpose: a platform
            # constraint (the same order on every repeated Discovery for a
            # device). Source, and the pin on this EMITTED array:
            # tests/test_ahu_status.py TestTheModeOrderIsStableAcrossBuilds.
            "modes": [{"value": mode} for mode in modes],
        },
    }


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
    """Drop outdoor/room rows whose MQTT control is dead or unconfigured.

    A device absent from `live_controls` (no live-cache file) is left alone:
    unknown is not dead, and a hand-bound probe must survive a bridge that
    has not flushed its cache yet. A present entry decides both ways — a
    name missing from the set (unconfigured, or flagged in `errors`) drops.
    """
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
        if mqtt_id not in live_controls:
            continue
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


def _fan_rows_for(family: Optional[str]) -> Tuple[Tuple[str, str, str], ...]:
    row = _FAN_ROW_BY_FAMILY.get(family or "")
    return (row,) if row else _ALL_FAN_ROWS


def _ensure_fan_mode_capability(
    dev: Dict[str, Any], prefix: str, family: Optional[str]
) -> bool:
    """Attach the fan-speed control, once, when the family is known.

    Withheld — not guessed — on an unknown family and on a board without the
    shared Carel package: a control that writes the wrong register is worse
    than no control, and the degradation is one WARNING in the journal.
    """
    if not family:
        return False
    carel_fan = carel_import.carel_fan()
    if carel_fan is None:
        return False
    control = carel_fan.mqtt_control(family)
    modes = carel_fan.modes_for(family)
    if not control or not modes:
        return False
    if _FAN_MODE_INSTANCE in _capability_instances(dev):
        return False
    caps = dev.get("capabilities")
    if not isinstance(caps, list):
        caps = []
        dev["capabilities"] = caps
    caps.append(fan_mode_item(prefix, family, control, modes))
    return True


def _narrow_setpoint_range(
    dev: Dict[str, Any], prefix: str, family: Optional[str]
) -> bool:
    """Clamp the setpoint binding's declared range to the family's own.

    The window writes 0..99 for every Carel; a uAria accepts 0..50
    (`sa02m_carel.controls.SETPOINT_RANGE`, the clamps carel_ahu applies —
    docs/contracts/carel-ahu.md §6). Narrow only: the result is the
    intersection, so a narrower stored range is never widened. Unknown family
    or no shared package → untouched. `doc` here is the catalogue's in-memory
    copy (`prepare_catalogue_doc`); the stored document is never rewritten.
    """
    if not family:
        return False
    controls = carel_import.carel_controls()
    bounds = getattr(controls, "SETPOINT_RANGE", {}).get(family) if controls else None
    if not bounds:
        return False
    precision = getattr(controls, "SETPOINT_PRECISION", None)
    changed = False
    for item in dev.get("capabilities") or []:
        if not isinstance(item, dict) or item.get("mqtt") != prefix + "/setpoint":
            continue
        params = item.get("parameters")
        rng = params.get("range") if isinstance(params, dict) else None
        if not isinstance(rng, dict):
            continue
        try:
            lo, hi = float(rng["min"]), float(rng["max"])
        except (KeyError, TypeError, ValueError):
            continue
        new_lo, new_hi = max(lo, float(bounds[0])), min(hi, float(bounds[1]))
        if (new_lo, new_hi) == (lo, hi) or new_lo > new_hi:
            continue
        rng["min"], rng["max"] = new_lo, new_hi
        if precision is not None:
            rng["precision"] = precision
        changed = True
    return changed


def ensure_ahu_cloud_status(
    doc: Dict[str, Any],
    live_controls: Optional[Dict[str, Set[str]]] = None,
    families: Optional[Dict[str, Optional[str]]] = None,
) -> bool:
    """Append missing status events and Carel extras. True if `doc` changed.

    Outdoor / room are appended only when `live_controls[mqtt_id]` contains
    that MQTT control name. Absent `live_controls` is fail-closed: do not
    add the optional probes.

    `families` maps an mqtt id to `"crst"` / `"uaria"` / None (`carel_family`
    over the raw control key set). A device missing from it, or mapped to
    None, is unknown: both fan rows are declared exactly as before and no fan
    control is attached.
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
        family = (families or {}).get(mqtt_id)
        for instance, control, unit in _COMMON_FLOAT_ROWS + _fan_rows_for(family):
            if instance in have:
                continue
            props.append(ahu_float_item(prefix, instance, control, unit))
            have.add(instance)
            changed = True
        if _ensure_fan_mode_capability(dev, prefix, family):
            changed = True
        if _narrow_setpoint_range(dev, prefix, family):
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


def carel_mqtt_ids(doc: Dict[str, Any]) -> List[str]:
    """Every distinct `carel-COM<n>-<addr>` the document binds, in order."""
    out: List[str] = []
    devices = doc.get("devices") if isinstance(doc, dict) else None
    for dev in devices if isinstance(devices, list) else []:
        if not isinstance(dev, dict):
            continue
        prefix = _carel_controls_prefix(dev)
        mid = _mqtt_id_from_prefix(prefix) if prefix else ""
        if mid and mid not in out:
            out.append(mid)
    return out


def carel_family(control_names: Iterable[str]) -> Optional[str]:
    """`"uaria"` | `"crst"` | None, from the RAW control key set.

    The bridge's own resolved family never leaves the poller — the live-cache
    file carries no `family` key — so the catalogue infers it the same way the
    «Устройства» tab does: by which fan control the controller publishes
    (`sa02m_devices.stand_devices._carel_family`, pinned by a parity test
    because the two trees cannot import each other).

    RAW, not live: `live_controls_from_cache` subtracts the names carrying a
    read error, and a transient `r` on `fan_step` would re-read a uAria as a
    c.pCOmini and flip its declared rows.

    Unknown stays None rather than guessing: the tab must name a family to
    draw a card at all, a declaration is under no such pressure.
    """
    names = {str(n) for n in (control_names or ())}
    if "fan_step" in names:
        return FAMILY_UARIA
    if "fan_supply" in names or "sys_mode" in names:
        return FAMILY_CRST
    return None


def carel_cache_snapshot(
    mqtt_ids: Iterable[str],
    cache_dir: Optional[str] = None,
) -> Dict[str, Dict[str, Set[str]]]:
    """`{mqtt_id: {"all": {control, …}, "live": {control, …}}}` — ONE read.

    `all` is every configured control name; `live` is `all` minus the names
    carrying a non-empty `errors[name]`. Both answers come from the same file
    read: the family needs `all`, the optional probes need `live`, and a
    second reader would double the per-device I/O of every catalogue build.

    A device whose cache file is absent or unreadable gets NO key (unknown);
    a present file with no usable `controls` gets empty sets (nothing live).
    One small JSON read per Carel device per catalogue build — a document
    reload, not a poll path.
    """
    out: Dict[str, Dict[str, Set[str]]] = {}
    directory = cache_dir or inventory._live_dir()
    for mid in mqtt_ids:
        if not mid:
            continue
        data = inventory._read_json(os.path.join(directory, "%s.json" % mid))
        if not isinstance(data, dict):
            continue
        controls = data.get("controls")
        errors = data.get("errors")
        names: Set[str] = set()
        if isinstance(controls, dict):
            names = {str(k) for k in controls}
        live = set(names)
        if isinstance(errors, dict):
            live -= {str(k) for k, v in errors.items() if v not in (None, "")}
        out[mid] = {"all": names, "live": live}
    return out


def live_controls_from_cache(
    mqtt_ids: Iterable[str],
    cache_dir: Optional[str] = None,
) -> Dict[str, Set[str]]:
    """`{mqtt_id: {control, …}}` — the live half of `carel_cache_snapshot`."""
    return {
        mid: snap["live"]
        for mid, snap in carel_cache_snapshot(mqtt_ids, cache_dir).items()
    }


def prepare_catalogue_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The document the catalogue is built from: `doc` itself when it binds
    no Carel unit, else a COPY carrying the AHU rows above.

    The copy is what keeps this in-memory: the caller's dict — a test
    fixture, or the object `load_devices` just returned — is never mutated,
    and nothing here ever calls `save_devices`.
    """
    if not isinstance(doc, dict):
        return doc
    mids = carel_mqtt_ids(doc)
    if not mids:
        return doc
    # ONE read per Carel device: the family needs the raw key set, the
    # optional probes need the live one, and both come out of this snapshot.
    snapshot = carel_cache_snapshot(mids)
    live = {mid: snap["live"] for mid, snap in snapshot.items()}
    families = {mid: carel_family(snap["all"]) for mid, snap in snapshot.items()}
    out = copy.deepcopy(doc)
    changed = ensure_ahu_cloud_status(out, live, families)
    changed = drop_unfitted_ahu_probes(out, live) or changed
    return out if changed else doc
