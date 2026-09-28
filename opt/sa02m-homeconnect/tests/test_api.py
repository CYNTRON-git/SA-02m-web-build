"""api.py (the CGI dispatch): allow-list, merge rules, verbs, no secret out."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from sa02m_homeconnect import api
from sa02m_homeconnect import config
from sa02m_homeconnect import constants as C

GOOD_ID = "CLIENT_ID_FOR_TESTS_0001"


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.patches = [
            mock.patch.object(C, "CONF_FILE", os.path.join(d, "hc.conf")),
            mock.patch.object(C, "STATUS_FILE", os.path.join(d, "status.json")),
            mock.patch.object(C, "LINK_FILE", os.path.join(d, "link.json")),
            mock.patch.object(C, "INVENTORY_FILE", os.path.join(d, "inventory.json")),
            mock.patch.object(api, "footprint_installed", return_value=True),
        ]
        for p in self.patches:
            p.start()
        self.now = time.time()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def conf(self, **kw) -> None:
        config.save(config.ClientConfig(**kw))

    def status_file(self, **kw) -> None:
        body = {"state": "connected", "ts": int(self.now), "reason": "", "linked": True,
                "appliances": 2, "appliances_connected": 1, "stream": "up",
                "budget_used": 12, "budget_remaining": 988}
        body.update(kw)
        with open(C.STATUS_FILE, "w") as fh:
            json.dump(body, fh)

    def run_cli(self, method: str, body: bytes):
        out = io.BytesIO()
        self.assertEqual(api.cli([method], io.BytesIO(body), out), 0)
        lines = out.getvalue().decode("utf-8").split("\n")
        self.assertEqual(len(lines), 3)  # two lines + trailing newline
        self.assertEqual(lines[2], "")
        return json.loads(lines[0]), lines[1]

    # ── merge rules ─────────────────────────────────────────────────────
    def test_not_installed_wins(self) -> None:
        self.conf(enabled=True, client_id=GOOD_ID)
        self.status_file()
        with mock.patch.object(api, "footprint_installed", return_value=False):
            self.assertEqual(api.merged_status(self.now)["state"], C.STATE_NOT_INSTALLED)

    def test_disabled_and_missing_client_id_come_from_the_conf(self) -> None:
        self.status_file()
        self.conf(enabled=False, client_id=GOOD_ID)
        self.assertEqual(api.merged_status(self.now)["state"], C.STATE_DISABLED)
        self.conf(enabled=True)
        st = api.merged_status(self.now)
        self.assertEqual(st["state"], C.STATE_MISSING_CLIENT_ID)
        self.assertFalse(st["linked"])
        self.assertEqual(st["appliance_list"], [])

    def test_connected_view(self) -> None:
        self.conf(enabled=True, client_id=GOOD_ID)
        self.status_file()
        with open(C.INVENTORY_FILE, "w") as fh:
            json.dump({"appliances": [
                {"device_id": "hc-bosch-1", "name": "Стиральная", "type": "Washer", "brand": "Bosch",
                 "connected": True, "controls": ["running", "door_open", "BAD CONTROL"]},
                {"device_id": "../etc", "name": "x"},
            ]}, fh)
        st = api.merged_status(self.now)
        self.assertEqual(st["state"], C.STATE_CONNECTED)
        self.assertTrue(st["linked"])
        self.assertEqual(st["budget"], {"day": "", "used": 12, "limit": 1000, "local": 800,
                                        "remaining": 988})
        self.assertEqual([a["device_id"] for a in st["appliance_list"]], ["hc-bosch-1"])
        self.assertEqual(st["appliance_list"][0]["controls"], ["running", "door_open"])
        self.assertIs(st["read_only"], True)
        self.assertIsNone(st["link"])

    def test_stale_live_status_is_error(self) -> None:
        self.conf(enabled=True, client_id=GOOD_ID)
        self.status_file(ts=int(self.now) - C.STATUS_STALE_S - 5)
        st = api.merged_status(self.now)
        self.assertEqual((st["state"], st["reason"]), (C.STATE_ERROR, C.REASON_STATUS_STALE))
        self.assertFalse(st["linked"])

    def test_exit_once_states_trusted_at_any_age(self) -> None:
        self.conf(enabled=True, client_id=GOOD_ID)
        self.status_file(state="missing_deps", ts=1)
        self.assertEqual(api.merged_status(self.now)["state"], C.STATE_MISSING_DEPS)

    def test_unknown_state_is_error(self) -> None:
        self.conf(enabled=True, client_id=GOOD_ID)
        self.status_file(state="pwned")
        self.assertEqual(api.merged_status(self.now)["state"], C.STATE_ERROR)

    def test_just_enabled_reads_connecting(self) -> None:
        self.conf(enabled=True, client_id=GOOD_ID)
        self.assertEqual(api.merged_status(self.now)["state"], C.STATE_CONNECTING)
        self.status_file(state="disabled", ts=1)
        self.assertEqual(api.merged_status(self.now)["state"], C.STATE_CONNECTING)
        # …but a daemon that never catches up is stale, counted from the conf write.
        old = self.now - C.STATUS_STALE_S - 10
        os.utime(C.CONF_FILE, (old, old))
        st = api.merged_status(self.now)
        self.assertEqual((st["state"], st["reason"]), (C.STATE_ERROR, C.REASON_STATUS_STALE))

    def test_link_code_only_while_awaiting_user_and_well_formed(self) -> None:
        self.conf(enabled=True, client_id=GOOD_ID)
        link = {"user_code": "ABCD-1234", "verification_uri": "https://api.home-connect.com/v",
                "verification_uri_complete": "https://api.home-connect.com/v?c=ABCD-1234",
                "expires_at": int(self.now) + 200}
        with open(C.LINK_FILE, "w") as fh:
            json.dump(link, fh)
        self.status_file(state="connected")
        self.assertIsNone(api.merged_status(self.now)["link"])
        self.status_file(state="awaiting_user", linked=False)
        self.assertEqual(api.merged_status(self.now)["link"]["user_code"], "ABCD-1234")
        with open(C.LINK_FILE, "w") as fh:
            json.dump(dict(link, verification_uri="javascript:alert(1)"), fh)
        self.assertIsNone(api.merged_status(self.now)["link"])
        with open(C.LINK_FILE, "w") as fh:
            json.dump(dict(link, expires_at=int(self.now) - 1), fh)
        self.assertIsNone(api.merged_status(self.now)["link"])

    # ── actions ─────────────────────────────────────────────────────────
    def test_enable_disable_verbs(self) -> None:
        self.conf()
        resp, verb = api.dispatch("POST", {"action": "enable"})
        self.assertTrue(resp["ok"])
        self.assertEqual(verb, "enable")
        self.assertTrue(config.load().enabled)
        resp, verb = api.dispatch("POST", {"action": "disable"})
        self.assertEqual(verb, "disable")
        self.assertFalse(config.load().enabled)

    def test_set_client_id(self) -> None:
        self.conf(enabled=True, link_requested_at=int(self.now))
        resp, verb = api.dispatch("POST", {"action": "set_client_id", "client_id": " %s " % GOOD_ID})
        self.assertTrue(resp["ok"])
        self.assertEqual(verb, "restart")
        c = config.load()
        self.assertEqual(c.client_id, GOOD_ID)
        self.assertEqual(c.link_requested_at, 0)
        for bad in ("x", "a b c d e f g h", "../../etc/passwd", 12345678, None):
            resp, verb = api.dispatch("POST", {"action": "set_client_id", "client_id": bad})
            self.assertEqual(resp, {"ok": False, "error": "invalid_client_id"})
            self.assertIsNone(verb)
        resp, verb = api.dispatch("POST", {"action": "set_client_id", "client_id": ""})
        self.assertTrue(resp["ok"])
        self.assertEqual(config.load().client_id, "")

    def test_set_client_id_when_disabled_nudges_nothing(self) -> None:
        self.conf(enabled=False)
        _, verb = api.dispatch("POST", {"action": "set_client_id", "client_id": GOOD_ID})
        self.assertIsNone(verb)

    def test_link_preconditions(self) -> None:
        self.conf(enabled=False, client_id=GOOD_ID)
        self.assertEqual(api.dispatch("POST", {"action": "link"}), ({"ok": False, "error": "not_enabled"}, None))
        self.conf(enabled=True)
        self.assertEqual(api.dispatch("POST", {"action": "link"}),
                         ({"ok": False, "error": "missing_client_id"}, None))
        self.conf(enabled=True, client_id=GOOD_ID)
        self.status_file(state="connected", linked=True)
        self.assertEqual(api.dispatch("POST", {"action": "link"}, now=self.now),
                         ({"ok": False, "error": "already_linked"}, None))
        self.status_file(state="unlinked", linked=False)
        resp, verb = api.dispatch("POST", {"action": "link"}, now=self.now)
        self.assertTrue(resp["ok"])
        self.assertEqual(verb, "restart")
        self.assertEqual(config.load().link_requested_at, int(self.now))

    def test_unlink_clears_pending_link_and_nudges_unlink(self) -> None:
        self.conf(enabled=True, client_id=GOOD_ID, link_requested_at=int(self.now))
        resp, verb = api.dispatch("POST", {"action": "unlink"})
        self.assertTrue(resp["ok"])
        self.assertEqual(verb, "unlink")
        self.assertEqual(config.load().link_requested_at, 0)

    def test_mutation_without_footprint(self) -> None:
        self.conf()
        with mock.patch.object(api, "footprint_installed", return_value=False):
            for action in api.MUTATIONS:
                self.assertEqual(api.dispatch("POST", {"action": action}),
                                 ({"ok": False, "error": "not_installed"}, None))
        self.assertFalse(config.load().enabled)

    def test_allow_list_and_protocol_errors(self) -> None:
        self.conf()
        self.assertEqual(api.dispatch("POST", {"action": "start_program"}),
                         ({"ok": False, "error": "not_found"}, None))
        self.assertEqual(api.dispatch("DELETE", {}), ({"ok": False, "error": "method_not_allowed"}, None))
        self.assertEqual(api.dispatch("POST", ["x"]), ({"ok": False, "error": "invalid_json"}, None))
        resp, verb = self.run_cli("POST", b"{not json")
        self.assertEqual(resp, {"ok": False, "error": "invalid_json"})
        self.assertEqual(verb, "")
        resp, verb = self.run_cli("POST", b"x" * (C.MAX_BODY_BYTES + 1))
        self.assertEqual(resp, {"ok": False, "error": "payload_too_large"})

    def test_cli_two_lines_and_pinned_verbs_only(self) -> None:
        self.conf()
        resp, verb = self.run_cli("GET", b"")
        self.assertTrue(resp["ok"])
        self.assertEqual(verb, "")
        resp, verb = self.run_cli("POST", b'{"action":"enable"}')
        self.assertEqual(verb, "enable")
        with mock.patch.object(api, "dispatch", return_value=({"ok": True}, "rm -rf /")):
            _, verb = self.run_cli("POST", b"{}")
        self.assertEqual(verb, "")

    def test_conf_write_failure(self) -> None:
        self.conf()
        with mock.patch.object(config, "atomic_write", side_effect=OSError("ro")):
            resp, verb = api.dispatch("POST", {"action": "enable"})
        self.assertEqual(resp["error"], "conf_write_failed")
        self.assertIsNone(verb)

    def test_no_secret_in_any_response(self) -> None:
        # Even a token file readable to this process is never opened.
        tokens = os.path.join(self.tmp.name, "tokens.json")
        with open(tokens, "w") as fh:
            json.dump({"access_token": "AT-SECRET", "refresh_token": "RT-SECRET"}, fh)
        self.conf(enabled=True, client_id=GOOD_ID)
        self.status_file(state="awaiting_user", linked=False)
        with mock.patch.object(C, "TOKENS_FILE", tokens), \
                mock.patch("builtins.open", wraps=open) as spy:
            text = json.dumps(api.merged_status(self.now))
        self.assertNotIn(tokens, [c.args[0] for c in spy.call_args_list if c.args])
        for bad in ("AT-SECRET", "RT-SECRET", "access_token", "refresh_token", "device_code"):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
