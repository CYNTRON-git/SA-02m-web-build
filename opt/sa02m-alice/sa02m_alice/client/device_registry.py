"""Device registry: Alice device map ↔ MQTT topics."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from ..common import constants as C
from ..common.config_store import load_devices
from . import converters

_DEVICES_PREFIX = "/devices/"
_CONTROL_ERROR_SUFFIX = "/meta/error"
_UPTIME_SUFFIX = "/controls/uptime_s"


def _item_scale(item: Dict[str, Any]) -> float:
    """Item-level unit conversion factor; absent or unusable ⇒ identity.

    `scale` is stored beside `mqtt`, never inside `parameters` — discovery
    copies `parameters` verbatim to Yandex and must not leak a local field.
    """
    scale = item.get("scale")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        return 1.0
    return float(scale)


def _item_inverted(item: Dict[str, Any]) -> bool:
    """Item-level active-low flag; absent or non-bool ⇒ False (as-is).

    Beside `mqtt` for the same reason `scale` is: discovery copies `parameters`
    verbatim to Yandex and must not leak a local field. The flag is only read
    here and handed to converters.apply_on_off_inversion, which owns the rule.
    """
    inverted = item.get("inverted")
    return inverted is True


def _mqtt_device_id(topic: str) -> Optional[str]:
    """Wiren Board `/devices/<id>/…` → `<id>`, or None when the topic is not one."""
    if not topic.startswith(_DEVICES_PREFIX):
        return None
    rest = topic[len(_DEVICES_PREFIX):]
    mid, _sep, _tail = rest.partition("/")
    return mid or None


def _device_meta_error_id(topic: str) -> Optional[str]:
    """`/devices/<id>/meta/error` → `<id>`. Control-level `/meta/error` is not this."""
    parts = topic.split("/")
    if (
        len(parts) == 5
        and parts[1] == "devices"
        and parts[3] == "meta"
        and parts[4] == "error"
        and parts[2]
    ):
        return parts[2]
    return None


def _control_error_target(topic: str) -> Optional[str]:
    """`/devices/<id>/controls/<name>/meta/error` → the control topic, else None."""
    if _device_meta_error_id(topic) is not None:
        return None
    if not topic.endswith(_CONTROL_ERROR_SUFFIX):
        return None
    base = topic[: -len(_CONTROL_ERROR_SUFFIX)]
    if "/controls/" not in base:
        return None
    return base


def _availability_topics_for(control_topic: str) -> Set[str]:
    """Poller availability topics that decide whether `control_topic` is live."""
    extra: Set[str] = set()
    if not control_topic:
        return extra
    extra.add(control_topic.rstrip("/") + _CONTROL_ERROR_SUFFIX)
    mid = _mqtt_device_id(control_topic)
    if mid:
        extra.add("%s%s/meta/error" % (_DEVICES_PREFIX, mid))
        if "-COM" in mid:
            extra.add("%s%s%s" % (_DEVICES_PREFIX, mid, _UPTIME_SUFFIX))
    return extra


def _modbus_slave_topic(topic: str) -> bool:
    """True when the topic is a poller slave (`/devices/<driver>-COM<n>-<addr>/…`).

    GPIO and board telemetry live under `/devices/SA-02m/…` and have no
    poller `/meta/error`. A Modbus coil or register is dead only when that
    flag is set or the cache never saw a successful poll — not when the
    last value is merely retained or unchanged (the poller skips a quiet
    republish).
    """
    mid = _mqtt_device_id(topic)
    return bool(mid and "-COM" in mid)


class DeviceRegistry:
    """In-memory registry backed by sa02m-alice-devices.conf."""

    def __init__(
        self,
        devices_doc: Optional[Dict[str, Any]] = None,
        *,
        profile: str = C.PROFILE_YANDEX,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._profile = profile
        self._clock = clock or time.monotonic
        self._doc = devices_doc if devices_doc is not None else load_devices()
        self._mqtt_cache: Dict[str, str] = {}  # topic -> last value seen (retained included)
        self._mqtt_ts: Dict[str, float] = {}
        self._mqtt_live: Dict[str, bool] = {}
        self._device_error: Dict[str, str] = {}
        self._control_error: Dict[str, str] = {}
        # Last live (non-retained) MQTT on a Modbus slave. Per-channel
        # `controls/<name>/meta/error=r` flickers while the poller still
        # publishes `uptime_s` / other coils — that is a busy bus, not a
        # dead module. Device-level `/devices/<id>/meta/error` is the
        # slave-offline flag.
        self._slave_live_ts: Dict[str, float] = {}
        # Last announced down-edge per device id. A DEVICE_UNREACHABLE
        # device_state is emitted once per transition, not on every 30 s
        # snapshot — Yandex/cloud learn the loss without a flood.
        self._announced_down: Dict[str, bool] = {}
        self._rebuild_indexes()

    def _items(self, dev: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
        """Items of a device visible to THIS profile.

        An item flagged `cloud_only` carries a reading Yandex has no instance
        for (a ventilation unit's return-water temperature, its status text, its
        alarm flag). On the Yandex profile it is dropped here, which is the one
        place every path reads from — so it never reaches discovery, query,
        state or a topic subscription. The cloud profile sees everything.
        """
        out = []
        cloud = self._profile == C.PROFILE_CLOUD
        for item in dev.get(key, []) or []:
            if not isinstance(item, dict):
                continue
            if not cloud and item.get("cloud_only") is True:
                continue
            out.append(item)
        return out

    def _rebuild_indexes(self) -> None:
        self._devices_by_id: Dict[str, Dict[str, Any]] = {}
        self._topic_map: Dict[str, List[Tuple[str, str, Dict[str, Any]]]] = {}
        # topic -> list of (device_id, kind, item) where kind is capability|property
        rooms = {r.get("id"): r for r in self._doc.get("rooms", []) if isinstance(r, dict)}
        self._rooms = rooms
        groups = {g.get("id"): g for g in self._doc.get("groups", []) if isinstance(g, dict)}
        self._groups = groups
        for dev in self._doc.get("devices", []):
            if not isinstance(dev, dict) or not dev.get("id"):
                continue
            did = str(dev["id"])
            self._devices_by_id[did] = dev
            for kind, key in (("capability", "capabilities"), ("property", "properties")):
                for item in self._items(dev, key):
                    topic = str(item.get("mqtt") or "").strip()
                    if not topic:
                        continue
                    self._topic_map.setdefault(topic, []).append((did, kind, item))

    def reload(self, devices_doc: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._doc = devices_doc if devices_doc is not None else load_devices()
            self._rebuild_indexes()

    def mqtt_topics(self) -> Set[str]:
        with self._lock:
            return set(self._topic_map.keys())

    def subscribe_topics(self) -> Set[str]:
        """Catalog MQTT topics plus the poller's availability `/meta/error` topics.

        Device-level `/devices/<id>/meta/error` is the slave-offline flag
        (`device_online(False)`). Control-level `<mqtt>/meta/error` is a
        per-channel read fail. A disconnected slave may assert only the
        control flag; a live slave (`uptime_s` or another coil still
        publishing) may leave a sticky `r` on one DO after a write — that
        is not offline. COM slaves also subscribe `controls/uptime_s`.
        """
        with self._lock:
            catalog = set(self._topic_map.keys())
            extra: Set[str] = set()
            for topic in catalog:
                extra.update(_availability_topics_for(topic))
            return catalog | extra

    def _slave_down(self, topic: str) -> bool:
        mid = _mqtt_device_id(topic)
        return bool(mid and (self._device_error.get(mid) or "").strip())

    def _note_slave_live(self, topic: str) -> None:
        mid = _mqtt_device_id(topic)
        if mid and "-COM" in mid:
            self._slave_live_ts[mid] = self._clock()

    def _slave_recently_live(self, topic: str) -> bool:
        mid = _mqtt_device_id(topic)
        if not mid:
            return False
        ts = self._slave_live_ts.get(mid)
        return ts is not None and (self._clock() - ts) <= C.STATUS_STALE_S

    def _control_dead(self, topic: str) -> bool:
        """True when this control's `/meta/error` means the channel is down.

        A Modbus slave that still publishes live (`uptime_s` or any coil)
        can leave a sticky per-channel `r` after a write or a skipped
        poll — query/action must not treat that as DEVICE_UNREACHABLE.
        GPIO / a quiet slave with only retained `r` still counts as down.
        """
        if not (self._control_error.get(topic) or "").strip():
            return False
        if _modbus_slave_topic(topic) and self._slave_recently_live(topic):
            return False
        return True

    def _fresh_payload(self, topic: str, *, age_retained: bool = False) -> Optional[str]:
        """Cached control payload if the poller still stands behind it, else None.

        A Modbus slave (`-COM` in the device id) answers from the last
        successful poll — retained or unchanged — until the slave itself
        is down (`/devices/<id>/meta/error`) or this control is dead
        (`<mqtt>/meta/error` with no live poll on that slave). The poller
        often skips republishing an unchanged coil, so a quiet cache is
        not a dead slave. GPIO / board telemetry still ages a live entry
        past `STATUS_STALE_S` and may answer from retained when
        `age_retained` is false.
        """
        if self._slave_down(topic):
            return None
        if self._control_dead(topic):
            return None
        raw = self._mqtt_cache.get(topic)
        if raw is None:
            return None
        if _modbus_slave_topic(topic):
            return raw
        if self._mqtt_live.get(topic):
            ts = self._mqtt_ts.get(topic)
            if ts is None or (self._clock() - ts) > C.STATUS_STALE_S:
                return None
            return raw
        if age_retained:
            return None
        return raw

    def note_mqtt(self, topic: str, payload: str, *, retained: bool = False) -> bool:
        """Cache an MQTT value; return True when it should also be REPORTED.

        Retained messages are cached like any other — they are the only state
        a freshly (re)started client has, and Yandex reads state through the
        query fan-out, which serves this cache. What retained must NOT do is
        emit a state event: the retained burst on subscribe would otherwise
        flood the gateway with hundreds of "changes" that never happened.
        Dropping them from the cache instead (the pre-1.0.6.16 behaviour) left
        every sensor value empty in the Alice app until the bridge happened to
        republish — up to a minute, or never for a steady reading.

        Availability `/meta/error` topics are cached as flags, never reported.
        """
        with self._lock:
            mid = _device_meta_error_id(topic)
            if mid is not None:
                self._device_error[mid] = payload.strip()
                return False
            ctrl = _control_error_target(topic)
            if ctrl is not None:
                self._control_error[ctrl] = payload.strip()
                return False
            self._mqtt_cache[topic] = payload
            self._mqtt_ts[topic] = self._clock()
            if not retained:
                self._mqtt_live[topic] = True
                self._note_slave_live(topic)
            if retained:
                return False
            return topic in self._topic_map

    def get_cached(self, topic: str) -> Optional[str]:
        with self._lock:
            return self._mqtt_cache.get(topic)

    def room_name(self, room_id: Optional[str]) -> str:
        if not room_id:
            return ""
        with self._lock:
            room = self._rooms.get(room_id) or {}
            return str(room.get("name") or "")

    def listed_rooms(self) -> List[Dict[str, str]]:
        with self._lock:
            out: List[Dict[str, str]] = []
            for rid, room in self._rooms.items():
                if not rid:
                    continue
                out.append({"id": str(rid), "name": str((room or {}).get("name") or "")})
            return out

    def listed_groups(self) -> List[Dict[str, Any]]:
        with self._lock:
            out: List[Dict[str, Any]] = []
            for gid, group in self._groups.items():
                if not gid:
                    continue
                ids = []
                raw = (group or {}).get("device_ids")
                if isinstance(raw, list):
                    ids = [str(x) for x in raw if x]
                row: Dict[str, Any] = {"id": str(gid), "name": str((group or {}).get("name") or ""),
                                       "device_ids": ids}
                if (group or {}).get("icon") in ("light", "ahu"):
                    row["icon"] = str(group["icon"])
                out.append(row)
            return out

    def discovery_devices(self, profile: str = C.PROFILE_YANDEX) -> List[Dict[str, Any]]:
        """Discovery device list (no live state), shaped per profile.

        The Yandex profile drops devices with `alice_visible: false` (absent ⇒
        visible) and carries nothing but the Yandex fields. The cloud profile
        lists EVERY device and adds the local tile fields (`icon`,
        `alice_visible`) — additive, its own consumer. Query / action / state
        stay unfiltered on both: a stale Yandex list is harmless and self-heals
        on «Обновить список устройств» (docs/contracts/alice-mqtt-mapping.md).
        """
        cloud = profile == C.PROFILE_CLOUD
        out: List[Dict[str, Any]] = []
        with self._lock:
            for did, dev in self._devices_by_id.items():
                visible = dev.get("alice_visible", True) is not False
                if not cloud and not visible:
                    continue
                caps = []
                for item in self._items(dev, "capabilities"):
                    block = {
                        "type": item.get("type"),
                        "retrievable": bool(item.get("retrievable", True)),
                        "reportable": bool(item.get("reportable", True)),
                    }
                    if item.get("parameters"):
                        block["parameters"] = item["parameters"]
                    # Cloud catalogue only: Yandex has no `writable`. Absent
                    # stays omitted (fleet treats missing as True). Explicit
                    # false is a latching DI — the fleet 400s on_off.
                    if cloud and item.get("writable") is False:
                        block["writable"] = False
                    caps.append(block)
                props = []
                for item in self._items(dev, "properties"):
                    block = {
                        "type": item.get("type"),
                        "retrievable": bool(item.get("retrievable", True)),
                        "reportable": bool(item.get("reportable", True)),
                    }
                    if item.get("parameters"):
                        block["parameters"] = item["parameters"]
                    props.append(block)
                entry: Dict[str, Any] = {
                    "id": did,
                    "name": dev.get("name") or did,
                    "room": self.room_name(dev.get("room_id")),
                    "type": dev.get("type") or "devices.types.other",
                    "capabilities": caps,
                    "properties": props,
                }
                if cloud:
                    entry["alice_visible"] = visible
                    if dev.get("icon"):
                        entry["icon"] = str(dev["icon"])
                    rid = dev.get("room_id")
                    if isinstance(rid, str) and rid:
                        entry["room_id"] = rid
                out.append(entry)
        return out

    def query_devices(self, device_ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        wanted = set(device_ids) if device_ids is not None else None
        out: List[Dict[str, Any]] = []
        with self._lock:
            ids = list(self._devices_by_id.keys()) if wanted is None else [i for i in wanted]
            for did in ids:
                dev = self._devices_by_id.get(did)
                if not dev:
                    out.append(
                        {
                            "id": did,
                            "error_code": C.ERR_DEVICE_UNREACHABLE,
                        }
                    )
                    continue
                cap_items = self._items(dev, "capabilities")
                prop_items = self._items(dev, "properties")
                slave_down = False
                for item in cap_items + prop_items:
                    topic = str(item.get("mqtt") or "")
                    if topic and self._slave_down(topic):
                        slave_down = True
                        break
                if slave_down:
                    out.append(
                        {
                            "id": did,
                            "capabilities": [],
                            "properties": [],
                            "error_code": C.ERR_DEVICE_UNREACHABLE,
                        }
                    )
                    continue
                caps = []
                props = []
                reachable = True
                for item in cap_items:
                    topic = str(item.get("mqtt") or "")
                    raw = self._fresh_payload(
                        topic, age_retained=_modbus_slave_topic(topic)
                    )
                    if raw is None:
                        reachable = False
                        continue
                    block = converters.capability_mqtt_to_yandex(
                        str(item.get("type") or ""), raw, item.get("parameters"),
                        _item_inverted(item),
                    )
                    if block:
                        caps.append(block)
                for item in prop_items:
                    topic = str(item.get("mqtt") or "")
                    # A live slave can leave sticky per-channel `r` on an
                    # unfitted analogue (Carel outdoor/room: retained 0.0).
                    # That must not become a reported °C. Capabilities still
                    # use `_fresh_payload` / `_control_dead` so a busy coil
                    # does not mark the whole device unreachable.
                    if (self._control_error.get(topic) or "").strip():
                        continue
                    raw = self._fresh_payload(
                        topic, age_retained=_modbus_slave_topic(topic)
                    )
                    if raw is None:
                        continue
                    block = converters.property_mqtt_to_yandex(
                        str(item.get("type") or ""),
                        raw,
                        item.get("parameters"),
                        _item_scale(item),
                    )
                    if block:
                        props.append(block)
                entry: Dict[str, Any] = {"id": did, "capabilities": caps, "properties": props}
                if not reachable and not caps and not props:
                    entry["error_code"] = C.ERR_DEVICE_UNREACHABLE
                elif not cap_items and prop_items and not props:
                    if all(
                        self._fresh_payload(
                            str(item.get("mqtt") or ""),
                            age_retained=_modbus_slave_topic(
                                str(item.get("mqtt") or "")
                            ),
                        )
                        is None
                        for item in prop_items
                    ):
                        entry["error_code"] = C.ERR_DEVICE_UNREACHABLE
                out.append(entry)
        return out

    def take_unreachable_transitions(self) -> List[Dict[str, Any]]:
        """One DEVICE_UNREACHABLE stub per device that just went down.

        Query already returns the stub when the poller raised `/meta/error`
        or the cache never saw a successful poll. Compact/list surfaces never query, so
        this down-edge is what the 30 s snapshot and the MQTT error path
        push once — not every cadence tick, not a retained flood.
        """
        entries = self.query_devices()
        out: List[Dict[str, Any]] = []
        with self._lock:
            for entry in entries:
                did = str(entry.get("id") or "")
                if not did:
                    continue
                down = str(entry.get("error_code") or "") == C.ERR_DEVICE_UNREACHABLE
                was = self._announced_down.get(did, False)
                if down and not was:
                    self._announced_down[did] = True
                    out.append(
                        {
                            "id": did,
                            "capabilities": [],
                            "properties": [],
                            "error_code": C.ERR_DEVICE_UNREACHABLE,
                        }
                    )
                elif not down:
                    self._announced_down[did] = False
        return out

    def apply_actions(
        self, devices_payload: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
        """Apply Yandex action payload → (results, mqtt_publishes)."""
        results: List[Dict[str, Any]] = []
        publishes: List[Tuple[str, str]] = []
        with self._lock:
            for d in devices_payload or []:
                if not isinstance(d, dict):
                    continue
                did = str(d.get("id") or "")
                dev = self._devices_by_id.get(did)
                cap_results = []
                if not dev:
                    for cap in d.get("capabilities") or []:
                        if not isinstance(cap, dict):
                            continue
                        cap_results.append(
                            {
                                "type": cap.get("type"),
                                "state": cap.get("state") or {},
                                "status": C.STATUS_ERROR,
                                "error_code": C.ERR_DEVICE_UNREACHABLE,
                            }
                        )
                    results.append({"id": did, "capabilities": cap_results})
                    continue
                by_type = {
                    str(c.get("type")): c
                    for c in (dev.get("capabilities") or [])
                    if isinstance(c, dict)
                }
                for cap in d.get("capabilities") or []:
                    if not isinstance(cap, dict):
                        continue
                    ctype = str(cap.get("type") or "")
                    local = by_type.get(ctype)
                    if not local:
                        cap_results.append(
                            {
                                "type": ctype,
                                "state": cap.get("state") or {},
                                "status": C.STATUS_ERROR,
                                "error_code": C.ERR_INVALID_ACTION,
                            }
                        )
                        continue
                    if local.get("writable") is False:
                        # Latching DI / report-only capability: query and
                        # report stay, a command must not publish `/on`.
                        cap_results.append(
                            {
                                "type": ctype,
                                "state": cap.get("state") or {},
                                "status": C.STATUS_ERROR,
                                "error_code": C.ERR_INVALID_ACTION,
                            }
                        )
                        continue
                    topic = str(local.get("mqtt") or "")
                    polled = _modbus_slave_topic(topic)
                    # A Modbus write goes out unless the slave itself is
                    # down. Sticky per-channel `r` (busy bus / scenario
                    # burst) must not refuse the command.
                    if self._slave_down(topic) or (
                        not polled
                        and self._fresh_payload(topic, age_retained=False) is None
                        and (
                            self._control_dead(topic)
                            or self._mqtt_live.get(topic)
                        )
                    ):
                        cap_results.append(
                            {
                                "type": ctype,
                                "state": cap.get("state") or {},
                                "status": C.STATUS_ERROR,
                                "error_code": C.ERR_DEVICE_UNREACHABLE,
                            }
                        )
                        continue
                    current = self._mqtt_cache.get(topic)
                    payload, err = converters.capability_yandex_to_mqtt(
                        ctype,
                        cap.get("state") or {},
                        current_raw=current,
                        parameters=local.get("parameters"),
                        inverted=_item_inverted(local),
                    )
                    if err or payload is None:
                        cap_results.append(
                            {
                                "type": ctype,
                                "state": cap.get("state") or {},
                                "status": C.STATUS_ERROR,
                                "error_code": err or C.ERR_INTERNAL_ERROR,
                            }
                        )
                        continue
                    publishes.append((topic + "/on", payload))
                    self._mqtt_cache[topic] = payload
                    self._mqtt_ts[topic] = self._clock()
                    self._mqtt_live[topic] = True
                    self._note_slave_live(topic)
                    cap_results.append(
                        {
                            "type": ctype,
                            "state": cap.get("state") or {},
                            "status": C.STATUS_DONE,
                        }
                    )
                results.append({"id": did, "capabilities": cap_results})
        return results, publishes

    def state_blocks_for_topic(self, topic: str) -> List[Dict[str, Any]]:
        """Build partial device_state devices[] for a topic change."""
        out: List[Dict[str, Any]] = []
        with self._lock:
            # Retained fills the query cache; only a live MQTT echo is a
            # reportable change (callback_state). A quiet Modbus coil must
            # not emit here or Yandex gets a retained-burst flood.
            if _modbus_slave_topic(topic) and not self._mqtt_live.get(topic):
                return out
            raw = self._fresh_payload(topic, age_retained=_modbus_slave_topic(topic))
            if raw is None:
                return out
            for did, kind, item in self._topic_map.get(topic, []):
                if kind == "capability":
                    block = converters.capability_mqtt_to_yandex(
                        str(item.get("type") or ""), raw, item.get("parameters"),
                        _item_inverted(item),
                    )
                    if block:
                        out.append({"id": did, "capabilities": [block], "properties": []})
                else:
                    block = converters.property_mqtt_to_yandex(
                        str(item.get("type") or ""),
                        raw,
                        item.get("parameters"),
                        _item_scale(item),
                    )
                    if block:
                        out.append({"id": did, "capabilities": [], "properties": [block]})
        return out
