"""MQTT loop: subscribe the state mirror + our own command level, publish
.../on, no retain.

Threading: paho's network thread only ENQUEUES inbound messages; the main
loop drains them inside tick(), so every Engine mutation (heap, trackers,
doc adoption) happens on one thread (verdict A12 — the package holds no
lock, and none is needed while this stays true).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import signal
import sys
import time
from typing import Any, Dict

LOG = logging.getLogger("sa02m-rules")
#: Inbound MQTT messages waiting for the next tick; a burst past this is
#: dropped (counted in the log) rather than growing without bound.
INBOX_MAX = 4096
#: Topics remembered for once-per-topic warnings (a non-finite reading, a
#: message that raised). Topics are LAN-controlled, so the memory is capped;
#: past it a new topic is simply not warned about again.
WARNED_TOPICS_MAX = 256


def _sampled(n: int) -> bool:
    """Repeat-failure log sampling: the 100th, 1000th, then every 10000th."""
    return n in (100, 1000) or n % 10000 == 0


# Package lives next to this file when installed to /opt/sa02m-rules.
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# load_mqtt_index is re-exported: callers and tests read the index from here.
from sa02m_rules.device_index import (  # noqa: F401
    DeviceIndex, cap_short, devices_path, load_index, load_mqtt_index, state_key)
from sa02m_rules.engine import Engine, non_finite
from sa02m_rules import capability_binding
from sa02m_rules.store import CAP_RE, DEFAULT_PATH, ID_RE, load

MQTT_HOST = os.environ.get("SA02M_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("SA02M_MQTT_PORT", "1883"))
LAT = float(os.environ.get("SA02M_LAT", "55.75"))
LON = float(os.environ.get("SA02M_LON", "37.62"))
PATH = os.environ.get("SA02M_RULES_PATH", DEFAULT_PATH)
ALICE_DEVICES = devices_path()

#: Our own virtual device per scenario (contract cloud-scenarios.md §MQTT
#: mirror). `RUN_CONTROL` is its ONE command control: `<device>/controls/run/on`
#: runs the scene (truthy) or switches its outputs off. Every other control
#: under this prefix is retained state WE publish.
RULES_DEVICE_PREFIX = "sa02m-rules-"
RUN_CONTROL = "run"

#: Subscribed on every (re)connect. The second level carries LAN COMMANDS —
#: MQTT `+` cannot match a prefix, so every `/on` in the house arrives here and
#: `_scene_command` drops all but ours at the length + prefix check. Commands
#: are rare (a voice phrase, a button), and the inbox is bounded either way.
SUBSCRIPTIONS = ("/devices/+/controls/+", "/devices/+/controls/+/on")

#: Engine availability (contract cloud-scenarios.md §MQTT mirror): ONE
#: retained device-level flag for the whole engine, because MQTT allows one
#: will per connection — the bridge/telemetry pattern. `"r"` = down (the will,
#: and a graceful stop), `""` = listening. The Alice registry refuses a scene
#: command while it is set; its mirror of this topic is
#: `sa02m_alice.config.scene_devices.ENGINE_AVAILABILITY_TOPIC`. `sa02m-rules`
#: has no dash, so it is never a scenario device `sa02m-rules-<sid>`.
AVAILABILITY_TOPIC = "/devices/sa02m-rules/meta/error"
OFFLINE = "r"


def subscribe_all(client: Any) -> None:
    """One home for the subscription set, so `on_connect` cannot drift from
    what `_apply_message` knows how to handle."""
    for topic in SUBSCRIPTIONS:
        client.subscribe(topic, qos=1)


def arm_availability(client: Any) -> None:
    """The will — before `connect()`, which is when paho sends it. Fail-soft:
    the flag is advisory, running scenarios is the job."""
    try:
        client.will_set(AVAILABILITY_TOPIC, OFFLINE, qos=1, retain=True)
    except Exception as exc:
        LOG.error("availability will not armed: %s", exc)


def _mark(client: Any, payload: str) -> None:
    try:
        client.publish(AVAILABILITY_TOPIC, payload, qos=1, retain=True)
    except Exception as exc:
        LOG.warning("availability mark %r refused: %s", payload, exc)


def announce_online(client: Any) -> None:
    """Clear the flag. Called AFTER `subscribe_all` on the same connection, so
    a reader that sees it clear sends its command to a subscribed engine."""
    _mark(client, "")


def announce_offline(client: Any) -> None:
    """Graceful stop. Not waited for: `main()` never sends DISCONNECT, so the
    broker fires the will when the process exits even if this mark is still
    queued. It is here for the day someone adds a `disconnect()`, which
    discards the will."""
    _mark(client, OFFLINE)


def _geo() -> tuple:
    for cand in ("/etc/sa02m-alice/location.json", "/etc/sa02m/location.json"):
        try:
            with open(cand, encoding="utf-8") as fh:
                data = json.load(fh)
            return float(data.get("lat", LAT)), float(data.get("lon", LON))
        except (OSError, ValueError, TypeError):
            continue
    return LAT, LON


class RulesApp:
    def __init__(self, client: Any, path: str = PATH, lat: float = LAT, lon: float = LON):
        self.client = client
        self.path = path
        self.lat, self.lon = lat, lon
        self._last_mtime = 0.0
        self._alice_mtime = 0.0
        self._alice_path = ALICE_DEVICES
        self._index = DeviceIndex()
        self._inbox: "queue.Queue[tuple]" = queue.Queue(maxsize=INBOX_MAX)
        self._dropped = 0
        self._failed = 0
        self._warned_non_finite: set = set()
        self._warned_failed: set = set()
        self._step_failures: Dict[str, int] = {}
        self.reload_index()
        # Pre-create the mirror: Engine.__init__ adopts the existing doc and
        # may publish template state (rule_enabled) via pub_state, which
        # touches self.state — before the alias below exists (boot crash on
        # any non-empty scenarios.json).
        self.state: Dict[str, Dict[str, Any]] = {}
        self.engine = Engine(self.pub, path, now=time.time, lat=lat, lon=lon,
                             pub_state=self.pub_state)
        self.state = self.engine.state  # shared MQTT state mirror
        self.engine.set_caps_provider(self.caps_of)
        self.engine.set_alias_provider(lambda device, name: self._index.alias(device, name))
        self.engine.set_target_check(self.check_target)
        try:
            self._last_mtime = os.path.getmtime(self.path)
        except OSError:
            self._last_mtime = 0.0

    def caps_of(self, device: str) -> tuple:
        return self._index.caps_of(device)

    def reload_index(self) -> None:
        try:
            mtime = os.path.getmtime(self._alice_path)
        except OSError:
            mtime = 0.0
        if mtime == self._alice_mtime and self._index.loaded:
            return
        self._index = load_index(self._alice_path)
        self._alice_mtime = mtime

    def mqtt_topic(self, device: str, cap: str) -> str:
        short = cap_short(cap)
        mapped = self._index.cap_topics.get((device, short))
        if mapped:
            return mapped
        return "/devices/%s/controls/%s" % (device, short)

    def target_topic(self, device: str, short: str, instance: str = "") -> tuple:
        """(state topic, "") or (None, reason) for a write, on every path
        (block, `type=code` Hub.set, logic templates, `end`). A device the
        Alice document knows is resolved by device_index only — an
        ambiguous type, an unknown instance or a cap the document does not
        list is refused, never guessed. The raw `/devices/<device>/controls/
        <cap>` topic is kept only for a device the document does not list at
        all (and for every device while the document cannot be read)."""
        if device in self._index.devices:
            topic, reason = self._index.resolve(device, short, instance)
            return (topic, "") if topic else (None, reason)
        if instance:
            return None, "unknown_target"
        return "/devices/%s/controls/%s" % (device, short), ""

    def check_target(self, device: str, cap: str, instance: str = "") -> str:
        """"" or the `last_error` a write to this target gets (the engine
        asks before it schedules a ramp)."""
        topic, reason = self.target_topic(str(device), cap_short(cap), instance)
        return "" if topic else reason.replace("_", " ")

    def pub(self, device: str, cap: str, value: Any, instance: str = "") -> Any:
        """Publish a control write. Returns None, or a reason string when
        the target cannot be resolved (the engine journals it as the run's
        `last_error`)."""
        if not device or not cap:
            return None
        short = cap_short(cap)
        # Last line before the wire: both scenario paths (block actions and
        # the sandbox's Hub.set) end here, so this is where a name that
        # escapes its topic segment — a wildcard, a `/`, whitespace — is
        # stopped whatever let it through upstream. Regex home: store.
        if not ID_RE.match(str(device)) or not CAP_RE.match(str(short)) \
                or (instance and not CAP_RE.match(str(instance))):
            LOG.warning("publish refused: bad device/cap %r/%r", device, short)
            return None
        if str(device).startswith(RULES_DEVICE_PREFIX):
            # Our own virtual devices are OURS: `pub_state` writes their
            # retained state and nothing writes their `/on`. Without this a
            # stored action naming `sa02m-rules-<id>` would publish a command
            # the intake below hands straight back to the engine — a scenario
            # able to re-trigger itself through the broker.
            LOG.warning("publish refused: %s is a scenario device", device)
            return None
        key = state_key(short, instance)
        if (device, key) in self._index.readonly:
            return "read only"
        topic, reason = self.target_topic(str(device), short, instance)
        if not topic:
            LOG.warning("publish refused: %s/%s%s — %s", device, short,
                        "[%s]" % instance if instance else "", reason)
            return reason.replace("_", " ")
        payload = "1" if value in (True, 1, "1", "on", "true") else (
            "0" if value in (False, 0, "0", "off", "false") else str(value)
        )
        binding = self._index.bindings.get((device, key))
        if binding is not None:
            payload, error = capability_binding.write(binding, value)
            if error or payload is None:
                return "invalid capability value"
        self._publish(topic + "/on", payload, retain=False)
        self.state.setdefault(device, {})[key] = value
        return None

    def pub_state(self, device: str, cap: str, value: Any) -> None:
        """State topic (retained) for our own virtual sa02m-rules-* devices —
        template state the cloud reads for debugging. Never `/on`."""
        if not device or not cap:
            return
        short = cap_short(cap)
        topic = self.mqtt_topic(device, short)
        payload = "1" if value in (True, 1, "1", "on", "true") else (
            "0" if value in (False, 0, "0", "off", "false") else str(value)
        )
        self._publish(topic, payload, retain=True)
        self.state.setdefault(device, {})[short] = value

    def _publish(self, topic: str, payload: str, retain: bool) -> None:
        # A publish must never escape on_boot()/tick(): one stored row with
        # a topic paho refuses (a wildcard) would otherwise exit the process
        # into Restart=on-failure and re-fire on every boot (verdict A2).
        try:
            self.client.publish(topic, payload, qos=1, retain=retain)
        except Exception as exc:
            LOG.warning("publish %s refused: %s", topic, exc)

    def on_message(self, _c: Any, _u: Any, msg: Any) -> None:
        """paho network thread: enqueue only (see module docstring)."""
        topic = getattr(msg, "topic", "") or ""
        payload = msg.payload
        raw = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        try:
            self._inbox.put_nowait((topic, raw))
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or _sampled(self._dropped):
                LOG.warning("inbox full: %d message(s) dropped", self._dropped)

    def _step(self, name: str, fn: Any, *args: Any) -> None:
        """Run one main-loop step so its exception cannot end the process:
        main() has no handler but the stop one, and a persistent cause (a
        stored scenario, a boot trigger) would crash-loop the unit. Logged
        with a traceback the first time, then sampled; Exception only, so the
        SIGTERM SystemExit and a KeyboardInterrupt still unwind."""
        try:
            fn(*args)
        except Exception:
            n = self._step_failures.get(name, 0) + 1
            self._step_failures[name] = n
            if n == 1:
                LOG.exception("%s failed; continuing", name)
            elif _sampled(n):
                LOG.warning("%s failed %d times so far", name, n)

    @staticmethod
    def _first_time(seen: set, topic: str) -> bool:
        """True once per topic, while the bounded memory has room."""
        if topic in seen or len(seen) >= WARNED_TOPICS_MAX:
            return False
        seen.add(topic)
        return True

    def _drain_inbox(self) -> None:
        while True:
            try:
                topic, raw = self._inbox.get_nowait()
            except queue.Empty:
                return
            # One bad message must not end the loop: a RETAINED payload that
            # raises would otherwise crash-loop the unit on every start.
            # Exception only — the SIGTERM SystemExit must still unwind.
            try:
                self._apply_message(topic, raw)
            except Exception:
                self._failed += 1
                if self._first_time(self._warned_failed, topic):
                    LOG.exception("message on %r skipped", topic)
                elif _sampled(self._failed):
                    LOG.warning("%d message(s) skipped after an error, "
                                "latest on %r", self._failed, topic)

    def _scene_command(self, parts: list, raw: str) -> None:
        """`devices/sa02m-rules-<sid>/controls/run/on` → run / run_off.

        The sid is LOOKED UP in the store, never interpolated into anything:
        the engine answers only for a row that exists, is `scene`-typed and
        enabled (`run_scene` / `run_off` share that gate), so an unknown or
        crafted id — or a `block` someone hoped to start from the LAN — is a
        no-op. `ID_RE` bounds the charset before the lookup all the same —
        the same fence every other name in this file passes.
        """
        if (parts[0] != "devices" or parts[2] != "controls"
                or parts[3] != RUN_CONTROL or parts[4] != "on"):
            return
        did = parts[1]
        if not did.startswith(RULES_DEVICE_PREFIX) or not ID_RE.match(did):
            return
        sid = did[len(RULES_DEVICE_PREFIX):]
        if not sid or not ID_RE.match(sid):
            return
        self.reload()
        if raw.strip() in ("1", "on", "true", "True", "ON"):
            self.engine.run_scene(sid)
        else:
            self.engine.run_off(sid)

    def _apply_message(self, topic: str, raw: str) -> None:
        parts = topic.strip("/").split("/")
        # devices / <id> / controls / <name> / on   — a command, ours only
        if len(parts) == 5:
            self._scene_command(parts, raw)
            return
        # devices / <id> / controls / <name>   — the state mirror
        if len(parts) != 4 or parts[0] != "devices" or parts[2] != "controls":
            return
        state_topic = "/" + "/".join(parts)
        # Every (device, key) the document binds to this topic: a capability
        # by its type and/or `type|instance`, a property by its instance.
        mapped = self._index.topic_keys.get(state_topic)
        try:
            value: Any = json.loads(raw)
        except ValueError:
            value = raw
        if non_finite(value):
            # nan / ±inf / a 309+ digit int is not a reading: the value is
            # UNKNOWN (a faulted Carel probe publishes "nan") — the last good
            # value must not live on, and nothing fires on the transition.
            if self._first_time(self._warned_non_finite, state_topic):
                LOG.warning("non-finite value on %r: value unknown", state_topic)
            # Every key this topic feeds: the raw (device, control) name, and
            # each (device, key) the document binds to it.
            self.engine.forget_state(parts[1], parts[3])
            for device, key in mapped or ():
                self.engine.forget_state(device, key)
            return
        self.reload()
        # The raw (MQTT device, control) path carries button counters, the
        # di_N edge classifier and raw-named state triggers: a topic the
        # document binds only as a PROPERTY (a DI as an `open`/`button`
        # event) still reaches it. A capability topic mirrors the raw name
        # without dispatch, as it always has.
        # One key has one representation: a typed key may coincide with the
        # MQTT alias. Writing raw then typed there fabricates a change on
        # every report (0 -> not_detected, or active-low 0 -> True).
        if (parts[1], parts[3]) not in (mapped or ()):
            if mapped and any(k in self._index.cap_topics for k in mapped):
                self.engine.alias_state(parts[1], parts[3], value)
            else:
                self.engine.on_state(parts[1], parts[3], value)
        for device, key in mapped or ():
            binding = self._index.bindings.get((device, key))
            converted = capability_binding.read(binding, raw) if binding else value
            if converted is None:
                self.engine.forget_state(device, key)
            else:
                self.engine.on_state(device, key, converted)

    def reload(self) -> None:
        self.reload_index()
        # Watches the DOCUMENT only: run records go to the sibling journal
        # (store.journal_path), so a scenario run never triggers a reload.
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = 0.0
        if mtime != self._last_mtime:
            self._last_mtime = mtime
            self.engine.adopt(load(self.path))

    def tick(self) -> None:
        # reload() stamps the new mtime before adopting, so a document that
        # fails to adopt is logged once and not re-read every second.
        self._step("reload", self.reload)
        self._drain_inbox()
        self._step("engine.tick", self.engine.tick)
        self._step("logic_status_poll", self.engine.logic_status_poll)
        run_flag = self.path + ".run"
        try:
            with open(run_flag, encoding="utf-8") as fh:
                sid = fh.read().strip()
            os.unlink(run_flag)
        except OSError:
            sid = ""
        if sid:
            # The flag is already unlinked: a run that raises is not retried.
            self._step("run_now", self.engine.run_now, sid)

    def boot(self) -> None:
        self._step("on_boot", self.engine.on_boot)

    def stop(self) -> None:
        """Shutdown: the buffered run records reach the journal."""
        self.engine.flush_runs()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s sa02m-rules %(message)s")
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        # Optional-tier dependency (scripts/06b-rules.sh): its absence is an
        # intentional standby, not a failure — exit 0 so Restart=on-failure
        # does not storm every 5 s (sa02m-cloud-control parity, verdict A17).
        LOG.error("paho-mqtt missing — scenario engine in standby (exit 0)")
        return 0
    lat, lon = _geo()
    client = mqtt.Client(client_id="sa02m-rules", clean_session=True)
    app = RulesApp(client, PATH, lat, lon)

    def on_connect(c, _u, _f, rc):
        LOG.info("mqtt rc=%s", rc)
        subscribe_all(c)
        if int(getattr(rc, "value", rc) or 0) == 0:
            announce_online(c)

    client.on_connect = on_connect
    client.on_message = app.on_message
    arm_availability(client)
    client.connect(MQTT_HOST, MQTT_PORT, 30)
    client.loop_start()
    app.boot()

    def _term(_signum, _frame):
        raise SystemExit(0)  # systemd stop: unwind into the flush below

    signal.signal(signal.SIGTERM, _term)
    try:
        while True:
            app.tick()
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        app.stop()
        announce_offline(client)
    client.loop_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
