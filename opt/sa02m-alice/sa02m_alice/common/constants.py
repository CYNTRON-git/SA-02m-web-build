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

# Client status states written for the web UI
STATE_DISABLED = "disabled"
STATE_OFFLINE = "offline"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_ERROR = "error"
STATE_MISSING_DEPS = "missing_deps"
STATE_MISSING_CERT = "missing_cert"
