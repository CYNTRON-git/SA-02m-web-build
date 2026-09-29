"""The client's link to the local broker: re-subscribe on every reconnect,
refuse a command while deaf.

paho brings the SOCKET back by itself after a broker restart (`loop_start`
runs `loop_forever(retry_first_connection=True)`, which re-dials on any loss);
the SUBSCRIPTIONS do not come back — `reconnect()` re-sends pending publishes
only. A client that subscribed once after its first connect therefore serves
a frozen cache for hours after «Стоп/Пуск» on the Mosquitto row, the image-prep
step, or an apt restart. And a QoS 1 `publish()` on a disconnected client
returns `MQTT_ERR_NO_CONN` while KEEPING the message for the next CONNACK, so
«выключи насос» spoken during the outage was answered `DONE` and executed
minutes later. Both are this module's to close; the contract is
docs/contracts/alice-mqtt-mapping.md §Broker reconnect.

Two rules the shape is built around:

* **A subscribe pass counts only if it ran whole inside one live session.**
  On a normal start the main thread's initial pass owns the first subscribe —
  after `sio.connect()` + `registry.reload()`, under the global retained
  window, exactly as shipped in 1.0.6.16/1.0.6.19 — so a CONNACK #1 that
  arrives before ANY pass subscribes nothing and the settle window is
  untouched. Every other CONNACK re-subscribes the CURRENT set (the provider
  is read at that moment, not at construction): every CONNACK past the
  first, and a CONNACK #1 that follows a pass made before it. That last case
  is the broker closing the fresh socket before its first CONNACK: the pass
  got NO_CONN — or, worse, paho accepted the SUBSCRIBEs into a socket that
  was already dying — and trusting it left the client deaf behind a status
  file that said `mqtt_connected: true` (review 1.0.6.66, B1). A pass that
  overlaps a CONNACK (failures before it, the CONNACK landing mid-pass) is
  re-run once by whoever finishes second, so neither thread can leave the
  other's topics unsubscribed.
* **Every paho callback catches its own exceptions.** A raising callback is
  re-raised into the network thread unless `suppress_exceptions`, which kills
  `loop_forever` — MQTT dead for good, worse than the defect. `*_` in the
  signatures absorbs the extra argument paho-mqtt 2.x passes on MQTTv5.

Same rule as `reload_watch.py`: nothing here imports `main`, every unit is
testable with a fake client (`tests/fake_paho.py`); the real library and its
own reconnect thread are exercised by `tests/test_mqtt_link_broker.py`.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterable, Optional, Tuple

from .reload_watch import RetainedGrace

# The bridge's values (opt/sa02m-modbus-mqtt/bridge_mqtt.py): paho's default
# ladder climbs to 120 s, which is longer than the outage it recovers from.
RECONNECT_MIN_DELAY_S = 1
RECONNECT_MAX_DELAY_S = 30
KEEPALIVE_S = 60


class MqttUnavailable(RuntimeError):
    """The broker refused or was unreachable at connect time. Names host:port
    so the journal and `status.message` stop reading like a gateway failure."""


def _paho_client(client_id: str) -> Any:
    try:
        import paho.mqtt.client as mqtt  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "paho-mqtt is not installed (required when client_enabled=true)"
        ) from exc
    # paho-mqtt 2.x keeps the 1.x callback signatures behind this flag; 1.x
    # has no such enum and takes client_id first.
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)  # type: ignore[attr-defined]
    except Exception:
        return mqtt.Client(client_id=client_id)


def _rc_value(rc: Any) -> int:
    """paho passes an int on MQTTv3 and a ReasonCode (with `.value`) on v5."""
    value = getattr(rc, "value", rc)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0 if value in (None, "Success") else 1


class MqttLink:
    """One broker connection with the reconnect discipline above.

    `topics` is read at every reconnect (the current catalogue + availability
    + watch topics — the same union the initial pass subscribes); `grace` is
    armed for the whole set BEFORE the first subscribe goes out, the
    `apply_reload` ordering rule; `on_message` is paho-shaped
    (`client, userdata, msg`) so the caller's handler stays as it is.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        topics: Callable[[], Iterable[str]],
        grace: RetainedGrace,
        grace_s: float,
        on_message: Callable[[Any, Any, Any], None],
        log: Optional[logging.Logger] = None,
        client_id: str = "",
        client_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._host = host
        self._port = int(port)
        self._topics = topics
        self._grace = grace
        self._grace_s = float(grace_s)
        self._handler = on_message
        self._log = log or logging.getLogger("sa02m_alice.client")
        self._client_id = client_id
        self._factory = client_factory or _paho_client
        self._client: Any = None
        # Set on CONNACK rc 0, cleared in on_disconnect and by close(). The
        # main thread reads it once per tick and before a command. The pass
        # bookkeeping below is decided against it under `_lock` — see
        # `subscribe_all` / `_on_connect` for the hand-off.
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._connacks = 0
        # True once any subscribe pass has run on this link — what tells a
        # CONNACK #1 «a pass already went out before me, redo it» from «no
        # pass yet, the main thread's initial pass is still to come».
        self._pass_ran = False
        self._resubscribes = 0

    # -- lifecycle (main thread) ----------------------------------------------
    def connect(self) -> "MqttLink":
        client = self._factory(self._client_id)
        # Callbacks BEFORE connect(): the CONNACK is read by the loop thread,
        # but the assignment must not race the thread's start.
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=RECONNECT_MIN_DELAY_S, max_delay=RECONNECT_MAX_DELAY_S)
        try:
            client.connect(self._host, self._port, keepalive=KEEPALIVE_S)
        except OSError as exc:
            raise MqttUnavailable(
                "MQTT broker %s:%d unreachable: %s" % (self._host, self._port, exc)
            ) from exc
        self._client = client
        client.loop_start()
        return self

    def close(self) -> None:
        """Idempotent, never raises. disconnect() first so the loop thread
        exits on its own state instead of waiting out a select timeout."""
        client, self._client = self._client, None
        self._connected.clear()
        if client is None:
            return
        for step in (client.disconnect, client.loop_stop):
            try:
                step()
            except Exception:
                pass

    # -- state (any thread) ---------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def reconnects(self) -> int:
        """CONNACKs past the first on this link — the `mqtt_reconnects`
        status counter. A fresh link (after a gateway re-dial) starts at 0."""
        with self._lock:
            return max(0, self._connacks - 1)

    @property
    def resubscribes(self) -> int:
        """Full passes made from `on_connect` on this link. The main thread
        pushes one snapshot, after the grace, each time this moves."""
        with self._lock:
            return self._resubscribes

    # -- subscriptions (main thread on the initial pass, paho thread after) ---
    def subscribe_all(self) -> int:
        """Subscribe the CURRENT set at QoS 1, grace armed for all of it first.
        Returns the count the client accepted; a refused topic is logged.

        The pass is TRUSTED only if the whole of it ran inside one live
        session — connected at the start, the same CONNACK at the end, every
        topic accepted. An untrusted pass that ends while connected (a CONNACK
        landed during it) is re-run once here; one that ends while down is
        left to the CONNACK that follows, which sees `_pass_ran`."""
        accepted, total, retry = self._pass()
        if retry:
            self._log.info(
                "MQTT subscribe pass overlapped a (re)connect (%d/%d accepted); re-running it",
                accepted, total)
            accepted, total, _retry = self._pass()
        return accepted

    def _pass(self) -> Tuple[int, int, bool]:
        with self._lock:
            start_n = self._connacks
            start_up = self._connected.is_set()
        topics = sorted(set(self._topics()))
        accepted = 0
        if topics:
            # ARM BEFORE SUBSCRIBE — the broker can deliver the retained burst
            # on the network thread before subscribe() returns (reload_watch).
            self._grace.arm(topics, self._grace_s)
            for topic in topics:
                try:
                    rc, _mid = self._client.subscribe(topic, qos=1)
                except Exception as exc:
                    self._log.error("MQTT subscribe failed for %s: %s", topic, exc)
                    continue
                if rc != 0:
                    self._log.warning("MQTT subscribe refused for %s (rc=%s)", topic, rc)
                    continue
                accepted += 1
        with self._lock:
            # Decided under the same lock `_on_connect` takes to count a
            # CONNACK and read `_pass_ran`: either that CONNACK sees this pass
            # and redoes it, or this pass sees that CONNACK and re-runs.
            self._pass_ran = True
            now_up = self._connected.is_set()
            trusted = (start_up and now_up and self._connacks == start_n
                       and accepted == len(topics))
            retry = not trusted and now_up
        return accepted, len(topics), retry

    def subscribe(self, topic: str, qos: int = 1) -> Any:
        """Pass-through for apply_reload and the auto-provision extra."""
        return self._client.subscribe(topic, qos=qos)

    def unsubscribe(self, topic: str) -> Any:
        return self._client.unsubscribe(topic)

    # -- commands (Socket.IO thread) ------------------------------------------
    def publish_command(self, topic: str, payload: str) -> None:
        """QoS 1, never retained; raises instead of queueing while deaf.

        Residual, named: a drop landing between the check and paho's own
        socket test leaves the message in paho's queue (it re-sends after the
        next CONNACK). On the loopback broker paho notices a closed socket
        within its select wake-up, so the window is milliseconds wide.
        """
        if not self._connected.is_set():
            raise ConnectionError("MQTT broker not connected")
        info = self._client.publish(topic, payload, qos=1, retain=False)
        rc = _rc_value(getattr(info, "rc", 0))
        if rc != 0:
            raise ConnectionError("MQTT publish refused (rc=%s)" % rc)

    # -- paho callbacks (network thread) --------------------------------------
    def _on_connect(self, client: Any, _userdata: Any, flags: Any, rc: Any, *_: Any) -> None:
        try:
            if _rc_value(rc) != 0:
                self._log.warning("MQTT CONNACK refused (rc=%s) by %s:%d", rc, self._host, self._port)
                return
            with self._lock:
                self._connacks += 1
                n = self._connacks
                self._connected.set()
                redo = n > 1 or self._pass_ran
            if not redo:
                # No pass yet: the main thread's initial pass subscribes.
                return
            count = self.subscribe_all()
            with self._lock:
                self._resubscribes += 1
            if isinstance(flags, dict):
                session_present = flags.get("session present", 0)
            else:
                session_present = getattr(flags, "session_present", 0)
            self._log.info(
                "MQTT reconnected (#%d, session_present=%s): resubscribed %d topics",
                n, int(bool(session_present)), count,
            )
        except Exception:
            self._log.exception("MQTT on_connect handler failed")

    def _on_disconnect(self, _client: Any, _userdata: Any, rc: Any, *_: Any) -> None:
        try:
            self._connected.clear()
            if _rc_value(rc) != 0:
                self._log.warning("MQTT disconnected (rc=%s); paho reconnects with backoff", rc)
        except Exception:
            self._log.exception("MQTT on_disconnect handler failed")

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        try:
            self._handler(client, userdata, msg)
        except Exception:
            self._log.exception("MQTT message handler failed for %s", getattr(msg, "topic", "?"))
