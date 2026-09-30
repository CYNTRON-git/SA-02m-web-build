"""A conf that EXISTS but cannot be read (EACCES — its group grant was
broken; docs/contracts/home-connect.md §11) is not «disabled». configparser's
read() skips such a file silently, so the client used to read defaults and
stop as if switched off — and, worse, drop every appliance's retained topics.
Now: `missing_deps` + reason `conf_unreadable`, exit 0, at start and while
running, with the appliances marked failing rather than removed. An ABSENT
conf still means disabled.

As root a 0000 file is still readable, so open() of exactly that path raises
PermissionError; configparser resolves open() from builtins at call time, so
the pre-fix code meets the same error a real EACCES gives it."""

from __future__ import annotations

import builtins
import contextlib
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from sa02m_homeconnect import config
from sa02m_homeconnect import constants as C
from sa02m_homeconnect import main as M
from sa02m_homeconnect.status import StatusWriter
from sa02m_homeconnect.token_store import TokenSet, TokenStore
from sa02m_homeconnect.transport import NetworkError, Transport

from .test_daemon import CLIENT, FAST, FakeLink


class NoNetworkTransport:
    """Fails every cloud call at once, touching no port. A real connect to a
    closed loopback port is refused at once on one host and times out on
    another (a dropped SYN under WSL mirrored networking), and that made the
    stop race the REST retry back-off — the test is about the conf, not the
    network."""

    def request(self, *_a, **_kw):
        raise NetworkError("no network in this test")

    def open_stream(self, *_a, **_kw):
        raise NetworkError("no network in this test")


@contextlib.contextmanager
def unreadable(path):
    real_open = builtins.open

    def guarded(file, *a, **kw):
        if isinstance(file, (str, bytes, os.PathLike)) and os.fspath(file) == path:
            raise PermissionError(13, "Permission denied", path)
        return real_open(file, *a, **kw)

    with mock.patch("builtins.open", guarded):
        yield


class ConfUnreadableTest(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(C, k, v) for k, v in FAST.items()]
        for p in self.patches:
            p.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.var = os.path.join(self.tmp.name, "var")
        self.run_dir = os.path.join(self.tmp.name, "run")
        os.makedirs(self.var, mode=0o700)
        os.makedirs(self.run_dir)
        self.conf_path = os.path.join(self.tmp.name, "hc.conf")
        self.link = FakeLink()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def write_conf(self, **kw):
        values = dict(enabled=True, client_id=CLIENT)
        values.update(kw)
        config.save(config.ClientConfig(**values), self.conf_path)

    def daemon(self, transport_factory=None):
        return M.Daemon(
            stop=threading.Event(), status=StatusWriter(self.run_dir), conf_path=self.conf_path,
            tokens_path=os.path.join(self.var, C.TOKENS_NAME),
            budget_path=os.path.join(self.var, C.BUDGET_NAME),
            appliances_path=os.path.join(self.var, C.APPLIANCES_NAME),
            base_url_override="http://127.0.0.1:9",
            transport_factory=transport_factory
            or (lambda b: Transport(b, allow_loopback_http=True, timeout=0.2)),
            mqtt_factory=lambda: self.link, check_dependencies=lambda: [])

    def status(self):
        with open(os.path.join(self.run_dir, "status.json")) as fh:
            return json.load(fh)

    # load()
    def test_an_unreadable_conf_is_flagged_not_disabled(self):
        self.write_conf()
        with unreadable(self.conf_path):
            conf = config.load(self.conf_path)
        self.assertTrue(getattr(conf, "unreadable", False))
        self.assertFalse(conf.enabled)

    def test_an_absent_conf_is_still_plain_disabled(self):
        conf = config.load(self.conf_path + ".absent")
        self.assertFalse(getattr(conf, "unreadable", False))
        self.assertFalse(conf.enabled)
        self.assertEqual(conf.warnings, [])

    # the daemon
    def test_at_start_it_is_a_standby_with_a_reason(self):
        self.write_conf()
        with unreadable(self.conf_path), self.assertLogs("sa02m_homeconnect", "ERROR"):
            rc = self.daemon().run()
        self.assertEqual(rc, M.EXIT_OK)
        st = self.status()
        self.assertEqual((st["state"], st["reason"]), (C.STATE_MISSING_DEPS, "conf_unreadable"), st)
        self.assertEqual(self.link.log, [])

    def test_while_running_it_stops_with_the_reason_and_keeps_the_topics(self):
        self.write_conf()
        # One known appliance with a retained topic from an earlier run.
        with open(os.path.join(self.var, C.APPLIANCES_NAME), "w") as fh:
            json.dump({"appliances": [{"ha_id": "BOSCH-X-68A40E000009",
                                       "device_id": "hc-bosch-x-68a40e000009"}]}, fh)
        os.chmod(os.path.join(self.var, C.APPLIANCES_NAME), 0o600)
        # Linked (a valid token), so nothing but the conf decides the topics.
        now = int(time.time())
        TokenStore(os.path.join(self.var, C.TOKENS_NAME)).save(TokenSet(
            access_token="AT-PRE", refresh_token="RT-PRE", expires_at=now + 86400,
            scope=C.SCOPES, host="api", client_id=CLIENT, linked_at=now))
        # The REST calls fail at once and retry with no pause, so the loop
        # reaches its next conf check within ticks, whatever the host does
        # with a connect to a closed port.
        backoff = mock.patch.multiple(C, API_BACKOFF_BASE_S=0.0, API_BACKOFF_MAX_S=0.0)
        backoff.start()
        self.addCleanup(backoff.stop)
        d = self.daemon(transport_factory=lambda _base: NoNetworkTransport())
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("rc", d.run()), daemon=True)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and not os.path.exists(os.path.join(self.run_dir, "status.json")):
            time.sleep(0.02)
        # Capture first: the loop re-reads the conf every CONF_POLL_S, so its
        # ERROR can come the instant the conf turns unreadable.
        with self.assertLogs("sa02m_homeconnect", "ERROR"), unreadable(self.conf_path):
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result.get("rc"), M.EXIT_OK)
        st = self.status()
        self.assertEqual((st["state"], st["reason"]), (C.STATE_MISSING_DEPS, "conf_unreadable"), st)
        # Non-vacuity: the known appliance was loaded (marked failing at start).
        self.assertIn(("/devices/hc-bosch-x-68a40e000009/meta/error", "r"), self.link.log)
        removed = [tp for tp, payload in self.link.log if payload == "" and tp.startswith("/devices/hc-bosch-x")
                   and not tp.endswith("/meta/error")]
        self.assertEqual(removed, [], "an unreadable conf must not drop the appliances' topics")


if __name__ == "__main__":
    unittest.main()
