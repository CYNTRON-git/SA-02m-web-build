"""Alice controller constants (Yandex Smart Home error codes + paths)."""

from __future__ import annotations

import os

# Runtime paths (device)
ETC_DIR = os.environ.get("SA02M_ALICE_ETC", "/etc/sa02m-alice")
CLIENT_CONF = os.path.join(ETC_DIR, "sa02m-alice-client.conf")
DEVICES_CONF = os.path.join(ETC_DIR, "sa02m-alice-devices.conf")
SERVER_CONF = os.path.join(ETC_DIR, "sa02m-alice-server.conf")
VAR_DIR = os.environ.get("SA02M_ALICE_VAR", "/var/lib/sa02m-alice")
CERT_FILE = os.path.join(VAR_DIR, "device.crt.pem")
KEY_FILE = os.path.join(VAR_DIR, "device.key.pem")
CA_FILE = os.path.join(VAR_DIR, "ca.crt.pem")
PENDING_CLAIM_FILE = os.path.join(VAR_DIR, "pending_claim.json")
STATUS_FILE = os.environ.get(
    "SA02M_ALICE_STATUS", "/run/sa02m-alice/status.json"
)
# Second client profile (docs/contracts/alice-mqtt-mapping.md §Profiles): the
# same package run by sa02m-cloud-control.service against the fleet cloud's
# control entry. Its own status file — the two units run side by side.
STATUS_FILE_CLOUD = os.environ.get(
    "SA02M_ALICE_STATUS_CLOUD", "/run/sa02m-alice/status-cloud.json"
)

# Client profiles. `alice` is the package's historical name; the package is the
# smart-home transport and the Yandex gateway is one consumer of it.
# PROFILE_YANDEX is the mTLS session to the Alice gateway (alice.cyntron.ru) —
# the only session that carries controller_unlink, and the one whose list
# payload must stay Yandex-discovery-shaped. PROFILE_CLOUD is a plain cloud-hub
# session (no enrolment to unlink; the list payload additionally carries
# rooms/groups/scenarios for the cloud control view).
PROFILE_YANDEX = "yandex"
PROFILE_CLOUD = "cloud"
PROFILES = (PROFILE_YANDEX, PROFILE_CLOUD)

# Cloud identity — written by the cloud agent at enrollment (Phase C), never by
# this package. The secret is 0600 root: only the root client reads it.
CLOUD_AGENT_CONF = os.environ.get("SA02M_CLOUD_AGENT_CONF", "/etc/sa02m-cloud/agent.conf")
CLOUD_DEVICE_SECRET = os.environ.get(
    "SA02M_CLOUD_DEVICE_SECRET", "/etc/sa02m-cloud/device_secret"
)

DEFAULT_GATEWAY_WSS = "wss://alice.cyntron.ru/controller/socket.io"
DEFAULT_GATEWAY_HTTP = "https://alice.cyntron.ru"
# Cloud profile seam (fixed with the sibling `cloud` repo, 1.0.6.26): nginx
# rewrites /control/socket.io to the hub's /controller/socket.io.
DEFAULT_CLOUD_CONTROL_URL = "wss://cloud.cyntron.ru/control/socket.io"
# Token mint endpoint, relative to the cloud agent's `api_url`
# (/etc/sa02m-cloud/agent.conf [cloud] api_url, default …/api/v1).
DEFAULT_CLOUD_API_URL = "https://cloud.cyntron.ru/api/v1"
CLOUD_TOKEN_PATH = "/control/token"
CLOUD_TOKEN_TIMEOUT_S = 10.0
# Handshake header carrying the minted JWT. Fixed seam name — never rename.
HDR_CONTROL_TOKEN = "X-Control-Token"
DEFAULT_MQTT_HOST = "127.0.0.1"
DEFAULT_MQTT_PORT = 1883
SIO_PATH = "/socket.io"

# Socket.IO events (controller ↔ gateway)
EVT_DEVICES_LIST = "alice_devices_list"
EVT_DEVICES_QUERY = "alice_devices_query"
EVT_DEVICES_ACTION = "alice_devices_action"
EVT_DEVICES_RENAME = "alice_devices_rename"
EVT_DEVICES_ROOMS = "alice_devices_rooms"
EVT_DEVICES_GROUPS = "alice_devices_groups"
EVT_DEVICE_STATE = "device_state"
EVT_CONTROLLER_UNLINK = "controller_unlink"
# Cloud catalogue/scenario channel (hub → board, request_id-bearing):
# rename one device, upsert/delete a room or a lighting group, drive the
# on-board scenario store (docs/contracts/cloud-scenarios.md §Channel).
EVT_DEVICES_SCENARIOS = "alice_devices_scenarios"

# `device_state.origin` (additive, both profiles): `live` = an MQTT-driven
# report through StateSender.offer, `snapshot` = offer_snapshot (reconnect /
# STATE_SNAPSHOT_S cadence / reload). What the cloud does with it is the cloud
# contract's (docs/contracts/cloud-device-control.md in the sibling repo:
# `live` frames confirm a tap, snapshots never do); an older gateway ignores
# the field.
ORIGIN_LIVE = "live"
ORIGIN_SNAPSHOT = "snapshot"

# Device-document tile icons (config/models.py allow-list; the ids are shared
# with the cloud control page's sprite).
DEVICE_ICONS = ("bulb", "fan", "socket", "relay", "pump", "valve", "siren", "generic")

# Yandex action/query error codes
ERR_DEVICE_UNREACHABLE = "DEVICE_UNREACHABLE"
ERR_INVALID_ACTION = "INVALID_ACTION"
ERR_INVALID_VALUE = "INVALID_VALUE"
ERR_INTERNAL_ERROR = "INTERNAL_ERROR"

STATUS_DONE = "DONE"
STATUS_ERROR = "ERROR"

# Gateway probe / reconnect
GATEWAY_PING_PATH = "/v1.0/ping"
GATEWAY_ENROLL_PATH = "/controller/enroll"
GATEWAY_UNLINK_PATH = "/controller/unlink"
# HTTP /v1.0/ping and enroll/unlink urllib budgets. Do NOT reuse this for
# Socket.IO wait_timeout — a live hub's websocket+namespace handshake from
# the ARM board is slower than a HEAD ping.
GATEWAY_PROBE_TIMEOUT_S = 5.0
# CGI `timeout` around python dispatch (sa02m_alice_api.cgi). Slowest honest
# path is unlink / enroll: probe + gateway POST, each GATEWAY_PROBE_TIMEOUT_S;
# a HEAD 405 retry on probe adds a third urllib wait. Import + JSON of ~15
# devices on a loaded ARM board adds ~1–3 s. Must stay below nginx
# fastcgi_read_timeout for /cgi-bin/ (20 s). Fail-closed: the CGI still
# returns alice_api_failed JSON when this budget is exceeded.
# Keep the default in sa02m_alice_api.cgi (`SA02M_ALICE_CGI_TIMEOUT`) in lockstep
# — tests/test_sio_connection.py TestCgiDispatchTimeout asserts both.
CGI_DISPATCH_TIMEOUT_S = 18
# Socket.IO namespace wait (python-socketio Client.connect wait_timeout).
# Board 1.136, 2026-09-07, n=3 against wss://cloud.cyntron.ru/control/socket.io:
# handshake 5.185 / 3.665 / 3.371 s (token mint 1.3–1.7 s is outside this
# wait). 5.185 > 5.0 is why the cloud card flashed gateway_unreachable on a
# live hub. 15 s ≈ 3× measured max.
SIO_CONNECT_TIMEOUT_S = 15.0
# Consecutive wait_timeouts that stay `connecting` (not gateway_unreachable).
# The reconnect loop keeps going after this; the card just stops lying on
# the first miss. A DNS / HTTP / refused error is still fail-closed immediately.
SIO_CONNECT_SOFT_FAILS = 3
SIO_RECONNECT_MIN_S = 2.0
SIO_RECONNECT_MAX_S = 60.0
SIO_WATCHDOG_S = 60.0
# Symmetric jitter fraction on the reconnect ladder: an OTA wave restarts many
# boards at once, and a flat ladder would reconnect them all in lockstep.
SIO_RECONNECT_JITTER = 0.25
# A session must last this long before the attempt counter resets. A gateway
# that drops us seconds after connect must be backed away from, not hammered:
# resetting on every successful connect would turn "connect → dropped at 16 s"
# into a hot retry loop.
SIO_STABLE_S = 60.0

# In-place device-document reload (docs/contracts/alice-mqtt-mapping.md).
# Grace window during which the retained burst of a NEWLY subscribed topic is
# cached but not reported. Bounded by the LOCAL broker (127.0.0.1) — the burst
# arrives in milliseconds; gateway latency is not on this path.
RETAINED_GRACE_S = 5.0
# Status-file heartbeat: `ts` must keep advancing in a quiet session, because
# the privileged web trigger uses its freshness as proof that the client is
# still alive and still watching the document.
STATUS_HEARTBEAT_S = 30.0
# How old `ts` may be before the trigger stops trusting `config_watch` and
# falls back to a restart (3× the heartbeat). Read by the shell helper too —
# usr/local/sbin/sa02m-alice-web-trigger.sh keeps the same value.
STATUS_STALE_S = 90
# Cloud-profile cache flush. The hub marks a tile stale past 60 s, so
# this must stay at 30 s on the cloud profile — never lengthened.
# Home: docs/contracts/alice-mqtt-mapping.md (History snapshot).
STATE_SNAPSHOT_S = 30.0
# Yandex-profile history cadence: one graph point per minute. Graphs
# read Callback state, not query; MQTT does not republish a steady float.
STATE_SNAPSHOT_YANDEX_S = 60.0

# Client status states written for the web UI
STATE_DISABLED = "disabled"
STATE_OFFLINE = "offline"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_ERROR = "error"
STATE_MISSING_DEPS = "missing_deps"
STATE_MISSING_CERT = "missing_cert"
# Cloud profile only: agent.conf has no device_id/serial or the device_secret
# file is absent — standby, exit 0, the cloud twin of missing_cert.
STATE_MISSING_IDENTITY = "missing_identity"
# The gateway unlinked this controller: the binding was erased locally and the
# board is claim-ready. Distinct from missing_cert (never bound) because the
# card must explain WHY the certificate is gone. Yandex profile only — the
# cloud profile holds no identity of its own and never stands down
# (docs/contracts/alice-mqtt-mapping.md §Profiles).
STATE_UNLINKED = "unlinked"
# The unlink was confirmed but the binding could NOT be erased (a read-only
# filesystem, a permission error). The board keeps retrying; the card must say
# so and never «привязан», never «отвязано».
STATE_UNLINK_FAILED = "unlink_failed"

# Durable stand-down marker in the client INI — the same three keys the cloud
# agent writes into agent.conf, so both doors restore the same explanation
# after a reboot (/run is tmpfs). Never a file under VAR_DIR: the factory-image
# build refuses any file in the identity dir but the shared CA
# (docs/contracts/image-identity-reset.md §3), so a marker there would abort
# image capture from a previously-unlinked donor. These keys are IDENTITY, not
# configuration — the image sites clear them (§2, mirroring §6).
KEY_UNLINKED_AT = "unlinked_at"
KEY_UNLINKED_REASON = "unlinked_reason"
KEY_UNLINKED_REASON_TEXT = "unlinked_reason_text"
UNLINK_MARKER_KEYS = (KEY_UNLINKED_AT, KEY_UNLINKED_REASON, KEY_UNLINKED_REASON_TEXT)

# The refusal class this door reports. One class only: the gateway's unlink
# event does not distinguish an owner revoke from a detach, and the handler
# deliberately does not read the optional payload — so `revoked` is N/A here
# and the descriptor declares it unwritable.
REFUSAL_CLASS_UNLINKED = "unlinked"
# What the durable marker records as the refusal text on this door.
UNLINK_REFUSAL = "controller_unlink"
# Machine-facing status messages. The user-facing Russian lives once, in
# ALICE_STATE_MAP (www/network_config/static/js/app/alice.js).
UNLINKED_MESSAGE = (
    "Cloud unlinked this controller; binding erased, ready to be claimed again"
)
UNLINK_FAILED_MESSAGE = (
    "Cloud unlinked this controller but the binding could not be erased; retrying"
)
