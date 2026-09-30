"""A fake `paho.mqtt.client` at the callback layer — the broker that drops and
re-accepts a connection, for tests that must run without a socket.

Not a test module (discover ignores it); `test_mqtt_link.py` drives it through
`MqttLink(client_factory=…)` and `test_mqtt_reconnect_run.py` injects it as
the `paho.mqtt.client` module so the SAME `run()` harness works on the tree
before the fix (which imported paho lazily inside `_mqtt_client`) and after.

Fidelity, stated so nobody trusts it past this: the VERSION1 callback arity
(`on_connect(client, userdata, flags, rc)`, `on_disconnect(client, userdata,
rc)`, `on_message(client, userdata, msg)`), `subscribe()` → `(rc, mid)` with
`MQTT_ERR_NO_CONN` while down, `publish()` → an object with `.rc`. What it
cannot check — the real library's arity, its own reconnect thread, the wire —
is `test_mqtt_link_broker.py`'s job against the real paho.
"""

from __future__ import annotations

import types
from typing import Any, List, Optional, Tuple

MQTT_ERR_SUCCESS = 0
MQTT_ERR_NO_CONN = 4
MQTT_ERR_CONN_LOST = 7


class FakeInfo:
    def __init__(self, rc: int) -> None:
        self.rc = rc


class FakeMsg:
    def __init__(self, topic: str, payload: Any, retain: bool = False) -> None:
        self.topic = topic
        self.payload = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode("utf-8")
        self.retain = retain


class FakePahoClient:
    """Records what the client under test does; `accept()` / `drop()` /
    `deliver()` are the test's broker."""

    instances: List["FakePahoClient"] = []
    # The run() harness flips this on: a CONNACK fires from `loop_start()`,
    # the earliest moment the real network thread could deliver it. Unit tests
    # leave it off and fire `accept()` by hand.
    auto_connack = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.client_id = kwargs.get("client_id") or (args[1] if len(args) > 1 else "")
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.on_subscribe_hook = kwargs.pop("on_subscribe_hook", None)
        self.up = False
        self.calls: List[Tuple[Any, ...]] = []
        self.subscribed: List[Tuple[str, int]] = []
        self.unsubscribed: List[str] = []
        self.published: List[Tuple[str, Any, int, bool]] = []
        FakePahoClient.instances.append(self)

    # -- what paho offers -------------------------------------------------
    def connect(self, host: str, port: int = 1883, keepalive: int = 60, *_a: Any, **_k: Any) -> int:
        self.calls.append(("connect", host, port, keepalive))
        # The socket opens synchronously; the CONNACK arrives on the loop.
        self.up = True
        return MQTT_ERR_SUCCESS

    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> None:
        self.calls.append(("reconnect_delay_set", min_delay, max_delay))

    def loop_start(self) -> None:
        self.calls.append(("loop_start",))
        if FakePahoClient.auto_connack:
            self.accept()

    def loop_stop(self, *_a: Any, **_k: Any) -> None:
        self.calls.append(("loop_stop",))

    def disconnect(self, *_a: Any, **_k: Any) -> int:
        self.calls.append(("disconnect",))
        self.up = False
        return MQTT_ERR_SUCCESS

    def subscribe(self, topic: str, qos: int = 0, *_a: Any, **_k: Any) -> Tuple[int, Optional[int]]:
        if not self.up:
            return (MQTT_ERR_NO_CONN, None)
        if self.on_subscribe_hook is not None:
            self.on_subscribe_hook(topic)
        self.subscribed.append((topic, qos))
        return (MQTT_ERR_SUCCESS, len(self.subscribed))

    def unsubscribe(self, topic: str, *_a: Any, **_k: Any) -> Tuple[int, Optional[int]]:
        self.unsubscribed.append(topic)
        return (MQTT_ERR_SUCCESS if self.up else MQTT_ERR_NO_CONN, None)

    def publish(self, topic: str, payload: Any = None, qos: int = 0, retain: bool = False,
                *_a: Any, **_k: Any) -> FakeInfo:
        # paho keeps a QoS 1 message queued on NO_CONN and re-sends it after
        # the next CONNACK; recording it here is what lets a test assert the
        # command was never handed to the library at all.
        self.published.append((topic, payload, qos, retain))
        return FakeInfo(MQTT_ERR_SUCCESS if self.up else MQTT_ERR_NO_CONN)

    # -- the test's broker --------------------------------------------------
    def accept(self, session_present: int = 0, rc: int = 0) -> None:
        self.up = rc == 0
        if self.on_connect is not None:
            self.on_connect(self, None, {"session present": session_present}, rc)

    def drop(self, rc: int = MQTT_ERR_CONN_LOST) -> None:
        self.up = False
        if self.on_disconnect is not None:
            self.on_disconnect(self, None, rc)

    def deliver(self, topic: str, payload: Any, retain: bool = False) -> None:
        if self.on_message is not None:
            self.on_message(self, None, FakeMsg(topic, payload, retain))


def fake_paho_modules() -> dict:
    """`sys.modules` entries for `import paho.mqtt.client as mqtt`: the three
    package levels, attribute-linked so the `as` binding resolves either way."""
    paho = types.ModuleType("paho")
    mqtt = types.ModuleType("paho.mqtt")
    client = types.ModuleType("paho.mqtt.client")
    client.Client = FakePahoClient  # type: ignore[attr-defined]
    client.CallbackAPIVersion = types.SimpleNamespace(VERSION1=1, VERSION2=2)  # type: ignore[attr-defined]
    client.MQTT_ERR_SUCCESS = MQTT_ERR_SUCCESS  # type: ignore[attr-defined]
    client.MQTT_ERR_NO_CONN = MQTT_ERR_NO_CONN  # type: ignore[attr-defined]
    mqtt.client = client  # type: ignore[attr-defined]
    paho.mqtt = mqtt  # type: ignore[attr-defined]
    return {"paho": paho, "paho.mqtt": mqtt, "paho.mqtt.client": client}
