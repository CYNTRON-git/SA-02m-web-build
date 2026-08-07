"""Unit tests: config API honest offline / Phase-0-down behavior."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import api  # noqa: E402


class TestApiOffline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        etc = os.path.join(self.tmp.name, "etc")
        os.makedirs(etc)
        os.environ["SA02M_ALICE_ETC"] = etc
        os.environ["SA02M_ALICE_VAR"] = os.path.join(self.tmp.name, "var")
        os.environ["SA02M_ALICE_STATUS"] = os.path.join(self.tmp.name, "status.json")
        # Reload path constants used by config_store via env — modules already
        # imported C.* ; patch module-level paths on config_store / constants.
        import sa02m_alice.common.config_store as store
        import sa02m_alice.common.constants as const

        const.ETC_DIR = etc
        const.CLIENT_CONF = os.path.join(etc, "sa02m-alice-client.conf")
        const.DEVICES_CONF = os.path.join(etc, "sa02m-alice-devices.conf")
        const.SERVER_CONF = os.path.join(etc, "sa02m-alice-server.conf")
        const.VAR_DIR = os.environ["SA02M_ALICE_VAR"]
        const.CERT_FILE = os.path.join(const.VAR_DIR, "device.crt.pem")
        const.KEY_FILE = os.path.join(const.VAR_DIR, "device.key.pem")
        const.STATUS_FILE = os.environ["SA02M_ALICE_STATUS"]
        store.C = const
        api.C = const
        with open(const.CLIENT_CONF, "w", encoding="utf-8") as fh:
            fh.write("[client]\nclient_enabled = false\n")
        with open(const.DEVICES_CONF, "w", encoding="utf-8") as fh:
            fh.write('{"rooms":[],"devices":[]}\n')
        with open(const.SERVER_CONF, "w", encoding="utf-8") as fh:
            fh.write("[gateway]\nhttp_url = https://alice.cyntron.ru\nwss_url = wss://alice.cyntron.ru/controller/socket.io\n")

    def test_link_fails_when_gateway_down(self):
        with mock.patch.object(
            api,
            "probe_gateway",
            return_value={
                "ok": True,
                "available": False,
                "error": "gateway_unreachable",
                "message": "down",
                "url": "https://alice.cyntron.ru/v1.0/ping",
            },
        ):
            result = api.start_link()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "gateway_unavailable")
        self.assertNotIn("registration_url", result)

    def test_unlink_fails_when_gateway_down_does_not_claim_success(self):
        with mock.patch.object(
            api,
            "probe_gateway",
            return_value={"ok": True, "available": False, "error": "gateway_unreachable"},
        ):
            result = api.unlink_controller()
        self.assertFalse(result["ok"])
        self.assertEqual(result.get("local_disabled"), False)

    def test_enable_default_false_and_toggle(self):
        code, cfg = api.dispatch("GET", "/integrations/alice/")
        self.assertEqual(code, 200)
        self.assertFalse(cfg["client_enabled"])
        self.assertFalse(cfg["link"]["linked"])
        code, out = api.dispatch("POST", "/integrations/alice/enable_client", {"enabled": True})
        self.assertTrue(out["client_enabled"])

    def test_client_main_exits_zero_when_disabled(self):
        from sa02m_alice.client import main as client_main

        with mock.patch.object(client_main, "_write_status"):
            rc = client_main.run()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
