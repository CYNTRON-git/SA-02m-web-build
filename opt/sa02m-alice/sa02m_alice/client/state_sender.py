"""Two-stage rate-limited device_state sender (clean-room algorithm)."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional


def _load_rates() -> Dict[str, Any]:
    path = os.path.join(os.path.dirname(__file__), "..", "common", "event_rates.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return {
            "capabilities": {"on_off": {"time_rate_s": 0.75}},
            "properties": {"float": {"time_rate_s": 300.0}, "event": {"time_rate_s": 0.01, "fast_batch_s": 0.1}},
            "batch": {"flush_normal_s": 1.0, "flush_fast_s": 0.1},
        }


def _instance_of(item: Dict[str, Any]) -> str:
    """The Yandex instance of a converted block, read from `state`.

    `state.instance` is emitted by every converter; `parameters` is not — a
    float block omits it when the unit is falsy. Keying on `parameters` would
    therefore collapse exactly the multi-property devices this key exists for.
    """
    if not isinstance(item, dict):
        return ""
    state = item.get("state")
    if not isinstance(state, dict):
        return ""
    return str(state.get("instance") or "")


class StateSender:
    """Stage1 per-topic rate → Stage2 batch flush → emit callback."""

    def __init__(
        self,
        emit: Callable[[Dict[str, Any]], None],
        rates: Optional[Dict[str, Any]] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._emit = emit
        self._rates = rates or _load_rates()
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._last_sent: Dict[str, float] = {}
        self._pending: Dict[str, Dict[str, Any]] = {}  # device_id -> merged device block
        self._fast = False
        self._stopped = True
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        with self._lock:
            if not self._stopped:
                return
            self._stopped = False
            self._thread = threading.Thread(target=self._loop, name="alice-state-sender", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._pending.clear()
            self._fast = False

    def _rate_for(self, kind: str, type_name: str) -> float:
        section = self._rates.get("capabilities" if kind == "capability" else "properties", {})
        short = type_name.rsplit(".", 1)[-1]
        conf = section.get(short) or section.get(type_name) or {}
        try:
            return float(conf.get("time_rate_s", 0.75))
        except (TypeError, ValueError):
            return 0.75

    def _is_event(self, type_name: str) -> bool:
        return type_name.endswith("event") or type_name == "devices.properties.event"

    def offer(self, devices: List[Dict[str, Any]]) -> None:
        """Accept partial device_state devices[] from registry."""
        now = self._clock()
        with self._lock:
            if self._stopped:
                return
            for dev in devices or []:
                if not isinstance(dev, dict) or not dev.get("id"):
                    continue
                did = str(dev["id"])
                allowed_caps = []
                for cap in dev.get("capabilities") or []:
                    if not isinstance(cap, dict):
                        continue
                    ctype = str(cap.get("type") or "")
                    # Keyed by (device, type, INSTANCE): the platform rate is a
                    # per-property limit, so two instances of one type on one
                    # device each get their own budget. Keyed by type alone,
                    # a multi-reading device refreshed one reading per window.
                    key = "c:%s:%s:%s" % (did, ctype, _instance_of(cap))
                    rate = self._rate_for("capability", ctype)
                    last = self._last_sent.get(key, 0.0)
                    if now - last < rate:
                        continue
                    self._last_sent[key] = now
                    allowed_caps.append(cap)
                allowed_props = []
                for prop in dev.get("properties") or []:
                    if not isinstance(prop, dict):
                        continue
                    ptype = str(prop.get("type") or "")
                    key = "p:%s:%s:%s" % (did, ptype, _instance_of(prop))
                    rate = self._rate_for("property", ptype)
                    last = self._last_sent.get(key, 0.0)
                    if now - last < rate:
                        continue
                    self._last_sent[key] = now
                    allowed_props.append(prop)
                    if self._is_event(ptype):
                        self._fast = True
                if not allowed_caps and not allowed_props:
                    continue
                merged = self._pending.setdefault(
                    did, {"id": did, "capabilities": [], "properties": []}
                )
                # last_value wins per (type, instance) — keyed on type alone,
                # two float blocks of one device collapsed into one and the
                # earlier reading was silently lost from the batch.
                def _merge(dst_key: str, items: List[Dict[str, Any]]) -> None:
                    by_key = {
                        (str(x.get("type")), _instance_of(x)): x
                        for x in merged[dst_key]
                        if isinstance(x, dict)
                    }
                    for it in items:
                        by_key[(str(it.get("type")), _instance_of(it))] = it
                    merged[dst_key] = list(by_key.values())

                _merge("capabilities", allowed_caps)
                _merge("properties", allowed_props)

    def flush_now(self) -> None:
        with self._lock:
            self._flush_unlocked()

    def _flush_unlocked(self) -> None:
        if not self._pending:
            self._fast = False
            return
        devices = list(self._pending.values())
        self._pending.clear()
        self._fast = False
        payload = {"ts": int(time.time()), "payload": {"devices": devices}}
        try:
            self._emit(payload)
        except Exception:
            pass

    def _loop(self) -> None:
        while True:
            with self._lock:
                if self._stopped:
                    return
                batch = self._rates.get("batch") or {}
                delay = float(batch.get("flush_fast_s", 0.1) if self._fast else batch.get("flush_normal_s", 1.0))
                if self._pending:
                    self._flush_unlocked()
            time.sleep(max(0.05, delay))
