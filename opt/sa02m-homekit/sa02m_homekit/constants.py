"""HomeKit bridge constants: paths, states, limits (docs/contracts/homekit-bridge.md)."""

from __future__ import annotations

import os

# ── Paths (docs/contracts/homekit-bridge.md §Файлы и права) ────────────────
# Env overrides exist for the test suite and a dev checkout; production uses
# the defaults, which the unit, the tmpfiles entry and the imaging sites name.
ETC_DIR = os.environ.get("SA02M_HOMEKIT_ETC", "/etc/sa02m-homekit")
CONF_FILE = os.path.join(ETC_DIR, "sa02m-homekit.conf")
VAR_DIR = os.environ.get("SA02M_HOMEKIT_VAR", "/var/lib/sa02m-homekit")
# HAP accessory long-term keys, pairings, setup code: daemon user only, 0600.
STATE_FILE = os.path.join(VAR_DIR, "state.json")
AIDS_FILE = os.path.join(VAR_DIR, "aids.json")
IDENTITY_FILE = os.path.join(VAR_DIR, "identity.json")
# Every atomic write in VAR_DIR uses this tmp prefix, so the reset helper and
# the imaging sites can name the sidecars of a torn write by one glob.
TMP_PREFIX = ".hk-"
RUN_DIR = os.environ.get("SA02M_HOMEKIT_RUN", "/run/sa02m-homekit")
STATUS_FILE = os.path.join(RUN_DIR, "status.json")
# The setup code for the web card — exists ONLY while running and unpaired.
SETUP_FILE = os.path.join(RUN_DIR, "setup.json")
# Accessory list + skipped list with device names (0640, group www-data).
PROJECTION_FILE = os.path.join(RUN_DIR, "projection.json")
MACHINE_ID_FILE = os.environ.get("SA02M_HOMEKIT_MACHINE_ID", "/etc/machine-id")
# The web VERSION file — FirmwareRevision of every accessory.
VERSION_FILE = os.environ.get(
    "SA02M_HOMEKIT_VERSION_FILE", "/var/www/network_config/VERSION"
)
SYS_CLASS_NET = os.environ.get("SA02M_HOMEKIT_SYS_NET", "/sys/class/net")
# Install footprint the CGI probes for `not_installed` (D9): the unit, the
# venv interpreter and the package.
UNIT_FILE = "/etc/systemd/system/sa02m-homekit.service"
VENV_PYTHON = "/opt/sa02m-homekit-venv/bin/python"
PACKAGE_DIR = "/opt/sa02m-homekit/sa02m_homekit"
# Group that may read setup.json / projection.json (the CGI runs as it).
WEB_GROUP = "www-data"

# ── Bridge states written to status.json ───────────────────────────────────
# `not_installed` is never written by the daemon: the CGI answers it when the
# install footprint is absent (a board updated by OTA only).
STATE_NOT_INSTALLED = "not_installed"
STATE_DISABLED = "disabled"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_MISSING_DEPS = "missing_deps"
STATE_NO_INTERFACE = "no_interface"
STATE_PORT_IN_USE = "port_in_use"
STATE_ERROR = "error"
STATES = (
    STATE_NOT_INSTALLED,
    STATE_DISABLED,
    STATE_STARTING,
    STATE_RUNNING,
    STATE_MISSING_DEPS,
    STATE_NO_INTERFACE,
    STATE_PORT_IN_USE,
    STATE_ERROR,
)

# `reason` — why the bridge identity or status is what it is. Additive to
# `state`; the card turns the first two into «Мост создан заново».
REASON_IDENTITY_REGENERATED = "identity_regenerated"
REASON_STATE_CORRUPT = "state_corrupt_regenerated"
REASON_PAIR_SETUP_LOCKED = "pair_setup_locked"
REASON_STATUS_STALE = "status_stale"
REASONS = (
    REASON_IDENTITY_REGENERATED,
    REASON_STATE_CORRUPT,
    REASON_PAIR_SETUP_LOCKED,
    REASON_STATUS_STALE,
)
# States a LIVE process re-writes every STATUS_HEARTBEAT_S. The others are
# written once by a process that then exits (or by the trigger), so their age
# says nothing and the CGI never calls them stale.
HEARTBEAT_STATES = frozenset((STATE_STARTING, STATE_RUNNING, STATE_NO_INTERFACE, STATE_PORT_IN_USE))

# ── Listener (D3) ──────────────────────────────────────────────────────────
DEFAULT_INTERFACE = "eth0"
# Wired ports only. A modem (ppp*, wwan*, usb*) or a wildcard is never bound.
INTERFACE_RE = r"^eth[01]$"
INTERFACES = ("eth0", "eth1")
DEFAULT_PORT = 21064
PORT_MIN = 1024
PORT_MAX = 65535
# Ports other board services own (MPLC/CODESYS/Node-RED/MQTT/web/gateway).
FORBIDDEN_PORTS = frozenset(
    [1880, 1883, 1884, 4840, 4841, 8082, 8765, 9999, 11740, 30750, 31550]
    + list(range(502, 507))
    + list(range(8502, 8507))
    + list(range(9502, 9507))
)

# ── Projection / accessories ───────────────────────────────────────────────
# DeviceRegistry profile: anything but yandex/cloud drops `cloud_only` items
# and attaches no scene rows (docs/contracts/alice-mqtt-mapping.md).
CATALOGUE_PROFILE = "homekit"
# HAP-NodeJS MAX_ACCESSORIES; HAP-python enforces nothing.
MAX_BRIDGED = 149
# aid 1 is the bridge; HAP-python never assigns 7 (pyhap/accessory.py).
AID_SKIP = frozenset((1, 7))
AID_FIRST = 2
NAME_MAX = 64
SKIPPED_CAP = 200
MANUFACTURER = "CYNTRON"

# ── Timing ─────────────────────────────────────────────────────────────────
FLUSH_S = 0.5
REBUILD_DEBOUNCE_S = 3.0
ADDR_POLL_S = 10.0
DOC_POLL_S = 2.0
STATUS_HEARTBEAT_S = 30.0
# Status older than this while enabled is not trusted by the CGI (3x heartbeat).
STATUS_STALE_S = 90
DRIVER_STOP_TIMEOUT_S = 10.0

# ── Pairing ────────────────────────────────────────────────────────────────
# HAP spec: refuse pair-setup after 100 failed attempts (until restart).
PAIR_SETUP_MAX_FAILS = 100
# HAP bridge category (pyhap.const.CATEGORY_BRIDGE).
CATEGORY_BRIDGE = 2
SETUP_CODE_RE = r"^[0-9]{3}-[0-9]{2}-[0-9]{3}$"
# HAP spec: codes a controller refuses — all-same-digit plus two sequences.
FORBIDDEN_SETUP_CODES = frozenset(
    ["%s%s%s-%s%s-%s%s%s" % ((str(d),) * 8) for d in range(10)]
    + ["123-45-678", "876-54-321"]
)
SETUP_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# ── MQTT (docs/contracts/alice-mqtt-mapping.md §Topics) ────────────────────
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
MQTT_QOS = 1
MQTT_KEEPALIVE_S = 60

# ── CGI dispatch ───────────────────────────────────────────────────────────
# The privileged verbs the CGI may nudge — exactly the sudoers pin.
TRIGGER_VERBS = ("enable", "disable", "restart", "reset-pairing")
# Request body cap (the CGI caps first; the dispatch re-checks).
MAX_BODY_BYTES = 16384
