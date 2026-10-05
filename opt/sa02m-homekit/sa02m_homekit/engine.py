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
import math
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Set, Tuple

from sa02m_alice.common import constants as AC

from . import constants as C
from . import projection as P
from .aid_store import AidStore

log = logging.getLogger("sa02m_homekit.engine")

Publish = Callable[[str, str], bool]
# () → (skipped rows, known scene device ids, store readable) — the part of the
# scene projection that is not a catalogue row (config/scene_devices.py
# homekit_scene_state). None = no scenes at all.
SceneState = Callable[[], Tuple[List[Dict[str, Any]], List[str], bool]]
# (service index, characteristic name, HAP value)
Value = Tuple[int, str, Any]
Update = Tuple[str, List[Value], bool]
# (device id, service index, characteristic name, HAP value) — a press event.
Event = Tuple[str, int, str, Any]


def _color_rgb(entries: List[Mapping[str, Any]]) -> Optional[int]:
    """The cached RGB word of the first colour capability, or None."""
    for entry in entries:
        for block in entry.get("capabilities") or []:
            if not isinstance(block, dict) or block.get("type") != P.CAP_COLOR:
                continue
            state = block.get("state")
            if not isinstance(state, dict):
                continue
            value = state.get("value")
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if 0 <= value <= 0xFFFFFF:
                return value
    return None

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
    def __init__(self, registry: Any, aid_store: AidStore, publish: Publish,
                 scene_state: Optional[SceneState] = None) -> None:
        self.registry = registry
        self.aids = aid_store
        self._publish = publish
        self._scene_state = scene_state
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
        # CarbonDioxideDetected memory per (device, service index): the
        # hysteresis needs the previous state. Per process — the first reading
        # after a start is decided on the level alone (homekit-bridge.md §3).
        self._co2: Dict[Tuple[str, int], int] = {}
        # Button press counters (M16): counter topic → (device, service index,
        # characteristic, HAP value), the last counter seen per topic, and the
        # bounded queue of events waiting for the next flush.
        self._counters: Dict[str, Tuple[str, int, str, Any]] = {}
        self._counter_last: Dict[str, float] = {}
        self._events: Deque[Event] = deque()
        self._events_dropped = 0

    # ── accessory set ────────────────────────────────────────────────────
    def rebuild(self, *, document_loaded: bool = True) -> bool:
        """Re-project the registry's catalogue. True when what iOS caches
        (aids, names, models, services) changed — the caller then rebuilds
        the driver; an unchanged projection costs nothing.

        `document_loaded=False` (the document failed to parse) never retires
        an aid: a broken read must not look like "every device deleted".
        """
        catalogue = self.registry.catalogue_items()
        scene_skipped, keep = self._scenes()
        aids_changed = False
        if document_loaded:
            aids_changed = self.aids.retire_absent(
                (did for did, _d, _c, _p in catalogue), keep=keep) > 0
        proj = P.project(catalogue, self.aids.aids)
        proj.skipped.extend(scene_skipped)
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
        counters: Dict[str, Tuple[str, int, str, Any]] = {}
        for spec in proj.accessories:
            for index, svc in enumerate(spec.services):
                for binding in svc.bindings:
                    for topic, value in P.button_counters(binding):
                        counters[topic] = (spec.device_id, index, binding.char, value)
        changed = proj.signature() != self.projection.signature()
        with self._lock:
            self.projection = proj
            self._specs = {spec.device_id: spec for spec in proj.accessories}
            self._by_topic = by_topic
            self._by_mid = by_mid
            self._last.clear()
            self._dirty = set(self._specs)
            self._co2 = {k: v for k, v in self._co2.items() if k[0] in self._specs}
            self._counters = counters
            # A counter still bound keeps its baseline across a rebuild;
            # an unbound one is forgotten (re-baselined if bound again).
            self._counter_last = {t: v for t, v in self._counter_last.items() if t in counters}
        return changed

    def _scenes(self) -> Tuple[List[Dict[str, Any]], Set[str]]:
        """(scene skip rows, aid ids to keep). An unticked or disabled scene
        keeps its aid like a hidden device; a store that could not be read
        retires no scene aid at all («не прочитал» ≠ «удалено»)."""
        if self._scene_state is None:
            return [], set()
        try:
            skipped, known, readable = self._scene_state()
        except Exception as exc:
            log.error("scene state unreadable: %s — no scene aid retired", exc)
            skipped, known, readable = [], [], False
        keep = set(known)
        if not readable:
            keep |= {did for did in self.aids.aids if did.startswith(C.SCENE_ID_PREFIX)}
        return list(skipped), keep

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

    def extra_topics(self) -> List[str]:
        """Topics the bridge needs beyond the registry's: the press counters
        of every admitted button (docs/contracts/homekit-bridge.md §6)."""
        with self._lock:
            return sorted(self._counters)

    def note_mqtt(self, topic: str, payload: str, retained: bool) -> None:
        with self._lock:
            counter = self._counters.get(topic)
            also_item = topic in self._by_topic
        if counter is not None:
            # An event source, not a value: never through the dirty/_last
            # dedupe (two single presses are two events). A counter another
            # accessory binds as an ordinary item still feeds that item below.
            self._note_counter(topic, counter, payload, retained)
            if not also_item:
                return
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

    def _note_counter(self, topic: str, counter: Tuple[str, int, str, Any],
                      payload: str, retained: bool) -> None:
        """The rules engine's counter guards (sa02m_rules engine
        `_button_state`), plus one stricter: a RETAINED message only
        re-baselines — a broker reconnect replays the latest counter, and a
        press made during the outage must not fire late."""
        try:
            value = float(str(payload).strip())
        except (TypeError, ValueError):
            return  # non-numeric payload: ignored, baseline kept
        if not math.isfinite(value):
            return
        with self._lock:
            prev = self._counter_last.get(topic)
            self._counter_last[topic] = value
            if prev is None or retained:
                return  # first sight / replay = baseline, never a press
            wrapped = (prev >= C.BUTTON_COUNTER_MAX - C.BUTTON_COUNTER_WRAP_SLACK
                       and value <= C.BUTTON_COUNTER_WRAP_SLACK)
            if not (value > prev or wrapped):
                return  # unchanged, or a decrease = counter reset: re-baselined
            # One event per observed increment, whatever the delta: a +3 jump
            # between two bridge polls is ONE event (lossy, stated in §6).
            if len(self._events) >= C.EVENT_QUEUE_MAX:
                self._events.popleft()
                self._events_dropped += 1
                dropped = self._events_dropped
            else:
                dropped = 0
            self._events.append(counter)
        if dropped:
            log.warning("press event queue full (%d) — oldest dropped (%d so far)",
                        C.EVENT_QUEUE_MAX, dropped)

    def take_events(self) -> List[Event]:
        """Press events since the last call (flush thread)."""
        with self._lock:
            out = list(self._events)
            self._events.clear()
        return out

    # ── outbound values ──────────────────────────────────────────────────
    def values_for(self, spec: P.AccessorySpec, entry: Mapping[str, Any]) -> Tuple[List[Value], bool]:
        """Caller holds `self._lock` (the CO₂ memory is engine state)."""
        available = not entry.get("error_code")
        index = P.state_index(entry)
        values: List[Value] = []
        for i, svc in enumerate(spec.services):
            for binding in svc.bindings:
                raw = index.get((binding.source_type, binding.instance))
                if binding.rule == P.RULE_CO2_DETECTED:
                    value = self._co2_value(spec.device_id, i, binding, raw)
                else:
                    value = P.hap_value(binding, raw)
                if value is not None:
                    values.append((i, binding.char, value))
        return values, available

    def _co2_value(self, device_id: str, index: int, binding: P.CharBinding,
                   level: Any) -> Optional[int]:
        if isinstance(level, bool) or not isinstance(level, (int, float)) \
                or not math.isfinite(level) or len(binding.threshold) < 2:
            return None
        key = (device_id, index)
        detected = P.co2_detected(float(level), binding.threshold[0], binding.threshold[1],
                                  self._co2.get(key))
        self._co2[key] = detected
        return detected

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
        if binding.rule == P.RULE_MOMENTARY and value is not True and value != 1:
            # The bridge's own reset (or a user tapping a lit tile): never the
            # engine's off verb — that switches off every output the scene set.
            return
        try:
            current_rgb = None
            if binding.rule in (P.RULE_HUE, P.RULE_SATURATION):
                current_rgb = _color_rgb(self.registry.query_devices([device_id]))
            cap = P.yandex_capability(binding, value, current_rgb=current_rgb)
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
