"""Unittest gate for the device-side frpc render hardening (O1 + O3).

unittest-style on purpose (no pytest) so the `py-unit-cloud` quality row can
discover it via `unittest discover -p "test_render*.py"` — pytest is not a
device/CI dependency, and the sibling test_agent.py (pytest-style) is excluded
by that pattern.

Asserts the device-side local-port allow-list (O1) and the pinned transport
TLS (O3):
  - a cloud proxy whose local_port is not in {80, 9999} is dropped (e.g. :22
    SSH, :1883 MQTT) while :80 web + :9999 cfg are kept;
  - the legacy single-proxy fallback goes through the same allow-list;
  - all-dropped renders a config with zero [[proxies]] (fail closed);
  - transport.tls.enable = true is present.

Contract: docs/contracts/cloud-enrollment.md (device-side mirror);
cloud repo docs/contracts/cloud-enrollment.md (authority).
"""
import contextlib
import importlib.util
import os
import re
import unittest

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_PATH = os.path.join(AGENT_DIR, "sa02m-cloud-agent.py")

_spec = importlib.util.spec_from_file_location("sa02m_cloud_agent_render", AGENT_PATH)
agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent)

with open(AGENT_PATH, encoding="utf-8") as _f:
    AGENT_SOURCE = _f.read()


def _identity_profile(secret="s3cr3t-token", did="sa02m-abc"):
    """A Phase-C profile carrying the per-device credential the cloud issues at
    enrollment (cloud contract §1 / §0.3)."""
    p = {
        "server_addr": "cloud.cyntron.ru", "server_port": 8890, "token": "t",
        "proxies": [{"name": "dev-%s" % did, "subdomain": did,
                     "local_port": 80, "role": "web"}],
    }
    if secret:
        p["device_id"] = did
        p["device_secret"] = secret
    return p


def _profile(*local_ports):
    """A claim/enroll frpc profile carrying one proxy per given local_port."""
    return {
        "server_addr": "cloud.cyntron.ru",
        "server_port": 8890,
        "token": "test-frp-token",
        "proxies": [
            {"name": "dev-p%d" % lp, "subdomain": "sa02m-p%d" % lp,
             "local_port": lp, "role": "x"}
            for lp in local_ports
        ],
    }


class AllowListTest(unittest.TestCase):
    def test_allow_list_constant(self):
        self.assertEqual(agent.ALLOWED_LOCAL_PORTS, frozenset({80, 9999}))

    def test_allowed_ports_kept(self):
        toml = agent.render_frpc_toml(_profile(80, 9999))
        self.assertEqual(toml.count("[[proxies]]"), 2)
        self.assertIn("localPort = 80", toml)
        self.assertIn("localPort = 9999", toml)

    def test_ssh_port_dropped_others_kept(self):
        # A compromised cloud pushes :22 (SSH) alongside the legit :80 (A5).
        toml = agent.render_frpc_toml(_profile(22, 80))
        self.assertEqual(toml.count("[[proxies]]"), 1)
        self.assertIn("localPort = 80", toml)
        self.assertNotIn("localPort = 22", toml)

    def test_mqtt_port_dropped(self):
        toml = agent.render_frpc_toml(_profile(1883))
        self.assertEqual(toml.count("[[proxies]]"), 0)

    def test_all_disallowed_fails_closed(self):
        toml = agent.render_frpc_toml(_profile(22, 1883))
        self.assertEqual(toml.count("[[proxies]]"), 0)
        # A valid top section still renders — no tunnel, not a malicious one.
        self.assertIn('serverAddr = "cloud.cyntron.ru"', toml)

    def test_legacy_fallback_disallowed_dropped(self):
        legacy = {
            "server_addr": "cloud.cyntron.ru", "server_port": 8890, "token": "t",
            "proxy_name": "dev-x", "subdomain": "sa02m-x", "local_port": 22,
        }
        toml = agent.render_frpc_toml(legacy)
        self.assertEqual(toml.count("[[proxies]]"), 0)

    def test_legacy_fallback_allowed_kept(self):
        legacy = {
            "server_addr": "cloud.cyntron.ru", "server_port": 8890, "token": "t",
            "proxy_name": "dev-x", "subdomain": "sa02m-x", "local_port": 9999,
        }
        toml = agent.render_frpc_toml(legacy)
        self.assertEqual(toml.count("[[proxies]]"), 1)
        self.assertIn("localPort = 9999", toml)

    def test_transport_tls_pinned(self):
        toml = agent.render_frpc_toml(_profile(80))
        self.assertIn("transport.tls.enable = true", toml)

    def test_kept_proxies_are_http(self):
        toml = agent.render_frpc_toml(_profile(80, 9999))
        types = [l for l in toml.splitlines() if l.startswith("type = ")]
        self.assertTrue(types)
        self.assertTrue(all(t == 'type = "http"' for t in types))


class PoolCountTest(unittest.TestCase):
    """transport.poolCount comes from the cloud profile (enroll/claim frpc
    object) and is validated as HOSTILE input (A5), the same posture the
    ALLOWED_LOCAL_PORTS allow-list applies to local_port: only a plain int in
    0..16 is emitted; anything else warns and emits NO line (fall back to frpc's
    own default, never an attacker-chosen count of held-open sockets). Absent
    key => no line at all (keeps the legacy render byte-identical)."""

    def _with_pool(self, value):
        p = _profile(80)
        p["pool_count"] = value
        return p

    def test_valid_pool_count_rendered_above_proxies(self):
        toml = agent.render_frpc_toml(self._with_pool(4))
        self.assertIn("transport.poolCount = 4", toml)
        # Top-level transport table: after the pinned TLS line, before [[proxies]].
        self.assertLess(toml.index("transport.tls.enable"),
                        toml.index("transport.poolCount"))
        self.assertLess(toml.index("transport.poolCount"),
                        toml.index("[[proxies]]"))

    def test_absent_key_renders_no_pool_count(self):
        self.assertNotIn("poolCount", agent.render_frpc_toml(_profile(80)))

    def test_boundary_values_accepted(self):
        for v in (0, 16):
            toml = agent.render_frpc_toml(self._with_pool(v))
            self.assertIn("transport.poolCount = %d" % v, toml)

    def test_hostile_values_dropped_with_warning(self):
        # A malicious cloud dictates a resource — reject like a bad port.
        for bad in ("abc", -1, 9999, 4.5, True):
            with self.assertLogs(agent.log, level="WARNING") as cm:
                toml = agent.render_frpc_toml(self._with_pool(bad))
            self.assertNotIn("poolCount", toml,
                             "hostile pool_count %r leaked a line" % (bad,))
            self.assertTrue(any("pool_count" in m for m in cm.output),
                            "no warning for hostile pool_count %r" % (bad,))


class IdentityMetadataTest(unittest.TestCase):
    """Phase C: the credential reaches frps as frpc `metadatas`, which the cloud
    Login hook verifies. Without a credential the render must be unchanged — that
    is the migration (grace) path for a device enrolled before Phase C."""

    def test_metadatas_emitted_when_secret_present(self):
        toml = agent.render_frpc_toml(_identity_profile())
        self.assertIn('metadatas.device_id = "sa02m-abc"', toml)
        self.assertIn('metadatas.device_secret = "s3cr3t-token"', toml)

    def test_metadatas_absent_without_secret(self):
        toml = agent.render_frpc_toml(_identity_profile(secret=""))
        self.assertNotIn("metadatas", toml)

    def test_legacy_render_is_byte_identical(self):
        # The grace guarantee, pinned as EXACT expected text: a profile with no
        # credential must render precisely what it rendered before Phase C.
        # Anything else risks the two devices already live in production.
        expected = "\n".join([
            'serverAddr = "cloud.cyntron.ru"',
            "serverPort = 8890",
            'auth.token = "t"',
            "transport.tls.enable = true",
            "",
            "[[proxies]]",
            'name = "dev-sa02m-abc"',
            'type = "http"',
            'subdomain = "sa02m-abc"',
            'localIP = "127.0.0.1"',
            "localPort = 80",
            "",
        ])
        self.assertEqual(agent.render_frpc_toml(_identity_profile(secret="")), expected)

    def test_metadatas_sit_in_the_top_section_not_a_proxy(self):
        # frp carries login metadata in the TOP-level table; inside [[proxies]] it
        # would never reach the Login hook.
        toml = agent.render_frpc_toml(_identity_profile())
        self.assertLess(toml.index("metadatas.device_id"), toml.index("[[proxies]]"))

    def test_identity_does_not_disturb_the_allow_list(self):
        # The A5 device-side defence stays in force alongside identity.
        p = _identity_profile()
        p["proxies"] = [{"name": "dev-ssh", "subdomain": "x", "local_port": 22}]
        toml = agent.render_frpc_toml(p)
        self.assertEqual(toml.count("[[proxies]]"), 0)
        self.assertIn("metadatas.device_id", toml)

    def test_tls_still_pinned_with_identity(self):
        self.assertIn("transport.tls.enable = true",
                      agent.render_frpc_toml(_identity_profile()))


class DeviceSecretStorageTest(unittest.TestCase):
    """The credential is persisted 0600 — it is a long-lived device identity."""

    def test_roundtrip_and_permissions(self):
        import stat, tempfile, os as _os
        path = _os.path.join(tempfile.mkdtemp(), "device_secret")
        agent.save_device_secret("abc123", path)
        self.assertEqual(agent.load_device_secret(path), "abc123")
        mode = stat.S_IMODE(_os.stat(path).st_mode)
        if _os.name == "posix":       # Windows does not model 0600
            self.assertEqual(mode, 0o600)

    def test_write_failure_reports_the_REAL_error(self):
        """A masked error here is expensive: finalize_enrollment logs this string,
        and under `strict` it is the operator's only warning that a device is
        about to sit Offline with a live tunnel. Double-closing the fd replaced
        the true cause with EBADF."""
        import tempfile, os as _os

        class Boom(Exception):
            pass

        real_fdopen = _os.fdopen

        def exploding_fdopen(fd, mode):
            f = real_fdopen(fd, mode)
            orig_write = f.write

            def write(_data):
                orig_write("")          # keep the fd genuinely open/valid
                raise Boom("disk full")
            f.write = write
            return f

        path = _os.path.join(tempfile.mkdtemp(), "device_secret")
        orig = agent.os.fdopen
        try:
            agent.os.fdopen = exploding_fdopen
            with self.assertRaises(Boom):        # NOT OSError(EBADF)
                agent.save_device_secret("abc123", path)
        finally:
            agent.os.fdopen = orig

    def test_missing_file_gives_empty_string(self):
        import tempfile, os as _os
        self.assertEqual(
            agent.load_device_secret(_os.path.join(tempfile.mkdtemp(), "absent")), "")


class HeartbeatIdentityTest(unittest.TestCase):
    def _run_one_heartbeat(self, secret):
        """Drive active_loop through exactly one heartbeat and return the payload
        it POSTed. Everything that touches the host is stubbed."""
        captured = {}

        def fake_post(url, payload, timeout=15):
            captured["url"] = url
            captured["payload"] = payload
            raise KeyboardInterrupt          # one beat, then unwind

        cfg = agent.load_config("/nonexistent")
        cfg["cloud"]["device_id"] = "sa02m-abc"
        cfg["cloud"]["heartbeat_interval"] = "0"
        cfg["device"]["serial"] = "abc"
        orig = (agent.api_post, agent.load_device_secret, agent.ensure_frpc_running,
                agent.collect_telemetry, agent._write_status)
        try:
            agent.api_post = fake_post
            agent.load_device_secret = lambda *a, **kw: secret
            agent.ensure_frpc_running = lambda *a, **kw: "running"
            agent.collect_telemetry = lambda snap: ({"uptime_s": 1}, None)
            agent._write_status = lambda *a, **kw: None
            try:
                agent.active_loop(cfg)
            except KeyboardInterrupt:
                pass
        finally:
            (agent.api_post, agent.load_device_secret, agent.ensure_frpc_running,
             agent.collect_telemetry, agent._write_status) = orig
        self.assertIn("payload", captured, "active_loop sent no heartbeat")
        self.assertTrue(captured["url"].endswith("/heartbeat"))
        return captured["payload"]

    """The credential travels UP with the heartbeat; nothing travels down.

    The send-only guarantee (threat-model F1 — the removed root command channel)
    is pinned in the pytest-only sibling test_agent.py, which the `py-unit-cloud`
    quality row does NOT run. Mirroring it here puts it inside the gate that
    actually executes on every build."""

    def test_secret_is_attached_to_the_heartbeat_payload(self):
        # Behavioural, not a source grep: drive the real active_loop once with a
        # stored credential and capture what it actually POSTs.
        sent = self._run_one_heartbeat(secret="s3cr3t")
        self.assertEqual(sent.get("device_secret"), "s3cr3t")
        self.assertIn("telemetry", sent)

    def test_no_secret_field_when_the_device_has_none(self):
        # The legacy/grace path must not send an empty credential — absent means
        # absent, or the cloud would verify "" against a stored secret and 403.
        sent = self._run_one_heartbeat(secret="")
        self.assertNotIn("device_secret", sent)

    def test_heartbeat_response_is_never_captured(self):
        # Same assertion as test_agent.py::test_heartbeat_response_not_interpreted.
        m = re.search(r"(\w+\s*=\s*)?api_post\(f?\"?\{?api_url\}?/heartbeat",
                      AGENT_SOURCE)
        self.assertIsNotNone(m, "heartbeat call not found")
        self.assertIsNone(m.group(1),
                          "heartbeat response captured - send-only violated")

    def test_no_command_channel_reintroduced(self):
        for needle in ("def handle_command", "commands/pending", "restart_mplc"):
            self.assertNotIn(needle, AGENT_SOURCE)


class RevocationHandlingTest(unittest.TestCase):
    """Contract §4: repeated login refusal => de-enrolled, stop dialing.

    Honesty rule: only a reason the SERVER actually stated counts as revocation.
    An unreachable cloud or a crashed frpc is not a revocation and must never be
    reported as one."""

    def _fake_run(self, stdout, returncode=0, calls=None):
        class R(object):
            pass
        r = R()
        r.stdout = stdout
        r.returncode = returncode

        def run(args, **kw):
            if calls is not None:
                calls.append(args)
            return r
        return run

    @contextlib.contextmanager
    def _standby_env(self, unit_alive_after_settle, reject_reason,
                     repair_after_cycles=None):
        """Stub the host surface revoked_standby touches.

        `unit_alive_after_settle` models the real distinction: `is-active` says 0
        right after spawn, but a login-refused frpc is gone by the time the settle
        elapses. `repair_after_cycles` makes the web-UI pairing file appear so a
        non-recovering run still terminates."""
        state = {"cycles": 0, "settled": False}

        def fake_systemctl(*args, **kw):
            if args and args[0] == "is-active":
                # Only consulted after the settle, by design.
                return 0 if unit_alive_after_settle else 1
            return 0

        def fake_sleep(_s):
            state["settled"] = True

        def fake_exists(path):
            return (repair_after_cycles is not None
                    and state["cycles"] >= repair_after_cycles
                    and path == agent.PAIR_REQUEST_FILE)

        def fake_ensure(*a, **kw):
            state["cycles"] += 1
            return "running"

        orig = (agent._systemctl, agent._write_status, agent.os.path.exists,
                agent.time.sleep, agent.ensure_frpc_running,
                agent.frpc_reject_reason)
        try:
            agent._systemctl = fake_systemctl
            agent._write_status = lambda *a, **kw: None
            agent.os.path.exists = fake_exists
            agent.time.sleep = fake_sleep
            agent.ensure_frpc_running = fake_ensure
            agent.frpc_reject_reason = lambda *a, **kw: reject_reason
            yield state
        finally:
            (agent._systemctl, agent._write_status, agent.os.path.exists,
             agent.time.sleep, agent.ensure_frpc_running,
             agent.frpc_reject_reason) = orig

    def test_server_reject_reason_detected(self):
        orig = agent.subprocess.run
        try:
            agent.subprocess.run = self._fake_run(
                "frpc: login to server failed: device revoked")
            self.assertEqual(agent.frpc_reject_reason(), "device revoked")
        finally:
            agent.subprocess.run = orig

    def test_identity_required_reason_detected(self):
        orig = agent.subprocess.run
        try:
            agent.subprocess.run = self._fake_run("reject: device identity required")
            self.assertEqual(agent.frpc_reject_reason(), "device identity required")
        finally:
            agent.subprocess.run = orig

    def test_ordinary_network_failure_is_NOT_a_revocation(self):
        orig = agent.subprocess.run
        try:
            agent.subprocess.run = self._fake_run(
                "dial tcp 84.201.134.96:8890: connect: connection refused")
            self.assertEqual(agent.frpc_reject_reason(), "")
        finally:
            agent.subprocess.run = orig

    def test_journalctl_unavailable_is_NOT_a_revocation(self):
        orig = agent.subprocess.run
        try:
            def boom(*a, **kw):
                raise OSError("no journalctl")
            agent.subprocess.run = boom
            self.assertEqual(agent.frpc_reject_reason(), "")
            agent.subprocess.run = self._fake_run("", returncode=1)
            self.assertEqual(agent.frpc_reject_reason(), "")
        finally:
            agent.subprocess.run = orig


    def test_journal_scan_is_time_bounded(self):
        # The stale-marker bug: an UNBOUNDED scan re-read a rejection from a
        # previous life, so a re-paired device stood itself down on the next
        # network blip. Every journalctl read must carry --since.
        calls = []
        orig = agent.subprocess.run
        try:
            agent.subprocess.run = self._fake_run("nothing here", calls=calls)
            agent.frpc_reject_reason()
        finally:
            agent.subprocess.run = orig
        journal = [a for a in calls if a and a[0] == "journalctl"]
        self.assertTrue(journal, "journalctl was never called")
        for args in journal:
            self.assertIn("--since", args)
            self.assertTrue(args[args.index("--since") + 1],
                            "--since given an empty bound")

    def test_unit_active_since_rejects_implausible_values(self):
        orig = agent.subprocess.run
        try:
            for junk in ("n/a", "", "not a timestamp"):
                agent.subprocess.run = self._fake_run(junk)
                self.assertEqual(agent._unit_active_since("x"), "")
            agent.subprocess.run = self._fake_run("Fri 2026-07-18 13:00:00 MSK")
            self.assertEqual(agent._unit_active_since("x"),
                             "Fri 2026-07-18 13:00:00 MSK")
        finally:
            agent.subprocess.run = orig

    def test_stand_down_never_exits_the_process(self):
        # sa02m-cloud-agent.service is Restart=on-failure, which does NOT restart
        # a clean exit — so returning out of main() would strand the device until
        # a human SSHes in. main() must loop instead.
        body = AGENT_SOURCE[AGENT_SOURCE.index("def main("):]
        self.assertIn("while True:", body,
                      "main() must not fall off the end after active_loop")
        self.assertNotIn("sys.exit", body)

    def test_stand_down_recovers_on_a_pairing_request(self):
        # The web-UI "connect" button must break the stand-down without SSH.
        orig = (agent._systemctl, agent._write_status, agent.os.path.exists,
                agent.time.sleep)
        try:
            agent._systemctl = lambda *a, **kw: 0
            agent._write_status = lambda *a, **kw: None
            agent.os.path.exists = lambda p: p == agent.PAIR_REQUEST_FILE
            agent.time.sleep = lambda s: None
            cfg = agent.load_config("/nonexistent")
            cfg["device"]["serial"] = "abc"
            self.assertEqual(
                agent.revoked_standby(cfg, "sa02m-abc", "device revoked"), "repair")
        finally:
            (agent._systemctl, agent._write_status, agent.os.path.exists,
             agent.time.sleep) = orig

    def test_stand_down_recovers_when_the_cloud_relents(self):
        # No pairing request, but the re-test finds the tunnel up AND the unit
        # still alive after the settle AND the journal clean -> resume on our
        # own, no truck roll.
        with self._standby_env(unit_alive_after_settle=True, reject_reason=""):
            cfg = agent.load_config("/nonexistent")
            cfg["device"]["serial"] = "abc"
            self.assertEqual(
                agent.revoked_standby(cfg, "sa02m-abc", "device revoked"), "recovered")

    def test_stand_down_does_NOT_recover_while_the_cloud_still_refuses(self):
        # The false-recovery shape: frpc is Type=simple, so is-active returns 0
        # the instant it spawns — before it has tried to log in — and the journal
        # window is briefly empty. Declaring "recovered" there makes the device
        # flap active/revoked. The unit must SURVIVE the settle.
        with self._standby_env(unit_alive_after_settle=False, reject_reason="",
                               repair_after_cycles=2) as state:
            cfg = agent.load_config("/nonexistent")
            cfg["device"]["serial"] = "abc"
            self.assertEqual(
                agent.revoked_standby(cfg, "sa02m-abc", "device revoked"), "repair")
        self.assertGreaterEqual(state["cycles"], 1,
                                "the re-test never ran")

    def test_stand_down_does_NOT_recover_when_the_journal_still_shows_refusal(self):
        with self._standby_env(unit_alive_after_settle=True,
                               reject_reason="device revoked",
                               repair_after_cycles=2) as state:
            cfg = agent.load_config("/nonexistent")
            cfg["device"]["serial"] = "abc"
            self.assertEqual(
                agent.revoked_standby(cfg, "sa02m-abc", "device revoked"), "repair")


    def test_active_loop_reaches_stand_down_after_repeated_refusals(self):
        """The TRIGGER path, which nothing else covers.

        `revoked_standby` and the journal parser are tested directly, but the
        transition into them — FRPC_FAIL_CYCLES consecutive failed watchdog
        samples, then a server-stated reason — is what makes the whole revocation
        path fire at all. Drive it end to end."""
        seen = {}

        def fake_standby(cfg, device_id, reason):
            seen["reason"] = reason
            seen["device_id"] = device_id
            return "repair"

        orig = (agent.ensure_frpc_running, agent.frpc_reject_reason,
                agent.revoked_standby, agent.time.sleep, agent._write_status,
                agent.load_device_secret, agent.api_post, agent.WATCHDOG_S)
        try:
            agent.ensure_frpc_running = lambda *a, **kw: "failed"
            agent.frpc_reject_reason = lambda *a, **kw: "device revoked"
            agent.revoked_standby = fake_standby
            agent.time.sleep = lambda _s: None
            agent._write_status = lambda *a, **kw: None
            agent.load_device_secret = lambda *a, **kw: "s3cr3t"
            agent.api_post = lambda *a, **kw: (200, {"ok": True})
            agent.WATCHDOG_S = -1          # every iteration is a watchdog sample
            cfg = agent.load_config("/nonexistent")
            cfg["cloud"]["device_id"] = "sa02m-abc"
            cfg["cloud"]["heartbeat_interval"] = "999999"   # keep heartbeats out
            cfg["device"]["serial"] = "abc"
            self.assertEqual(agent.active_loop(cfg), "repair")
        finally:
            (agent.ensure_frpc_running, agent.frpc_reject_reason,
             agent.revoked_standby, agent.time.sleep, agent._write_status,
             agent.load_device_secret, agent.api_post, agent.WATCHDOG_S) = orig
        self.assertEqual(seen.get("reason"), "device revoked")
        self.assertEqual(seen.get("device_id"), "sa02m-abc")

    def test_active_loop_does_NOT_stand_down_without_a_server_reason(self):
        """A dead network is not a revocation. The tunnel can fail forever and the
        agent must keep retrying, never claim `revoked`."""
        calls = {"standby": 0, "cycles": 0}

        def fake_standby(*a, **kw):
            calls["standby"] += 1
            return "repair"

        def fake_ensure(*a, **kw):
            calls["cycles"] += 1
            if calls["cycles"] > 25:
                raise KeyboardInterrupt      # unwind a deliberately endless loop
            return "failed"

        orig = (agent.ensure_frpc_running, agent.frpc_reject_reason,
                agent.revoked_standby, agent.time.sleep, agent._write_status,
                agent.load_device_secret, agent.api_post, agent.WATCHDOG_S)
        try:
            agent.ensure_frpc_running = fake_ensure
            agent.frpc_reject_reason = lambda *a, **kw: ""     # no server reason
            agent.revoked_standby = fake_standby
            agent.time.sleep = lambda _s: None
            agent._write_status = lambda *a, **kw: None
            agent.load_device_secret = lambda *a, **kw: ""
            agent.api_post = lambda *a, **kw: (200, {"ok": True})
            agent.WATCHDOG_S = -1
            cfg = agent.load_config("/nonexistent")
            cfg["cloud"]["device_id"] = "sa02m-abc"
            cfg["cloud"]["heartbeat_interval"] = "999999"
            cfg["device"]["serial"] = "abc"
            try:
                agent.active_loop(cfg)
            except KeyboardInterrupt:
                pass
        finally:
            (agent.ensure_frpc_running, agent.frpc_reject_reason,
             agent.revoked_standby, agent.time.sleep, agent._write_status,
             agent.load_device_secret, agent.api_post, agent.WATCHDOG_S) = orig
        self.assertEqual(calls["standby"], 0,
                         "stood down on a plain tunnel failure — that is a truck roll")


if __name__ == "__main__":
    unittest.main()
