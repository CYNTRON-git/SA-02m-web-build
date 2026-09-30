"""The client's MQTT connection to the local broker (127.0.0.1:1883) — publish only.

READ-ONLY (Operator decision Q-E): this client subscribes to nothing, so no
MQTT message can reach the cloud through it. Every (re)connect calls
`on_connected`, which republishes the retained state the Publisher owns
(a broker restarted without persistence would otherwise lose it).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from . import constants as C

log = logging.getLogger("sa02m_homeconnect.mqtt")


def _default_client_factory() -> Any:
    import paho.mqtt.client as mqtt  # type: ignore

    try:  # paho-mqtt 2.x keeps the 1.x callback signatures behind this flag
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)  # type: ignore[attr-defined]
    except AttributeError:
        return mqtt.Client()


class MqttLink:
    def __init__(
        self,
        *,
        on_connected: Optional[Callable[[], Any]] = None,
        host: str = C.MQTT_HOST,
        port: int = C.MQTT_PORT,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._on_connected = on_connected
        self._connected = threading.Event()
        self._client = (client_factory or _default_client_factory)()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def set_on_connected(self, callback: Callable[[], Any]) -> None:
        self._on_connected = callback

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: Any, *_: Any) -> None:
        if rc not in (0, None) and str(rc) not in ("0", "Success"):
            log.error("MQTT connect refused: rc=%s", rc)
            return
        self._connected.set()
        log.info("MQTT connected to %s:%d", self._host, self._port)
        if self._on_connected is not None:
            try:
                self._on_connected()
            except Exception:  # never let a callback bug kill the network thread
                log.exception("MQTT on_connected callback failed")

    def _on_disconnect(self, client: Any, userdata: Any, rc: Any, *_: Any) -> None:
        self._connected.clear()
        if rc not in (0, None):
            log.warning("MQTT disconnected (rc=%s) — paho reconnects", rc)

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

    def publish(self, topic: str, payload: str, retain: bool) -> bool:
        """QoS 1. False when not connected or paho refused — the Publisher's
        cache republishes on the next connect."""
        if topic.endswith("/on"):
            raise ValueError("read-only client: no command topics (%r)" % topic)
        if not self._connected.is_set():
            return False
        info = self._client.publish(topic, payload, qos=C.MQTT_QOS, retain=retain)
        rc = getattr(info, "rc", info)
        return rc in (0, None)
