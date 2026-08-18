"""MQTT publishing and the live value cache for the SA-02m bridge.

MQTTPublisher (Wiren Board conventions, availability tracking),
DeviceLiveCache (the /run snapshot mqtt_live.cgi reads), and the WB
precision/title helpers. Leaf module. Split out of modbus_mqtt_bridge.py
verbatim by the bridge decompose (backlog "Decompose worklist" — the entry
was the fastest-growing module across three audits).
"""

from __future__ import annotations

import json as _json
import os
import time
import threading
import logging
from pathlib import Path

import paho.mqtt.client as mqtt

log = logging.getLogger("bridge")

DEVICE_BASE = "/devices"
LIVE_CACHE_DIR = Path(os.environ.get("SA02M_MQTT_LIVE_CACHE", "/run/sa02m-modbus-mqtt"))


class DeviceLiveCache:
    """Снимок последних значений controls для быстрого mqtt_live.cgi (<10 ms)."""

    _lock = threading.Lock()
    _controls: dict[str, dict[str, str]] = {}
    _units: dict[str, dict[str, str]] = {}
    _errors: dict[str, dict[str, str]] = {}
    _sensor_types: dict[str, dict[str, str]] = {}
    _online: dict[str, bool] = {}

    @classmethod
    def set_online(cls, device_id: str, online: bool) -> None:
        with cls._lock:
            cls._online[device_id] = bool(online)

    @classmethod
    def set_control(cls, device_id: str, name: str, value: str) -> None:
        with cls._lock:
            cls._controls.setdefault(device_id, {})[name] = value

    @classmethod
    def set_unit(cls, device_id: str, name: str, units: str) -> None:
        if not units:
            return
        with cls._lock:
            cls._units.setdefault(device_id, {})[name] = units

    @classmethod
    def set_error(cls, device_id: str, name: str, err: str) -> None:
        with cls._lock:
            bucket = cls._errors.setdefault(device_id, {})
            if err:
                bucket[name] = err
            else:
                bucket.pop(name, None)

    @classmethod
    def set_sensor_type(cls, device_id: str, ai_index: int, code: int) -> None:
        with cls._lock:
            cls._sensor_types.setdefault(device_id, {})[f"ai_{ai_index}"] = str(code)

    @classmethod
    def get_sensor_type(cls, device_id: str, ai_index: int) -> int:
        """Код типа датчика AI-канала (для масштабирования событий FMB)."""
        with cls._lock:
            raw = cls._sensor_types.get(device_id, {}).get(f"ai_{ai_index}", 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def flush_file(cls, device_id: str) -> None:
        with cls._lock:
            controls = dict(cls._controls.get(device_id, {}))
            units = dict(cls._units.get(device_id, {}))
            errors = dict(cls._errors.get(device_id, {}))
            sensor_types = dict(cls._sensor_types.get(device_id, {}))
            online = bool(cls._online.get(device_id, True))
        if not controls and not units and not sensor_types:
            return
        try:
            LIVE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = LIVE_CACHE_DIR / f"{device_id}.json"
            tmp = path.with_suffix(".json.tmp")
            payload = {
                "ok": online,
                "device": device_id,
                "source": "cache",
                "controls": controls,
                "units": units,
                "errors": errors,
                "sensor_types": sensor_types,
                "ts": time.time(),
            }
            tmp.write_text(
                _json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            log.debug("live cache %s: %s", device_id, e)

# ── WB conventions: precision per units ───────────────────────────────────────
# /devices/.../controls/.../meta/precision (number of decimal places to display)
# Source: https://github.com/wirenboard/conventions
_PRECISION_BY_UNITS: dict[str, str] = {
    "°C":    "1",  "°F":    "1",
    "V":     "3",  "kV":    "3",  "mV": "1",
    "A":     "3",  "mA":    "2",
    "W":     "1",  "kW":    "3",
    "kWh":   "3",  "Wh":    "1",
    "var":   "1",  "kvar":  "3",
    "VA":    "1",  "kVA":   "3",
    "Hz":    "2",
    "%":     "1",  "%, RH": "1",
    "kPa":   "2",  "Pa":    "0",  "mbar": "1", "bar": "3", "mmHg": "0",
    "kΩ":    "2",  "Ω":     "1",
    "ppm":   "1",  "ppb":   "2",
    "mg/m³": "2",
    "IAQ":   "1",
    "cm":    "0",  "m":     "2",
}

def _ctrl_precision(units: str) -> str | None:
    """Return WB precision meta value for given units string, or None."""
    return _PRECISION_BY_UNITS.get(units)


# ── WB conventions: bilingual title helper ────────────────────────────────────
def _make_title(label_ru: str, label_en: str = "") -> str:
    """Return JSON bilingual title if both provided, else plain string."""
    if label_en:
        return _json.dumps({"ru": label_ru, "en": label_en}, ensure_ascii=False)
    return label_ru


# ── WB conventions: /meta JSON blob assembly ──────────────────────────────────
# Modern WB tooling (wb-mqtt-serial 2.x, HA autodiscovery via WB) reads the
# single retained /meta JSON blob rather than the individual /meta/<key>
# subtopics. We publish BOTH: the subtopics (legacy-compat + our own consumers)
# and, additively, the blob assembled from the SAME accumulated values so the
# two can never drift. The blob is typed to WB's JSON shape (readonly boolean,
# order/min/max/precision numeric, title/enum structured) while the subtopics
# stay byte-for-byte the string values they always were.
# Source: https://github.com/wirenboard/conventions

# Control-meta keys WB defines as JSON numbers (published as decimal strings).
_CTRL_META_NUMERIC_KEYS = ("min", "max", "order", "precision")
# Control-meta keys WB defines as structured JSON (published as a JSON string
# by _make_title / the enum builder, or as a plain string when single-valued).
_CTRL_META_STRUCTURED_KEYS = ("title", "enum")


def _num_or_str(value: str):
    """Return int/float for a numeric string, else the string unchanged."""
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _obj_or_str(value: str):
    """Return the parsed object for a JSON-object string, else the string."""
    try:
        parsed = _json.loads(value)
    except (TypeError, ValueError):
        return value
    return parsed if isinstance(parsed, dict) else value


def _control_meta_blob(meta: dict) -> dict:
    """Assemble the WB control /meta blob from the accumulated subtopic values.

    Same source dict that feeds the /meta/<key> subtopics — the blob is a typed
    mirror, never an independent copy, so the two can't drift.
    """
    blob: dict = {}
    for key, val in meta.items():
        if key == "readonly":
            blob[key] = (val == "1")
        elif key in _CTRL_META_NUMERIC_KEYS:
            blob[key] = _num_or_str(val)
        elif key in _CTRL_META_STRUCTURED_KEYS:
            blob[key] = _obj_or_str(val)
        else:
            blob[key] = val
    return blob


def _device_meta_blob(meta: dict) -> dict:
    """Assemble the WB device /meta blob ({driver, title:{ru,en}}) from the
    accumulated device-meta subtopic values (name → title, driver → driver)."""
    blob: dict = {}
    driver = meta.get("driver")
    if driver is not None:
        blob["driver"] = driver
    name = meta.get("name")
    if name is not None:
        blob["title"] = {"ru": name, "en": name}
    return blob


# ── MQTTPublisher ──────────────────────────────────────────────────────────────
class MQTTPublisher:
    """Wiren Board MQTT publisher with availability tracking (wb-mqtt-serial style).

    Reliability features modelled on wb-mqtt-serial:
      * Last Will Testament — broker marks the bridge device offline if the
        process crashes or loses its connection, so consumers never trust
        stale retained data.
      * Per-device availability — a whole device is flagged offline via
        ``/devices/<id>/meta/error = "r"`` when it stops answering, and cleared
        on recovery (driven by the pollers' error back-off state machine).
      * Bridge status device — ``/devices/<bridge_id>/...`` exposes connection
        state and online/total device counters for monitoring.
    """

    def __init__(self, cfg: dict):
        self._broker = cfg.get("broker", "127.0.0.1")
        self._port   = int(cfg.get("port", 1883))
        self._client_id = cfg.get("client_id", "sa02m-modbus-bridge")
        self._qos    = int(cfg.get("qos", 1))
        self._retain = bool(cfg.get("retain", True))
        self._reconnect_delay = int(cfg.get("reconnect_delay_s", 5))
        self._availability = bool(cfg.get("availability", True))
        self._bridge_id = cfg.get("bridge_device_id", "sa02m-bridge")
        self._username = cfg.get("username") or None
        self._password = cfg.get("password") or None
        self._lock   = threading.Lock()
        # D2 аудита (PublishSomeUnchanged wb-mqtt-serial): control публикуется
        # при изменении значения или раз в max_unchanged_interval; в live-кэш
        # значение пишется всегда. 0 — публиковать каждый полл (как раньше).
        self._unchanged_republish_s = float(
            cfg.get("max_unchanged_interval",
                    os.environ.get("SA02M_MQTT_UNCHANGED_REPUBLISH_S", "60")))
        self._last_pub: dict[tuple[str, str], tuple[str, float]] = {}

        # Availability bookkeeping
        self._device_online: dict[str, bool] = {}
        self._poll_errors = 0
        self._bridge_meta_done = False
        # Последнее meta/error по каналу — не дублировать пустое «сброс ошибки» в MQTT.
        self._ctrl_errors: dict[tuple[str, str], str] = {}
        # WB /meta JSON blob: accumulate the per-control / per-device meta values
        # as their subtopics are published, then publish the assembled blob (the
        # *_pub caches dedup so a re-published identical blob is skipped). Fed by
        # the SAME values as the subtopics — see _control_meta_blob.
        self._ctrl_meta: dict[tuple[str, str], dict[str, str]] = {}
        self._ctrl_meta_pub: dict[tuple[str, str], str] = {}
        self._dev_meta: dict[str, dict[str, str]] = {}
        self._dev_meta_pub: dict[str, str] = {}

        try:
            # paho-mqtt >= 2.0: use VERSION2 to avoid deprecation warning
            self._client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=self._client_id,
            )
        except (AttributeError, TypeError):
            # paho-mqtt < 2.0
            self._client = mqtt.Client(client_id=self._client_id)
        if self._username:
            self._client.username_pw_set(self._username, self._password)
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        # Last Will: MQTT allows exactly ONE will per connection, so use the
        # bridge device-level error as the unified offline signal — a monitor
        # watching /devices/+/meta/error catches a bridge crash the same way it
        # catches a single device going offline. ``connection`` is published
        # actively (1 while running, 0 on graceful stop).
        if self._availability:
            self._client.will_set(
                f"{DEVICE_BASE}/{self._bridge_id}/meta/error", "r",
                qos=1, retain=True,
            )
        self._connected = False
        # Track subscriptions for re-subscribe on reconnect
        self._subscriptions: dict[str, callable] = {}

    @property
    def bridge_id(self) -> str:
        return self._bridge_id

    # paho-mqtt v1: (client, userdata, flags, rc)
    # paho-mqtt v2: (client, userdata, connect_flags, reason_code, properties)
    # Using *_ absorbs the extra `properties` arg in v2.
    def _on_connect(self, client, userdata, flags, rc, *_):
        failed = rc.is_failure if hasattr(rc, "is_failure") else bool(rc)
        if failed:
            log.warning("MQTT connect failed: %s", rc)
            return
        self._connected = True
        log.info("MQTT connected to %s:%d", self._broker, self._port)
        for topic, cb in self._subscriptions.items():
            client.subscribe(topic, qos=1)
            client.message_callback_add(topic, cb)
        if self._availability:
            self._publish_bridge_status(online=True)

    # paho-mqtt v1: (client, userdata, rc)
    # paho-mqtt v2: (client, userdata, disconnect_flags, reason_code, properties)
    def _on_disconnect(self, client, userdata, flags_or_rc, *extra):
        self._connected = False
        rc = extra[0] if extra else flags_or_rc
        unexpected = rc.is_failure if hasattr(rc, "is_failure") else bool(rc)
        if unexpected:
            log.warning("MQTT unexpected disconnect: %s — reconnecting", rc)

    def connect(self) -> None:
        while True:
            try:
                self._client.connect(self._broker, self._port, keepalive=60)
                self._client.loop_start()
                # Wait up to 5s for connection
                for _ in range(50):
                    if self._connected:
                        return
                    time.sleep(0.1)
                return
            except Exception as e:
                log.error("MQTT connect error: %s — retry in %ds", e, self._reconnect_delay)
                time.sleep(self._reconnect_delay)

    def pub(self, topic: str, payload: str, retain: bool | None = None) -> None:
        r = self._retain if retain is None else retain
        try:
            self._client.publish(topic, payload, qos=self._qos, retain=r)
        except Exception as e:
            log.debug("MQTT publish %s: %s", topic, e)

    def pub_meta(self, device_id: str, key: str, value: str) -> None:
        self.pub(f"{DEVICE_BASE}/{device_id}/meta/{key}", value, retain=True)
        self._publish_device_meta_blob(device_id, key, value)

    def pub_control(self, device_id: str, name: str, value: str,
                    force: bool = False) -> None:
        DeviceLiveCache.set_control(device_id, name, value)
        if self._unchanged_republish_s > 0 and not force:
            key = (device_id, name)
            now = time.monotonic()
            with self._lock:
                prev = self._last_pub.get(key)
                if (prev is not None and prev[0] == value
                        and now - prev[1] < self._unchanged_republish_s):
                    return
                self._last_pub[key] = (value, now)
        elif force:
            with self._lock:
                self._last_pub[(device_id, name)] = (value, time.monotonic())
        self.pub(f"{DEVICE_BASE}/{device_id}/controls/{name}", value)

    def pub_control_meta(self, device_id: str, name: str,
                         key: str, value: str) -> None:
        self.pub(f"{DEVICE_BASE}/{device_id}/controls/{name}/meta/{key}",
                 value, retain=True)
        self._publish_control_meta_blob(device_id, name, key, value)

    def _publish_control_meta_blob(self, device_id: str, name: str,
                                   key: str, value: str) -> None:
        """Additively publish the WB control /meta JSON blob alongside the
        /meta/<key> subtopics — assembled from the same accumulated values,
        deduped, retained (matching the subtopic retain flag)."""
        topic_key = (device_id, name)
        with self._lock:
            meta = self._ctrl_meta.setdefault(topic_key, {})
            meta[key] = value
            blob = _json.dumps(_control_meta_blob(meta),
                               ensure_ascii=False, sort_keys=True)
            if self._ctrl_meta_pub.get(topic_key) == blob:
                return
            self._ctrl_meta_pub[topic_key] = blob
        self.pub(f"{DEVICE_BASE}/{device_id}/controls/{name}/meta",
                 blob, retain=True)

    def _publish_device_meta_blob(self, device_id: str,
                                  key: str, value: str) -> None:
        """Additively publish the WB device /meta JSON blob ({driver,title})
        alongside the /meta/<key> subtopics — same accumulated values, deduped,
        retained. Keeps the existing /meta/name and /meta/driver subtopics."""
        with self._lock:
            meta = self._dev_meta.setdefault(device_id, {})
            meta[key] = value
            blob_obj = _device_meta_blob(meta)
            if not blob_obj:            # no driver/name yet — nothing to publish
                return
            blob = _json.dumps(blob_obj, ensure_ascii=False, sort_keys=True)
            if self._dev_meta_pub.get(device_id) == blob:
                return
            self._dev_meta_pub[device_id] = blob
        self.pub(f"{DEVICE_BASE}/{device_id}/meta", blob, retain=True)

    def pub_control_units(self, device_id: str, name: str, units: str) -> None:
        """Publish units + auto precision (WB conventions)."""
        if units:
            DeviceLiveCache.set_unit(device_id, name, units)
            self.pub_control_meta(device_id, name, "units", units)
            prec = _ctrl_precision(units)
            if prec is not None:
                self.pub_control_meta(device_id, name, "precision", prec)

    def pub_error(self, device_id: str, name: str, error: str) -> None:
        err = error if error else ""
        key = (device_id, name)
        with self._lock:
            prev = self._ctrl_errors.get(key)
            if prev == err:
                return
            if err == "" and prev is None:
                return
            self._ctrl_errors[key] = err
        self.pub(f"{DEVICE_BASE}/{device_id}/controls/{name}/meta/error",
                 err, retain=True)
        DeviceLiveCache.set_error(device_id, name, err)

    def pub_device_error(self, device_id: str, error: str) -> None:
        """Device-level error flag (wb-mqtt-serial: whole device offline = "r")."""
        self.pub(f"{DEVICE_BASE}/{device_id}/meta/error", error, retain=True)

    # --- Availability registry -------------------------------------------------

    def register_device(self, device_id: str) -> None:
        with self._lock:
            self._device_online.setdefault(device_id, True)
        # Clear any stale retained device-level meta/error="r" left by a prior
        # process's shutdown()/LWT. Devices default online=True at startup, so a
        # device that stays healthy across a restart never transitions and would
        # never clear the stale "r" — the retained tree would keep lying offline
        # while the bridge reports devices_online==total, flooding the cloud
        # watchdog with false incidents (bench-confirmed). Published once per
        # device at registration (no dedup in pub_device_error, but this fires
        # exactly once per process); a genuinely-offline device re-asserts "r"
        # via the back-off machine after offline_after_fails polls.
        if self._availability:
            self.pub_device_error(device_id, "")

    def device_online(self, device_id: str, online: bool) -> None:
        """Update one device's online state; refresh bridge counters on change."""
        with self._lock:
            changed = self._device_online.get(device_id) != online
            self._device_online[device_id] = online
        if not online:
            with self._lock:
                self._poll_errors += 1
        if changed and self._availability:
            self.pub_device_error(device_id, "" if online else "r")
            self._publish_bridge_status(online=True)

    def device_online_snapshot(self) -> dict:
        """Copy of the per-device online map (device_id → bool) for the roster writer."""
        with self._lock:
            return dict(self._device_online)

    def _publish_bridge_status(self, online: bool) -> None:
        if not self._availability:
            return
        if not self._bridge_meta_done:
            self.pub_meta(self._bridge_id, "name", "SA-02m Modbus→MQTT bridge")
            self.pub_meta(self._bridge_id, "driver", "sa02m-modbus-mqtt")
            for ctrl, ctype in (("connection", "switch"),
                                ("devices_total", "value"),
                                ("devices_online", "value"),
                                ("poll_errors", "value")):
                self.pub_control_meta(self._bridge_id, ctrl, "type", ctype)
                self.pub_control_meta(self._bridge_id, ctrl, "readonly", "1")
            self._bridge_meta_done = True
        with self._lock:
            total = len(self._device_online)
            up = sum(1 for v in self._device_online.values() if v)
            errors = self._poll_errors
        self.pub(f"{DEVICE_BASE}/{self._bridge_id}/controls/connection",
                 "1" if online else "0", retain=True)
        self.pub_control(self._bridge_id, "devices_total", str(total))
        self.pub_control(self._bridge_id, "devices_online", str(up))
        self.pub_control(self._bridge_id, "poll_errors", str(errors))
        self.pub_device_error(self._bridge_id, "" if online else "r")

    def announce_bridge(self) -> None:
        self._publish_bridge_status(online=True)

    def shutdown(self, device_ids: list[str]) -> None:
        """Graceful offline: mark bridge + all devices offline, then disconnect."""
        if self._availability:
            for did in device_ids:
                self.pub_device_error(did, "r")
            self._publish_bridge_status(online=False)
        time.sleep(0.2)   # let final publishes flush
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    def subscribe_writeback(self, device_id: str, name: str, callback) -> None:
        topic = f"{DEVICE_BASE}/{device_id}/controls/{name}/on"
        self._subscriptions[topic] = callback
        self._client.subscribe(topic, qos=1)
        self._client.message_callback_add(topic, callback)
