"""Two-stage rate-limited device_state sender (clean-room algorithm)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from ..common import constants as C

log = logging.getLogger("sa02m_alice.sender")


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


def _norm_state(item: Dict[str, Any]) -> str:
    """The block's `state` as a canonical string — the value the batch carries,
    used to tell a changed capability from a repeat of the last sent one."""
    state = item.get("state") if isinstance(item, dict) else None
    try:
        return json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(state)


def _cap_key(did: str, cap: Dict[str, Any]) -> str:
    """Budget key of a capability block: (device, type, instance)."""
    return "c:%s:%s:%s" % (did, str(cap.get("type") or ""), _instance_of(cap))


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
        # Last SENT capability value per (device, type, instance), normalised
        # exactly as it goes into the batch. A live change whose value differs
        # from it bypasses the per-instance budget (B4, 1.0.6.26): a snapshot
        # stamps the budget too, so without this the MQTT echo of a tap that
        # lands inside on_off.time_rate_s after a cadence tick was dropped and
        # no `live` frame ever left the board.
        self._last_value: Dict[str, str] = {}
        # One pending batch PER ORIGIN (origin -> device_id -> merged block):
        # a live report and a cadence snapshot landing in the same flush window
        # go out as two payloads, so a tap-confirming `live` change is never
        # relabelled `snapshot` by a coincident cache push — the cloud side
        # reads the tag per its own contract (cloud-device-control.md).
        self._pending: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._fast = False
        # Capability keys admitted ONLY by the change-bypass (their rate window
        # had not elapsed). They wait for the NORMAL flush and never ride the
        # 0.1 s fast cadence an event property latches — that is what bounds
        # them, by construction, to one report per flush_normal_s per key
        # (B6, 1.0.6.26). `_last_normal_flush` is when the last FULL flush ran.
        self._hold_for_normal: Set[str] = set()
        self._last_normal_flush = self._clock()
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
            self._hold_for_normal.clear()
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
        """Accept partial device_state devices[] from registry (origin `live`)."""
        self._ingest(devices, bypass_rate=False, origin=C.ORIGIN_LIVE)

    def offer_snapshot(self, devices: List[Dict[str, Any]]) -> None:
        """Same merge as offer; skip the per-instance rate, still stamp last_sent.

        The stamp is deliberate for properties (a float sits in its 300 s
        window either way). For capabilities it no longer suppresses a CHANGED
        value — see the change-bypass in _ingest.

        Used after the MQTT retained-settle window and on the history cadence
        (`STATE_SNAPSHOT_S`) so reconnect and graphs both report the cache
        even when floats sit inside the live 300 s window. No-op if stopped.
        Tagged origin `snapshot` on the wire.
        """
        self._ingest(devices, bypass_rate=True, origin=C.ORIGIN_SNAPSHOT)

    def _ingest(
        self, devices: List[Dict[str, Any]], *, bypass_rate: bool, origin: str
    ) -> None:
        now = self._clock()
        with self._lock:
            if self._stopped:
                return
            pending = self._pending.setdefault(origin, {})
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
                    key = _cap_key(did, cap)
                    rate = self._rate_for("capability", ctype)
                    norm = _norm_state(cap)
                    # The budget suppresses a REPEAT of the value last sent; a
                    # value that changed goes out regardless (the tap's echo
                    # is exactly that) — but a report admitted by the bypass
                    # alone is scheduled on the NORMAL flush only (see _tick /
                    # _split_fast_lane), so the per-key ceiling is one such
                    # report per flush_normal_s (1.0 s) by construction, inside
                    # Yandex's 0.75 s on_off floor, even while an event
                    # property has the 0.1 s fast cadence latched.
                    changed = self._last_value.get(key) != norm
                    in_window = (now - self._last_sent.get(key, 0.0)) < rate
                    if not bypass_rate and in_window:
                        if not changed:
                            continue
                        self._hold_for_normal.add(key)
                    self._last_sent[key] = now
                    self._last_value[key] = norm
                    allowed_caps.append(cap)
                allowed_props = []
                for prop in dev.get("properties") or []:
                    if not isinstance(prop, dict):
                        continue
                    ptype = str(prop.get("type") or "")
                    key = "p:%s:%s:%s" % (did, ptype, _instance_of(prop))
                    rate = self._rate_for("property", ptype)
                    if not bypass_rate:
                        last = self._last_sent.get(key, 0.0)
                        if now - last < rate:
                            continue
                    self._last_sent[key] = now
                    allowed_props.append(prop)
                    if self._is_event(ptype):
                        self._fast = True
                if not allowed_caps and not allowed_props:
                    continue
                merged = pending.setdefault(
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

    def _has_pending(self) -> bool:
        return any(self._pending.values())

    def _split_fast_lane(self, bucket: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fast-cadence flush of one origin bucket: take everything EXCEPT the
        capabilities admitted by the change-bypass, which stay pending (and
        keep merging) until the next full flush."""
        out: List[Dict[str, Any]] = []
        for did in list(bucket.keys()):
            block = bucket[did]
            held: List[Dict[str, Any]] = []
            free: List[Dict[str, Any]] = []
            for cap in block.get("capabilities") or []:
                (held if _cap_key(did, cap) in self._hold_for_normal else free).append(cap)
            props = list(block.get("properties") or [])
            if free or props:
                out.append({"id": did, "capabilities": free, "properties": props})
            if held:
                bucket[did] = {"id": did, "capabilities": held, "properties": []}
            else:
                del bucket[did]
        return out

    def _flush_unlocked(self, *, fast_only: bool = False) -> None:
        """Emit what is due. `fast_only` (a 0.1 s step inside the normal
        window) leaves the bypass-admitted capabilities pending; a full flush
        (the normal cadence, or flush_now) emits everything."""
        if not self._has_pending():
            self._pending.clear()
            self._fast = False
            if not fast_only:
                self._hold_for_normal.clear()
                self._last_normal_flush = self._clock()
            return
        # Snapshot FIRST, live LAST — defence in depth, not a requirement:
        # the current cloud hub confirms a tap on the last `live` frame
        # (newer than the command, live value AND current value both equal
        # the commanded one), so push order does not affect confirmation
        # (the rule's one home: the cloud repo's
        # docs/contracts/cloud-device-control.md §Подтверждение). An older
        # hub kept only the last `origin` per device, and there a snapshot
        # carrying the tapped device (it is the whole cache) sent after the
        # live frame would have masked it. Only these two origins exist
        # (offer / offer_snapshot).
        order = [C.ORIGIN_SNAPSHOT, C.ORIGIN_LIVE]
        batches = []
        for origin in order:
            bucket = self._pending.get(origin)
            if not bucket:
                continue
            if fast_only:
                devices = self._split_fast_lane(bucket)
            else:
                devices = list(bucket.values())
                bucket.clear()
            if devices:
                batches.append((origin, devices))
        if not fast_only:
            self._pending.clear()
            self._hold_for_normal.clear()
            self._last_normal_flush = self._clock()
        self._fast = False
        ts = int(time.time())
        for origin, devices in batches:
            payload = {"ts": ts, "origin": origin, "payload": {"devices": devices}}
            try:
                self._emit(payload)
            except Exception as exc:
                # The emit callback already reports a dead socket; anything
                # else here is a bug worth a line, never a silent drop.
                log.error("device_state (%s, %d device(s)) emit failed: %s", origin, len(devices), exc)

    def _tick(self) -> float:
        """One scheduler step (caller holds the lock): flush what is due and
        return the sleep before the next step. Split out of _loop so the
        cadence runs on the injectable clock — the ceiling test drives it.

        `_fast` is read BEFORE the flush clears it, so an event burst keeps
        the 0.1 s cadence as long as events keep arriving. A fast step inside
        the normal window flushes the fast lane only (events, floats, rate-
        passed capabilities); once flush_normal_s has elapsed since the last
        full flush the step is a full flush and the held capabilities leave.
        """
        batch = self._rates.get("batch") or {}
        normal = float(batch.get("flush_normal_s", 1.0))
        fast = float(batch.get("flush_fast_s", 0.1))
        use_fast = bool(self._fast)
        fast_only = use_fast and (self._clock() - self._last_normal_flush) < normal
        if self._has_pending():
            self._flush_unlocked(fast_only=fast_only)
        return fast if use_fast else normal

    def _loop(self) -> None:
        while True:
            with self._lock:
                if self._stopped:
                    return
                delay = self._tick()
            time.sleep(max(0.05, delay))
