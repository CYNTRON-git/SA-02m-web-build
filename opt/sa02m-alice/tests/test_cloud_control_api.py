"""Unit tests: the web API's `cloud_control` block + enable/disable actions (1.0.6.26).

Validating tests for docs/contracts/alice-mqtt-mapping.md §Profiles (web API
paragraph): the flag gates exactly like client_enabled, `cloud_enrolled` is
tri-state like mtls.cert_present (the root client's status file first, a local
stat only when the dir is traversable, never a false False), and the factory
reset helper clears both flags.
"""

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

from sa02m_alice.common import config_store, constants as C  # noqa: E402
from sa02m_alice.config import api  # noqa: E402

PROBE_DOWN = {"ok": True, "available": False, "error": "gateway_unreachable", "url": "https://x/v1.0/ping"}


class _CloudApiBase(unittest.TestCase):
    """Temp etc/var/run/cloud homes patched onto the constants module (the
    idiom of test_cert_status: modules already hold `C`, so patch its
    attributes)."""

    KEYS = (
        "ETC_DIR", "CLIENT_CONF", "DEVICES_CONF", "SERVER_CONF", "VAR_DIR", "CERT_FILE",
        "KEY_FILE", "CA_FILE", "PENDING_CLAIM_FILE", "STATUS_FILE", "STATUS_FILE_CLOUD",
        "CLOUD_AGENT_CONF", "CLOUD_DEVICE_SECRET",
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = {k: getattr(C, k) for k in self.KEYS}
        self.addCleanup(self._restore)
        etc = os.path.join(self.tmp.name, "etc")
        os.makedirs(etc)
        self.cloud_dir = os.path.join(self.tmp.name, "cloud")
        C.ETC_DIR = etc
        C.CLIENT_CONF = os.path.join(etc, "sa02m-alice-client.conf")
        C.DEVICES_CONF = os.path.join(etc, "sa02m-alice-devices.conf")
        C.SERVER_CONF = os.path.join(etc, "sa02m-alice-server.conf")
        C.VAR_DIR = os.path.join(self.tmp.name, "var")
        C.CERT_FILE = os.path.join(C.VAR_DIR, "device.crt.pem")
        C.KEY_FILE = os.path.join(C.VAR_DIR, "device.key.pem")
        C.CA_FILE = os.path.join(C.VAR_DIR, "ca.crt.pem")
        C.PENDING_CLAIM_FILE = os.path.join(C.VAR_DIR, "pending_claim.json")
        C.STATUS_FILE = os.path.join(self.tmp.name, "run", "status.json")
        C.STATUS_FILE_CLOUD = os.path.join(self.tmp.name, "run", "status-cloud.json")
        C.CLOUD_AGENT_CONF = os.path.join(self.cloud_dir, "agent.conf")
        C.CLOUD_DEVICE_SECRET = os.path.join(self.cloud_dir, "device_secret")
        with open(C.CLIENT_CONF, "w", encoding="utf-8") as fh:
            fh.write("[client]\nclient_enabled = false\n")
        with open(C.DEVICES_CONF, "w", encoding="utf-8") as fh:
            fh.write('{"rooms":[],"devices":[]}\n')
        with open(C.SERVER_CONF, "w", encoding="utf-8") as fh:
            fh.write("[gateway]\nhttp_url = https://alice.cyntron.ru\n")

    def _restore(self):
        for k, v in self._old.items():
            setattr(C, k, v)

    def write_cloud_status(self, **payload):
        os.makedirs(os.path.dirname(C.STATUS_FILE_CLOUD), exist_ok=True)
        with open(C.STATUS_FILE_CLOUD, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def enroll(self):
        os.makedirs(self.cloud_dir, exist_ok=True)
        with open(C.CLOUD_AGENT_CONF, "w", encoding="utf-8") as fh:
            fh.write("[cloud]\ndevice_id = sa02m-abc\n")
        with open(C.CLOUD_DEVICE_SECRET, "w", encoding="utf-8") as fh:
            fh.write("s3cr3t\n")

    def full(self):
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_DOWN):
            return api.full_config()


class TestCloudControlBlock(_CloudApiBase):
    def test_default_disabled_block_present(self):
        cc = self.full()["cloud_control"]
        self.assertFalse(cc["enabled"])
        self.assertEqual(cc["state"], C.STATE_DISABLED)
        self.assertIn("cloud_enrolled", cc)
        self.assertIn("cloud_check", cc)

    def test_client_flag_wins_over_local_check(self):
        # No identity files locally, but the root client says present → True.
        self.write_cloud_status(state="connected", ts=1, identity_present=True)
        cc = self.full()["cloud_control"]
        self.assertIs(cc["cloud_enrolled"], True)
        self.assertEqual(cc["cloud_check"], api.CERT_CHECK_CLIENT)
        self.assertEqual(cc["state"], C.STATE_DISABLED)  # flag off → disabled, not the file's state

    def test_missing_cloud_dir_is_definite_false(self):
        cc = self.full()["cloud_control"]
        self.assertIs(cc["cloud_enrolled"], False)
        self.assertEqual(cc["cloud_check"], api.CERT_CHECK_LOCAL)

    def test_local_identity_files_read_when_dir_traversable(self):
        self.enroll()
        cc = self.full()["cloud_control"]
        self.assertIs(cc["cloud_enrolled"], True)
        self.assertEqual(cc["cloud_check"], api.CERT_CHECK_LOCAL)

    def test_local_check_never_opens_the_secret(self):
        # M1: presence only. The secret is 0600 root on a board; a www-data
        # open() would raise, and the old path turned that into a false False
        # that locked the cloud-control button on an enrolled board.
        self.enroll()
        real_open = open

        def guarded_open(path, *a, **kw):
            if os.fspath(path) == C.CLOUD_DEVICE_SECRET:
                raise PermissionError("secret must not be opened by the web API")
            return real_open(path, *a, **kw)

        with mock.patch("builtins.open", side_effect=guarded_open):
            cc = self.full()["cloud_control"]
        self.assertIs(cc["cloud_enrolled"], True)
        self.assertEqual(cc["cloud_check"], api.CERT_CHECK_LOCAL)

    def test_unreadable_agent_conf_is_unknown_not_false(self):
        self.enroll()
        real_open = open

        def guarded_open(path, *a, **kw):
            if os.fspath(path) == C.CLOUD_AGENT_CONF:
                raise PermissionError("conf not readable")
            return real_open(path, *a, **kw)

        with mock.patch("builtins.open", side_effect=guarded_open):
            cc = self.full()["cloud_control"]
        self.assertIsNone(cc["cloud_enrolled"])
        self.assertEqual(cc["cloud_check"], api.CERT_CHECK_UNREADABLE)

    def test_secret_without_identity_in_conf_is_false(self):
        os.makedirs(self.cloud_dir, exist_ok=True)
        with open(C.CLOUD_AGENT_CONF, "w", encoding="utf-8") as fh:
            fh.write("[cloud]\napi_url = https://x/api/v1\n")
        with open(C.CLOUD_DEVICE_SECRET, "w", encoding="utf-8") as fh:
            fh.write("s3cr3t\n")
        cc = self.full()["cloud_control"]
        self.assertIs(cc["cloud_enrolled"], False)

    def test_web_api_module_does_not_import_the_secret_reader(self):
        # The only open() of the device secret must stay in the root client.
        import sa02m_alice.config.api as api_mod

        self.assertFalse(hasattr(api_mod, "cloud_identity_present"))
        self.assertFalse(hasattr(api_mod, "read_cloud_identity"))

    def test_untraversable_dir_is_unknown_not_false(self):
        self.enroll()
        with mock.patch("sa02m_alice.config.api.os.access", return_value=False):
            cc = self.full()["cloud_control"]
        self.assertIsNone(cc["cloud_enrolled"])
        self.assertEqual(cc["cloud_check"], api.CERT_CHECK_UNREADABLE)

    def test_non_bool_status_value_is_ignored(self):
        self.write_cloud_status(state="connected", ts=1, identity_present="yes")
        cc = self.full()["cloud_control"]
        self.assertIs(cc["cloud_enrolled"], False)  # fell through to the local (missing dir) check

    def test_enabled_reads_state_ts_error_from_status_file(self):
        config_store.set_cloud_control_enabled(True)
        self.write_cloud_status(state="error", ts=1234, error="revoked", identity_present=True)
        cc = self.full()["cloud_control"]
        self.assertTrue(cc["enabled"])
        self.assertEqual(cc["state"], "error")
        self.assertEqual(cc["ts"], 1234)
        self.assertEqual(cc["error"], "revoked")


class TestCloudControlActions(_CloudApiBase):
    def test_enable_disable_flip_only_the_cloud_flag(self):
        code, out = api.dispatch("POST", "/", {"action": "cloud_control_enable"})
        self.assertEqual(code, 200)
        self.assertTrue(out["ok"])
        self.assertTrue(out["cloud_control_enabled"])
        self.assertEqual(out["restart_unit"], "sa02m-cloud-control")
        self.assertTrue(config_store.cloud_control_enabled())
        self.assertFalse(config_store.client_enabled())
        code, out = api.dispatch("POST", "/", {"action": "cloud_control_disable"})
        self.assertFalse(out["cloud_control_enabled"])
        self.assertFalse(config_store.cloud_control_enabled())

    def test_yandex_enable_leaves_cloud_flag_alone(self):
        config_store.set_cloud_control_enabled(True)
        api.dispatch("POST", "/", {"action": "enable"})
        self.assertTrue(config_store.client_enabled())
        self.assertTrue(config_store.cloud_control_enabled())
        api.dispatch("POST", "/", {"action": "disable"})
        self.assertTrue(config_store.cloud_control_enabled())

    def test_profile_enabled_dispatches_per_profile(self):
        config_store.set_cloud_control_enabled(True)
        self.assertTrue(config_store.profile_enabled(C.PROFILE_CLOUD))
        self.assertFalse(config_store.profile_enabled(C.PROFILE_YANDEX))

    def test_unknown_action_still_not_found(self):
        code, out = api.dispatch("POST", "/", {"action": "cloud_control_nuke"})
        self.assertEqual(code, 404)
        self.assertFalse(out["ok"])

    def test_reset_mappings_clears_both_flags(self):
        config_store.set_client_enabled(True)
        config_store.set_cloud_control_enabled(True)
        out = api.reset_mappings()
        self.assertFalse(out["client_enabled"])
        self.assertFalse(out["cloud_control_enabled"])
        self.assertFalse(config_store.client_enabled())
        self.assertFalse(config_store.cloud_control_enabled())

    def test_flag_toggle_keeps_operator_keys(self):
        with open(C.CLIENT_CONF, "w", encoding="utf-8") as fh:
            fh.write("[client]\nclient_enabled = true\nlog_level = DEBUG\n")
        config_store.set_cloud_control_enabled(True)
        cfg = config_store.default_client_cfg()
        self.assertTrue(cfg.getboolean("client", "client_enabled"))
        self.assertEqual(cfg.get("client", "log_level"), "DEBUG")
        self.assertTrue(cfg.getboolean("client", "cloud_control_enabled"))


if __name__ == "__main__":
    unittest.main()
