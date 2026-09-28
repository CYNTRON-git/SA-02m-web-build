"""Appliances as WB devices on the local broker (docs/MQTT_TOPICS.md convention).

Topics, all retained, QoS 1 (docs/contracts/home-connect.md §3):

* `/devices/hc-<id>/meta` — `{"driver":"sa02m-homeconnect","title":{…},"type":…}`
  plus the legacy subtopics `/meta/name`, `/meta/driver`;
* `/devices/hc-<id>/controls/<name>` — the value; its `/meta` JSON
  (`type`, `readonly: true`, `order`, `units`, `title`) and subtopics
  `/meta/type`, `/meta/readonly` = `1`, `/meta/order`, `/meta/units`;
* `/devices/hc-<id>/meta/error` — `"r"` while the appliance is disconnected
  or the cloud stream is down, `""` when live (P2).

READ-ONLY: nothing is subscribed, no `/on` topic exists. Controls come only
from mapping.MAPPING. The publisher keeps the retained state it owns in a
cache and republishes all of it on every broker (re)connect, so a broker that
restarted without persistence gets the state back.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import constants as C
from . import mapping

log = logging.getLogger("sa02m_homeconnect.publisher")

# publish(topic, payload, retain) -> sent?
PublishFn = Callable[[str, str, bool], bool]


def device_topic(dev_id: str) -> str:
    return "/devices/%s" % dev_id


def control_topic(dev_id: str, control: str) -> str:
    return "/devices/%s/controls/%s" % (dev_id, control)


def _control_meta(control: mapping.Control) -> List[Tuple[str, str]]:
    blob: Dict[str, object] = {
        "type": control.wb_type,
        "readonly": True,
        "order": mapping.CONTROL_ORDER[control.name],
        "title": {"en": control.name},
    }
    subs = [("meta/type", control.wb_type), ("meta/readonly", "1"),
            ("meta/order", str(mapping.CONTROL_ORDER[control.name]))]
    if control.units:
        blob["units"] = control.units
        subs.append(("meta/units", control.units))
    return [("meta", json.dumps(blob, ensure_ascii=False, sort_keys=True))] + subs


def all_topics(dev_id: str) -> List[str]:
    """Every topic this client may have published for `dev_id` (the closed set
    a cleanup clears)."""
    base = device_topic(dev_id)
    topics = [base + "/meta", base + "/meta/name", base + "/meta/driver", base + "/meta/error"]
    for control in mapping.MAPPING:
        ctopic = control_topic(dev_id, control.name)
        topics.append(ctopic)
        topics.extend("%s/%s" % (ctopic, sub) for sub, _ in _control_meta(control))
    return topics


class Publisher:
    def __init__(self, publish: PublishFn) -> None:
        self._publish = publish
        self._lock = threading.Lock()
        self._retained: Dict[str, str] = {}
        self._control_meta_done: set = set()

    def _put(self, topic: str, payload: str) -> None:
        with self._lock:
            if self._retained.get(topic) == payload:
                return
            self._retained[topic] = payload
        self._publish(topic, payload, True)

    def announce(self, dev_id: str, title: str, appliance_type: str) -> None:
        base = device_topic(dev_id)
        blob: Dict[str, object] = {"driver": C.DRIVER, "title": {"en": title, "ru": title}}
        if appliance_type:
            blob["type"] = appliance_type
        self._put(base + "/meta", json.dumps(blob, ensure_ascii=False, sort_keys=True))
        self._put(base + "/meta/name", title)
        self._put(base + "/meta/driver", C.DRIVER)

    def set_control(self, dev_id: str, name: str, payload: str) -> None:
        control = mapping.CONTROLS.get(name)
        if control is None:
            raise ValueError("control %r is not in mapping.MAPPING" % name)
        ctopic = control_topic(dev_id, name)
        key = (dev_id, name)
        if key not in self._control_meta_done:
            for sub, value in _control_meta(control):
                self._put("%s/%s" % (ctopic, sub), value)
            self._control_meta_done.add(key)
        self._put(ctopic, payload)

    def apply(self, dev_id: str, updates: Iterable[mapping.Update]) -> List[str]:
        names = []
        for name, payload in updates:
            self.set_control(dev_id, name, payload)
            names.append(name)
        return names

    def set_error(self, dev_id: str, failing: bool) -> None:
        self._put(device_topic(dev_id) + "/meta/error", "r" if failing else "")

    def error_of(self, dev_id: str) -> Optional[str]:
        with self._lock:
            return self._retained.get(device_topic(dev_id) + "/meta/error")

    def clear_device(self, dev_id: str) -> None:
        """Remove every retained topic of `dev_id` from the broker (empty
        retained payload), cached or not — the depair / unlink cleanup."""
        with self._lock:
            for topic in list(self._retained):
                if topic.startswith(device_topic(dev_id) + "/"):
                    del self._retained[topic]
            self._control_meta_done = {k for k in self._control_meta_done if k[0] != dev_id}
        for topic in all_topics(dev_id):
            self._publish(topic, "", True)

    def published_controls(self, dev_id: str) -> List[str]:
        with self._lock:
            prefix = device_topic(dev_id) + "/controls/"
            return sorted({t[len(prefix):] for t in self._retained
                           if t.startswith(prefix) and "/" not in t[len(prefix):]})

    def republish_all(self) -> int:
        with self._lock:
            items = sorted(self._retained.items())
        for topic, payload in items:
            self._publish(topic, payload, True)
        return len(items)
