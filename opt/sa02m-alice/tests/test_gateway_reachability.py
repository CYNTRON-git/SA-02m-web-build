"""Gateway reachability on the status poll: no per-poll network probe, no flicker.

Measured on bench 1.135 (2026-09-28): 8 of 22 status polls in 3 min answered
`gateway.available=false` («timed out», an SSL handshake timeout) while
`status.state` stayed `connected` the whole time, and every status GET took
4-12 s. `full_config()` probed https://alice.cyntron.ru/v1.0/ping with a 5 s
timeout on EVERY 5 s card poll, so one slow ping flipped the pill to «Шлюз
недоступен» and every poll paid the network round trip
(web-code-rigor.md ## Architecture: no per-request network work on a polled
endpoint).

Pinned here (docs/contracts/alice-mqtt-mapping.md §Gateway reachability):
- a live client session (`status.state == connected`, `ts` fresh) IS the
  proof the gateway is reachable — no probe runs, a probe that would time out
  cannot flip the answer;
- otherwise the answer comes from a probe cache in VAR_DIR, refreshed at most
  once per TTL; under the CGI the refresh is a detached process, never inline;
- one failed probe after a success does not flip the answer; a second
  consecutive failure does.
"""

from __future__ import annotations

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

from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import api  # noqa: E402

PROBE_UP = {"ok": True, "available": True, "url": "https://alice.cyntron.ru/v1.0/ping", "http_status": 200}
PROBE_TIMEOUT = {
    "ok": True,
    "available": False,
    "url": "https://alice.cyntron.ru/v1.0/ping",
    "error": "gateway_unreachable",
    "message": "The read operation timed out",
}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        etc = os.path.join(self.tmp.name, "etc")
        os.makedirs(etc)
        self.var = os.path.join(self.tmp.name, "var")
        os.makedirs(self.var)
        self.status_path = os.path.join(self.tmp.name, "run", "status.json")
        saved = {k: getattr(C, k) for k in (
            "ETC_DIR", "CLIENT_CONF", "DEVICES_CONF", "SERVER_CONF", "VAR_DIR",
            "CERT_FILE", "KEY_FILE", "CA_FILE", "PENDING_CLAIM_FILE", "STATUS_FILE",
        )}
        self.addCleanup(lambda: [setattr(C, k, v) for k, v in saved.items()])
        C.ETC_DIR = etc
        C.CLIENT_CONF = os.path.join(etc, "sa02m-alice-client.conf")
        C.DEVICES_CONF = os.path.join(etc, "sa02m-alice-devices.conf")
        C.SERVER_CONF = os.path.join(etc, "sa02m-alice-server.conf")
        C.VAR_DIR = self.var
        C.CERT_FILE = os.path.join(self.var, "device.crt.pem")
        C.KEY_FILE = os.path.join(self.var, "device.key.pem")
        C.CA_FILE = os.path.join(self.var, "ca.crt.pem")
        C.PENDING_CLAIM_FILE = os.path.join(self.var, "pending_claim.json")
        C.STATUS_FILE = self.status_path
        with open(C.CLIENT_CONF, "w", encoding="utf-8") as fh:
            fh.write("[client]\nclient_enabled = true\n")
        with open(C.DEVICES_CONF, "w", encoding="utf-8") as fh:
            fh.write('{"rooms":[],"devices":[]}\n')
        with open(C.SERVER_CONF, "w", encoding="utf-8") as fh:
            fh.write(
                "[gateway]\nhttp_url = https://alice.cyntron.ru\n"
                "wss_url = wss://alice.cyntron.ru/controller/socket.io\n"
            )
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SA02M_ALICE_PROBE_REFRESH", None)

    def write_status(self, **payload):
        os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
        with open(self.status_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def gateway(self, probe=PROBE_UP):
        with mock.patch.object(api, "probe_gateway", return_value=probe) as p:
            cfg = api.full_config()
        return cfg["gateway"], cfg, p.call_count


class TestConnectedClientIsTheProof(_Base):
    def test_probe_timeout_while_connected_still_reads_available(self):
        """The measured 1.135 flicker: the client is connected, the ping times out."""
        self.write_status(state="connected", ts=int(time.time()), client_enabled=True, cert_present=True)
        gw, cfg, calls = self.gateway(PROBE_TIMEOUT)
        self.assertTrue(gw["available"], gw)
        self.assertEqual(gw.get("state"), "reachable")
        self.assertEqual(gw.get("source"), "client")
        self.assertTrue(cfg["link"]["linked"])
        self.assertEqual(calls, 0, "a connected client must not cost a network probe per poll")

    def test_connected_status_without_ts_is_trusted(self):
        # A client older than the 30 s heartbeat writes no `ts`: its state is
        # what full_config has always trusted for link.linked.
        self.write_status(state="connected", client_enabled=True, cert_present=True)
        gw, _, calls = self.gateway(PROBE_TIMEOUT)
        self.assertTrue(gw["available"])
        self.assertEqual(calls, 0)

    def test_stale_connected_status_falls_back_to_the_probe(self):
        # A client that died without rewriting its file must not keep the card
        # green: past STATUS_STALE_S the status is not proof any more.
        self.write_status(state="connected", ts=int(time.time()) - C.STATUS_STALE_S - 5,
                          client_enabled=True, cert_present=True)
        gw, _, calls = self.gateway(PROBE_TIMEOUT)
        self.assertEqual(calls, 1)
        self.assertFalse(gw["available"])
        self.assertEqual(gw.get("source"), "probe")


class TestProbeCache(_Base):
    def test_fresh_cache_answers_without_a_probe(self):
        self.write_status(state="missing_cert", ts=int(time.time()), client_enabled=True)
        gw, _, calls = self.gateway(PROBE_UP)
        self.assertEqual(calls, 1)
        self.assertTrue(gw["available"])
        for _ in range(5):
            gw, _, calls = self.gateway(PROBE_TIMEOUT)
            self.assertEqual(calls, 0, "within the TTL the poll must not probe again")
            self.assertTrue(gw["available"])

    def test_one_failure_after_success_does_not_flip_two_do(self):
        self.write_status(state="missing_cert", ts=int(time.time()), client_enabled=True)
        now = [1_000_000.0]
        with mock.patch.object(api.time, "time", side_effect=lambda: now[0]):
            gw, _, _ = self.gateway(PROBE_UP)
            self.assertTrue(gw["available"])
            now[0] += C.GATEWAY_PROBE_TTL_S + 1
            gw, _, calls = self.gateway(PROBE_TIMEOUT)
            self.assertEqual(calls, 1)
            self.assertTrue(gw["available"], "one timed-out probe must not flip the pill")
            now[0] += C.GATEWAY_PROBE_RETRY_S + 1
            gw, _, calls = self.gateway(PROBE_TIMEOUT)
            self.assertEqual(calls, 1)
            self.assertFalse(gw["available"], "two consecutive failures are unavailable")
            self.assertEqual(gw.get("state"), "unreachable")
            self.assertEqual(gw["probe"].get("message"), PROBE_TIMEOUT["message"])
            now[0] += C.GATEWAY_PROBE_RETRY_S + 1
            gw, _, _ = self.gateway(PROBE_UP)
            self.assertTrue(gw["available"], "a success clears the failure count")

    def test_first_ever_probe_failing_is_unavailable(self):
        # Nothing to flip from: no success on record.
        self.write_status(state="offline", ts=int(time.time()), client_enabled=True)
        gw, _, _ = self.gateway(PROBE_TIMEOUT)
        self.assertFalse(gw["available"])

    def test_no_var_dir_no_cache_file_created(self):
        os.rmdir(self.var)
        self.write_status(state="offline", ts=int(time.time()), client_enabled=True)
        gw, _, calls = self.gateway(PROBE_UP)
        self.assertEqual(calls, 1)
        self.assertTrue(gw["available"])
        self.assertFalse(os.path.exists(self.var), "the web layer must not create the state dir")


class TestCorruptCache(_Base):
    """A cache file with a malformed field must not take the status poll down
    (review A5, 1.0.6.58): `int(cache["fail_count"])` raised on a non-numeric
    value, and the ValueError escaped full_config() — the card lost its whole
    answer, not just the gateway pill. A corrupt cache is no evidence: it is
    treated as absent, refreshed, and overwritten."""

    def write_cache(self, **fields):
        cache = {"ts": time.time(), "probe": dict(PROBE_TIMEOUT), "ok_ts": time.time()}
        cache.update(fields)
        with open(api._probe_cache_path(), "w", encoding="utf-8") as fh:
            json.dump(cache, fh)

    def test_non_numeric_fail_count_does_not_crash_the_poll(self):
        self.write_status(state="offline", ts=int(time.time()), client_enabled=True)
        for bad in ("x", [1], {"n": 1}, True):
            with self.subTest(fail_count=bad):
                self.write_cache(fail_count=bad)
                gw, _, calls = self.gateway(PROBE_UP)
                self.assertEqual(calls, 1, "a corrupt cache is refreshed, not trusted")
                self.assertTrue(gw["available"])
                self.assertEqual(api._read_probe_cache()["fail_count"], 0, "the refresh overwrites the bad field")

    def test_non_numeric_ok_ts_is_not_a_success_on_record(self):
        self.write_status(state="offline", ts=int(time.time()), client_enabled=True)
        self.write_cache(ok_ts="yesterday", fail_count=0)
        gw, _, calls = self.gateway(PROBE_TIMEOUT)
        self.assertEqual(calls, 1)
        self.assertFalse(gw["available"], "a malformed ok_ts must not stand in for a success")

    # Review A10 (1.0.6.58 round 2): json.load parses NaN / Infinity, both are
    # floats, and int(cache["ts"]) then raised out of gateway_reachability —
    # with `due` False the cache was never refreshed, so the card stayed broken.
    # A ts in the future (a backward clock step) froze the last verdict.
    def test_non_finite_ts_does_not_break_the_poll(self):
        self.write_status(state="offline", ts=int(time.time()), client_enabled=True)
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(ts=bad):
                self.write_cache(ts=bad, fail_count=5, ok_ts=None)
                gw, _, calls = self.gateway(PROBE_UP)
                self.assertEqual(calls, 1, "a non-finite ts is no evidence: refresh")
                self.assertTrue(gw["available"])

    def test_future_ts_does_not_freeze_the_verdict(self):
        self.write_status(state="offline", ts=int(time.time()), client_enabled=True)
        self.write_cache(ts=time.time() + 3600, fail_count=5, ok_ts=None)
        gw, _, calls = self.gateway(PROBE_UP)
        self.assertEqual(calls, 1, "a cache stamped in the future is no evidence: refresh")
        self.assertTrue(gw["available"], "a stale «unreachable» must not survive a backward clock step")

    def test_small_clock_skew_is_tolerated(self):
        # Not every future ts is corrupt: the refresher and the poll read the
        # clock a moment apart. Within the skew the cache is still evidence.
        self.write_status(state="offline", ts=int(time.time()), client_enabled=True)
        self.write_cache(ts=time.time() + C.GATEWAY_PROBE_CLOCK_SKEW_S / 2, probe=dict(PROBE_UP), fail_count=0)
        gw, _, calls = self.gateway(PROBE_TIMEOUT)
        self.assertEqual(calls, 0)
        self.assertTrue(gw["available"])

    def test_non_finite_or_future_ok_ts_is_not_a_success_on_record(self):
        self.write_status(state="offline", ts=int(time.time()), client_enabled=True)
        for bad in (float("nan"), float("inf"), time.time() + 3600):
            with self.subTest(ok_ts=bad):
                self.write_cache(ok_ts=bad, fail_count=0)
                gw, _, calls = self.gateway(PROBE_TIMEOUT)
                self.assertEqual(calls, 1)
                self.assertFalse(gw["available"])


class TestCgiNeverProbesInline(_Base):
    def setUp(self):
        super().setUp()
        os.environ["SA02M_ALICE_PROBE_REFRESH"] = "spawn"
        self.write_status(state="missing_cert", ts=int(time.time()), client_enabled=True)

    def test_no_cache_spawns_and_reports_checking(self):
        with mock.patch.object(api, "_spawn_probe_refresh", return_value=True) as spawn:
            gw, _, calls = self.gateway(PROBE_TIMEOUT)
        self.assertEqual(calls, 0, "the CGI poll must never probe inline")
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(gw.get("state"), "checking")
        self.assertFalse(gw["available"])

    def test_stale_cache_answers_last_value_and_spawns(self):
        now = [2_000_000.0]
        with mock.patch.object(api.time, "time", side_effect=lambda: now[0]):
            with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP):
                api.refresh_probe_cache()
            now[0] += C.GATEWAY_PROBE_TTL_S + 1
            with mock.patch.object(api, "_spawn_probe_refresh", return_value=True) as spawn:
                gw, _, calls = self.gateway(PROBE_TIMEOUT)
            self.assertEqual(calls, 0)
            self.assertEqual(spawn.call_count, 1)
            self.assertTrue(gw["available"])
            now[0] += C.GATEWAY_PROBE_MAX_AGE_S
            with mock.patch.object(api, "_spawn_probe_refresh", return_value=True):
                gw, _, _ = self.gateway(PROBE_TIMEOUT)
            self.assertEqual(gw.get("state"), "checking", "a cache nobody refreshed is not evidence")

    def test_fresh_cache_does_not_spawn(self):
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP):
            api.refresh_probe_cache()
        with mock.patch.object(api, "_spawn_probe_refresh", return_value=True) as spawn:
            gw, _, calls = self.gateway(PROBE_TIMEOUT)
        self.assertEqual((calls, spawn.call_count), (0, 0))
        self.assertTrue(gw["available"])

    def test_refresh_lock_admits_one_refresher(self):
        self.assertTrue(api._take_probe_lock())
        self.assertFalse(api._take_probe_lock(), "a second refresher must not start")
        api._release_probe_lock()
        self.assertTrue(api._take_probe_lock())
        api._release_probe_lock()

    def test_stale_lock_is_broken(self):
        self.assertTrue(api._take_probe_lock())
        old = time.time() - C.GATEWAY_PROBE_LOCK_STALE_S - 5
        os.utime(api._probe_lock_path(), (old, old))
        self.assertTrue(api._take_probe_lock(), "a lock left by a killed refresher must not wedge the cache")
        api._release_probe_lock()

    @unittest.skipUnless(hasattr(os, "fork"), "detached refresh needs os.fork (POSIX)")
    def test_detached_refresh_writes_the_cache(self):
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP):
            self.assertTrue(api._spawn_probe_refresh())
        deadline = time.time() + 10
        cache = None
        while time.time() < deadline:
            cache = api._read_probe_cache()
            if cache and not os.path.exists(api._probe_lock_path()):
                break
            time.sleep(0.05)
        self.assertIsNotNone(cache, "the detached refresher wrote no cache")
        self.assertTrue(cache["probe"]["available"])
        self.assertFalse(os.path.exists(api._probe_lock_path()), "the refresher must release its lock")


class TestActionPathsStillProbeFresh(_Base):
    def test_start_link_probes_even_with_a_fresh_cache(self):
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP):
            api.refresh_probe_cache()
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_TIMEOUT) as p:
            result = api.start_link()
        self.assertEqual(p.call_count, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "gateway_unavailable")


class TestCgiSelectsSpawnMode(unittest.TestCase):
    def test_cgi_exports_spawn_mode(self):
        cgi = os.path.join(ROOT, "..", "..", "www", "network_config", "cgi-bin", "sa02m_alice_api.cgi")
        with open(cgi, encoding="utf-8") as fh:
            live = [ln for ln in fh.read().splitlines() if not ln.lstrip().startswith("#")]
        self.assertTrue(
            any("SA02M_ALICE_PROBE_REFRESH" in ln and "spawn" in ln and "export" in ln for ln in live),
            "sa02m_alice_api.cgi must select the detached probe refresh — else the poll probes inline",
        )


if __name__ == "__main__":
    unittest.main()
