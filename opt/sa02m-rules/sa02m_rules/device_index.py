"""The Alice device document → MQTT index. Stdlib only.

One home for how a scenario's (device, cap[, instance]) names an MQTT topic,
shared by the service (publish / inbound state) and the store (save-time
target check). Contract: docs/contracts/cloud-scenarios.md §Store
(«Цель записи»).

Keys, per Alice device id:
- a capability of a type the device carries ONCE: its short type
  (`on_off`, `range`, …) — the form a scenario without `instance` uses;
- a capability with `parameters.instance`: `state_key(short, instance)` —
  the cloud sends that same `parameters.instance` (cloud
  scenario_templates.py `_set_range`), so two `range` capabilities (a Carel
  fan speed and its setpoint) stay two targets. A type the device carries
  more than once is AMBIGUOUS without an instance and resolves to nothing:
  the last capability listed used to win silently;
- a property (`devices.properties.float` / `.event`): its
  `parameters.instance` (`temperature`, `motion`, …) — read-only, a state
  trigger / condition / logic-template input, never a write target.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Set, Tuple

DEFAULT_PATH = "/etc/sa02m-alice/sa02m-alice-devices.conf"

Key = Tuple[str, str]


def devices_path() -> str:
    """Read at CALL time: the service, the store (inside the Alice daemon)
    and the tests resolve the same file without a module reload."""
    return os.environ.get("SA02M_ALICE_DEVICES") or DEFAULT_PATH


def cap_short(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("devices.capabilities."):
        return s.rsplit(".", 1)[-1]
    return s or "on_off"


def state_key(cap: str, instance: Any = "") -> str:
    """The engine's state-mirror key: `cap`, or `cap|instance` when the
    scenario names an instance. `|` is outside the cap charset (store
    CAP_RE), so a composite key can never collide with a plain cap."""
    return "%s|%s" % (cap, instance) if isinstance(instance, str) and instance else cap


class DeviceIndex:
    """One parse of the device document. `loaded` is False when the file is
    missing, unreadable or not a device document — callers then fall back
    (the service to raw topics, the store to skipping its target check)."""

    def __init__(self) -> None:
        self.loaded = False
        self.cap_topics: Dict[Key, str] = {}
        self.prop_topics: Dict[Key, str] = {}
        #: topic → every (device, key) a state message on it updates.
        self.topic_keys: Dict[str, List[Key]] = {}
        self.readonly: Set[Key] = set()
        self.ambiguous: Set[Key] = set()
        self.cap_types: Dict[str, Set[str]] = {}
        #: (device, parameters.instance) → the capability types carrying it.
        self.cap_instances: Dict[Key, List[str]] = {}
        self.devices: Set[str] = set()

    def _add_topic(self, topic: str, key: Key) -> None:
        keys = self.topic_keys.setdefault(topic, [])
        if key not in keys:
            keys.append(key)

    def resolve(self, device: Any, cap: Any, instance: Any = "") -> Tuple[Optional[str], str]:
        """A write target → (state topic, "") or (None, reason) with reason
        `unknown_target` | `ambiguous_target`. Capabilities only."""
        if not isinstance(device, str) or not isinstance(cap, str):
            return None, "unknown_target"
        short = cap_short(cap)
        if isinstance(instance, str) and instance:
            topic = self.cap_topics.get((device, state_key(short, instance)))
            return (topic, "") if topic else (None, "unknown_target")
        topic = self.cap_topics.get((device, short))
        if topic:
            return topic, ""
        if (device, short) in self.ambiguous:
            return None, "ambiguous_target"
        return None, "unknown_target"

    def alias(self, device: str, name: str) -> Tuple[str, str]:
        """A logic template's capability name → (cap, instance). Templates
        speak Alice instance names (`brightness`), the document carries them
        as `parameters.instance` of a typed capability (`range`). The name
        maps to that capability when exactly one capability of the device
        carries it and nothing else on the device answers to the name (a
        capability type or a property instance — `temperature` on a Carel
        unit is both a setpoint instance and a sensor); otherwise the name
        is used as it stands."""
        if (device, name) in self.cap_topics or (device, name) in self.prop_topics:
            return name, ""
        types = self.cap_instances.get((device, name)) or []
        if len(types) == 1:
            return types[0], name
        return name, ""

    def caps_of(self, device: str) -> Tuple[str, ...]:
        """Capability types plus the instance names `alias` resolves."""
        names = set(self.cap_types.get(device, ()))
        names.update(inst for (did, inst) in self.cap_instances
                     if did == device and self.alias(device, inst)[1])
        return tuple(sorted(names))


def _instance(item: Dict[str, Any]) -> str:
    params = item.get("parameters")
    inst = params.get("instance") if isinstance(params, dict) else None
    return inst.strip() if isinstance(inst, str) else ""


def load_index(path: Optional[str] = None) -> DeviceIndex:
    ix = DeviceIndex()
    try:
        with open(path or devices_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return ix
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        return ix
    ix.loaded = True
    for d in data["devices"]:
        if not isinstance(d, dict):
            continue
        did = str(d.get("id") or "").strip()
        if not did:
            continue
        ix.devices.add(did)
        caps = [c for c in d.get("capabilities") or [] if isinstance(c, dict)
                and str(c.get("mqtt") or "").rstrip("/")]
        count: Dict[str, int] = {}
        for cap in caps:
            short = cap_short(str(cap.get("type") or ""))
            count[short] = count.get(short, 0) + 1
        for cap in caps:
            topic = str(cap.get("mqtt")).rstrip("/")
            short = cap_short(str(cap.get("type") or ""))
            inst = _instance(cap)
            ix.cap_types.setdefault(did, set()).add(short)
            keys: List[str] = []
            if count[short] == 1:
                keys.append(short)
            else:
                ix.ambiguous.add((did, short))
            if inst:
                keys.append(state_key(short, inst))
                types = ix.cap_instances.setdefault((did, inst), [])
                if short not in types:
                    types.append(short)
            for key in keys:
                ix.cap_topics[(did, key)] = topic
                ix._add_topic(topic, (did, key))
                if cap.get("writable") is False:
                    ix.readonly.add((did, key))
        for prop in d.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            topic = str(prop.get("mqtt") or "").rstrip("/")
            inst = _instance(prop)
            if not topic or not inst:
                continue
            ix.prop_topics[(did, inst)] = topic
            ix._add_topic(topic, (did, inst))
    return ix


def load_mqtt_index(path: Optional[str] = None):
    """(device, key) → state topic, topic → its first (device, key), and the
    read-only keys — the service's original three-map view of `load_index`."""
    ix = load_index(path)
    by_key = dict(ix.prop_topics)
    by_key.update(ix.cap_topics)
    by_topic = {topic: keys[0] for topic, keys in ix.topic_keys.items() if keys}
    return by_key, by_topic, set(ix.readonly)
