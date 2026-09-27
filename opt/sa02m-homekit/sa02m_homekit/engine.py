"""The bridge's pyhap-free core: registry + projection + dirty set + write path.

Everything the daemon decides about WHAT HomeKit sees lives here, so it is
testable without HAP-python: the accessory set (projection + the aid store),
which accessories an MQTT message touched, the HAP values of a touched
accessory and whether it is available, and the HAP write → `/on` publish.
`bridge.py` only renders these decisions into pyhap objects.

Threads: the paho network thread calls `note_mqtt`, the HAP event loop (or
its executor) calls `write`, the main thread calls everything else. The
registry is RLock-guarded already; the dirty set and the last-pushed cache
have their own lock (plan §Race / re-entry).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from sa02m_alice.common import constants as AC

from . import projection as P
from .aid_store import AidStore

log = logging.getLogger("sa02m_homekit.engine")

Publish = Callable[[str, str], bool]
# (service index, characteristic name, HAP value)
Value = Tuple[int, str, Any]
Update = Tuple[str, List[Value], bool]

_DEVICES_PREFIX = "/devices/"
_META_ERROR = "/meta/error"


class WriteRefused(Exception):
    """A HAP write that did not reach the broker. Raised inside the pyhap
    setter so HAP-python answers -70402 SERVICE_COMMUNICATION_FAILURE."""


def mqtt_device_of(topic: str) -> Optional[str]:
    """`/devices/<mid>/…` → `<mid>`; anything else → None."""
    if not topic.startswith(_DEVICES_PREFIX):
        return None
    rest = topic[len(_DEVICES_PREFIX):]
    mid = rest.split("/", 1)[0]
    return mid or None


class Engine:
    def __init__(self, registry: Any, aid_store: AidStore, publish: Publish) -> None:
        self.registry = registry
        self.aids = aid_store
        self._publish = publish
        self._lock = threading.Lock()
        self._dirty: Set[str] = set()
        # Last (values, available) handed to the bridge per device: an
        # unchanged accessory costs no HAP event.
        self._last: Dict[str, Tuple[Tuple[Value, ...], bool]] = {}
        self.projection = P.Projection()
        self._specs: Dict[str, P.AccessorySpec] = {}
        # Exact item topic → devices; MQTT device id → devices (for the
        # availability topics: device `/meta/error`, `controls/uptime_s`).
        self._by_topic: Dict[str, Set[str]] = {}
        self._by_mid: Dict[str, Set[str]] = {}

    # ── accessory set ────────────────────────────────────────────────────
    def rebuild(self, *, document_loaded: bool = True) -> bool:
        """Re-project the registry's catalogue. True when what iOS caches
        (aids, names, models, services) changed — the caller then rebuilds
        the driver; an unchanged projection costs nothing.

        `document_loaded=False` (the document failed to parse) never retires
        an aid: a broken read must not look like "every device deleted".
        """
        catalogue = self.registry.catalogue_items()
        aids_changed = False
        if document_loaded:
            aids_changed = self.aids.retire_absent(did for did, _d, _c, _p in catalogue) > 0
        proj = P.project(catalogue, self.aids.aids)
        for spec in proj.accessories:
            if spec.aid is None:
                spec.aid = self.aids.allocate(spec.device_id)
                aids_changed = True
        if aids_changed:
            try:
                self.aids.save()
            except OSError as exc:
                # The in-memory map stays authoritative for this process;
                # the next successful save carries it (never a reissued aid).
                log.error("aid map save failed: %s", exc)
        by_topic: Dict[str, Set[str]] = {}
        by_mid: Dict[str, Set[str]] = {}
        admitted = {spec.device_id for spec in proj.accessories}
        for did, _dev, caps, props in catalogue:
            if did not in admitted:
                continue
            for item in list(caps) + list(props):
                topic = str(item.get("mqtt") or "").strip()
                if not topic:
                    continue
                by_topic.setdefault(topic, set()).add(did)
                mid = mqtt_device_of(topic)
                if mid:
                    by_mid.setdefault(mid, set()).add(did)
        changed = proj.signature() != self.projection.signature()
        with self._lock:
            self.projection = proj
            self._specs = {spec.device_id: spec for spec in proj.accessories}
            self._by_topic = by_topic
            self._by_mid = by_mid
            self._last.clear()
            self._dirty = set(self._specs)
        return changed

    @property
    def specs(self) -> List[P.AccessorySpec]:
        return list(self.projection.accessories)

    # ── inbound MQTT ─────────────────────────────────────────────────────
    def devices_for(self, topic: str) -> Set[str]:
        """Accessories a message on `topic` can change: an item topic → its
        devices; `<item>/meta/error` (one channel's read fail) → that item's
        devices only; any other `/devices/<mid>/…` topic — the module's
        `/meta/error` (slave offline) or a liveness control (`uptime_s`) →
        every accessory bound to that module."""
        with self._lock:
            hit = self._by_topic.get(topic)
            if hit is None and topic.endswith(_META_ERROR):
                hit = self._by_topic.get(topic[: -len(_META_ERROR)])
            if hit is None:
                mid = mqtt_device_of(topic)
                hit = self._by_mid.get(mid, set()) if mid else set()
            return set(hit)

    def note_mqtt(self, topic: str, payload: str, retained: bool) -> None:
        self.registry.note_mqtt(topic, payload, retained=retained)
        touched = self.devices_for(topic)
        if touched:
            with self._lock:
                self._dirty |= touched

    def mark_all_dirty(self) -> None:
        """Availability is time-based (a stale non-Modbus topic goes
        «Не отвечает» without any message arriving) — the heartbeat re-checks."""
        with self._lock:
            self._dirty = set(self._specs)

    def resend_all(self) -> None:
        """A freshly started driver holds no values: push every accessory."""
        with self._lock:
            self._last.clear()
            self._dirty = set(self._specs)

    def forget(self, device_id: str) -> None:
        """Drop the last-pushed cache of one device and re-check it: after a
        failed write HAP-python already shows the refused value, the next
        flush must push the real one back."""
        with self._lock:
            self._last.pop(device_id, None)
            if device_id in self._specs:
                self._dirty.add(device_id)

    # ── outbound values ──────────────────────────────────────────────────
    def values_for(self, spec: P.AccessorySpec, entry: Mapping[str, Any]) -> Tuple[List[Value], bool]:
        available = not entry.get("error_code")
        index = P.state_index(entry)
        values: List[Value] = []
        for i, svc in enumerate(spec.services):
            for binding in svc.bindings:
                value = P.hap_value(binding, index.get((binding.source_type, binding.instance)))
                if value is not None:
                    values.append((i, binding.char, value))
        return values, available

    def take_updates(self) -> List[Update]:
        """The dirty accessories whose (values, available) changed."""
        with self._lock:
            dirty = sorted(self._dirty)
            self._dirty.clear()
            specs = {did: self._specs[did] for did in dirty if did in self._specs}
        if not specs:
            return []
        entries = {str(e.get("id")): e for e in self.registry.query_devices(list(specs))}
        out: List[Update] = []
        with self._lock:
            for did, spec in specs.items():
                entry = entries.get(did) or {"id": did, "error_code": AC.ERR_DEVICE_UNREACHABLE}
                values, available = self.values_for(spec, entry)
                key = (tuple(values), available)
                if self._last.get(did) == key:
                    continue
                self._last[did] = key
                out.append((did, values, available))
        return out

    # ── HAP write → MQTT ─────────────────────────────────────────────────
    def write(self, device_id: str, binding: P.CharBinding, value: Any) -> None:
        """Apply one HAP write through the registry's guards and publish the
        resulting `/on` commands. Raises WriteRefused on any refusal so the
        controller sees a failure, never a fake success."""
        try:
            cap = P.yandex_capability(binding, value)
            if cap is None:
                raise WriteRefused("%s: characteristic %s is not writable with %r"
                                   % (device_id, binding.char, value))
            results, publishes = self.registry.apply_actions(
                [{"id": device_id, "capabilities": [cap]}])
            errors = [
                str(c.get("error_code") or "")
                for r in results for c in (r.get("capabilities") or [])
                if c.get("status") != AC.STATUS_DONE
            ]
            if errors:
                raise WriteRefused("%s: write refused (%s)" % (device_id, ",".join(errors)))
            for topic, payload in publishes:
                if not self._publish(topic, payload):
                    raise WriteRefused("%s: broker not connected, %s not published"
                                       % (device_id, topic))
            log.info("HomeKit write %s %s=%r → %d publish(es)",
                     device_id, binding.char, value, len(publishes))
        except WriteRefused as exc:
            log.warning("%s", exc)
            self.forget(device_id)
            raise
        # Derived characteristics (OutletInUse, InUse) follow the new value.
        with self._lock:
            if device_id in self._specs:
                self._dirty.add(device_id)

    # ── card view ────────────────────────────────────────────────────────
    def summary(self) -> Dict[str, Any]:
        return P.summary(self.projection)

    def skipped_count(self) -> int:
        return len(self.projection.skipped)
