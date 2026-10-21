"""The recorder on the local broker (contract §MQTT; docs/MQTT_TOPICS.md).

* Intake: the two command topics only (`record/on`, `delete/on`). paho's
  network thread ENQUEUES (bounded, overflow dropped and counted); the
  daemon's main thread applies. The grammar is closed — `parse_record` /
  `parse_delete` are its one home.
* Retained guard: a command carrying the MQTT retain flag is never executed.
  It would re-record on every daemon start; it is logged once per topic and
  cleared with a zero-length retained publish. An EMPTY payload on a command
  topic is ignored silently — it is that clear (or anyone's) coming back.
* State: every control retained, QoS 1, cached and republished on each
  (re)connect; LWT `meta/error = "r"`.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
from collections import namedtuple
from typing import Any, Callable, Dict, Optional, Tuple

from . import constants as C

log = logging.getLogger("sa02m_audio.mqtt")

Inbound = namedtuple("Inbound", "topic payload retain")

_INT = re.compile(r"^[0-9]{1,6}$")
_ID = re.compile(C.ID_RE)

# Parsed command kinds.
CMD_START = "start"
CMD_STOP = "stop"
CMD_DELETE = "delete"
CMD_BAD = "bad"
CMD_IGNORE = "ignore"


def control_topic(name: str) -> str:
    return "%s/controls/%s" % (C.DEVICE_TOPIC, name)


def command_topic(name: str) -> str:
    return control_topic(name) + "/on"


COMMAND_TOPICS = {command_topic(n): n for n in C.COMMAND_CONTROLS}
# Absolute write the voice path may publish: /devices/<id>/controls/<name>/on
# `\Z`, not `$`: `$` would let a topic with a trailing newline through.
_ABS_COMMAND_RE = re.compile(
    r"^/devices/[A-Za-z0-9_.-]{1,64}/controls/[A-Za-z0-9_]{1,32}/on\Z"
)
META_ERROR_TOPIC = C.DEVICE_TOPIC + "/meta/error"


def parse_record(payload: str, *, default_s: int, max_s: int) -> Tuple[str, int]:
    """(kind, seconds). `1|on|true` = the default duration (an Alice-style
    switch and the rules engine's `pub` both send `1`); an integer
    2..max_s = that many seconds; anything above max_s is REFUSED, never
    clamped — a caller asking 60 s must learn it got nothing."""
    text = payload.strip()
    if text == "":
        return CMD_IGNORE, 0
    word = text.lower()
    if word in C.START_WORDS:
        return CMD_START, default_s
    if word in C.STOP_WORDS:
        return CMD_STOP, 0
    if _INT.match(text):
        seconds = int(text)
        if C.MIN_DURATION_S <= seconds <= max_s:
            return CMD_START, seconds
    return CMD_BAD, 0


def parse_delete(payload: str) -> Tuple[str, str]:
    """(kind, target): an id or `all`."""
    text = payload.strip()
    if text == "":
        return CMD_IGNORE, ""
    if text == C.DELETE_ALL or _ID.match(text):
        return CMD_DELETE, text
    return CMD_BAD, ""


def _control_meta(control: C.Control, order: int) -> list:
    readonly = control.name not in C.WRITABLE_CONTROLS
    blob: Dict[str, object] = {"type": control.wb_type, "readonly": readonly,
                               "order": order, "title": {"en": control.name}}
    subs = [("meta/type", control.wb_type), ("meta/readonly", "1" if readonly else "0"),
            ("meta/order", str(order))]
    if control.units:
        blob["units"] = control.units
        subs.append(("meta/units", control.units))
    return [("meta", json.dumps(blob, sort_keys=True))] + subs


def meta_topics() -> list:
    """(topic, payload) for the device meta and every control's meta."""
    blob = {"driver": C.DRIVER, "title": {"en": C.DEVICE_NAME, "ru": C.DEVICE_NAME}}
    out = [(C.DEVICE_TOPIC + "/meta", json.dumps(blob, ensure_ascii=False, sort_keys=True)),
           (C.DEVICE_TOPIC + "/meta/name", C.DEVICE_NAME),
           (C.DEVICE_TOPIC + "/meta/driver", C.DRIVER)]
    for order, control in enumerate(C.CONTROLS):
        base = control_topic(control.name)
        out.extend(("%s/%s" % (base, sub), value) for sub, value in _control_meta(control, order))
    return out


def _default_client_factory() -> Any:
    import paho.mqtt.client as mqtt  # type: ignore

    try:  # paho 2.x keeps the 1.x callback signatures behind this flag; the board has 1.6.1
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="")  # type: ignore[attr-defined]
    except AttributeError:
        return mqtt.Client(client_id="")


class Link:
    """The broker connection: subscribe the command topics, enqueue what
    arrives, publish retained state."""

    def __init__(self, inbox: "queue.Queue[Any]", *, host: str = C.MQTT_HOST,
                 port: int = C.MQTT_PORT,
                 client_factory: Optional[Callable[[], Any]] = None) -> None:
        self._inbox = inbox
        self._host = host
        self._port = port
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._retained: Dict[str, str] = {}
        self.dropped = 0
        self._client = (client_factory or _default_client_factory)()
        self._client.will_set(META_ERROR_TOPIC, "r", qos=C.MQTT_QOS, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: Any, *_: Any) -> None:
        if rc not in (0, None) and str(rc) not in ("0", "Success"):
            log.error("MQTT connect refused: rc=%s", rc)
            return
        self._connected.set()
        for topic in COMMAND_TOPICS:
            client.subscribe(topic, qos=C.MQTT_QOS)
        with self._lock:
            items = sorted(self._retained.items())
        for topic, payload in items:
            self._send(topic, payload, True)
        log.info("MQTT connected to %s:%d", self._host, self._port)

    def _on_disconnect(self, client: Any, userdata: Any, rc: Any, *_: Any) -> None:
        self._connected.clear()
        if rc not in (0, None):
            log.warning("MQTT disconnected (rc=%s) — paho reconnects", rc)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """paho network thread: enqueue only."""
        payload = msg.payload
        text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        item = Inbound(str(getattr(msg, "topic", "") or ""), text[:256], bool(getattr(msg, "retain", False)))
        try:
            self._inbox.put_nowait(item)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                log.warning("command inbox full: %d message(s) dropped", self.dropped)

    def _send(self, topic: str, payload: str, retain: bool) -> bool:
        if not self._connected.is_set():
            return False
        try:
            info = self._client.publish(topic, payload, qos=C.MQTT_QOS, retain=retain)
        except Exception as exc:  # a refused publish must not end the main loop
            log.warning("publish %s refused: %s", topic, exc)
            return False
        rc = getattr(info, "rc", info)
        return rc in (0, None)

    def put(self, topic: str, payload: str, *, force: bool = False) -> None:
        """Retained state: cached, sent when it changed (or `force`: an
        outcome that repeats is still a new outcome), re-sent on reconnect."""
        if topic.endswith("/on"):
            raise ValueError("state publish on a command topic: %r" % topic)
        with self._lock:
            if self._retained.get(topic) == payload and not force:
                return
            self._retained[topic] = payload
        self._send(topic, payload, True)

    def publish_command(self, topic: str, payload: str) -> bool:
        """Non-retained QoS 1 write to another device's `/on`.

        Returns False at once when the broker is down or the topic is not a
        device command. Does not wait for PUBACK and does not cache the value.
        """
        if not _ABS_COMMAND_RE.match(topic):
            log.warning("command topic refused: %s", topic[:80])
            return False
        if not self._connected.is_set():
            return False
        return self._send(topic, str(payload), False)

    def clear_retained_command(self, topic: str) -> None:
        """Zero-length retained publish: removes a retained `/on` from the broker."""
        if topic not in COMMAND_TOPICS:
            raise ValueError("not a command topic: %r" % topic)
        self._send(topic, "", True)

    def start(self) -> None:
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect_async(self._host, self._port, keepalive=C.MQTT_KEEPALIVE_S)
        self._client.loop_start()

    def stop(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
