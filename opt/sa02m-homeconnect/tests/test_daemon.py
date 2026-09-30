"""main.Daemon end-to-end against the fake cloud and an in-memory broker.

Covers the link flow into `connected`, SSE events → WB topics, meta/error on
a disconnected appliance and on a stale stream (P2), unlink / disable
cleanup, revocation, rate limiting, the standby exits, the insecure token
store — and, throughout, that no token or device code reaches a log line,
status.json, link.json or inventory.json, and that every request the fake
received was charged to the budget (failures included).
"""

from __future__ import annotations

import functools
import json
import logging
import os
import stat
import tempfile
import threading
import time
import unittest
from typing import Any, Callable, Dict, List
from unittest import mock

from sa02m_homeconnect import config
from sa02m_homeconnect import constants as C
from sa02m_homeconnect import main as M
from sa02m_homeconnect.sse import EventStream
from sa02m_homeconnect.status import StatusWriter
from sa02m_homeconnect.token_store import TokenSet, TokenStore
from sa02m_homeconnect.transport import Transport

from .fake_bsh import FakeBSH, sse_event

CLIENT = "CLIENT_ID_FOR_TESTS_0001"
HA_A = "SIEMENS-SN65ZX49CE-68A40E000001"
HA_B = "BOSCH-WAT28400-68A40E000002"
DEV_A = "hc-siemens-sn65zx49ce-68a40e000001"
DEV_B = "hc-bosch-wat28400-68a40e000002"
E = "BSH.Common.EnumType."
SECRETS = ("AT-", "RT-", "DEVICE-CODE-SECRET")

FAST = {
    "TICK_S": 0.01, "CONF_POLL_S": 0.02, "INVENTORY_PACE_S": 0.0, "STREAM_STALE_S": 0.5,
    "POLL_INTERVAL_MIN_S": 0, "STATUS_HEARTBEAT_S": 0.2, "SSE_BACKOFF_MIN_S": 0.05,
}


class FakeLink:
    """The MQTT link: an in-memory retained store, always connected."""

    def __init__(self) -> None:
        self.retained: Dict[str, str] = {}
        self.log: List[Any] = []
        self.connected = True
        self._cb: Callable[[], Any] = lambda: None

    def set_on_connected(self, cb: Callable[[], Any]) -> None:
        self._cb = cb

    def start(self) -> None:
        self._cb()

    def stop(self) -> None:
        pass

    def publish(self, topic: str, payload: str, retain: bool) -> bool:
        assert not topic.endswith("/on"), topic
        assert retain, topic
        self.log.append((topic, payload))
        self.retained[topic] = payload
        return True


class CaptureAll(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class DaemonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.patches = [mock.patch.object(C, k, v) for k, v in FAST.items()]
        self.patches.append(mock.patch.object(M, "RETRY_MIN_S", 0.05))
        for p in self.patches:
            p.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.var = os.path.join(self.tmp.name, "var")
        self.run_dir = os.path.join(self.tmp.name, "run")
        os.makedirs(self.var, mode=0o700)
        os.makedirs(self.run_dir)
        self.conf_path = os.path.join(self.tmp.name, "hc.conf")
        self.tokens_path = os.path.join(self.var, C.TOKENS_NAME)
        self.budget_path = os.path.join(self.var, C.BUDGET_NAME)
        self.fake = FakeBSH()
        self.base = self.fake.start()
        self.link = FakeLink()
        self.logs = CaptureAll()
        root = logging.getLogger()
        self._old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.logs)
        self.threads: List[Any] = []
        self.fake.appliances = [
            {"haId": HA_A, "name": "Посудомойка", "type": "Dishwasher", "brand": "Siemens", "connected": True},
            {"haId": HA_B, "name": "Стиральная", "type": "Washer", "brand": "Bosch", "connected": False},
        ]
        self.fake.status[HA_A] = [
            {"key": "BSH.Common.Status.OperationState", "value": E + "OperationState.Run"},
            {"key": "BSH.Common.Status.DoorState", "value": E + "DoorState.Closed"},
            {"key": "BSH.Common.Status.RemoteControlActive", "value": True},
        ]
        self.fake.programs[HA_A] = {"key": "Dishcare.Dishwasher.Program.Eco50", "options": [
            {"key": "BSH.Common.Option.RemainingProgramTime", "value": 1800, "unit": "seconds"},
            {"key": "BSH.Common.Option.ProgramProgress", "value": 10, "unit": "%"},
        ]}

    def tearDown(self) -> None:
        for stop, thread, _ in self.threads:
            stop.set()
            thread.join(5)
        self.fake.stop()
        root = logging.getLogger()
        root.removeHandler(self.logs)
        root.setLevel(self._old_level)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    # ── helpers ─────────────────────────────────────────────────────────
    def write_conf(self, **kw: Any) -> None:
        values = dict(enabled=True, client_id=CLIENT)
        values.update(kw)
        config.save(config.ClientConfig(**values), self.conf_path)

    def prelink(self, expires_in: int = 86400) -> None:
        now = int(time.time())
        TokenStore(self.tokens_path).save(TokenSet(
            access_token="AT-PRE", refresh_token="RT-PRE", expires_at=now + expires_in,
            scope=C.SCOPES, host="api", client_id=CLIENT, linked_at=now))
        self.fake.access = "AT-PRE"

    def daemon(self, **kw: Any) -> M.Daemon:
        args = dict(
            stop=threading.Event(),
            status=StatusWriter(self.run_dir),
            conf_path=self.conf_path,
            tokens_path=self.tokens_path,
            budget_path=self.budget_path,
            appliances_path=os.path.join(self.var, C.APPLIANCES_NAME),
            base_url_override=self.base,
            transport_factory=lambda b: Transport(b, allow_loopback_http=True, timeout=3),
            mqtt_factory=lambda: self.link,
            # The fake sends no keep-alive while a script waits on a gate; a
            # long deadline keeps such a wait from turning into a reconnect.
            stream_factory=functools.partial(EventStream, read_timeout=15.0, backoff_min=0.05,
                                             backoff_max=0.2),
            check_dependencies=lambda: [],
        )
        args.update(kw)
        return M.Daemon(**args)

    def start(self, d: M.Daemon) -> M.Daemon:
        result: Dict[str, Any] = {}
        thread = threading.Thread(target=lambda: result.setdefault("rc", d.run()), daemon=True)
        thread.start()
        self.threads.append((d.stop, thread, result))
        return d

    def stop(self, d: M.Daemon) -> int:
        for stop, thread, result in self.threads:
            if stop is d.stop:
                stop.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                return result.get("rc")
        raise AssertionError("daemon not started")

    def status(self) -> Dict[str, Any]:
        try:
            with open(os.path.join(self.run_dir, "status.json")) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def wait(self, cond: Callable[[], bool], what: str, timeout: float = 8.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for %s; status=%r" % (what, self.status()))

    def topic(self, dev: str, suffix: str) -> Any:
        return self.link.retained.get("/devices/%s/%s" % (dev, suffix))

    def assert_no_secret_anywhere(self) -> None:
        blobs = ["\n".join(self.logs.lines)]
        for name in os.listdir(self.run_dir):
            with open(os.path.join(self.run_dir, name)) as fh:
                blobs.append(fh.read())
        blobs.append(json.dumps(self.link.log))
        for blob in blobs:
            for secret in SECRETS:
                self.assertNotIn(secret, blob)
            self.assertNotIn("access_token", blob)
            self.assertNotIn("refresh_token", blob)

    def assert_every_request_charged(self) -> None:
        with open(self.budget_path) as fh:
            used = json.load(fh)["used"]
        self.assertEqual(used, len(self.fake.calls()))

    # ── scenarios ───────────────────────────────────────────────────────
    def test_link_flow_to_connected_and_events(self) -> None:
        self.write_conf(link_requested_at=int(time.time()))
        self.fake.poll_script = []  # pending until the test lets it through
        self.fake.sse_scripts = [[sse_event("KEEP-ALIVE"), "WAIT:events",
            sse_event("STATUS", {"items": [{"key": "BSH.Common.Status.DoorState",
                                            "value": E + "DoorState.Open", "haId": HA_A,
                                            "timestamp": 1790000000}]}, HA_A),
            sse_event("EVENT", {"items": [{"key": "BSH.Common.Event.ProgramFinished",
                                           "value": E + "EventPresentState.Present", "haId": HA_A,
                                           "timestamp": 1790000100}]}, HA_A),
            sse_event("NOTIFY", {"items": [{"key": "BSH.Common.Option.RemainingProgramTime",
                                            "value": 0, "haId": HA_A}]}),
            sse_event("DISCONNECTED", {"haId": HA_A}),
            "HANG"]]
        d = self.start(self.daemon())
        self.wait(lambda: self.status().get("state") == C.STATE_AWAITING_USER, "awaiting_user")
        link_path = os.path.join(self.run_dir, "link.json")
        self.assertEqual(stat.S_IMODE(os.stat(link_path).st_mode), 0o640)
        with open(link_path) as fh:
            link = json.load(fh)
        self.assertEqual(link["user_code"], "ABCD-1234")
        self.assertNotIn("device_code", link)
        self.fake.poll_script = ["ok"]
        self.wait(lambda: self.status().get("state") == C.STATE_CONNECTED, "connected")
        self.wait(lambda: self.topic(DEV_A, "controls/remaining_s") == "1800", "detail read")
        self.assertFalse(os.path.exists(link_path))
        st = self.status()
        self.assertEqual((st["linked"], st["appliances"], st["appliances_connected"], st["stream"]),
                         (True, 2, 1, "up"))
        self.assertEqual(stat.S_IMODE(os.stat(self.tokens_path).st_mode), 0o600)
        meta = json.loads(self.topic(DEV_A, "meta"))
        self.assertEqual((meta["driver"], meta["title"]["ru"], meta["type"]),
                         ("sa02m-homeconnect", "Посудомойка", "Dishwasher"))
        for control, value in (("connected", "1"), ("running", "1"), ("finished", "0"),
                               ("operation_state", "Run"), ("door_open", "0"),
                               ("remote_control_active", "1"), ("active_program", "Eco50"),
                               ("progress_pct", "10")):
            self.assertEqual(self.topic(DEV_A, "controls/" + control), value, control)
        self.assertEqual(self.topic(DEV_A, "controls/running/meta/readonly"), "1")
        self.wait(lambda: self.topic(DEV_A, "meta/error") == "", "A live")
        self.assertEqual(self.topic(DEV_B, "meta/error"), "r")  # disconnected appliance
        self.assertEqual(self.topic(DEV_B, "controls/connected"), "0")
        self.assertIsNone(self.topic(DEV_B, "controls/running"))  # never read ⇒ never fabricated
        with open(os.path.join(self.run_dir, "inventory.json")) as fh:
            inv = json.load(fh)
        self.assertEqual({a["device_id"] for a in inv["appliances"]}, {DEV_A, DEV_B})
        self.fake.gate("events").set()
        self.wait(lambda: self.topic(DEV_A, "meta/error") == "r", "DISCONNECTED → meta/error r")
        self.assertEqual(self.topic(DEV_A, "controls/door_open"), "1")
        self.assertEqual(self.topic(DEV_A, "controls/last_event"), "ProgramFinished")
        self.assertEqual(self.topic(DEV_A, "controls/last_event_ts"), "1790000100")
        self.assertEqual(self.topic(DEV_A, "controls/remaining_s"), "0")
        self.assertEqual(self.topic(DEV_A, "controls/connected"), "0")
        self.assertEqual(self.topic(DEV_A, "controls/door_open"), "1")  # values kept
        self.assertEqual(self.fake.sse_connections, 1)  # the flag came from the event, not a reconnect
        self.assertEqual(self.stop(d), 0)
        self.assertEqual(self.status()["state"], C.STATE_CONNECTING)  # enabled, "stopped"
        self.assert_no_secret_anywhere()
        self.assert_every_request_charged()
        self.assertTrue(all(r["method"] in ("GET", "POST") for r in self.fake.calls()))
        self.assertFalse(any(r["method"] == "POST" and r["path"].startswith("/api/")
                             for r in self.fake.calls()))

    def test_stale_stream_marks_every_appliance_then_recovers(self) -> None:
        self.write_conf()
        self.prelink()
        self.fake.sse_scripts = [[sse_event("KEEP-ALIVE"), "WAIT:drop"], ["HANG"]]
        d = self.start(self.daemon())
        self.wait(lambda: self.status().get("state") == C.STATE_CONNECTED, "connected")
        self.wait(lambda: self.topic(DEV_A, "meta/error") == "", "A live")
        reads_before = len(self.fake.calls("/api/homeappliances/%s/status" % HA_A))
        self.fake.overrides[C.EVENTS_PATH] = [(503, {}, {})] * 12
        self.fake.gate("drop").set()
        self.wait(lambda: self.topic(DEV_A, "meta/error") == "r", "stale → r")
        self.wait(lambda: self.status().get("reason") == C.REASON_STREAM_DOWN, "reason stream_down")
        self.assertEqual(self.topic(DEV_B, "meta/error"), "r")
        self.wait(lambda: self.status().get("state") == C.STATE_CONNECTED
                  and self.topic(DEV_A, "meta/error") == "", "recovered", timeout=12)
        self.wait(lambda: len(self.fake.calls("/api/homeappliances/%s/status" % HA_A)) > reads_before,
                  "re-read after the outage")
        self.stop(d)
        self.assert_every_request_charged()

    def test_unlink_restart_clears_retained_topics(self) -> None:
        self.write_conf()
        self.prelink()
        d = self.start(self.daemon())
        self.wait(lambda: self.topic(DEV_A, "controls/running") == "1", "published")
        self.stop(d)
        self.assertEqual(self.topic(DEV_A, "meta/error"), "r")  # we are down ⇒ not live
        # The trigger's `unlink`: stop, remove tokens, start.
        os.unlink(self.tokens_path)
        self.link.log.clear()
        d2 = self.start(self.daemon())
        self.wait(lambda: self.status().get("state") == C.STATE_UNLINKED, "unlinked")
        self.wait(lambda: self.topic(DEV_A, "controls/running") == "", "A cleared")
        self.assertEqual(self.topic(DEV_A, "meta"), "")
        self.assertEqual(self.topic(DEV_B, "meta"), "")
        with open(os.path.join(self.var, C.APPLIANCES_NAME)) as fh:
            self.assertEqual(json.load(fh)["appliances"], [])
        self.stop(d2)

    def test_disable_while_running_clears_and_exits(self) -> None:
        self.write_conf()
        self.prelink()
        d = self.start(self.daemon())
        self.wait(lambda: self.topic(DEV_A, "controls/running") == "1", "published")
        self.write_conf(enabled=False)
        self.wait(lambda: not self.threads[0][1].is_alive(), "exit on disable")
        self.assertEqual(self.threads[0][2]["rc"], 0)
        self.assertEqual(self.status()["state"], C.STATE_DISABLED)
        self.assertEqual(self.topic(DEV_A, "controls/running"), "")
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "inventory.json")))
        self.assertTrue(os.path.exists(self.tokens_path))  # disable keeps the sign-in

    def test_disabled_start_cleans_leftovers_and_exits(self) -> None:
        self.write_conf(enabled=False)
        with open(os.path.join(self.var, C.APPLIANCES_NAME), "w") as fh:
            json.dump({"appliances": [{"ha_id": HA_A, "device_id": DEV_A}]}, fh)
        os.chmod(os.path.join(self.var, C.APPLIANCES_NAME), 0o600)
        self.assertEqual(self.daemon().run(), 0)
        self.assertEqual(self.status()["state"], C.STATE_DISABLED)
        self.assertEqual(self.topic(DEV_A, "meta/error"), "")
        self.assertEqual(self.topic(DEV_A, "controls/running"), "")
        self.assertEqual(self.fake.calls(), [])

    def test_revoked_refresh_token(self) -> None:
        self.write_conf()
        self.prelink(expires_in=100)  # inside REFRESH_BEFORE_S ⇒ refresh first
        with open(os.path.join(self.var, C.APPLIANCES_NAME), "w") as fh:
            json.dump({"appliances": [{"ha_id": HA_A, "device_id": DEV_A}]}, fh)
        os.chmod(os.path.join(self.var, C.APPLIANCES_NAME), 0o600)
        self.fake.refresh_script = ["invalid_grant"]
        d = self.start(self.daemon())
        self.wait(lambda: self.status().get("state") == C.STATE_TOKEN_REVOKED, "token_revoked")
        self.assertEqual(self.topic(DEV_A, "meta/error"), "r")  # kept, flagged
        loaded = TokenStore(self.tokens_path).load()
        self.assertIsNone(loaded.tokens)
        self.assertGreater(loaded.revoked_at, 0)
        self.stop(d)
        d2 = self.start(self.daemon())
        self.wait(lambda: self.status().get("state") == C.STATE_TOKEN_REVOKED
                  and self.status().get("message") != "stopped", "revoked survives restart")
        self.stop(d2)
        self.assert_no_secret_anywhere()

    def test_429_is_rate_limited_with_until(self) -> None:
        self.write_conf()
        self.prelink()
        self.fake.overrides[C.APPLIANCES_PATH] = [(429, {"Retry-After": "120"}, {})]
        t0 = int(time.time())
        d = self.start(self.daemon())
        self.wait(lambda: self.status().get("state") == C.STATE_RATE_LIMITED, "rate_limited")
        st = self.status()
        self.assertEqual(st["reason"], C.REASON_RETRY_AFTER)
        self.assertGreaterEqual(st["rate_limited_until"], t0 + 119)
        calls = len(self.fake.calls())
        time.sleep(0.3)
        self.assertEqual(len(self.fake.calls()), calls)  # nothing sent while blocked
        self.stop(d)

    def test_missing_client_id_calls_nothing(self) -> None:
        self.write_conf(client_id="")
        d = self.start(self.daemon())
        self.wait(lambda: self.status().get("state") == C.STATE_MISSING_CLIENT_ID, "missing_client_id")
        self.stop(d)
        self.assertEqual(self.fake.calls(), [])

    def test_link_expired(self) -> None:
        self.write_conf(link_requested_at=int(time.time()))
        self.fake.poll_script = ["expired_token"]
        d = self.start(self.daemon())
        self.wait(lambda: self.status().get("state") == C.STATE_LINK_EXPIRED, "link_expired")
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "link.json")))
        self.stop(d)

    def test_stale_link_request_is_not_honoured(self) -> None:
        self.write_conf(link_requested_at=int(time.time()) - C.LINK_REQUEST_TTL_S - 10)
        d = self.start(self.daemon())
        self.wait(lambda: self.status().get("state") == C.STATE_UNLINKED, "unlinked")
        self.stop(d)
        self.assertEqual(self.fake.calls(C.DEVICE_AUTH_PATH), [])

    def test_insecure_token_store_is_error_and_unused(self) -> None:
        self.write_conf()
        self.prelink()
        os.chmod(self.tokens_path, 0o644)
        d = self.start(self.daemon())
        self.wait(lambda: self.status().get("reason") == C.REASON_TOKEN_STORE_INSECURE, "insecure")
        self.assertEqual(self.status()["state"], C.STATE_ERROR)
        self.stop(d)
        self.assertEqual([r for r in self.fake.calls() if r["path"].startswith("/api/")], [])

    def test_missing_deps_is_standby(self) -> None:
        self.write_conf()
        rc = self.daemon(check_dependencies=lambda: ["paho.mqtt.client"]).run()
        self.assertEqual(rc, 0)
        st = self.status()
        self.assertEqual(st["state"], C.STATE_MISSING_DEPS)
        self.assertIn("paho", st["message"])

    def test_depaired_appliance_topics_removed(self) -> None:
        self.write_conf()
        self.prelink()
        self.fake.sse_scripts = [["WAIT:depair", sse_event("DEPAIRED", {"haId": HA_B}), "HANG"]]
        d = self.start(self.daemon())
        self.wait(lambda: self.status().get("state") == C.STATE_CONNECTED, "connected")
        self.assertIsNotNone(self.topic(DEV_B, "meta"))
        self.fake.gate("depair").set()
        self.wait(lambda: self.topic(DEV_B, "meta") == "", "B removed")
        self.wait(lambda: self.status().get("appliances") == 1, "count 1")
        self.stop(d)

    # ── value heartbeat (Phase-3 staleness: consumers age values after 90 s) ─
    def value_resends(self, since: int, dev: str) -> List[Any]:
        """Control-VALUE publishes for `dev` after log index `since`."""
        prefix = "/devices/%s/controls/" % dev
        return [(t, p) for t, p in self.link.log[since:]
                if t.startswith(prefix) and "/" not in t[len(prefix):]]

    def test_value_heartbeat_only_while_stream_alive(self) -> None:
        self.write_conf()
        self.prelink()
        self.fake.sse_scripts = [[sse_event("KEEP-ALIVE"), "WAIT:drop"]]
        with mock.patch.object(M, "VALUE_HEARTBEAT_S", 0.15), \
                mock.patch.object(M, "STREAM_QUIET_MAX_S", 30.0):
            d = self.start(self.daemon())
            self.wait(lambda: self.status().get("state") == C.STATE_CONNECTED, "connected")
            self.wait(lambda: self.topic(DEV_A, "controls/remaining_s") == "1800", "detail read")
            self.wait(lambda: self.topic(DEV_A, "meta/error") == "", "A live")
            mark = len(self.link.log)
            calls_before = len(self.fake.calls())
            self.wait(lambda: len([1 for t, _ in self.value_resends(mark, DEV_A)
                                   if t.endswith("/controls/running")]) >= 2,
                      "running re-sent twice by the heartbeat (value unchanged)")
            resent = self.value_resends(mark, DEV_A)
            self.assertIn(("/devices/%s/controls/running" % DEV_A, "1"), resent)
            self.assertIn(("/devices/%s/controls/remaining_s" % DEV_A, "1800"), resent)
            # Disconnected appliance ("r"): its values are not known-current.
            self.assertEqual(self.value_resends(mark, DEV_B), [])
            # Values only: no meta topic rides the heartbeat.
            self.assertFalse(any("/meta" in t for t, _ in self.link.log[mark:]
                                 if not t.endswith("/meta/error")))
            # No cloud call for it: the fake saw nothing new, the budget agrees.
            self.assertEqual(len(self.fake.calls()), calls_before)
            self.assert_every_request_charged()

            # Stream down ⇒ the re-sends stop (consumers then age the values).
            self.fake.overrides[C.EVENTS_PATH] = [(503, {}, {})] * 400
            self.fake.gate("drop").set()
            self.wait(lambda: self.status().get("stream") == "down", "stream down")
            mark = len(self.link.log)
            time.sleep(1.2)  # 8 heartbeat periods
            self.assertEqual(self.status().get("stream"), "down")  # still down: window valid
            self.assertEqual(self.value_resends(mark, DEV_A), [])
            self.stop(d)

    def test_value_heartbeat_stops_on_a_silent_stream(self) -> None:
        self.write_conf()
        self.prelink()
        # Up, one keep-alive on the test's signal, then silence (the reader's
        # own deadline — 15 s here — would still call the stream up).
        self.fake.sse_scripts = [["WAIT:ka", sse_event("KEEP-ALIVE"), "HANG"]]
        with mock.patch.object(M, "VALUE_HEARTBEAT_S", 0.1), \
                mock.patch.object(M, "STREAM_QUIET_MAX_S", 0.6):
            d = self.start(self.daemon())
            self.wait(lambda: self.status().get("state") == C.STATE_CONNECTED, "connected")
            self.wait(lambda: self.topic(DEV_A, "meta/error") == "", "A live")
            self.wait(lambda: self.topic(DEV_A, "controls/remaining_s") == "1800", "detail read")
            # Silent since the stream opened: once STREAM_QUIET_MAX_S has passed,
            # nothing is re-sent even though the stream is up.
            time.sleep(0.8)
            mark = len(self.link.log)
            time.sleep(0.4)
            self.assertTrue(d.stream_up)
            self.assertEqual(self.value_resends(mark, DEV_A), [])
            # The keep-alive alone proves the socket alive again: the beat resumes.
            mark = len(self.link.log)
            self.fake.gate("ka").set()
            self.wait(lambda: self.value_resends(mark, DEV_A) != [],
                      "heartbeat resumed by the keep-alive")
            time.sleep(0.8)  # past STREAM_QUIET_MAX_S since the keep-alive
            mark = len(self.link.log)
            time.sleep(0.6)  # 6 heartbeat periods of silence
            self.assertTrue(d.stream_up)  # the reader still calls it up: the window is valid
            self.assertEqual(self.value_resends(mark, DEV_A), [])
            self.stop(d)


if __name__ == "__main__":
    unittest.main()
