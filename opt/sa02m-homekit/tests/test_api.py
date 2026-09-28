"""api.py — the CGI dispatch: allow-lists, verbs, status merge, setup gate, CLI contract."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from sa02m_homekit import api
from sa02m_homekit import config
from sa02m_homekit import constants as C

from . import HOMEKIT_ROOT

CODE = "031-45-154"
QR = ["10" * 10 + "1"] * 21


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        d = self.dir
        self.paths = {
            "CONF_FILE": os.path.join(d, "etc", "sa02m-homekit.conf"),
            "STATUS_FILE": os.path.join(d, "run", "status.json"),
            "SETUP_FILE": os.path.join(d, "run", "setup.json"),
            "PROJECTION_FILE": os.path.join(d, "run", "projection.json"),
            "UNIT_FILE": os.path.join(d, "unit", "sa02m-homekit.service"),
            "VENV_PYTHON": os.path.join(d, "venv", "python"),
            "PACKAGE_DIR": os.path.join(d, "pkg"),
        }
        for sub in ("etc", "run", "unit", "venv", "pkg"):
            os.makedirs(os.path.join(d, sub))
        for key in ("UNIT_FILE", "VENV_PYTHON"):
            open(self.paths[key], "w").close()
        self.patches = [mock.patch.object(C, k, v) for k, v in self.paths.items()]
        self.patches.append(mock.patch.object(api.netif, "ipv4_address",
                                              lambda n: "192.168.1.136" if n == "eth0" else None))
        self.patches.append(mock.patch.object(api.netif, "interface_present", lambda n: n == "eth0"))
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def conf(self, **kw):
        config.save(config.BridgeConfig(**kw), self.paths["CONF_FILE"])

    def status(self, state, age=0, **kw):
        body = {"state": state, "ts": int(time.time()) - age}
        body.update(kw)
        with open(self.paths["STATUS_FILE"], "w", encoding="utf-8") as fh:
            json.dump(body, fh)

    def setup_file(self, **over):
        body = {"code": CODE, "setup_uri": "X-HM://0000000007OSX", "setup_id": "7OSX", "qr": QR}
        body.update(over)
        with open(self.paths["SETUP_FILE"], "w", encoding="utf-8") as fh:
            json.dump(body, fh)

    def post(self, **body):
        return api.dispatch("POST", body)


class StatusMergeTests(ApiCase):
    def test_missing_footprint_is_not_installed(self):
        os.unlink(self.paths["VENV_PYTHON"])
        self.conf(enabled=True)
        self.status(C.STATE_RUNNING, paired=True)
        resp, verb = api.dispatch("GET", {})
        self.assertEqual(resp["state"], C.STATE_NOT_INSTALLED)
        self.assertIsNone(verb)

    def test_disabled_conf_wins(self):
        self.conf(enabled=False)
        self.status(C.STATE_RUNNING, paired=True)
        self.assertEqual(api.merged_status()["state"], C.STATE_DISABLED)

    def test_enabled_without_status_is_starting(self):
        self.conf(enabled=True)
        self.assertEqual(api.merged_status()["state"], C.STATE_STARTING)

    def test_enabled_with_a_disabled_status_is_starting(self):
        self.conf(enabled=True)
        self.status(C.STATE_DISABLED)
        self.assertEqual(api.merged_status()["state"], C.STATE_STARTING)

    def _age_conf(self, age):
        t = time.time() - age
        os.utime(self.paths["CONF_FILE"], (t, t))

    def test_just_enabled_over_an_old_disabled_status_is_starting_not_stale(self):
        # The disabled status predates the stale window; the user enabled now.
        self.status(C.STATE_DISABLED, age=C.STATUS_STALE_S + 600)
        self.conf(enabled=True)
        m = api.merged_status()
        self.assertEqual((m["state"], m["reason"]), (C.STATE_STARTING, ""))

    def test_daemon_silent_past_the_window_after_enable_is_stale(self):
        self.status(C.STATE_DISABLED, age=C.STATUS_STALE_S + 600)
        self.conf(enabled=True)
        self._age_conf(C.STATUS_STALE_S + 5)
        m = api.merged_status()
        self.assertEqual((m["state"], m["reason"]), (C.STATE_ERROR, C.REASON_STATUS_STALE))

    def test_fresh_running(self):
        self.conf(enabled=True)
        self.status(C.STATE_RUNNING, paired=False, pairings=0, accessories=3, address="192.168.1.136",
                    bridge_name="SA-02m ABCDEF")
        self.setup_file()
        m = api.merged_status()
        self.assertEqual((m["state"], m["paired"], m["accessories"], m["setup_available"]),
                         (C.STATE_RUNNING, False, 3, True))
        self.assertEqual(m["interfaces"][0], {"name": "eth0", "present": True, "address": "192.168.1.136"})
        self.assertNotIn(CODE, json.dumps(m))

    def test_stale_heartbeat_states_become_error_status_stale(self):
        self.conf(enabled=True)
        for state in C.HEARTBEAT_STATES:
            self.status(state, age=C.STATUS_STALE_S + 5, paired=False)
            m = api.merged_status()
            self.assertEqual((m["state"], m["reason"]), (C.STATE_ERROR, C.REASON_STATUS_STALE), state)

    def test_future_dated_status_is_stale_too(self):
        self.conf(enabled=True)
        self.status(C.STATE_RUNNING, age=-(C.STATUS_STALE_S + 60))
        self.assertEqual(api.merged_status()["reason"], C.REASON_STATUS_STALE)

    def test_exit_once_states_are_trusted_at_any_age(self):
        self.conf(enabled=True)
        for state in (C.STATE_MISSING_DEPS, C.STATE_ERROR):
            self.status(state, age=3600)
            self.assertEqual(api.merged_status()["state"], state)

    def test_unknown_state_is_error(self):
        self.conf(enabled=True)
        self.status("connected")
        self.assertEqual(api.merged_status()["state"], C.STATE_ERROR)

    def test_projection_lists_are_passed_capped(self):
        self.conf(enabled=True)
        self.status(C.STATE_RUNNING, paired=True)
        skipped = [{"device_id": "d%d" % i, "name": "n", "item": None, "reason": "hidden"}
                   for i in range(300)]
        with open(self.paths["PROJECTION_FILE"], "w", encoding="utf-8") as fh:
            json.dump({"accessories": [{"device_id": "a", "name": "A", "aid": 2, "services": ["Switch"]}],
                       "skipped": skipped, "skipped_total": 300}, fh)
        m = api.merged_status()
        self.assertEqual(len(m["skipped"]), C.SKIPPED_CAP)
        self.assertEqual(m["skipped_total"], 300)
        self.assertEqual(m["accessory_list"][0]["aid"], 2)


class SetupGateTests(ApiCase):
    def test_available_only_while_running_and_unpaired(self):
        self.conf(enabled=True)
        self.setup_file()
        self.status(C.STATE_RUNNING, paired=False)
        resp, _ = self.post(action="setup")
        self.assertEqual((resp["ok"], resp["code"], resp["qr"]), (True, CODE, QR))
        self.status(C.STATE_RUNNING, paired=True)
        self.assertEqual(self.post(action="setup")[0], {"ok": False, "error": "not_available"})
        self.status(C.STATE_NO_INTERFACE)
        self.assertEqual(self.post(action="setup")[0]["error"], "not_available")

    def test_absent_or_malformed_setup_file_is_not_available(self):
        self.conf(enabled=True)
        self.status(C.STATE_RUNNING, paired=False)
        self.assertEqual(self.post(action="setup")[0]["error"], "not_available")
        for over in ({"code": "111"}, {"setup_uri": "javascript:1"}, {"setup_id": "7osx"},
                     {"qr": ["12"] * 21}, {"qr": ["1" * 21] * 5}, {"qr": "x"}):
            self.setup_file(**over)
            resp = self.post(action="setup")[0]
            self.assertEqual(resp, {"ok": False, "error": "not_available"}, over)

    def test_disabled_never_answers_a_code(self):
        self.conf(enabled=False)
        self.status(C.STATE_RUNNING, paired=False)
        self.setup_file()
        self.assertEqual(self.post(action="setup")[0]["error"], "not_available")


class MutationTests(ApiCase):
    def _conf(self):
        return config.load(self.paths["CONF_FILE"])

    def test_enable_disable_write_the_conf_and_name_the_verb(self):
        resp, verb = self.post(action="enable")
        self.assertTrue(resp["ok"])
        self.assertEqual(verb, "enable")
        self.assertTrue(self._conf().enabled)
        resp, verb = self.post(action="disable")
        self.assertEqual(verb, "disable")
        self.assertFalse(self._conf().enabled)

    def test_set_interface_allow_list(self):
        for bad in ("ppp0", "wwan0", "usb0", "eth2", "", None, 0, "eth0 ", "../eth0"):
            resp, verb = self.post(action="set_interface", interface=bad)
            self.assertEqual((resp, verb), ({"ok": False, "error": "invalid_interface"}, None), bad)
        self.assertEqual(self._conf().interface, "eth0")
        resp, verb = self.post(action="set_interface", interface="eth1")
        self.assertTrue(resp["ok"])
        self.assertIsNone(verb)  # disabled: nothing to restart
        self.assertEqual(self._conf().interface, "eth1")

    def test_set_port_allow_list(self):
        for bad in (1883, 502, 8765, 9999, 80, 1023, 65536, "abc", True, None, "1883", -5, 21064.5):
            resp, verb = self.post(action="set_port", port=bad)
            self.assertEqual((resp, verb), ({"ok": False, "error": "invalid_port"}, None), bad)
        self.conf(enabled=True)
        resp, verb = self.post(action="set_port", port="30000")
        self.assertTrue(resp["ok"])
        self.assertEqual(verb, "restart")
        self.assertEqual(self._conf().port, 30000)

    def test_reset_pairing_names_its_verb_and_writes_nothing(self):
        resp, verb = self.post(action="reset_pairing")
        self.assertEqual((resp["ok"], verb), (True, "reset-pairing"))
        self.assertFalse(os.path.exists(self.paths["CONF_FILE"]))

    def test_unknown_actions_and_methods(self):
        self.assertEqual(self.post(action="rm -rf /")[0], {"ok": False, "error": "not_found"})
        self.assertEqual(self.post(action="restart"), ({"ok": False, "error": "not_found"}, None))
        self.assertEqual(api.dispatch("DELETE", {})[0]["error"], "method_not_allowed")
        self.assertEqual(api.dispatch("POST", ["enable"])[0]["error"], "invalid_json")

    def test_mutations_refused_when_not_installed(self):
        shutil.rmtree(self.paths["PACKAGE_DIR"])
        for action in ("enable", "disable", "reset_pairing", "set_port"):
            self.assertEqual(self.post(action=action, port=30000),
                             ({"ok": False, "error": "not_installed"}, None), action)

    def test_conf_write_failure_is_reported_without_a_verb(self):
        with mock.patch.object(api.conf_mod, "save", side_effect=OSError("read-only")):
            resp, verb = self.post(action="enable")
        self.assertEqual((resp["ok"], resp["error"], verb), (False, "conf_write_failed", None))

    def test_every_returned_verb_is_a_pinned_trigger_verb(self):
        self.conf(enabled=True)
        verbs = set()
        for body in ({"action": "enable"}, {"action": "disable"}, {"action": "reset_pairing"},
                     {"action": "enable"}, {"action": "set_interface", "interface": "eth1"},
                     {"action": "set_port", "port": 30001}, {"action": "status"}, {"action": "setup"}):
            verbs.add(api.dispatch("POST", body)[1])
        verbs.discard(None)
        self.assertEqual(verbs, set(C.TRIGGER_VERBS))


class CliTests(ApiCase):
    def run_cli(self, method, body):
        out = io.BytesIO()
        rc = api.cli([method], io.BytesIO(body), out)
        lines = out.getvalue().decode("utf-8").split("\n")
        return rc, lines

    def test_two_lines_response_then_verb(self):
        rc, lines = self.run_cli("POST", b'{"action":"enable"}')
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[2], "")
        self.assertTrue(json.loads(lines[0])["ok"])
        self.assertEqual(lines[1], "enable")

    def test_get_has_an_empty_verb_line(self):
        rc, lines = self.run_cli("GET", b"")
        self.assertEqual((rc, lines[1]), (0, ""))
        self.assertIn("state", json.loads(lines[0]))

    def test_invalid_json_and_oversized_bodies(self):
        _, lines = self.run_cli("POST", b"{nope")
        self.assertEqual((json.loads(lines[0])["error"], lines[1]), ("invalid_json", ""))
        _, lines = self.run_cli("POST", b'{"action":"enable","x":"' + b"a" * C.MAX_BODY_BYTES + b'"}')
        self.assertEqual((json.loads(lines[0])["error"], lines[1]), ("payload_too_large", ""))
        self.assertFalse(config.load(self.paths["CONF_FILE"]).enabled)


class StdlibOnlyTests(unittest.TestCase):
    """The CGI runs the dispatch on the SYSTEM python with only this package on
    the path — it must import nothing from the venv or the Alice package."""

    def test_api_imports_only_the_standard_library(self):
        code = ("import sys, sa02m_homekit.api; "
                "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
                "('pyhap', 'segno', 'paho', 'sa02m_alice', 'zeroconf', 'cryptography')); "
                "print(','.join(bad))")
        env = dict(os.environ, PYTHONPATH=HOMEKIT_ROOT)
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                             text=True, timeout=60, check=True)
        self.assertEqual(out.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
