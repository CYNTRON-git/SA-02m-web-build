"""Unit tests: the cloud-unlink binding reset — and, above all, its inverse.

The dangerous direction here is NOT "the wipe did not happen"; it is "the wipe
happened when it must not have". The office link flaps roughly 33 times per
14 h, so the fleet meets refusals constantly and a wipe-on-refusal defect would
brick working boards. N1-N6 below are therefore assertions about ABSENCE of
change, and they outnumber the positive ones on purpose.

Path idiom is test_cert_status.py's: the modules already hold `C`, so the temp
etc/var/status homes are patched onto the constants module. Unlike that file
this one restores the originals, so ordering between suites cannot leak.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest

NL = chr(10)


class _BudgetExhausted(BaseException):
    """The suite's escape hatch when the code under test will not terminate.

    Deliberately NOT an `Exception` subclass: `run()`'s outer loop carries a
    broad `except Exception`, which would swallow this and hand the hang
    straight back.

    Why raising and not `_stop.set()`: setting the stop flag asks the code
    under test to COOPERATE, and a mutation that breaks exactly that
    cooperation makes the whole suite hang with no named failure. Every budget
    in this file therefore ends in a raise.

    That is only HALF the problem, and saying otherwise is what made the first
    attempt at this wrong. A budget must also be REACHABLE. Round 2's budgets
    all hung off `_stop.wait` and the clock, so a mutation producing a loop
    that touches neither still hung the suite with nothing named - the same
    class one level down (review F-A, second pass). `_BindingBase.cert_ceiling`
    is the reachability half: see it for which call every loop must make.

    A test that passes - or fails - by timing out is not a test.
    """


def _budget(limit, what):
    """(counter, tick) - a call counter whose EXHAUSTION RAISES.

    `tick()` returns the current count so a caller can also drive the
    legitimate termination path (a real stop signal arriving) on the way.
    """
    counter = {"n": 0}

    def tick():
        counter["n"] += 1
        if counter["n"] > limit:
            raise _BudgetExhausted(
                "%s did not terminate within %d iterations" % (what, limit)
            )
        return counter["n"]

    return counter, tick
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client import main as client_main  # noqa: E402
from sa02m_alice.client import sio_connection  # noqa: E402
from sa02m_alice.client.sio_handlers import SioHandlers  # noqa: E402
from sa02m_alice.common import binding_reset  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.common import config_store  # noqa: E402
from sa02m_alice.config import api  # noqa: E402

PROBE_UP = {"ok": True, "available": True, "url": "https://alice.cyntron.ru/v1.0/ping", "http_status": 200}
PROBE_DOWN = {"ok": True, "available": False, "error": "gateway_unreachable", "message": "down"}

_PATCHED = (
    "ETC_DIR", "CLIENT_CONF", "DEVICES_CONF", "SERVER_CONF", "VAR_DIR",
    "CERT_FILE", "KEY_FILE", "CA_FILE", "PENDING_CLAIM_FILE", "STATUS_FILE",
)

CLIENT_CONF_SEED = (
    "[client]\n"
    "client_enabled = true\n"
    "log_level = DEBUG\n"
    "mqtt_host = 10.1.2.3\n"
    "mqtt_port = 1884\n"
)
DEVICES_CONF_SEED = (
    '{"rooms": [{"id": "r1", "name": "\\u0426\\u0435\\u0445"}],\n'
    ' "devices": [{"id": "d1", "name": "\\u041f\\u0438\\u0449\\u0430\\u043b\\u043a\\u0430"}]}\n'
)
SERVER_CONF_SEED = (
    "[gateway]\n"
    "http_url = https://alice.cyntron.ru\n"
    "wss_url = wss://alice.cyntron.ru/controller/socket.io\n"
)


class _BindingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = {name: getattr(C, name) for name in _PATCHED}
        self.addCleanup(lambda: [setattr(C, k, v) for k, v in saved.items()])

        self.etc = os.path.join(self.tmp.name, "etc")
        self.var = os.path.join(self.tmp.name, "var")
        os.makedirs(self.etc)
        os.makedirs(self.var)
        C.ETC_DIR = self.etc
        C.CLIENT_CONF = os.path.join(self.etc, "sa02m-alice-client.conf")
        C.DEVICES_CONF = os.path.join(self.etc, "sa02m-alice-devices.conf")
        C.SERVER_CONF = os.path.join(self.etc, "sa02m-alice-server.conf")
        C.VAR_DIR = self.var
        C.CERT_FILE = os.path.join(self.var, "device.crt.pem")
        C.KEY_FILE = os.path.join(self.var, "device.key.pem")
        C.CA_FILE = os.path.join(self.var, "ca.crt.pem")
        C.PENDING_CLAIM_FILE = os.path.join(self.var, "pending_claim.json")
        self.status_path = os.path.join(self.tmp.name, "run", "status.json")
        C.STATUS_FILE = self.status_path

        self.write(C.CLIENT_CONF, CLIENT_CONF_SEED)
        self.write(C.DEVICES_CONF, DEVICES_CONF_SEED)
        self.write(C.SERVER_CONF, SERVER_CONF_SEED)

        # The unlinked flag is process-global by design (one client process per
        # board); tests must not inherit it from each other.
        client_main._unlinked.clear()
        client_main._stop.clear()
        self.addCleanup(client_main._unlinked.clear)
        self.addCleanup(client_main._stop.clear)

    # -- harness ---------------------------------------------------------
    def cert_ceiling(self, limit=400):
        """A RAISING ceiling on the one call every loop here must make.

        `cert_paths_present()` is reached on every iteration of the outer
        reconnect loop (through `_reconcile_unlink_state` and the
        `_should_wait_for_cert` argument) and on every iteration of
        `_await_cert`. So a ceiling here cannot be stepped around by a
        mutation that produces a hot loop touching neither `_stop.wait` nor
        the clock - which is exactly how `_should_wait_for_cert -> True` hung
        the suite for a full 90 s wall with zero named failures.

        The inner watchdog loop is the one loop that never asks about
        certificates; the `_Clock` tick budget covers that one instead.
        Between them every loop in `client/main.py` has a reachable ceiling.
        """
        real = client_main.cert_paths_present
        _counter, tick = _budget(limit, "the outer loop / no-certificate wait")

        def probe():
            tick()
            return real()

        patcher = mock.patch.object(
            client_main, "cert_paths_present", side_effect=probe
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def nonblocking_stop_wait(self):
        """`_stop.wait` must never really sleep in a test.

        Without this a ceiling still terminates, but at 60 s of real wall per
        iteration - which is a hang by any measure the Operator cares about.
        """
        patcher = mock.patch.object(client_main._stop, "wait", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def write(path, text):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    @staticmethod
    def read(path):
        with io.open(path, "rb") as fh:
            return fh.read()

    def use_ws_gateway(self):
        """A lab ws:// gateway: needs_cert is False on this transport."""
        self.write(
            C.SERVER_CONF,
            "[gateway]" + NL + "http_url = http://127.0.0.1:8899" + NL
            + "wss_url = ws://127.0.0.1:8899/controller/socket.io" + NL,
        )

    def seed_binding(self):
        """A fully bound board, including an atomic-write sidecar."""
        self.write(C.CERT_FILE, "-----BEGIN CERT-----\n")
        self.write(C.KEY_FILE, "-----BEGIN KEY-----\n")
        self.write(C.CA_FILE, "-----BEGIN CA-----\n")
        self.write(C.PENDING_CLAIM_FILE, '{"claim_token": "tok", "controller_sn": "SN1"}\n')
        # The private key one character off any literal list — a crash between
        # the write and the os.replace strands exactly this.
        self.write(C.KEY_FILE + ".tmp", "-----BEGIN STRANDED KEY-----\n")

    def binding_present(self):
        return [
            os.path.basename(p)
            for p in binding_reset.binding_files() + [C.KEY_FILE + ".tmp"]
            if os.path.exists(p)
        ]

    def status(self):
        with io.open(self.status_path, encoding="utf-8") as fh:
            return json.load(fh)


# =====================================================================
# Positive: the wipe itself
# =====================================================================
class TestResetCloudBinding(_BindingBase):
    def test_removes_every_binding_file_and_its_sidecar(self):
        self.seed_binding()
        summary = binding_reset.reset_cloud_binding(binding_reset.SOURCE_GATEWAY)
        self.assertEqual(self.binding_present(), [])
        self.assertEqual(summary["source"], "gateway")
        self.assertEqual(
            sorted(summary["removed"]),
            ["ca.crt.pem", "device.crt.pem", "device.key.pem", "device.key.pem.tmp",
             "pending_claim.json"],
        )
        self.assertEqual(summary["absent"], [])
        self.assertTrue(summary["unlinked_at"])

    def test_ca_is_deleted_here_although_the_image_reset_keeps_it(self):
        """The two clear-lists differ on purpose (contract §3).

        The client never LOADS ca.crt.pem — build_ssl_context takes a cafile
        and discards it — and both start_link and complete_link re-deliver it,
        so removing it here costs nothing and leaves no stale identity behind.
        """
        self.seed_binding()
        binding_reset.reset_cloud_binding(binding_reset.SOURCE_LOCAL)
        self.assertFalse(os.path.exists(C.CA_FILE))

    def test_absent_files_are_reported_not_hidden(self):
        self.write(C.CERT_FILE, "c")
        summary = binding_reset.reset_cloud_binding(binding_reset.SOURCE_GATEWAY)
        self.assertEqual(summary["removed"], ["device.crt.pem"])
        self.assertEqual(
            sorted(summary["absent"]),
            ["ca.crt.pem", "device.key.pem", "pending_claim.json"],
        )

    def test_writes_the_durable_marker_into_client_conf(self):
        self.seed_binding()
        summary = binding_reset.reset_cloud_binding(binding_reset.SOURCE_GATEWAY)
        self.assertEqual(config_store.unlinked_at(), summary["unlinked_at"])
        self.assertIn("unlinked_at", self.read(C.CLIENT_CONF).decode("utf-8"))

    def test_marker_never_lands_in_the_identity_dir(self):
        """A marker file under VAR_DIR would abort the factory-image build:
        patch-firstboot-image.sh accepts nothing there but ca.crt.pem."""
        self.seed_binding()
        binding_reset.reset_cloud_binding(binding_reset.SOURCE_GATEWAY)
        self.assertEqual(sorted(os.listdir(self.var)), [])

    def test_var_dir_itself_survives(self):
        """tmpfiles.d owns the directory — no recursive delete anywhere."""
        self.seed_binding()
        binding_reset.reset_cloud_binding(binding_reset.SOURCE_GATEWAY)
        self.assertTrue(os.path.isdir(self.var))

    def test_sidecar_glob_cannot_reach_the_gateway_ca(self):
        self.seed_binding()
        sidecars = binding_reset._sidecars()
        self.assertEqual([os.path.basename(p) for p in sidecars], ["device.key.pem.tmp"])
        self.assertNotIn(C.CA_FILE, sidecars)


# =====================================================================
# B2 — a failed wipe must never read as a successful one
# =====================================================================
class TestFailedWipeIsNeverReportedAsDone(_BindingBase):
    """The worst lie this feature could tell: the binding is still on disk and
    the card says the board was unlinked in the cloud.

    Every guard here is one token away from being flipped — an `except OSError`
    in place of `except FileNotFoundError`, a `pass` where the `raise` is, a
    dropped `return` in the caller's handler — and each of those was invisible
    to the suite until this class existed (review B2).
    """

    def test_a_permission_error_propagates_and_is_not_reported_as_absent(self):
        self.seed_binding()
        real_unlink = os.unlink

        def deny_the_key(path, *a, **kw):
            if os.path.basename(path) == "device.key.pem":
                raise PermissionError(13, "Permission denied", path)
            return real_unlink(path, *a, **kw)

        with mock.patch("os.unlink", side_effect=deny_the_key):
            with self.assertRaises(PermissionError):
                binding_reset.reset_cloud_binding(binding_reset.SOURCE_GATEWAY)
        # The undeletable key is STILL THERE — the binding was not erased.
        self.assertTrue(os.path.exists(C.KEY_FILE))
        # And nothing recorded the board as unlinked.
        self.assertEqual(config_store.unlinked_at(), "")

    def test_only_a_missing_file_counts_as_already_absent(self):
        """FileNotFoundError is idempotency; any other OSError is a failure.
        Widening the except clause collapses the two and buries a real error."""
        self.write(C.CERT_FILE, "c")
        with mock.patch("os.unlink", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(OSError) as cm:
                binding_reset.reset_cloud_binding(binding_reset.SOURCE_GATEWAY)
        self.assertNotIsInstance(cm.exception, FileNotFoundError)
        self.assertTrue(os.path.exists(C.CERT_FILE))

    def test_the_failure_is_logged_before_it_propagates(self):
        self.seed_binding()
        with mock.patch("os.unlink", side_effect=PermissionError(13, "denied")):
            with self.assertLogs("sa02m_alice.binding_reset", level="ERROR") as cm:
                with self.assertRaises(PermissionError):
                    binding_reset.reset_cloud_binding(binding_reset.SOURCE_GATEWAY)
        self.assertTrue(any("could not remove" in line for line in cm.output))


# =====================================================================
# N4 — idempotency
# =====================================================================
class TestN4Idempotent(_BindingBase):
    def test_second_wipe_on_a_clean_dir_is_a_no_op(self):
        self.seed_binding()
        first = binding_reset.reset_cloud_binding(binding_reset.SOURCE_GATEWAY)
        unrelated = os.path.join(self.var, "keep-me.txt")
        self.write(unrelated, "not ours\n")
        before = self.read(unrelated)
        second = binding_reset.reset_cloud_binding(binding_reset.SOURCE_GATEWAY)
        self.assertEqual(sorted(first.keys()), sorted(second.keys()))
        self.assertEqual(second["removed"], [])
        self.assertEqual(len(second["absent"]), 4)
        self.assertEqual(self.read(unrelated), before)
        self.assertTrue(os.path.isdir(self.var))


# =====================================================================
# N5 — the hard boundary, as a byte-identical assertion
# =====================================================================
class TestN5Boundary(_BindingBase):
    def test_wipe_touches_the_cloud_binding_and_nothing_else(self):
        """§7 of the plan turned into a test.

        devices.conf and server.conf must come out BYTE-IDENTICAL. client.conf
        cannot be byte-identical — the durable marker is one key inside it —
        so the assertion there is per key: every pre-existing key keeps its
        value, `unlinked_at` is the ONLY addition, and client_enabled stays
        ON (switching it off would hide the link button the next owner needs).
        """
        self.seed_binding()
        unrelated_etc = os.path.join(self.etc, "sa02m-alice-unrelated.conf")
        self.write(unrelated_etc, "[other]\nkeep = yes\n")
        cloud_agent = os.path.join(self.etc, "cloud-agent.conf")
        self.write(cloud_agent, "serial = SN1\n")
        before = {p: self.read(p) for p in (C.DEVICES_CONF, C.SERVER_CONF, unrelated_etc, cloud_agent)}

        binding_reset.reset_cloud_binding(binding_reset.SOURCE_GATEWAY)

        for path, blob in before.items():
            self.assertEqual(self.read(path), blob, "%s was modified" % path)

        cfg = config_store.default_client_cfg()
        self.assertEqual(cfg.get("client", "client_enabled"), "true")
        self.assertEqual(cfg.get("client", "mqtt_host"), "10.1.2.3")
        self.assertEqual(cfg.get("client", "mqtt_port"), "1884")
        self.assertEqual(cfg.get("client", "log_level"), "DEBUG")
        self.assertEqual(
            sorted(k for k, _ in cfg.items("client")),
            ["client_enabled", "log_level", "mqtt_host", "mqtt_port", "unlinked_at"],
        )

    def test_the_clear_list_is_exactly_the_four_binding_files(self):
        self.assertEqual(
            sorted(os.path.basename(p) for p in binding_reset.binding_files()),
            ["ca.crt.pem", "device.crt.pem", "device.key.pem", "pending_claim.json"],
        )
        for path in binding_reset.binding_files():
            self.assertTrue(
                os.path.abspath(path).startswith(os.path.abspath(C.VAR_DIR)),
                "%s escapes the identity dir" % path,
            )


# =====================================================================
# The handler: N2 and N6
# =====================================================================
class _Recorder:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


class TestHandlerUnlinkBranch(_BindingBase):
    def make(self, on_unlink=None):
        registry = mock.MagicMock()
        registry.discovery_devices.return_value = []
        registry.query_devices.return_value = []
        registry.apply_actions.return_value = ([], [])
        return SioHandlers(
            registry,
            publish_mqtt=mock.MagicMock(),
            emit_response=mock.MagicMock(),
            on_unlink=on_unlink,
        )

    def test_unlink_event_calls_the_callback(self):
        rec = _Recorder()
        self.make(rec).handle(C.EVT_CONTROLLER_UNLINK, {"reason": "unlinked"})
        self.assertEqual(rec.calls, 1)

    def test_n6_non_dict_payload_still_triggers_the_unlink(self):
        """Any receipt is authoritative — the payload is not the authority,
        the verified mTLS session is. A pre-0.6.0 gateway sends {}, and the
        delivery contract makes every field optional; the dict guard used to
        sit ABOVE this branch and would have dropped a null payload."""
        for payload in (None, "unlinked", 42, [], b"x"):
            rec = _Recorder()
            self.make(rec).handle(C.EVT_CONTROLLER_UNLINK, payload)
            self.assertEqual(rec.calls, 1, "payload %r was dropped" % (payload,))

    def test_empty_dict_payload_triggers_the_unlink(self):
        rec = _Recorder()
        self.make(rec).handle(C.EVT_CONTROLLER_UNLINK, {})
        self.assertEqual(rec.calls, 1)

    def test_n2_no_other_event_can_reach_the_unlink(self):
        rec = _Recorder()
        h = self.make(rec)
        events = [
            C.EVT_DEVICES_LIST,
            C.EVT_DEVICES_QUERY,
            C.EVT_DEVICES_ACTION,
            C.EVT_DEVICE_STATE,
            "alice_devices_response",
            "connect",
            "disconnect",
            "controller_unlin",
            "Controller_Unlink",
            "controller_unlink ",
            "",
        ]
        for event in events:
            for payload in ({"request_id": "r"}, {}, None, "x"):
                h.handle(event, payload)
        self.assertEqual(rec.calls, 0)

    def test_missing_callback_is_reported_not_a_crash(self):
        h = self.make(None)
        with self.assertLogs("sa02m_alice.handlers", level="WARNING") as cm:
            h.handle(C.EVT_CONTROLLER_UNLINK, {})
        self.assertTrue(any("no reset callback" in line for line in cm.output))


# =====================================================================
# The status-overwrite race — the plan's biggest correctness risk
# =====================================================================
class TestStatusSuppression(_BindingBase):
    def test_suppression_rule_truth_table(self):
        for state in (
            C.STATE_OFFLINE, C.STATE_ERROR, C.STATE_DISABLED, C.STATE_CONNECTED,
            C.STATE_CONNECTING, C.STATE_MISSING_CERT, C.STATE_MISSING_DEPS,
        ):
            self.assertFalse(client_main._suppress_status(state, False), state)
            self.assertTrue(client_main._suppress_status(state, True), state)
        self.assertFalse(client_main._suppress_status(C.STATE_UNLINKED, True))
        self.assertFalse(client_main._suppress_status(C.STATE_UNLINKED, False))

    def test_the_four_racing_writers_cannot_overwrite_unlinked(self):
        """main.py writes STATE_OFFLINE on the disconnect the gateway performs
        right after the event, STATE_ERROR on the namespace-connect exception
        the Operator's journal shows, and STATE_DISABLED at both loop entries.
        Each of them raced the unlinked write before this change."""
        # on_unlink sets the flag BEFORE it writes, so the window in which a
        # racing writer could still land is closed by construction. Without the
        # flag every one of those writers overwrites -- that is the defect.
        for state in (C.STATE_OFFLINE, C.STATE_ERROR, C.STATE_DISABLED):
            client_main._write_status(C.STATE_UNLINKED, message=C.UNLINKED_MESSAGE)
            client_main._write_status(state, message="racing write")
            self.assertEqual(self.status()["state"], state)
        client_main._unlinked.set()
        client_main._write_status(C.STATE_UNLINKED, message=C.UNLINKED_MESSAGE)
        for state in (C.STATE_OFFLINE, C.STATE_ERROR, C.STATE_DISABLED,
                      C.STATE_CONNECTING, C.STATE_MISSING_CERT):
            client_main._write_status(state, message="racing write")
            self.assertEqual(self.status()["state"], C.STATE_UNLINKED)
            self.assertEqual(self.status()["message"], C.UNLINKED_MESSAGE)

    def test_a_rebind_lifts_the_suppression(self):
        client_main._unlinked.set()
        client_main._write_status(C.STATE_UNLINKED, message=C.UNLINKED_MESSAGE)
        client_main._unlinked.clear()
        client_main._write_status(C.STATE_CONNECTED, message="Connected to Alice gateway")
        self.assertEqual(self.status()["state"], C.STATE_CONNECTED)

    def test_unlinked_status_still_carries_the_reload_handshake_flag(self):
        """config_watch is a static property of the BINARY and is written in
        every state; the new state must not be the exception, or the web
        trigger loses its skip-the-restart evidence."""
        client_main._write_status(C.STATE_UNLINKED, message=C.UNLINKED_MESSAGE)
        data = self.status()
        self.assertIs(data["config_watch"], True)
        self.assertIs(data["cert_present"], False)


# =====================================================================
# The no-certificate route: this is what stops the reconnect loop
# =====================================================================
class TestCertWaitRouting(_BindingBase):
    def test_state_choice_distinguishes_never_bound_from_unlinked(self):
        state, message, extra = client_main._cert_wait_state("nope", "https://gw")
        self.assertEqual(state, C.STATE_MISSING_CERT)
        self.assertEqual(message, "nope")
        self.assertEqual(extra, {"error": "missing_cert", "gateway_http": "https://gw"})
        client_main._unlinked.set()
        state, message, extra = client_main._cert_wait_state("nope", "https://gw")
        self.assertEqual(state, C.STATE_UNLINKED)
        self.assertEqual(message, C.UNLINKED_MESSAGE)
        self.assertEqual(extra, {})

    def test_should_wait_for_cert_truth_table(self):
        """The unlink stop must NOT hang off needs_cert (review B1).

        Key order: (needs_cert, unlinked, cert_present) -> wait instead of dial?
        """
        cases = {
            # A certificate is present: dial, whatever else holds.
            (True, True, True): False,
            (True, False, True): False,
            (False, True, True): False,
            (False, False, True): False,
            # wss:// with no certificate: the pre-existing rule, unchanged.
            (True, False, False): True,
            (True, True, False): True,
            # ws:// and never bound: still dials the lab gateway (unchanged).
            (False, False, False): False,
            # ws:// AFTER a cloud unlink: must go quiet. THIS is the case the
            # needs_cert coupling got wrong - the board kept dialling and
            # _unlinked stayed terminal because _await_cert was unreachable.
            (False, True, False): True,
        }
        for (needs, unlinked, present), expected in cases.items():
            self.assertIs(
                client_main._should_wait_for_cert(needs, unlinked, present),
                expected,
                "needs_cert=%s unlinked=%s cert_present=%s" % (needs, unlinked, present),
            )

    def test_marker_written_by_the_cgi_is_adopted_by_the_client_loop(self):
        config_store.set_unlinked_at("2026-09-02T10:00:00Z")
        client_main._reconcile_unlink_state()
        self.assertTrue(client_main._unlinked.is_set())

    def test_a_certificate_reappearing_lifts_the_flag_here_too(self):
        """review F-B: the lift may not live only inside _await_cert.

        A re-bind can land while the loop is elsewhere - during the
        post-disconnect backoff, say - and then the soft wait is never entered.
        Before the fix the flag stayed terminal and the card was frozen at
        «unlinked» on a board that was connected and working.
        """
        client_main._unlinked.set()
        self.write(C.CERT_FILE, "c")
        self.write(C.KEY_FILE, "k")
        client_main._reconcile_unlink_state()
        self.assertFalse(client_main._unlinked.is_set())

    def test_a_stale_marker_beside_a_real_certificate_is_inert(self):
        config_store.set_unlinked_at("2026-09-02T10:00:00Z")
        self.write(C.CERT_FILE, "c")
        self.write(C.KEY_FILE, "k")
        client_main._reconcile_unlink_state()
        self.assertFalse(client_main._unlinked.is_set())

    def test_no_marker_no_adoption(self):
        client_main._reconcile_unlink_state()
        self.assertFalse(client_main._unlinked.is_set())

    def test_await_cert_returns_on_rebind_and_clears_the_terminal_flag(self):
        # Round 2 named this defect and then fixed its two siblings while
        # leaving it untouched: no wait patch and no ceiling, so a build whose
        # _await_cert never returns True hung for the full wall (review F-A).
        self.nonblocking_stop_wait()
        self.cert_ceiling()
        client_main._unlinked.set()
        self.write(C.CERT_FILE, "c")
        self.write(C.KEY_FILE, "k")
        self.assertTrue(client_main._await_cert(C.STATE_UNLINKED, C.UNLINKED_MESSAGE, {}))
        self.assertFalse(client_main._unlinked.is_set())

    def test_await_cert_publishes_the_unlinked_card_then_waits(self):
        self.cert_ceiling()
        client_main._unlinked.set()
        ticks, tick = _budget(20, "the no-certificate soft wait")

        def fake_wait(_timeout):
            if tick() >= 2:
                self.write(C.CERT_FILE, "c")
                self.write(C.KEY_FILE, "k")
            return False

        with mock.patch.object(client_main._stop, "wait", side_effect=fake_wait):
            self.assertTrue(
                client_main._await_cert(C.STATE_UNLINKED, C.UNLINKED_MESSAGE, {})
            )
        self.assertEqual(ticks["n"], 2)
        self.assertEqual(self.status()["state"], C.STATE_UNLINKED)

    def test_await_cert_exits_when_the_client_is_switched_off(self):
        self.cert_ceiling()
        # Budgeted, non-blocking: a build that lost the client_enabled exit
        # would otherwise sit on the real 60 s wait and this test would pass
        # by TIMING OUT. A test that hangs under mutation is not a test
        # (review: the third hollow ratchet, same class as M11).
        self.write(C.CLIENT_CONF, "[client]\nclient_enabled = false\n")
        waits, tick = _budget(20, "the no-certificate soft wait")

        def budget_wait(_timeout=None):
            tick()
            return False

        with mock.patch.object(client_main._stop, "wait", side_effect=budget_wait):
            self.assertFalse(client_main._await_cert(C.STATE_MISSING_CERT, "m", {}))
        # It must exit on the FLAG, not on the budget running out.
        self.assertEqual(waits["n"], 0)
        self.assertFalse(client_main._stop.is_set())


# =====================================================================
# N1 — the outer-loop error path erases NOTHING
# =====================================================================
class _ExplodingSio:
    """Stands in for AliceSocketIO: connect() raises the exact exception the
    Operator's journal shows. The board is bound the whole time."""

    instances = 0

    def __init__(self, **_kw):
        _ExplodingSio.instances += 1
        self.connected = False

    def connect(self):
        raise RuntimeError("One or more namespaces failed to connect")

    def disconnect(self):
        pass


class TestN1ErrorPathWipesNothing(_BindingBase):
    def test_connect_failures_never_erase_the_binding(self):
        self.cert_ceiling()
        self.seed_binding()
        before = {
            p: self.read(p)
            for p in binding_reset.binding_files() + [C.KEY_FILE + ".tmp"]
        }
        conf_before = self.read(C.CLIENT_CONF)
        devices_before = self.read(C.DEVICES_CONF)
        _ExplodingSio.instances = 0
        attempts = {"n": 0}
        delays = []

        def fake_delay(attempt, **_kw):
            attempts["n"] += 1
            delays.append(attempt)
            if attempts["n"] >= 3:
                client_main._stop.set()
            return 0.0

        # Non-blocking, budgeted. A build that erased the binding here would
        # fall into the no-certificate soft wait, and a real 60 s wait would
        # HANG the test instead of naming the file it erased.
        _waits, wtick = _budget(50, "the reconnect loop")

        def budget_wait(_timeout=None):
            wtick()
            return client_main._stop.is_set()

        with mock.patch.object(client_main, "AliceSocketIO", _ExplodingSio), \
                mock.patch.object(client_main, "_mqtt_client", return_value=mock.MagicMock()), \
                mock.patch.object(client_main, "reconnect_delay", side_effect=fake_delay), \
                mock.patch.object(client_main._stop, "wait", side_effect=budget_wait), \
                mock.patch.object(sio_connection, "import_socketio", return_value=None):
            rc = client_main.run()

        self.assertEqual(rc, 0)
        self.assertGreaterEqual(_ExplodingSio.instances, 3)
        # The backoff ladder still climbs — the loop retried, it did not wipe.
        self.assertEqual(delays, [1, 2, 3])
        for path, blob in before.items():
            self.assertEqual(self.read(path), blob, "%s was erased on an error" % path)
        self.assertEqual(self.read(C.CLIENT_CONF), conf_before)
        self.assertEqual(self.read(C.DEVICES_CONF), devices_before)
        self.assertEqual(config_store.unlinked_at(), "")
        self.assertFalse(client_main._unlinked.is_set())
        self.assertEqual(self.status()["state"], C.STATE_ERROR)


# =====================================================================
# The loop wiring: after the event the board stops dialling
# =====================================================================
class _UnlinkingSio:
    """Stands in for AliceSocketIO on a bound board that the cloud unlinks.

    connect() succeeds and then delivers a real `controller_unlink` through the
    SAME on_event seam the library uses, so the whole chain under test is the
    shipped one: sio_connection's registered handler -> SioHandlers.handle ->
    on_unlink -> reset_cloud_binding -> the status write and the loop exit.
    """

    instances = 0
    events = []

    def __init__(self, *, on_event=None, **_kw):
        _UnlinkingSio.instances += 1
        self._on_event = on_event
        self.connected = False

    def connect(self):
        self.connected = True
        _UnlinkingSio.events.append("connect")
        if self._on_event:
            self._on_event(C.EVT_CONTROLLER_UNLINK, {"reason": "unlinked"})
        # The gateway closes the socket right after the event (cloud contract,
        # quarantine-notify): the board never keeps a live session past it.
        self.connected = False

    def emit(self, *_a, **_kw):
        pass

    def emit_response(self, *_a, **_kw):
        pass

    def session_summary(self):
        return "session summary"

    def session_duration_s(self):
        return 5.0

    def disconnect(self):
        self.connected = False


class _Clock:
    """A time shim installed on client.main only.

    The tick budget below must count the WATCHDOG loop, not the StateSender
    thread that also sleeps — patching the real time.sleep counted both and
    stopped the run before it had reconnected.
    """

    def __init__(self, on_tick):
        self._on_tick = on_tick

    def sleep(self, _s=None):
        self._on_tick()

    @staticmethod
    def monotonic():
        return time.monotonic()

    @staticmethod
    def time():
        return time.time()


class TestUnlinkStopsTheReconnectLoop(_BindingBase):
    def test_one_dial_then_the_board_goes_quiet_and_claim_ready(self):
        """The Operator's headline symptom was a reconnect storm: event ->
        gateway closes the socket -> reconnect -> same event, until the rate
        limit silenced it. After the wipe there is no certificate, so the
        outer loop must route into the soft wait instead of dialling again."""
        self.cert_ceiling()
        self.seed_binding()
        _UnlinkingSio.instances = 0
        _UnlinkingSio.events = []
        waits = {"n": 0}
        ticks = {"n": 0}

        def fake_wait(_timeout=None):
            waits["n"] += 1
            if waits["n"] > 40:
                raise _BudgetExhausted(
                    "the reconnect loop ran 40 waits without exiting"
                )
            if waits["n"] >= 2:
                client_main._stop.set()
                return True
            return False

        def budget(_s=None):
            ticks["n"] += 1
            if ticks["n"] > 200:
                raise _BudgetExhausted(
                    "the watchdog loop ran 200 ticks without exiting"
                )

        with mock.patch.object(client_main, "AliceSocketIO", _UnlinkingSio), \
                mock.patch.object(client_main, "_mqtt_client", return_value=mock.MagicMock()), \
                mock.patch.object(client_main, "reconnect_delay", return_value=0.0), \
                mock.patch.object(client_main, "time", _Clock(budget)), \
                mock.patch.object(sio_connection, "import_socketio", return_value=None), \
                mock.patch.object(client_main._stop, "wait", side_effect=fake_wait):
            rc = client_main.run()

        self.assertEqual(rc, 0)
        # ONE dial. A second instance means the board went back to the gateway
        # with no certificate -- the storm this change removes.
        self.assertEqual(_UnlinkingSio.instances, 1)
        self.assertEqual(self.binding_present(), [])
        self.assertTrue(client_main._unlinked.is_set())
        self.assertEqual(self.status()["state"], C.STATE_UNLINKED)
        self.assertEqual(self.status()["message"], C.UNLINKED_MESSAGE)
        self.assertIs(self.status()["cert_present"], False)
        # Claim-ready: the client is still switched on, so alice.js keeps the
        # link row (and its button) on the card.
        self.assertTrue(config_store.client_enabled())
        self.assertTrue(config_store.unlinked_at())

    def test_a_rebind_reconnects_without_a_service_restart(self):
        """complete_link writes fresh certificates into VAR_DIR; the running
        process must pick them up from the soft wait and dial again."""
        self.cert_ceiling()
        self.seed_binding()
        _UnlinkingSio.instances = 0
        waits = {"n": 0}
        ticks = {"n": 0}

        def fake_wait(_timeout=None):
            waits["n"] += 1
            if waits["n"] > 40:
                raise _BudgetExhausted(
                    "the reconnect loop ran 40 waits without exiting"
                )
            if waits["n"] == 2:
                # The next owner finishes binding while we sit in the wait.
                self.write(C.CERT_FILE, "-----BEGIN NEW CERT-----\n")
                self.write(C.KEY_FILE, "-----BEGIN NEW KEY-----\n")
            if waits["n"] >= 4:
                client_main._stop.set()
                return True
            return False

        def budget(_s=None):
            ticks["n"] += 1
            if ticks["n"] > 200:
                raise _BudgetExhausted(
                    "the watchdog loop ran 200 ticks without exiting"
                )

        with mock.patch.object(client_main, "AliceSocketIO", _UnlinkingSio), \
                mock.patch.object(client_main, "_mqtt_client", return_value=mock.MagicMock()), \
                mock.patch.object(client_main, "reconnect_delay", return_value=0.0), \
                mock.patch.object(client_main, "time", _Clock(budget)), \
                mock.patch.object(sio_connection, "import_socketio", return_value=None), \
                mock.patch.object(client_main._stop, "wait", side_effect=fake_wait):
            client_main.run()

        # Dialled again once the certificate reappeared -- no restart involved.
        self.assertGreaterEqual(_UnlinkingSio.instances, 2)


class _UnlinkOnceSio:
    """Unlinks on the FIRST connect only.

    That is the gateway's real behaviour once the owner has re-claimed the
    board: the next session is a normal one. It lets a test observe the
    `connected` status write after a re-bind - which can only land if
    `_unlinked` was actually lifted, since the suppression drops every other
    state while it is set.
    """

    instances = 0

    def __init__(self, *, on_event=None, **_kw):
        _UnlinkOnceSio.instances += 1
        self._first = _UnlinkOnceSio.instances == 1
        self._on_event = on_event
        self.connected = False

    def connect(self):
        self.connected = True
        if self._first and self._on_event:
            self._on_event(C.EVT_CONTROLLER_UNLINK, {"reason": "unlinked"})
            self.connected = False

    def emit(self, *_a, **_kw):
        pass

    def emit_response(self, *_a, **_kw):
        pass

    def session_summary(self):
        return "session summary"

    def session_duration_s(self):
        return 5.0

    def disconnect(self):
        self.connected = False


class TestFailedWipeNeverMarksTheBoardUnlinked(_BindingBase):
    """The caller half of review B2.

    on_unlink must bail on a failed erase: no terminal flag, no `unlinked`
    card. Dropping the `return` from its error handler is a one-token edit
    that makes a board whose binding is STILL ON DISK advertise itself as
    claim-ready.
    """

    def test_a_failed_erase_leaves_the_state_alone(self):
        self.cert_ceiling()
        self.seed_binding()
        _UnlinkingSio.instances = 0
        waits = {"n": 0}
        ticks = {"n": 0}

        def fake_wait(_timeout=None):
            waits["n"] += 1
            if waits["n"] > 40:
                raise _BudgetExhausted(
                    "the reconnect loop ran 40 waits without exiting"
                )
            if waits["n"] >= 4:
                client_main._stop.set()
                return True
            return False

        def budget(_s=None):
            ticks["n"] += 1
            if ticks["n"] > 200:
                raise _BudgetExhausted(
                    "the watchdog loop ran 200 ticks without exiting"
                )

        with mock.patch.object(client_main, "AliceSocketIO", _UnlinkingSio),                 mock.patch.object(client_main, "_mqtt_client", return_value=mock.MagicMock()),                 mock.patch.object(client_main, "reconnect_delay", return_value=0.0),                 mock.patch.object(client_main, "time", _Clock(budget)),                 mock.patch.object(sio_connection, "import_socketio", return_value=None),                 mock.patch.object(client_main, "reset_cloud_binding",
                                  side_effect=PermissionError(13, "denied")),                 mock.patch.object(client_main._stop, "wait", side_effect=fake_wait):
            client_main.run()

        # The binding is still there, so nothing may claim it was erased.
        self.assertNotEqual(self.binding_present(), [])
        self.assertFalse(client_main._unlinked.is_set())
        self.assertNotEqual(self.status()["state"], C.STATE_UNLINKED)
        self.assertEqual(config_store.unlinked_at(), "")


class TestUnlinkStopsTheLoopOnWsToo(_BindingBase):
    """review B1 - the stop and the flag-lift must not hang off needs_cert.

    On a lab ws:// gateway needs_cert is False. Before the fix the board kept
    dialling after the wipe (the reviewer's probe measured 12 dials where the
    wss:// test asserts 1), and `_unlinked` stayed terminal for the life of
    the process because `_await_cert` is the only `_unlinked.clear()` site and
    was unreachable on that path.
    """

    def _budgets(self, on_second_wait=None, on_wait=2, stop_after_ticks=None):
        """(counter, wait-stub, clock-tick) for driving run() to a decision.

        Two different things live here and they must not be confused. The
        SCRIPT - a stop signal arriving, a re-bind landing - is part of the
        scenario and legitimately uses `_stop`. The BUDGET is the escape hatch
        and always RAISES, never `_stop.set()`: asking the code under test to
        honour `_stop` is exactly what a mutation breaks, and that is how a
        suite ends up hanging with no named failure (review F-A).
        """
        waits, wtick = _budget(40, "the reconnect loop")
        _ticks, ctick = _budget(200, "the watchdog loop")

        def fake_wait(_timeout=None):
            n = wtick()
            if on_second_wait is not None and n == on_wait:
                on_second_wait()
            if n >= 6:
                client_main._stop.set()  # scripted: the stop signal arrives
                return True
            return False

        def budget(_s=None):
            n = ctick()
            # Scripted end of a HEALTHY session: _UnlinkOnceSio stays
            # connected after a re-bind, so without this the watchdog loop
            # would legitimately never exit.
            if stop_after_ticks is not None and n >= stop_after_ticks:
                client_main._stop.set()

        return waits, fake_wait, budget

    def test_one_dial_on_a_ws_gateway_too(self):
        self.cert_ceiling()
        self.use_ws_gateway()
        self.seed_binding()
        _UnlinkingSio.instances = 0
        _waits, fake_wait, budget = self._budgets()

        with mock.patch.object(client_main, "AliceSocketIO", _UnlinkingSio),                 mock.patch.object(client_main, "_mqtt_client", return_value=mock.MagicMock()),                 mock.patch.object(client_main, "reconnect_delay", return_value=0.0),                 mock.patch.object(client_main, "time", _Clock(budget)),                 mock.patch.object(sio_connection, "import_socketio", return_value=None),                 mock.patch.object(client_main._stop, "wait", side_effect=fake_wait):
            self.assertEqual(client_main.run(), 0)

        self.assertEqual(_UnlinkingSio.instances, 1)
        self.assertEqual(self.binding_present(), [])
        self.assertEqual(self.status()["state"], C.STATE_UNLINKED)

    def test_a_rebind_during_the_backoff_still_unfreezes_the_card(self):
        """review F-B, through the real loop.

        The certificate comes back on the FIRST wait - the post-disconnect
        backoff - so the outer loop never enters the soft wait at all. The
        reviewer's probe measured 13 dials with the status frozen at
        «unlinked» on a bound board; the card must reach `connected`.
        """
        self.cert_ceiling()
        self.use_ws_gateway()
        self.seed_binding()
        _UnlinkOnceSio.instances = 0

        def rebind():
            self.write(C.CERT_FILE, "-----BEGIN NEW CERT-----")
            self.write(C.KEY_FILE, "-----BEGIN NEW KEY-----")

        _waits, fake_wait, budget = self._budgets(
            on_second_wait=rebind, on_wait=1, stop_after_ticks=3
        )

        with mock.patch.object(client_main, "AliceSocketIO", _UnlinkOnceSio),                 mock.patch.object(client_main, "_mqtt_client", return_value=mock.MagicMock()),                 mock.patch.object(client_main, "reconnect_delay", return_value=0.0),                 mock.patch.object(client_main, "time", _Clock(budget)),                 mock.patch.object(sio_connection, "import_socketio", return_value=None),                 mock.patch.object(client_main._stop, "wait", side_effect=fake_wait):
            client_main.run()

        self.assertFalse(client_main._unlinked.is_set())
        self.assertEqual(self.status()["state"], C.STATE_CONNECTED)
        self.assertEqual(_UnlinkOnceSio.instances, 2)

    def test_the_flag_is_lifted_on_a_ws_rebind(self):
        self.cert_ceiling()
        self.use_ws_gateway()
        self.seed_binding()
        _UnlinkOnceSio.instances = 0

        def rebind():
            self.write(C.CERT_FILE, "-----BEGIN NEW CERT-----")
            self.write(C.KEY_FILE, "-----BEGIN NEW KEY-----")

        _waits, fake_wait, budget = self._budgets(
            on_second_wait=rebind, stop_after_ticks=3
        )

        with mock.patch.object(client_main, "AliceSocketIO", _UnlinkOnceSio),                 mock.patch.object(client_main, "_mqtt_client", return_value=mock.MagicMock()),                 mock.patch.object(client_main, "reconnect_delay", return_value=0.0),                 mock.patch.object(client_main, "time", _Clock(budget)),                 mock.patch.object(sio_connection, "import_socketio", return_value=None),                 mock.patch.object(client_main._stop, "wait", side_effect=fake_wait):
            client_main.run()

        # Exactly two dials: the unlink session, then the re-bound one. More
        # than two means the board never stopped; the `connected` status is
        # only writable once the terminal flag was lifted.
        self.assertEqual(_UnlinkOnceSio.instances, 2)
        self.assertFalse(client_main._unlinked.is_set())
        self.assertEqual(self.status()["state"], C.STATE_CONNECTED)


# =====================================================================
# N3 — the local button refuses on an outage or a rejection, and wipes nothing
# =====================================================================
class _Resp:
    def __init__(self, body=b"{}", status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class TestN3LocalUnlinkRefusals(_BindingBase):
    def assert_nothing_erased(self, conf_before):
        self.assertEqual(
            sorted(self.binding_present()),
            ["ca.crt.pem", "device.crt.pem", "device.key.pem", "device.key.pem.tmp",
             "pending_claim.json"],
        )
        self.assertEqual(self.read(C.CLIENT_CONF), conf_before)
        self.assertEqual(config_store.unlinked_at(), "")
        self.assertTrue(config_store.client_enabled())

    def test_gateway_unreachable_refuses_and_wipes_nothing(self):
        self.seed_binding()
        conf_before = self.read(C.CLIENT_CONF)
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_DOWN):
            result = api.unlink_controller()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "gateway_unavailable")
        self.assert_nothing_erased(conf_before)

    def test_http_error_status_refuses_and_wipes_nothing(self):
        self.seed_binding()
        conf_before = self.read(C.CLIENT_CONF)
        for code in (400, 403, 500):
            with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP), \
                    mock.patch.object(api.urllib.request, "urlopen",
                                      return_value=_Resp(b"{}", code)):
                result = api.unlink_controller()
            self.assertFalse(result["ok"], "HTTP %s must not claim success" % code)
            self.assertEqual(result["http_status"], code)
            self.assert_nothing_erased(conf_before)

    def test_transport_exception_refuses_and_wipes_nothing(self):
        self.seed_binding()
        conf_before = self.read(C.CLIENT_CONF)
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP), \
                mock.patch.object(api.urllib.request, "urlopen",
                                  side_effect=OSError("connection reset")):
            result = api.unlink_controller()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unlink_failed")
        self.assert_nothing_erased(conf_before)


class TestLocalUnlinkSuccess(_BindingBase):
    def test_confirmed_unlink_erases_the_binding_and_keeps_the_client_on(self):
        """One home for the wipe: leaving certificates behind after a CONFIRMED
        cloud unlink is exactly what produced the false «привязан» card."""
        self.seed_binding()
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP), \
                mock.patch.object(api.urllib.request, "urlopen", return_value=_Resp()):
            result = api.unlink_controller()
        self.assertTrue(result["ok"])
        self.assertEqual(self.binding_present(), [])
        self.assertIs(result["client_enabled"], True)
        self.assertTrue(config_store.client_enabled())
        self.assertTrue(config_store.unlinked_at())

    def test_the_claim_token_is_used_before_the_file_is_deleted(self):
        """Ordering the wipe ahead of the HTTP call would authenticate the
        unlink with an empty token — the file it reads is the one it erases."""
        self.seed_binding()
        seen = {}

        def capture(req, **_kw):
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return _Resp()

        with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP), \
                mock.patch.object(api.urllib.request, "urlopen", side_effect=capture):
            api.unlink_controller()
        self.assertEqual(seen["body"]["claim_token"], "tok")
        self.assertEqual(seen["body"]["controller_sn"], "SN1")


class TestRebindClearsTheMarker(_BindingBase):
    def test_complete_link_clears_the_marker(self):
        config_store.set_unlinked_at("2026-09-02T10:00:00Z")
        api._save_pending_claim({"claim_token": "tok", "controller_sn": "SN1"})
        issue = {"ok": True, "device.crt.pem": "C", "device.key.pem": "K", "ca.crt.pem": "A"}
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP), \
                mock.patch.object(api.urllib.request, "urlopen",
                                  return_value=_Resp(json.dumps(issue).encode("utf-8"))):
            result = api.complete_link()
        self.assertTrue(result["ok"])
        self.assertEqual(config_store.unlinked_at(), "")

    def test_start_link_clears_the_marker(self):
        config_store.set_unlinked_at("2026-09-02T10:00:00Z")
        enroll = {"ok": True, "claim_token": "t2", "registration_url": "https://u", "expires_in": 600}
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP), \
                mock.patch.object(api.urllib.request, "urlopen",
                                  return_value=_Resp(json.dumps(enroll).encode("utf-8"))):
            result = api.start_link()
        self.assertTrue(result["ok"])
        self.assertEqual(config_store.unlinked_at(), "")

    def test_a_failed_rebind_does_not_clear_the_marker(self):
        config_store.set_unlinked_at("2026-09-02T10:00:00Z")
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_DOWN):
            self.assertFalse(api.start_link()["ok"])
            self.assertFalse(api.complete_link()["ok"])
        self.assertEqual(config_store.unlinked_at(), "2026-09-02T10:00:00Z")


class TestMarkerStore(_BindingBase):
    def test_set_and_clear_round_trip(self):
        self.assertEqual(config_store.unlinked_at(), "")
        config_store.set_unlinked_at("2026-09-02T10:00:00Z")
        self.assertEqual(config_store.unlinked_at(), "2026-09-02T10:00:00Z")
        config_store.set_unlinked_at(None)
        self.assertEqual(config_store.unlinked_at(), "")
        self.assertNotIn("unlinked_at", self.read(C.CLIENT_CONF).decode("utf-8"))

    def test_clearing_an_absent_marker_does_not_rewrite_the_file(self):
        before = self.read(C.CLIENT_CONF)
        config_store.set_unlinked_at(None)
        self.assertEqual(self.read(C.CLIENT_CONF), before)


class TestUnlinkedStatusEndToEnd(_BindingBase):
    def test_the_card_reads_unlinked_and_not_linked(self):
        """The reported symptom was «Статус: привязан» beside «Шлюз
        недоступен»: alice.js treats a certificate on disk as proof of
        binding. After the wipe the API must report neither."""
        self.seed_binding()
        binding_reset.reset_cloud_binding(binding_reset.SOURCE_GATEWAY)
        client_main._unlinked.set()
        client_main._write_status(C.STATE_UNLINKED, message=C.UNLINKED_MESSAGE,
                                  client_enabled=True)
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP):
            cfg = api.full_config()
        self.assertEqual(cfg["link"]["state"], C.STATE_UNLINKED)
        self.assertFalse(cfg["link"]["linked"])
        self.assertIs(cfg["mtls"]["cert_present"], False)
        self.assertIsNone(cfg["link"]["registration_url"])
        # client_enabled stays ON: alice.js hides the whole link row when it is
        # off, and that row carries the «Привязать» button the next owner needs.
        self.assertTrue(cfg["client_enabled"])


if __name__ == "__main__":
    unittest.main()
