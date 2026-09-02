"""Unit tests: the cloud profile of the smart-home client (1.0.6.26).

Validating tests for docs/contracts/alice-mqtt-mapping.md §Profiles: profile
parsing, URL/header selection (token instead of serial, no client cert on the
cloud profile), token minting (fresh on every connect, never persisted), the
`missing_identity` standby, and `controller_unlink` being unreachable on the
cloud profile.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_alice.client import fleet_token  # noqa: E402
from sa02m_alice.client import main as client_main  # noqa: E402
from sa02m_alice.client.device_registry import DeviceRegistry  # noqa: E402
from sa02m_alice.client.sio_connection import AliceSocketIO, split_engine_url  # noqa: E402
from sa02m_alice.client.sio_handlers import SioHandlers  # noqa: E402
from sa02m_alice.common import config_store, constants as C  # noqa: E402


class TestProfileParsing(unittest.TestCase):
    def test_default_is_yandex(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SA02M_ALICE_PROFILE", None)
            self.assertEqual(client_main.parse_args([]).profile, C.PROFILE_YANDEX)

    def test_flag_selects_cloud(self):
        self.assertEqual(client_main.parse_args(["--profile", "cloud"]).profile, C.PROFILE_CLOUD)

    def test_env_selects_cloud(self):
        with mock.patch.dict(os.environ, {"SA02M_ALICE_PROFILE": "cloud"}):
            self.assertEqual(client_main.parse_args([]).profile, C.PROFILE_CLOUD)

    def test_unknown_profile_rejected(self):
        with self.assertRaises(SystemExit):
            client_main.parse_args(["--profile", "nope"])

    def test_status_path_per_profile(self):
        self.assertEqual(client_main.status_path(C.PROFILE_YANDEX), C.STATUS_FILE)
        self.assertEqual(client_main.status_path(C.PROFILE_CLOUD), C.STATUS_FILE_CLOUD)
        self.assertNotEqual(C.STATUS_FILE, C.STATUS_FILE_CLOUD)


class TestHandshakeSelection(unittest.TestCase):
    """Cloud: X-Control-Token + the three existing headers, no serial;
    Yandex: byte-identical to before (serial, no token)."""

    def test_cloud_headers_carry_token_not_serial(self):
        sio = AliceSocketIO(
            profile=C.PROFILE_CLOUD,
            controller_sn="SN-MUST-NOT-LEAK",
            client_version="1.0.6.26",
            fw_version="1.0.6.26",
            hw_variant="sa02m-1eth",
        )
        headers = sio._build_headers("jwt-abc")
        self.assertEqual(headers[C.HDR_CONTROL_TOKEN], "jwt-abc")
        self.assertEqual(headers["X-Client-Version"], "1.0.6.26")
        self.assertEqual(headers["X-FW-Version"], "1.0.6.26")
        self.assertEqual(headers["X-HW-Variant"], "sa02m-1eth")
        self.assertNotIn("X-Controller-SN", headers)
        self.assertNotIn("X-Device-Id", headers)
        self.assertNotIn("X-Device-Secret", headers)

    def test_yandex_headers_unchanged(self):
        sio = AliceSocketIO(controller_sn="SN123", client_version="1.0.0")
        headers = sio._build_headers()
        self.assertEqual(headers["X-Controller-SN"], "SN123")
        self.assertNotIn(C.HDR_CONTROL_TOKEN, headers)

    def test_cloud_target_is_plain_tls_without_client_cert(self):
        sio = AliceSocketIO(profile=C.PROFILE_CLOUD)
        with mock.patch.object(
            config_store, "default_server_cfg", side_effect=config_store.default_server_cfg
        ):
            url, engine_path, use_tls, ws_extra = sio._connect_target()
        self.assertEqual(url, "wss://cloud.cyntron.ru")
        self.assertEqual(engine_path, "control/socket.io")
        self.assertTrue(use_tls)
        self.assertIsNone(ws_extra)  # no sslopt certfile/keyfile → no client cert

    def test_cloud_target_does_not_require_the_alice_cert(self):
        # The Yandex profile raises FileNotFoundError without a cert; the
        # cloud profile must not even look at it.
        sio = AliceSocketIO(profile=C.PROFILE_CLOUD)
        with mock.patch("sa02m_alice.client.sio_connection.cert_paths_present", return_value=False):
            url, _p, _t, _x = sio._connect_target()
        self.assertTrue(url.startswith("wss://"))

    def test_cloud_profile_does_not_register_controller_unlink(self):
        self.assertNotIn(C.EVT_CONTROLLER_UNLINK, AliceSocketIO(profile=C.PROFILE_CLOUD)._events())
        self.assertIn(C.EVT_CONTROLLER_UNLINK, AliceSocketIO()._events())
        # The three request events stay on both.
        for ev in (C.EVT_DEVICES_LIST, C.EVT_DEVICES_QUERY, C.EVT_DEVICES_ACTION):
            self.assertIn(ev, AliceSocketIO(profile=C.PROFILE_CLOUD)._events())

    def test_token_minted_inside_every_connect(self):
        calls = []

        def provider():
            calls.append(1)
            return "jwt-%d" % len(calls)

        seen = []

        class _FakeSio:
            def __init__(self, **_kw):
                pass

            def event(self, fn):
                return fn

            def on(self, *_a):
                pass

            def connect(self, url, **kw):
                seen.append((url, kw["socketio_path"], kw["headers"]))

            def get_sid(self):
                return "sid1"

        fake_socketio = mock.Mock(Client=_FakeSio)
        sio = AliceSocketIO(profile=C.PROFILE_CLOUD, token_provider=provider)
        with mock.patch("sa02m_alice.client.sio_connection.import_socketio", return_value=fake_socketio):
            sio.connect()
            sio.connect()
        self.assertEqual(len(calls), 2)
        self.assertEqual(seen[0][2][C.HDR_CONTROL_TOKEN], "jwt-1")
        self.assertEqual(seen[1][2][C.HDR_CONTROL_TOKEN], "jwt-2")
        self.assertEqual(seen[0][1], "control/socket.io")

    def test_cloud_profile_without_provider_refuses_to_connect(self):
        sio = AliceSocketIO(profile=C.PROFILE_CLOUD)
        with mock.patch("sa02m_alice.client.sio_connection.import_socketio", return_value=mock.Mock()):
            with self.assertRaises(RuntimeError):
                sio.connect()


class TestSplitEngineUrl(unittest.TestCase):
    def test_control_entry(self):
        self.assertEqual(
            split_engine_url("wss://cloud.cyntron.ru/control/socket.io"),
            ("wss://cloud.cyntron.ru", "control/socket.io"),
        )

    def test_lab_ws_with_port(self):
        self.assertEqual(
            split_engine_url("ws://192.168.1.10:8080/control/socket.io"),
            ("ws://192.168.1.10:8080", "control/socket.io"),
        )

    def test_bare_host_gets_default_engine_path(self):
        self.assertEqual(split_engine_url("wss://h"), ("wss://h", "socket.io"))

    def test_prefix_without_socket_io_segment(self):
        self.assertEqual(split_engine_url("wss://h/control"), ("wss://h", "control/socket.io"))


class _TempIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = os.path.join(self.tmp.name, "agent.conf")
        self.secret = os.path.join(self.tmp.name, "device_secret")
        self._old = (C.CLOUD_AGENT_CONF, C.CLOUD_DEVICE_SECRET)
        C.CLOUD_AGENT_CONF = self.conf
        C.CLOUD_DEVICE_SECRET = self.secret
        self.addCleanup(self._restore)

    def _restore(self):
        C.CLOUD_AGENT_CONF, C.CLOUD_DEVICE_SECRET = self._old

    def write_conf(self, text):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(text)

    def write_secret(self, text="s3cr3t"):
        with open(self.secret, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")


class TestCloudIdentity(_TempIdentity):
    def test_device_id_from_agent_conf(self):
        self.write_conf("[cloud]\ndevice_id = sa02m-abc\n")
        self.write_secret()
        self.assertEqual(fleet_token.read_cloud_identity(), ("sa02m-abc", "s3cr3t"))
        self.assertTrue(fleet_token.cloud_identity_present())

    def test_serial_fallback_matches_the_agent_convention(self):
        self.write_conf("[cloud]\ndevice_id =\n[device]\nserial = AB12CD\n")
        self.write_secret()
        self.assertEqual(fleet_token.read_cloud_identity()[0], "sa02m-ab12cd")

    def test_missing_secret_is_not_enrolled(self):
        self.write_conf("[cloud]\ndevice_id = sa02m-abc\n")
        self.assertFalse(fleet_token.cloud_identity_present())

    def test_missing_conf_is_not_enrolled(self):
        self.write_secret()
        self.assertFalse(fleet_token.cloud_identity_present())

    def test_conf_without_id_or_serial_is_not_enrolled(self):
        self.write_conf("[cloud]\napi_url = https://x/api/v1\n")
        self.write_secret()
        self.assertFalse(fleet_token.cloud_identity_present())

    def test_token_url_derives_from_agent_api_url(self):
        self.write_conf("[cloud]\napi_url = https://bench.local/api/v1/\n")
        _wss, token_url = config_store.cloud_control_urls()
        self.assertEqual(token_url, "https://bench.local/api/v1/control/token")

    def test_token_url_default_without_agent_conf(self):
        _wss, token_url = config_store.cloud_control_urls()
        self.assertEqual(token_url, "https://cloud.cyntron.ru/api/v1/control/token")


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _http_error(code, body=b""):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


class TestMintControlToken(unittest.TestCase):
    def _opener(self, body=None, error=None):
        captured = {}

        def opener(req, timeout):
            captured["req"] = req
            captured["timeout"] = timeout
            if error is not None:
                raise error
            return _Resp(json.dumps(body).encode("utf-8"))

        return opener, captured

    def test_posts_identity_and_returns_token(self):
        opener, cap = self._opener({"ok": True, "token": "jwt", "expires_in_s": 600})
        tok = fleet_token.mint_control_token("https://c/api/v1/control/token", "sa02m-1", "sec", opener=opener)
        self.assertEqual(tok, "jwt")
        req = cap["req"]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data.decode("utf-8")), {"device_id": "sa02m-1", "device_secret": "sec"})
        self.assertEqual(cap["timeout"], C.CLOUD_TOKEN_TIMEOUT_S)

    def test_ok_false_is_error_with_fleet_reason(self):
        opener, _ = self._opener({"ok": False, "error": "revoked"})
        with self.assertRaises(fleet_token.FleetTokenError) as ctx:
            fleet_token.mint_control_token("https://c", "d", "s", opener=opener)
        self.assertEqual(ctx.exception.state, C.STATE_ERROR)
        self.assertEqual(ctx.exception.reason, "revoked")

    def test_403_is_error_with_fleet_reason(self):
        opener, _ = self._opener(error=_http_error(403, b'{"ok":false,"error":"invalid credential"}'))
        with self.assertRaises(fleet_token.FleetTokenError) as ctx:
            fleet_token.mint_control_token("https://c", "d", "s", opener=opener)
        self.assertEqual(ctx.exception.state, C.STATE_ERROR)
        self.assertEqual(ctx.exception.reason, "invalid credential")

    def test_429_throttle_is_error_with_fleet_reason(self):
        opener, _ = self._opener(error=_http_error(429, b'{"ok":false,"error":"too many requests"}'))
        with self.assertRaises(fleet_token.FleetTokenError) as ctx:
            fleet_token.mint_control_token("https://c", "d", "s", opener=opener)
        self.assertEqual(ctx.exception.state, C.STATE_ERROR)
        self.assertEqual(ctx.exception.reason, "too many requests")

    def test_503_is_offline(self):
        opener, _ = self._opener(error=_http_error(503))
        with self.assertRaises(fleet_token.FleetTokenError) as ctx:
            fleet_token.mint_control_token("https://c", "d", "s", opener=opener)
        self.assertEqual(ctx.exception.state, C.STATE_OFFLINE)

    def test_transport_failure_propagates_as_generic_error(self):
        opener, _ = self._opener(error=urllib.error.URLError("down"))
        with self.assertRaises(urllib.error.URLError):
            fleet_token.mint_control_token("https://c", "d", "s", opener=opener)

    def test_empty_token_refused(self):
        opener, _ = self._opener({"ok": True, "token": ""})
        with self.assertRaises(fleet_token.FleetTokenError):
            fleet_token.mint_control_token("https://c", "d", "s", opener=opener)


class TestUnlinkIgnoredOnCloud(unittest.TestCase):
    """The handler's own log line is the observable — the same constructor
    the client uses, no test-only hook."""

    def _handlers(self, profile):
        return SioHandlers(
            DeviceRegistry({"rooms": [], "devices": []}),
            publish_mqtt=lambda *_a: None,
            emit_response=lambda *_a: None,
            profile=profile,
        )

    def test_cloud_profile_ignores_unlink(self):
        with self.assertLogs("sa02m_alice.handlers", level="INFO") as cm:
            self._handlers(C.PROFILE_CLOUD).handle(C.EVT_CONTROLLER_UNLINK, {})
        self.assertTrue(any("ignored on the cloud profile" in line for line in cm.output))
        self.assertFalse(any("Gateway requested controller unlink" in line for line in cm.output))

    def test_yandex_profile_handles_unlink(self):
        with self.assertLogs("sa02m_alice.handlers", level="INFO") as cm:
            self._handlers(C.PROFILE_YANDEX).handle(C.EVT_CONTROLLER_UNLINK, {})
        self.assertTrue(any("Gateway requested controller unlink" in line for line in cm.output))


class TestMissingIdentityStandby(_TempIdentity):
    """Cloud profile with no identity: standby state, exit 0 when disabled —
    the twin of the missing_cert wait, driven through the real run()."""

    def setUp(self):
        super().setUp()
        etc = os.path.join(self.tmp.name, "etc")
        os.makedirs(etc)
        self._old_paths = (C.ETC_DIR, C.CLIENT_CONF, C.DEVICES_CONF, C.SERVER_CONF, C.STATUS_FILE_CLOUD)
        C.ETC_DIR = etc
        C.CLIENT_CONF = os.path.join(etc, "sa02m-alice-client.conf")
        C.DEVICES_CONF = os.path.join(etc, "sa02m-alice-devices.conf")
        C.SERVER_CONF = os.path.join(etc, "sa02m-alice-server.conf")
        C.STATUS_FILE_CLOUD = os.path.join(self.tmp.name, "run", "status-cloud.json")
        self.addCleanup(self._restore_paths)
        with open(C.DEVICES_CONF, "w", encoding="utf-8") as fh:
            fh.write('{"rooms":[],"devices":[]}\n')

    def _restore_paths(self):
        (C.ETC_DIR, C.CLIENT_CONF, C.DEVICES_CONF, C.SERVER_CONF, C.STATUS_FILE_CLOUD) = self._old_paths

    def _conf(self, cloud_flag):
        with open(C.CLIENT_CONF, "w", encoding="utf-8") as fh:
            fh.write("[client]\nclient_enabled = false\ncloud_control_enabled = %s\n" % cloud_flag)

    def _status(self):
        with open(C.STATUS_FILE_CLOUD, encoding="utf-8") as fh:
            return json.load(fh)

    def test_disabled_flag_exits_zero_and_writes_own_status_file(self):
        self._conf("false")
        rc = client_main.run(C.PROFILE_CLOUD)
        self.assertEqual(rc, 0)
        st = self._status()
        self.assertEqual(st["state"], C.STATE_DISABLED)
        self.assertEqual(st["profile"], C.PROFILE_CLOUD)
        self.assertIn("identity_present", st)
        self.assertFalse(st["identity_present"])
        # The flag this unit reflects is named for what it is.
        self.assertIs(st["cloud_control_enabled"], False)
        self.assertNotIn("client_enabled", st)

    def test_file_not_found_mid_run_is_error_not_missing_cert(self):
        # M2: the cloud profile has no cert to miss — a FileNotFoundError out
        # of the connect path is published as `error` with the real reason.
        self._conf("true")
        self.write_conf("[cloud]\ndevice_id = sa02m-abc\n")
        self.write_secret()
        written = []
        real_write = client_main._write_status

        def spy(state, **kw):
            written.append((state, kw.get("error")))
            real_write(state, **kw)

        class _BoomSio:
            def __init__(self, **_kw):
                self.connected = False

            def connect(self):
                raise FileNotFoundError("/no/such/thing")

            def disconnect(self):
                pass

        def fake_wait(_s):
            self._conf("false")  # the wait after the error clears the flag → exit 0
            return False

        with mock.patch.object(client_main, "_write_status", side_effect=spy), \
                mock.patch.object(client_main, "_mqtt_client", return_value=mock.Mock()), \
                mock.patch.object(client_main, "AliceSocketIO", _BoomSio), \
                mock.patch.object(client_main._stop, "wait", side_effect=fake_wait), \
                mock.patch("sa02m_alice.client.sio_connection.import_socketio", return_value=mock.Mock()):
            rc = client_main.run(C.PROFILE_CLOUD)
        self.assertEqual(rc, 0)
        states = [s for s, _e in written]
        self.assertIn(C.STATE_ERROR, states)
        self.assertNotIn(C.STATE_MISSING_CERT, states)
        self.assertIn((C.STATE_ERROR, "file_not_found"), written)

    def test_no_identity_is_standby_then_exit_zero_on_disable(self):
        self._conf("true")
        # The wait loop's first tick sees the flag cleared → return 0.
        wait_calls = []

        def fake_wait(_s):
            wait_calls.append(1)
            self._conf("false")
            return False

        with mock.patch.object(client_main._stop, "wait", side_effect=fake_wait), \
                mock.patch("sa02m_alice.client.main.import_socketio", create=True), \
                mock.patch("sa02m_alice.client.sio_connection.import_socketio", return_value=mock.Mock()):
            rc = client_main.run(C.PROFILE_CLOUD)
        self.assertEqual(rc, 0)
        self.assertEqual(len(wait_calls), 1)
        st = self._status()
        self.assertEqual(st["state"], C.STATE_MISSING_IDENTITY)
        self.assertEqual(st["error"], "missing_identity")
        self.assertFalse(st["identity_present"])

    def test_yandex_profile_status_file_untouched_by_cloud_run(self):
        self._conf("false")
        client_main.run(C.PROFILE_CLOUD)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "run", "status.json")))


if __name__ == "__main__":
    unittest.main()
