"""Through `run()`: the smart-home client survives a broker restart (1.0.6.66).

The guarantee under test is docs/contracts/alice-mqtt-mapping.md §Broker
reconnect. After the local broker drops and re-accepts the connection the
client:

  T2  re-subscribes its CURRENT topic set at QoS 1 (catalogue, availability
      `/meta/error`, `uptime_s`, the auto-provision watch topics);
  T3  re-sends ONE `origin=snapshot` frame once the retained grace has elapsed
      — and sends no cadence snapshot while the link is down (a frozen cache
      is not current state);
  T4  answers a command `ERROR / DEVICE_UNREACHABLE` while the broker is down
      and hands NOTHING to paho (a QoS 1 message queued on NO_CONN would be
      replayed — actuated — on reconnect);
  T5  reflects the link in the status file within a tick (`mqtt_connected`,
      `mqtt_reconnects`);

and a broker refusing at cold start is named in `status.message` instead of
reading like a gateway failure.

This file is the defect's RED: on the tree before the fix `on_connect` was
never assigned, so the fake broker's re-accept re-subscribes nothing, a
cadence snapshot leaves while deaf, a command gets `DONE` with rc=NO_CONN,
and the status keys are absent. The record is in the commit body.

Harness: `tests/fake_paho.py` injected as the `paho.mqtt.client` module (both
the old `_mqtt_client` and `mqtt_link` import it lazily, so the same file runs
on both trees); a fake Socket.IO that stays connected and records emits; the
`test_binding_reset.py` idioms — a `time` shim on `client.main` ONLY (the
StateSender thread keeps the real clock), `time.sleep` as the scripted tick,
`monotonic` carrying a test-advanced offset so the grace window and the
snapshot cadence are crossed without waiting, and every wait budgeted with a
RAISE (a test that ends by timing out is not a test).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client import main as client_main  # noqa: E402
from sa02m_alice.client import sio_connection  # noqa: E402
from sa02m_alice.client.auto_provision import WATCH_TOPICS  # noqa: E402
from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.common import binding_core  # noqa: E402
from sa02m_alice.common import config_store  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402
from tests.fake_paho import FakePahoClient, fake_paho_modules  # noqa: E402

_PATCHED = (
    "ETC_DIR", "CLIENT_CONF", "DEVICES_CONF", "SERVER_CONF", "VAR_DIR",
    "CERT_FILE", "KEY_FILE", "CA_FILE", "PENDING_CLAIM_FILE", "STATUS_FILE",
)
CLIENT_CONF_SEED = (
    "[client]\n"
    "client_enabled = true\n"
    "log_level = INFO\n"
    "mqtt_host = 127.0.0.1\n"
    "mqtt_port = 1883\n"
)
SERVER_CONF_SEED = (
    "[gateway]\n"
    "http_url = https://alice.cyntron.ru\n"
    "wss_url = wss://alice.cyntron.ru/controller/socket.io\n"
)
DO_TOPIC = "/devices/mr02m-COM3-10/controls/do_1"
SWITCH_DOC = {
    "rooms": [],
    "devices": [
        {
            "id": "sw1",
            "name": "Light 1",
            "type": "devices.types.switch",
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "parameters": {"instance": "on"},
                    "mqtt": DO_TOPIC,
                }
            ],
            "properties": [],
        }
    ],
}
# The set a (re)connect must hold: the registry's catalogue + availability
# topics, plus the Yandex unit's auto-provision watch topics.
EXPECTED_TOPICS = set(DeviceRegistry(SWITCH_DOC).subscribe_topics()) | set(WATCH_TOPICS)
ACTION_ON = {
    "request_id": "r1",
    "payload": {
        "devices": [
            {
                "id": "sw1",
                "capabilities": [
                    {
                        "type": "devices.capabilities.on_off",
                        "state": {"instance": "on", "value": True},
                    }
                ],
            }
        ]
    },
}


class _BudgetExhausted(BaseException):
    """Not an Exception subclass: run()'s broad `except Exception` would
    swallow it and hand the hang straight back."""


class _LiveSio:
    """Stands in for AliceSocketIO: connects, STAYS up, records every emit,
    and lets the test deliver a gateway event through the same `on_event`
    seam the library uses."""

    instances = []

    def __init__(self, *, on_event=None, **_kw):
        self._on_event = on_event
        self.connected = False
        self.emits = []
        self.responses = []
        _LiveSio.instances.append(self)

    def connect(self):
        self.connected = True

    def emit(self, event, data):
        self.emits.append((event, data))

    def emit_response(self, data):
        self.responses.append(data)

    def session_summary(self):
        return "session summary"

    def session_duration_s(self):
        return 5.0

    def disconnect(self):
        self.connected = False

    def fire(self, event, data):
        self._on_event(event, data)


class _Clock:
    """The `time` shim for client.main: `sleep` is the scripted tick,
    `monotonic` carries an offset the script advances."""

    def __init__(self):
        self.offset = 0.0
        self.on_tick = lambda: None

    def sleep(self, _s=None):
        self.on_tick()

    def monotonic(self):
        return time.monotonic() + self.offset

    @staticmethod
    def time():
        return time.time()


class _RunHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = {name: getattr(C, name) for name in _PATCHED}
        self.addCleanup(lambda: [setattr(C, k, v) for k, v in saved.items()])
        etc = os.path.join(self.tmp.name, "etc")
        var = os.path.join(self.tmp.name, "var")
        os.makedirs(etc)
        os.makedirs(var)
        C.ETC_DIR = etc
        C.CLIENT_CONF = os.path.join(etc, "sa02m-alice-client.conf")
        C.DEVICES_CONF = os.path.join(etc, "sa02m-alice-devices.conf")
        C.SERVER_CONF = os.path.join(etc, "sa02m-alice-server.conf")
        C.VAR_DIR = var
        C.CERT_FILE = os.path.join(var, "device.crt.pem")
        C.KEY_FILE = os.path.join(var, "device.key.pem")
        C.CA_FILE = os.path.join(var, "ca.crt.pem")
        C.PENDING_CLAIM_FILE = os.path.join(var, "pending_claim.json")
        self.status_path = os.path.join(self.tmp.name, "run", "status.json")
        C.STATUS_FILE = self.status_path
        self._write(C.CLIENT_CONF, CLIENT_CONF_SEED)
        self._write(C.SERVER_CONF, SERVER_CONF_SEED)
        for path in (C.CERT_FILE, C.KEY_FILE, C.CA_FILE):
            self._write(path, "-----BEGIN PEM-----\n")
        config_store.save_devices(SWITCH_DOC, C.DEVICES_CONF)

        self._reset_globals()
        self.addCleanup(self._reset_globals)
        FakePahoClient.instances = []
        _LiveSio.instances = []
        auto = mock.patch.object(FakePahoClient, "auto_connack", True)
        auto.start()
        self.addCleanup(auto.stop)

    @staticmethod
    def _reset_globals():
        client_main._unlinked.clear()
        client_main._stop.clear()
        binding_core.PENDING_STAND_DOWN.update({"cls": "", "reason": ""})

    @staticmethod
    def _write(path, text):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    # -- the world under test ------------------------------------------------
    @staticmethod
    def paho():
        return FakePahoClient.instances[-1]

    @staticmethod
    def sio():
        return _LiveSio.instances[-1]

    def status(self):
        with io.open(self.status_path, encoding="utf-8") as fh:
            return json.load(fh)

    def snapshots(self):
        return [d for e, d in self.sio().emits
                if e == C.EVT_DEVICE_STATE and d.get("origin") == C.ORIGIN_SNAPSHOT]

    def run_client(self, script, tick_budget=60):
        """Drive run() to a scripted end. `script(n, clock)` runs on every
        client.main `time.sleep` — n=1 is the retained-settle sleep after the
        initial subscribe pass, n>=2 the end of watchdog iteration n-1."""
        clock = _Clock()
        ticks = {"n": 0}

        def on_tick():
            ticks["n"] += 1
            if ticks["n"] > tick_budget:
                raise _BudgetExhausted("the watchdog loop ran %d ticks without the script ending it" % tick_budget)
            script(ticks["n"], clock)

        clock.on_tick = on_tick
        waits = {"n": 0}

        def budget_wait(_timeout=None):
            waits["n"] += 1
            if waits["n"] > 10:
                raise _BudgetExhausted("the reconnect loop ran 10 waits — the session did not hold")
            return client_main._stop.is_set()

        with mock.patch.dict(sys.modules, fake_paho_modules()), \
                mock.patch.object(client_main, "AliceSocketIO", _LiveSio), \
                mock.patch.object(client_main, "time", clock), \
                mock.patch.object(client_main, "reconnect_delay", return_value=0.0), \
                mock.patch.object(sio_connection, "import_socketio", return_value=None), \
                mock.patch.object(client_main._stop, "wait", side_effect=budget_wait):
            return client_main.run()


class TestResubscribeThroughRun(_RunHarness):
    def test_a_broker_reconnect_resubscribes_the_current_set_at_qos1(self):
        """T2 — the defect. paho brings the socket back by itself; the
        subscriptions come back only if on_connect re-sends them."""
        marks = {}

        def script(n, _clock):
            paho = self.paho()
            if n == 2:
                marks["initial"] = list(paho.subscribed)
                paho.drop()
            elif n == 3:
                paho.accept()
            elif n >= 5:
                client_main._stop.set()

        self.assertEqual(self.run_client(script), 0)
        initial = marks["initial"]
        self.assertEqual({t for t, _q in initial}, EXPECTED_TOPICS,
                         "the initial pass must subscribe the whole set")
        again = self.paho().subscribed[len(initial):]
        self.assertEqual(
            {t for t, _q in again}, EXPECTED_TOPICS,
            "after the broker re-accepted the connection the CURRENT set was not "
            "re-subscribed; subscribes after the drop: %r" % (again,))
        self.assertTrue(all(q == 1 for _t, q in again), again)
        self.assertEqual(self.paho().unsubscribed, [])
        # Unique per process, greppable in the broker log (contract §Broker reconnect).
        self.assertEqual(self.paho().client_id, "sa02m-alice-%s-%d" % (C.PROFILE_YANDEX, os.getpid()))

    def test_one_snapshot_after_the_grace_and_none_while_deaf(self):
        """T3 — re-report once the retained burst has settled; a cadence
        boundary crossed while the link is down sends nothing."""
        marks = {}

        def script(n, clock):
            paho = self.paho()
            if n == 1:
                # The retained burst of the initial subscribe: cached, so the
                # snapshot path has a value to carry.
                paho.deliver(DO_TOPIC, "1", retain=True)
            elif n == 2:
                marks["at_drop"] = len(self.snapshots())
                paho.drop()
                clock.offset += C.STATE_SNAPSHOT_YANDEX_S + 1.0
            elif n == 3:
                marks["at_accept"] = len(self.snapshots())
                paho.accept()
            elif n == 5:
                clock.offset += C.RETAINED_GRACE_S + 1.0
            elif n >= 8:
                client_main._stop.set()

        with self.assertLogs(client_main.log, level="DEBUG") as captured:
            self.assertEqual(self.run_client(script), 0)
        self.assertGreaterEqual(marks["at_drop"], 1, "the connect-time snapshot never left (harness)")
        self.assertEqual(
            marks["at_accept"] - marks["at_drop"], 0,
            "a cadence snapshot left the board while the broker link was down")
        self.assertEqual(
            len(self.snapshots()) - marks["at_accept"], 1,
            "expected exactly one snapshot after the reconnect + grace, got %d"
            % (len(self.snapshots()) - marks["at_accept"]))
        # The bench's positive send evidence (plan P3): one DEBUG line per
        # snapshot frame that actually left through the fake Socket.IO.
        sent = [line for line in captured.output if "device_state sent: origin=snapshot" in line]
        self.assertEqual(len(sent), len(self.snapshots()), captured.output)

    def test_a_command_while_the_broker_is_down_is_refused_not_queued(self):
        """T4 (F2) — no DONE for a command that did not go out, and nothing
        handed to paho that a reconnect would replay minutes later."""

        def script(n, _clock):
            paho = self.paho()
            if n == 1:
                paho.deliver(DO_TOPIC, "0", retain=True)
            elif n == 2:
                paho.drop()
                self.sio().fire(C.EVT_DEVICES_ACTION, ACTION_ON)
            elif n >= 4:
                client_main._stop.set()

        self.assertEqual(self.run_client(script), 0)
        responses = self.sio().responses
        self.assertEqual(len(responses), 1, responses)
        cap = responses[0]["payload"]["devices"][0]["capabilities"][0]
        self.assertEqual(
            (cap.get("status"), cap.get("error_code")),
            (C.STATUS_ERROR, C.ERR_DEVICE_UNREACHABLE),
            "a command the broker could not take was answered %r" % (cap,))
        self.assertEqual(
            self.paho().published, [],
            "the command was handed to paho while the link was down — paho keeps a "
            "QoS 1 message on NO_CONN and re-sends it after the next CONNACK")

    def test_status_file_follows_the_link_within_a_tick(self):
        """T5 (F3) — additive keys; `state` stays `connected` (the gateway
        session IS up) while `mqtt_connected` tells the truth about the broker."""
        seen = {}

        def script(n, _clock):
            paho = self.paho()
            if n == 2:
                seen["before"] = self.status()
                paho.drop()
            elif n == 3:
                seen["down"] = self.status()
                paho.accept()
            elif n == 4:
                seen["up"] = self.status()
                client_main._stop.set()

        self.assertEqual(self.run_client(script), 0)
        self.assertIs(seen["before"].get("mqtt_connected"), True, seen["before"])
        self.assertEqual(seen["before"].get("mqtt_reconnects"), 0)
        self.assertIs(seen["down"].get("mqtt_connected"), False, seen["down"])
        self.assertEqual(seen["down"]["state"], C.STATE_CONNECTED)
        self.assertIs(seen["up"].get("mqtt_connected"), True, seen["up"])
        self.assertEqual(seen["up"].get("mqtt_reconnects"), 1)
        self.assertEqual(seen["up"]["state"], C.STATE_CONNECTED)


class TestColdStartBrokerRefusal(_RunHarness):
    def test_a_refused_broker_is_named_in_the_status_message(self):
        """A broker refusing at connect must not read like a gateway failure
        in the journal / `status.message` (the card token is residual R1 and
        deliberately not asserted here)."""
        dials = {"n": 0}

        def refuse(*_a, **_k):
            dials["n"] += 1
            client_main._stop.set()  # scripted: one attempt, then the stop signal
            raise ConnectionRefusedError(111, "Connection refused")

        with mock.patch.object(FakePahoClient, "connect", side_effect=refuse):
            self.assertEqual(self.run_client(lambda _n, _c: None), 0)
        self.assertEqual(dials["n"], 1)
        self.assertEqual(_LiveSio.instances, [], "the gateway was dialled without a broker")
        message = self.status()["message"]
        self.assertIn("MQTT broker 127.0.0.1:1883", message)


if __name__ == "__main__":
    unittest.main()
