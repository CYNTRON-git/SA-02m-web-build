"""MQTT-driven append of DTV / CE-02m-3 devices into the Alice document.

One home — the same `sa02m-alice-devices.conf` the UI and both client
profiles already read. Discovery watches Wiren-style `/devices/+/meta/name`
(or `/meta/driver`); a prefix `dtv-` / `ce02m3-` that no existing binding
already maps is turned into the same property set as the live stand
(«ДТВ цех», «Анализатор фаза А/В/С»). Persist is `save_devices` — atomic,
mode/owner preserved. A second pass is a no-op.

A meta/name (or meta/driver) alone is not enough: the MQTT id must be
listed in `/etc/sa02m-modbus-mqtt.yaml`, or at least one live
`/controls/<name>` value must have arrived (not `…/meta/*`). A
`meta/name` containing `test` is ignored.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from ..common.config_store import load_devices, save_devices
from ..config import models
from ..config.topics import YAML_CANDIDATES

log = logging.getLogger("sa02m_alice.provision")

WATCH_TOPICS = (
    "/devices/+/meta/name",
    "/devices/+/meta/driver",
)

_DEVICES_PREFIX = "/devices/"
_META_RE = re.compile(r"^/devices/([^/]+)/meta/(name|driver)$")
_CONTROL_RE = re.compile(r"^/devices/([^/]+)/controls/([^/]+)$")
_ID_TAIL_RE = re.compile(r"^(?:dtv|ce02m3)-([A-Za-z0-9]+)-(\d+)$")

DTV_PREFIX = "dtv-"
CE_PREFIX = "ce02m3-"

# First existing control wins per instance — live «ДТВ цех» uses BME280
# for T/RH/P and BME680 for eCO₂; a newer slave may publish BME680 for all.
_DTV_FLOAT = (
    (
        "temperature",
        "unit.temperature.celsius",
        None,
        ("temp_bme680", "temp_bme280", "temp_hdc1080", "temp_mcp9808", "temp_ds18b20", "temp_ext"),
    ),
    (
        "humidity",
        "unit.percent",
        None,
        ("humidity_bme680", "humidity_bme280", "humidity_hdc1080"),
    ),
    (
        "pressure",
        "unit.pressure.mmhg",
        7.50062,
        ("pressure_bme680_kpa", "pressure_bme280_kpa"),
    ),
    (
        "co2_level",
        "unit.ppm",
        None,
        ("eco2_bme680", "eco2_zmod"),
    ),
    (
        "tvoc",
        "unit.density.mcg_m3",
        1000.0,
        ("tvoc_zmod",),
    ),
)
_DTV_MOTION = ("presence",)

_CE_PHASES = (
    ("a", "А"),
    ("b", "В"),
    ("c", "С"),
)
_CE_ENERGY_SCALE = 0.001
_SETTLE_S = 1.0


def mqtt_id_from_meta_topic(topic: str) -> Optional[str]:
    """`/devices/<id>/meta/name|driver` → `<id>`, else None."""
    m = _META_RE.match(topic or "")
    return m.group(1) if m else None


def is_dtv_id(mqtt_id: str) -> bool:
    return bool(mqtt_id) and mqtt_id.startswith(DTV_PREFIX)


def is_ce_id(mqtt_id: str) -> bool:
    return bool(mqtt_id) and mqtt_id.startswith(CE_PREFIX)


def mapped_mqtt_ids(doc: Dict[str, Any]) -> Set[str]:
    """MQTT device ids already referenced by any capability or property."""
    out: Set[str] = set()
    for dev in doc.get("devices") or []:
        if not isinstance(dev, dict):
            continue
        for key in ("properties", "capabilities"):
            for item in dev.get(key) or []:
                if not isinstance(item, dict):
                    continue
                mqtt = str(item.get("mqtt") or "")
                if not mqtt.startswith(_DEVICES_PREFIX):
                    continue
                rest = mqtt[len(_DEVICES_PREFIX) :]
                mid, _sep, _tail = rest.partition("/")
                if mid:
                    out.add(mid)
    return out


def already_mapped(doc: Dict[str, Any], mqtt_id: str) -> bool:
    return mqtt_id in mapped_mqtt_ids(doc)


def is_test_meta_name(payload: Optional[str]) -> bool:
    """True when `meta/name` contains `test` (case-insensitive). Extra belt."""
    return "test" in (payload or "").lower()


def mqtt_ids_from_yaml_doc(doc: Any) -> Set[str]:
    """Explicit `id` keys from a parsed bridge yaml document."""
    out: Set[str] = set()
    if not isinstance(doc, dict):
        return out
    devices = doc.get("devices") or doc.get("Devices") or []
    if isinstance(devices, dict):
        for key, raw in devices.items():
            if key:
                out.add(str(key))
            if isinstance(raw, dict):
                did = str(raw.get("id") or "").strip()
                if did:
                    out.add(did)
        return out
    if not isinstance(devices, list):
        return out
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        did = str(dev.get("id") or "").strip()
        if did:
            out.add(did)
    return out


def yaml_mqtt_ids(path: Optional[str] = None) -> Set[str]:
    """MQTT device ids listed in the bridge yaml. Empty if unreadable."""
    paths: Tuple[str, ...]
    if path:
        paths = (path,)
    else:
        paths = tuple(p for p in YAML_CANDIDATES if p)
    try:
        import yaml  # type: ignore
    except ImportError:
        return set()
    for raw in paths:
        ap = os.path.abspath(raw)
        if not os.path.isfile(ap):
            continue
        try:
            with open(ap, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        except (OSError, Exception):
            continue
        ids = mqtt_ids_from_yaml_doc(doc)
        if ids:
            return ids
    return set()


def live_control_names(mqtt_id: str, present: Optional[Iterable[str]]) -> Set[str]:
    """Control names from live `/controls/<name>` topics; empty if unknown.

    `present is None` means no live evidence (the old meta-only path).
    Names that look like `meta` are dropped so a `/meta/*` topic cannot pass.
    """
    if present is None:
        return set()
    names = _present_names(mqtt_id, present) or set()
    return {n for n in names if n and n != "meta" and not n.startswith("meta/")}


def may_provision(
    mqtt_id: str,
    present_topics: Optional[Iterable[str]] = None,
    *,
    meta_name: Optional[str] = None,
    yaml_ids: Optional[Iterable[str]] = None,
) -> bool:
    """Yaml row or at least one live control; `test` in meta/name vetoes."""
    if not mqtt_id or is_test_meta_name(meta_name):
        return False
    in_yaml = mqtt_id in set(yaml_ids or ())
    return in_yaml or bool(live_control_names(mqtt_id, present_topics))


def _control_name(topic_or_name: str, mqtt_id: str) -> str:
    prefix = "/devices/%s/controls/" % mqtt_id
    if topic_or_name.startswith(prefix):
        rest = topic_or_name[len(prefix) :]
        return rest.split("/", 1)[0]
    if "/" not in topic_or_name:
        return topic_or_name
    return ""


def _present_names(mqtt_id: str, present: Optional[Iterable[str]]) -> Optional[Set[str]]:
    if present is None:
        return None
    return {_control_name(t, mqtt_id) for t in present if _control_name(t, mqtt_id)}


def _has(names: Optional[Set[str]], control: str) -> bool:
    return names is None or control in names


def _float_prop(
    mqtt_id: str, control: str, instance: str, unit: str, scale: Optional[float]
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "type": "devices.properties.float",
        "mqtt": "/devices/%s/controls/%s" % (mqtt_id, control),
        "retrievable": True,
        "reportable": True,
        "parameters": {"instance": instance, "unit": unit},
    }
    if scale is not None:
        item["scale"] = scale
    return item


def _motion_prop(mqtt_id: str, control: str) -> Dict[str, Any]:
    return {
        "type": "devices.properties.event",
        "mqtt": "/devices/%s/controls/%s" % (mqtt_id, control),
        "retrievable": True,
        "reportable": True,
        "parameters": {
            "instance": "motion",
            "events": [{"value": "detected"}, {"value": "not_detected"}],
        },
    }


def _id_label(mqtt_id: str) -> str:
    m = _ID_TAIL_RE.match(mqtt_id or "")
    if not m:
        return mqtt_id.replace("-", " ")
    return "%s %s" % (m.group(1), m.group(2))


def dtv_name(mqtt_id: str) -> str:
    """Yandex-web-safe (≤25, no punctuation): «ДТВ COM9 99»."""
    return "ДТВ %s" % _id_label(mqtt_id)


def ce_name(mqtt_id: str, phase_letter: str) -> str:
    """Yandex-web-safe: «Анализатор COM9 99 А»."""
    return "Анализатор %s %s" % (_id_label(mqtt_id), phase_letter)


def dtv_properties(mqtt_id: str, present: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    names = _present_names(mqtt_id, present)
    props: List[Dict[str, Any]] = []
    for instance, unit, scale, candidates in _DTV_FLOAT:
        for control in candidates:
            if _has(names, control):
                props.append(_float_prop(mqtt_id, control, instance, unit, scale))
                break
    for control in _DTV_MOTION:
        if _has(names, control):
            props.append(_motion_prop(mqtt_id, control))
            break
    return props


def ce_devices(mqtt_id: str, present: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    names = _present_names(mqtt_id, present)
    per_phase = all(_has(names, "energy_active_import_%s" % ph) for ph, _letter in _CE_PHASES)
    total = _has(names, "energy_active_import")
    out: List[Dict[str, Any]] = []
    for ph, letter in _CE_PHASES:
        props: List[Dict[str, Any]] = []
        # Official float order on the live phase-C tile: amperage, power, voltage, meter.
        for instance, unit, control in (
            ("amperage", "unit.ampere", "current_%s" % ph),
            ("power", "unit.watt", "power_%s" % ph),
            ("voltage", "unit.volt", "voltage_%s" % ph),
        ):
            if _has(names, control):
                props.append(_float_prop(mqtt_id, control, instance, unit, None))
        energy_ctrl = None
        if per_phase:
            energy_ctrl = "energy_active_import_%s" % ph
        elif total and ph == "c":
            energy_ctrl = "energy_active_import"
        if energy_ctrl:
            props.append(
                _float_prop(
                    mqtt_id,
                    energy_ctrl,
                    "electricity_meter",
                    "unit.kilowatt_hour",
                    _CE_ENERGY_SCALE,
                )
            )
        if not props:
            continue
        out.append(
            {
                "id": models.new_id(),
                "name": ce_name(mqtt_id, letter),
                "type": "devices.types.smart_meter.electricity",
                "capabilities": [],
                "properties": props,
            }
        )
    return out


def build_dtv(mqtt_id: str, present: Optional[Iterable[str]] = None) -> Optional[Dict[str, Any]]:
    props = dtv_properties(mqtt_id, present)
    if not props:
        return None
    return {
        "id": models.new_id(),
        "name": dtv_name(mqtt_id),
        "type": "devices.types.sensor.climate",
        "capabilities": [],
        "properties": props,
    }


def provision(
    doc: Dict[str, Any],
    mqtt_id: str,
    present_topics: Optional[Iterable[str]] = None,
    *,
    yaml_ids: Optional[Iterable[str]] = None,
    meta_name: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Append devices for `mqtt_id` if that prefix is not already mapped.

    `present_topics` None is the old meta-only path and now adds nothing
    unless `mqtt_id` is in `yaml_ids` (then the full candidate set is
    used). A set/list filters to controls that exist on that slave.
    Returns (doc, added) — `doc` is mutated; `added` is empty on a no-op.
    """
    if not mqtt_id or already_mapped(doc, mqtt_id):
        return doc, []
    yids = set(yaml_ids or ())
    if not may_provision(mqtt_id, present_topics, meta_name=meta_name, yaml_ids=yids):
        return doc, []
    present = present_topics
    if mqtt_id in yids and not live_control_names(mqtt_id, present_topics):
        present = None
    added: List[Dict[str, Any]] = []
    if is_dtv_id(mqtt_id):
        dev = build_dtv(mqtt_id, present)
        if dev:
            added.append(dev)
    elif is_ce_id(mqtt_id):
        added.extend(ce_devices(mqtt_id, present))
    if not added:
        return doc, []
    cleaned: List[Dict[str, Any]] = []
    for raw in added:
        out, err = models.validate_device(raw)
        if err or out is None:
            log.error("auto-provision rejected %s: %s", mqtt_id, err)
            return doc, []
        cleaned.append(out)
    devices = doc.setdefault("devices", [])
    if not isinstance(devices, list):
        return doc, []
    devices.extend(cleaned)
    return doc, cleaned


def commit_provision(
    mqtt_id: str,
    present_topics: Optional[Iterable[str]] = None,
    *,
    path: Optional[str] = None,
    yaml_ids: Optional[Iterable[str]] = None,
    meta_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load, append if needed, persist via `save_devices`. Empty = no-op."""
    doc = load_devices(path)
    ids = set(yaml_ids) if yaml_ids is not None else yaml_mqtt_ids()
    _doc, added = provision(
        doc, mqtt_id, present_topics, yaml_ids=ids, meta_name=meta_name
    )
    if not added:
        return []
    save_devices(doc, path)
    return added


class AutoProvisioner:
    """Collect meta + retained controls, then persist and ask the caller to reload.

    `note` is safe on the MQTT thread (records only). `tick` (main loop)
    commits after a 1 s settle and returns True when the document changed.
    """

    def __init__(
        self,
        *,
        load: Callable[[], Dict[str, Any]] = load_devices,
        save: Callable[[Dict[str, Any]], None] = save_devices,
        clock: Callable[[], float] = time.monotonic,
        settle_s: float = _SETTLE_S,
        yaml_ids: Optional[Callable[[], Set[str]]] = None,
    ) -> None:
        self._load = load
        self._save = save
        self._clock = clock
        self._settle_s = float(settle_s)
        self._yaml_ids = yaml_ids if yaml_ids is not None else yaml_mqtt_ids
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._done: Set[str] = set()

    def note(self, topic: str, payload: str) -> Optional[str]:
        """Record a discovery message. Returns an extra topic to subscribe, or None."""
        mid = mqtt_id_from_meta_topic(topic)
        if mid:
            if not (is_dtv_id(mid) or is_ce_id(mid)):
                return None
            if mid in self._done:
                return None
            if topic.endswith("/meta/name") and is_test_meta_name(payload):
                self._done.add(mid)
                self._pending.pop(mid, None)
                return None
            if already_mapped(self._load(), mid):
                self._done.add(mid)
                return None
            rec = self._pending.setdefault(
                mid,
                {
                    "controls": set(),
                    "first": self._clock(),
                    "last": self._clock(),
                    "name": "",
                },
            )
            rec["last"] = self._clock()
            if topic.endswith("/meta/name"):
                rec["name"] = payload
            return "/devices/%s/controls/+" % mid
        m = _CONTROL_RE.match(topic or "")
        if not m:
            return None
        mid = m.group(1)
        if mid not in self._pending:
            return None
        rec = self._pending[mid]
        rec["controls"].add(m.group(2))
        rec["last"] = self._clock()
        return None

    def tick(self) -> bool:
        """Commit any pending device whose settle window has elapsed. True if saved."""
        if not self._pending:
            return False
        now = self._clock()
        ready = [
            mid
            for mid, rec in self._pending.items()
            if (now - rec["first"]) >= self._settle_s
        ]
        if not ready:
            return False
        doc = self._load()
        yids = set(self._yaml_ids() or ())
        changed = False
        for mid in ready:
            rec = self._pending.pop(mid, None)
            if rec is None:
                continue
            if already_mapped(doc, mid):
                self._done.add(mid)
                continue
            controls = rec["controls"]
            name = rec.get("name") or None
            if not may_provision(
                mid, controls or None, meta_name=name, yaml_ids=yids
            ):
                self._done.add(mid)
                continue
            present: Optional[Set[str]]
            if mid in yids and not controls:
                present = None
            else:
                present = controls
            _doc, added = provision(
                doc, mid, present, yaml_ids=yids, meta_name=name
            )
            if added:
                changed = True
                self._done.add(mid)
                log.info(
                    "auto-provisioned %s → %d device(s)",
                    mid,
                    len(added),
                )
            else:
                self._done.add(mid)
        if changed:
            self._save(doc)
        return changed
