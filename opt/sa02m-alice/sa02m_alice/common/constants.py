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

DEFAULT_GATEWAY_WSS = "wss://alice.cyntron.ru/controller/socket.io"
DEFAULT_GATEWAY_HTTP = "https://alice.cyntron.ru"
DEFAULT_MQTT_HOST = "127.0.0.1"
DEFAULT_MQTT_PORT = 1883
SIO_PATH = "/socket.io"

# Socket.IO events (controller ↔ gateway)
EVT_DEVICES_LIST = "alice_devices_list"
EVT_DEVICES_QUERY = "alice_devices_query"
EVT_DEVICES_ACTION = "alice_devices_action"
EVT_DEVICE_STATE = "device_state"
EVT_CONTROLLER_UNLINK = "controller_unlink"

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
GATEWAY_PROBE_TIMEOUT_S = 5.0
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
# While Socket.IO is up, push the MQTT cache through offer_snapshot this
# often so Yandex Station graphs/history get a point even when the broker
# is quiet (a steady reading is cached, not re-published). Same 30 s as
# Yandex's own devices (~1 min) tightened to the Operator's 30 s choice.
# Live on_off/event still uses the shorter rates in event_rates.json.
# Home: docs/contracts/alice-mqtt-mapping.md (History snapshot).
STATE_SNAPSHOT_S = 30.0

# Client status states written for the web UI
STATE_DISABLED = "disabled"
STATE_OFFLINE = "offline"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_ERROR = "error"
STATE_MISSING_DEPS = "missing_deps"
STATE_MISSING_CERT = "missing_cert"
