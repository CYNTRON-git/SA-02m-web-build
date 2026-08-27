"""Device registry: Alice device map ↔ MQTT topics."""

from __future__ import annotations

import threading
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..common import constants as C
from ..common.config_store import load_devices
from . import converters


class DeviceRegistry:
    """In-memory registry backed by sa02m-alice-devices.conf."""

    def __init__(self, devices_doc: Optional[Dict[str, Any]] = None) -> None:
        self._lock = threading.RLock()
        self._doc = devices_doc if devices_doc is not None else load_devices()
        self._mqtt_cache: Dict[str, str] = {}  # topic -> last value seen (retained included)
        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        self._devices_by_id: Dict[str, Dict[str, Any]] = {}
        self._topic_map: Dict[str, List[Tuple[str, str, Dict[str, Any]]]] = {}
        # topic -> list of (device_id, kind, item) where kind is capability|property
        rooms = {r.get("id"): r for r in self._doc.get("rooms", []) if isinstance(r, dict)}
        self._rooms = rooms
        for dev in self._doc.get("devices", []):
            if not isinstance(dev, dict) or not dev.get("id"):
                continue
            did = str(dev["id"])
            self._devices_by_id[did] = dev
            for kind, key in (("capability", "capabilities"), ("property", "properties")):
                for item in dev.get(key, []) or []:
                    if not isinstance(item, dict):
                        continue
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
        """
        with self._lock:
            self._mqtt_cache[topic] = payload
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

    def discovery_devices(self) -> List[Dict[str, Any]]:
        """Yandex discovery device list (no live state)."""
        out: List[Dict[str, Any]] = []
        with self._lock:
            for did, dev in self._devices_by_id.items():
                caps = []
                for item in dev.get("capabilities", []) or []:
                    if not isinstance(item, dict):
                        continue
                    block = {
                        "type": item.get("type"),
                        "retrievable": bool(item.get("retrievable", True)),
                        "reportable": bool(item.get("reportable", True)),
                    }
                    if item.get("parameters"):
                        block["parameters"] = item["parameters"]
                    caps.append(block)
                props = []
                for item in dev.get("properties", []) or []:
                    if not isinstance(item, dict):
                        continue
                    block = {
                        "type": item.get("type"),
                        "retrievable": bool(item.get("retrievable", True)),
                        "reportable": bool(item.get("reportable", True)),
                    }
                    if item.get("parameters"):
                        block["parameters"] = item["parameters"]
                    props.append(block)
                out.append(
                    {
                        "id": did,
                        "name": dev.get("name") or did,
                        "room": self.room_name(dev.get("room_id")),
                        "type": dev.get("type") or "devices.types.other",
                        "capabilities": caps,
                        "properties": props,
                    }
                )
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
                caps = []
                props = []
                reachable = True
                for item in dev.get("capabilities", []) or []:
                    if not isinstance(item, dict):
                        continue
                    topic = str(item.get("mqtt") or "")
                    raw = self._mqtt_cache.get(topic)
                    if raw is None:
                        reachable = False
                        continue
                    block = converters.capability_mqtt_to_yandex(
                        str(item.get("type") or ""), raw, item.get("parameters")
                    )
                    if block:
                        caps.append(block)
                for item in dev.get("properties", []) or []:
                    if not isinstance(item, dict):
                        continue
                    topic = str(item.get("mqtt") or "")
                    raw = self._mqtt_cache.get(topic)
                    if raw is None:
                        continue
                    block = converters.property_mqtt_to_yandex(
                        str(item.get("type") or ""), raw, item.get("parameters")
                    )
                    if block:
                        props.append(block)
                entry: Dict[str, Any] = {"id": did, "capabilities": caps, "properties": props}
                if not reachable and not caps and not props:
                    entry["error_code"] = C.ERR_DEVICE_UNREACHABLE
                out.append(entry)
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
                    topic = str(local.get("mqtt") or "")
                    current = self._mqtt_cache.get(topic)
                    payload, err = converters.capability_yandex_to_mqtt(
                        ctype,
                        cap.get("state") or {},
                        current_raw=current,
                        parameters=local.get("parameters"),
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
            raw = self._mqtt_cache.get(topic)
            if raw is None:
                return out
            for did, kind, item in self._topic_map.get(topic, []):
                if kind == "capability":
                    block = converters.capability_mqtt_to_yandex(
                        str(item.get("type") or ""), raw, item.get("parameters")
                    )
                    if block:
                        out.append({"id": did, "capabilities": [block], "properties": []})
                else:
                    block = converters.property_mqtt_to_yandex(
                        str(item.get("type") or ""), raw, item.get("parameters")
                    )
                    if block:
                        out.append({"id": did, "capabilities": [], "properties": [block]})
        return out
