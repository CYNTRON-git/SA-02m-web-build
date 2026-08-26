"""Unit tests: cert-presence / link truth must survive a root-only cert dir.

Root cause pinned here (1.0.5.80): the web API runs as www-data, which cannot
traverse /var/lib/sa02m-alice; `os.path.isfile()` then returns False for certs
that exist and the card showed «Сертификат: Нет / не привязан» on a device that
was connected over mTLS. The client (root) now publishes `cert_present` in its
world-readable status file; the API reads it from there and reports an honest
None (never a false False) when it cannot check itself.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client import main as client_main  # noqa: E402
from sa02m_alice.common import constants as C  # noqa: E402
from sa02m_alice.config import api  # noqa: E402

PROBE_UP = {"ok": True, "available": True, "url": "https://alice.cyntron.ru/v1.0/ping", "http_status": 200}

_IS_POSIX_NON_ROOT = os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0


class _CertStatusBase(unittest.TestCase):
    """Temp etc/var/status homes patched onto the constants module (the idiom
    of test_api_offline: modules already hold `C`, so patch its attributes)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        etc = os.path.join(self.tmp.name, "etc")
        os.makedirs(etc)
        self.var = os.path.join(self.tmp.name, "var")
        self.status_path = os.path.join(self.tmp.name, "run", "status.json")
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

    # -- helpers ---------------------------------------------------------
    def write_status(self, **payload):
        os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
        with open(self.status_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def make_certs(self):
        os.makedirs(self.var, exist_ok=True)
        for p in (C.CERT_FILE, C.KEY_FILE):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("-----BEGIN-----\n")

    def full_config(self):
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP):
            return api.full_config()


class TestApiCertPresence(_CertStatusBase):
    def test_client_flag_true_wins_over_unreadable_dir(self):
        # The device case: certs exist, client connected, www-data cannot enter
        # the dir (os.access → False). isfile() would lie; the client's flag wins.
        self.make_certs()
        self.write_status(state="connected", client_enabled=True, cert_present=True)
        with mock.patch("os.access", return_value=False):
            cfg = self.full_config()
        self.assertIs(cfg["mtls"]["cert_present"], True)
        self.assertEqual(cfg["mtls"]["cert_check"], api.CERT_CHECK_CLIENT)
        self.assertTrue(cfg["link"]["linked"])
        self.assertEqual(cfg["link"]["state"], "connected")

    def test_old_client_no_key_unreadable_dir_is_unknown_not_false(self):
        # Old client (no cert_present key) + dir present but not traversable:
        # honest None / "unreadable" — never a false False. Connected still links.
        os.makedirs(self.var, exist_ok=True)
        self.write_status(state="connected", client_enabled=True)
        with mock.patch("os.access", return_value=False):
            cfg = self.full_config()
        self.assertIsNone(cfg["mtls"]["cert_present"])
        self.assertEqual(cfg["mtls"]["cert_check"], api.CERT_CHECK_UNREADABLE)
        self.assertTrue(cfg["link"]["linked"])

    @unittest.skipUnless(_IS_POSIX_NON_ROOT, "real dir-permission check needs POSIX non-root")
    def test_real_untraversable_dir_is_unknown_not_false(self):
        # No mock: a real 0o000 dir holding real cert files, read as this user.
        self.make_certs()
        os.chmod(self.var, 0)
        self.addCleanup(os.chmod, self.var, stat.S_IRWXU)
        self.write_status(state="connected", client_enabled=True)
        cfg = self.full_config()
        self.assertIsNone(cfg["mtls"]["cert_present"])
        self.assertEqual(cfg["mtls"]["cert_check"], api.CERT_CHECK_UNREADABLE)
        self.assertTrue(cfg["link"]["linked"])

    def test_no_key_traversable_dir_falls_back_to_isfile(self):
        os.makedirs(self.var, exist_ok=True)
        self.write_status(state="connecting", client_enabled=True)
        cfg = self.full_config()
        self.assertIs(cfg["mtls"]["cert_present"], False)
        self.assertEqual(cfg["mtls"]["cert_check"], api.CERT_CHECK_LOCAL)
        self.make_certs()
        cfg = self.full_config()
        self.assertIs(cfg["mtls"]["cert_present"], True)
        self.assertEqual(cfg["mtls"]["cert_check"], api.CERT_CHECK_LOCAL)

    def test_missing_cert_dir_is_definite_absence(self):
        # Never enrolled: no var dir at all — a known False, not "unknown".
        self.assertFalse(os.path.exists(self.var))
        cfg = self.full_config()  # no status file either → state unknown
        self.assertIs(cfg["mtls"]["cert_present"], False)
        self.assertEqual(cfg["mtls"]["cert_check"], api.CERT_CHECK_LOCAL)
        self.assertFalse(cfg["link"]["linked"])

    def test_explicit_false_from_client_vetoes_link(self):
        self.write_status(state="connected", client_enabled=True, cert_present=False)
        cfg = self.full_config()
        self.assertIs(cfg["mtls"]["cert_present"], False)
        self.assertFalse(cfg["link"]["linked"])

    def test_not_connected_never_linked_even_with_cert(self):
        self.write_status(state="offline", client_enabled=True, cert_present=True)
        cfg = self.full_config()
        self.assertIs(cfg["mtls"]["cert_present"], True)
        self.assertFalse(cfg["link"]["linked"])

    def test_non_bool_status_value_is_ignored(self):
        # A corrupt/foreign value in the status file must not be trusted as bool.
        os.makedirs(self.var, exist_ok=True)
        self.write_status(state="connected", client_enabled=True, cert_present="yes")
        cfg = self.full_config()
        self.assertEqual(cfg["mtls"]["cert_check"], api.CERT_CHECK_LOCAL)
        self.assertIs(cfg["mtls"]["cert_present"], False)


class TestClientStatusPublishesCert(_CertStatusBase):
    def test_write_status_includes_cert_present(self):
        client_main._write_status("missing_cert", client_enabled=True)
        with open(self.status_path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("cert_present", data)
        self.assertIs(data["cert_present"], False)
        self.assertEqual(data["state"], "missing_cert")
        self.make_certs()
        client_main._write_status("connected", client_enabled=True)
        with open(self.status_path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIs(data["cert_present"], True)
        self.assertEqual(data["client_enabled"], True)

    @unittest.skipUnless(os.name == "posix", "file mode bits are POSIX")
    def test_status_file_is_world_readable(self):
        old = os.umask(0o077)  # a restrictive unit umask must not hide the file
        self.addCleanup(os.umask, old)
        client_main._write_status("connecting", client_enabled=True)
        mode = stat.S_IMODE(os.stat(self.status_path).st_mode)
        self.assertEqual(mode & 0o444, 0o444)


class _Resp:
    status = 200

    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class TestPendingClaimLifecycle(_CertStatusBase):
    """Decision (1.0.5.80): pending_claim.json is KEPT after enrollment — the
    gateway needs claim_token for unlink. Since 1.0.6.14 the status/link view
    reads it for registration_url, triple-gated (claim_token + not issued +
    fresh expires_at) so an issued or stale leftover cannot flip the UI."""

    def test_complete_link_keeps_claim_token_marked_issued(self):
        api._save_pending_claim({"claim_token": "tok", "registration_url": "u", "controller_sn": "SN1"})
        issue = {"ok": True, "device.crt.pem": "C", "device.key.pem": "K", "ca.crt.pem": "A"}
        with mock.patch.object(api, "probe_gateway", return_value=PROBE_UP), mock.patch.object(
            api.urllib.request, "urlopen", return_value=_Resp(json.dumps(issue).encode("utf-8"))
        ):
            result = api.complete_link()
        self.assertTrue(result["ok"])
        self.assertTrue(result["cert_present"])
        pending = api._load_pending_claim()
        self.assertEqual(pending.get("claim_token"), "tok")
        self.assertEqual(pending.get("controller_sn"), "SN1")
        self.assertIs(pending.get("issued"), True)

    def test_status_view_ignores_leftover_pending_claim(self):
        self.make_certs()
        self.write_status(state="connected", client_enabled=True, cert_present=True)
        without = self.full_config()
        api._save_pending_claim({"claim_token": "tok", "controller_sn": "SN1", "issued": True})
        with_file = self.full_config()
        self.assertEqual(without["link"], with_file["link"])
        self.assertEqual(without["mtls"], with_file["mtls"])
        self.assertTrue(with_file["link"]["linked"])

    def test_registration_url_served_only_while_claim_fresh(self):
        """Pins the 1.0.6.14 freshness gate: a fresh un-issued claim serves its
        registration_url; an expired one does not; a LEGACY claim without
        expires_at (pre-1.0.6.14 abandoned link) does not either — otherwise a
        dead URL locks the UI into «Завершить привязку» with no way back."""
        self.write_status(state="missing_cert", client_enabled=True, cert_present=False)
        fresh = {"claim_token": "tok", "registration_url": "https://u/1",
                 "controller_sn": "SN1", "expires_at": time.time() + 500}
        api._save_pending_claim(fresh)
        link = self.full_config()["link"]
        self.assertEqual(link["registration_url"], "https://u/1")
        self.assertIs(link["pending"], True)
        api._save_pending_claim(dict(fresh, expires_at=time.time() - 5))
        link = self.full_config()["link"]
        self.assertIsNone(link["registration_url"])
        self.assertIs(link["pending"], False)
        legacy = {"claim_token": "tok", "registration_url": "https://u/1",
                  "controller_sn": "SN1"}
        api._save_pending_claim(legacy)
        link = self.full_config()["link"]
        self.assertIsNone(link["registration_url"])
        self.assertIs(link["pending"], False)
        api._save_pending_claim(dict(fresh, issued=True))
        self.assertIsNone(self.full_config()["link"]["registration_url"])


if __name__ == "__main__":
    unittest.main()
