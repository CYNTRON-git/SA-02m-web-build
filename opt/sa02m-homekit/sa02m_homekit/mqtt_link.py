"""The bridge's MQTT client on the local broker (127.0.0.1:1883).

Two rules this module exists to hold:

* **Subscribe inside `on_connect`.** A subscription made once after the first
  connect is lost when the broker restarts; paho reconnects silently and the
  bridge would then serve a frozen cache (the class the backlog records for
  the Alice client, `.ai-dev/backlog.md` — «Alice client never resubscribes
  after broker restart»). Every (re)connect re-subscribes the CURRENT set.
* **Writes are `<topic>/on`, QoS 1, never retained** — the write convention of
  docs/contracts/alice-mqtt-mapping.md §Topics (a retained command would be
  replayed on every reconnect of every client).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterable, Optional, Set

from . import constants as C

log = logging.getLogger("sa02m_homekit.mqtt")

MessageHandler = Callable[[str, str, bool], None]


def _default_client_factory() -> Any:
    import paho.mqtt.client as mqtt  # type: ignore

    try:  # paho-mqtt 2.x keeps the 1.x callback signatures behind this flag
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)  # type: ignore[attr-defined]
    except AttributeError:
        return mqtt.Client()


class MqttLink:
    def __init__(
        self,
        on_message: MessageHandler,
        topics: Iterable[str],
        *,
        host: str = C.MQTT_HOST,
        port: int = C.MQTT_PORT,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._handler = on_message
        self._host = host
        self._port = port
        self._lock = threading.Lock()
        self._topics: Set[str] = set(topics)
        self._connected = threading.Event()
        self._client = (client_factory or _default_client_factory)()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # paho callbacks (network thread) ------------------------------------
    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: Any, *_: Any) -> None:
        if rc not in (0, None) and str(rc) not in ("0", "Success"):
            log.error("MQTT connect refused: rc=%s", rc)
            return
        with self._lock:
            topics = sorted(self._topics)
        for topic in topics:
            client.subscribe(topic, qos=C.MQTT_QOS)
        self._connected.set()
        log.info("MQTT connected to %s:%d, subscribed %d topics", self._host, self._port, len(topics))

    def _on_disconnect(self, client: Any, userdata: Any, rc: Any, *_: Any) -> None:
        self._connected.clear()
        if rc not in (0, None):
            log.warning("MQTT disconnected (rc=%s) — paho reconnects and resubscribes", rc)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        try:
            payload = msg.payload.decode("utf-8", "replace") if isinstance(msg.payload, (bytes, bytearray)) \
                else str(msg.payload)
            self._handler(str(msg.topic), payload, bool(getattr(msg, "retain", False)))
        except Exception:  # never let a handler bug kill the network thread
            log.exception("MQTT message handler failed for %s", getattr(msg, "topic", "?"))

    # public API ---------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect_async(self._host, self._port, keepalive=C.MQTT_KEEPALIVE_S)
        self._client.loop_start()

    def stop(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    def set_topics(self, topics: Iterable[str]) -> None:
        """Replace the subscribed set: subscribe the new, unsubscribe the gone.
        The stored set is what the next `on_connect` re-subscribes."""
        new = set(topics)
        with self._lock:
            added = sorted(new - self._topics)
            removed = sorted(self._topics - new)
            self._topics = new
        if not self._connected.is_set():
            return
        for topic in added:
            self._client.subscribe(topic, qos=C.MQTT_QOS)
        for topic in removed:
            self._client.unsubscribe(topic)

    def publish_command(self, topic: str, payload: str) -> bool:
        """Publish one command (`topic` already ends in `/on`). False when the
        broker is not connected or paho refused it — the caller fails the HAP
        write instead of pretending it went out."""
        if not topic.endswith("/on"):
            raise ValueError("command topic must end in /on: %r" % topic)
        if not self._connected.is_set():
            return False
        info = self._client.publish(topic, payload, qos=C.MQTT_QOS, retain=False)
        rc = getattr(info, "rc", info)
        return rc in (0, None)
