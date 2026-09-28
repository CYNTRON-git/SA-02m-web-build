"""Home Connect client constants: paths, hosts, limits, states (docs/contracts/home-connect.md)."""

from __future__ import annotations

import os

# ── Paths (docs/contracts/home-connect.md §Файлы и права) ──────────────────
# Env overrides exist for the test suite and a dev checkout; production uses
# the defaults, which the unit, the tmpfiles entry and the imaging sites name.
ETC_DIR = os.environ.get("SA02M_HOMECONNECT_ETC", "/etc/sa02m-homeconnect")
CONF_FILE = os.path.join(ETC_DIR, "sa02m-homeconnect.conf")
VAR_DIR = os.environ.get("SA02M_HOMECONNECT_VAR", "/var/lib/sa02m-homeconnect")
# OAuth tokens: daemon user only, 0600, never logged, never in a backup.
TOKENS_NAME = "tokens.json"
# Daily API-call accountant; survives an unlink (BSH counts per client+user).
BUDGET_NAME = "budget.json"
# Device ids this client published, so a restart can clear their retained
# topics after an unlink or a depair (no secret; daemon-only like the rest).
APPLIANCES_NAME = "appliances.json"
TOKENS_FILE = os.path.join(VAR_DIR, TOKENS_NAME)
BUDGET_FILE = os.path.join(VAR_DIR, BUDGET_NAME)
APPLIANCES_FILE = os.path.join(VAR_DIR, APPLIANCES_NAME)
# Every atomic write in the package uses this tmp prefix, so the unlink helper
# and the imaging sites can name a torn write's sidecar by one glob.
TMP_PREFIX = ".hc-"
TMP_SUFFIX = ".tmp"
RUN_DIR = os.environ.get("SA02M_HOMECONNECT_RUN", "/run/sa02m-homeconnect")
STATUS_FILE = os.path.join(RUN_DIR, "status.json")
# Device-flow user code for the web card — exists ONLY while awaiting_user.
LINK_FILE = os.path.join(RUN_DIR, "link.json")
# Appliance list with names and published controls (card + Phase 3 picker).
INVENTORY_FILE = os.path.join(RUN_DIR, "inventory.json")
# Install footprint the dispatch probes for `not_installed` (D9): the unit,
# the package and the daemon's system user. OTA delivers the first two only.
UNIT_FILE = "/etc/systemd/system/sa02m-homeconnect.service"
PACKAGE_DIR = "/opt/sa02m-homeconnect/sa02m_homeconnect"
DAEMON_USER = "sa02m-homeconnect"
# Group that may read link.json / inventory.json (the CGI runs as it).
WEB_GROUP = "www-data"

# ── BSH cloud (docs/decisions/homekit-home-connect.md G6/G7) ───────────────
# The conf names a host KEY; the URL comes only from this table, so a
# hand-edited conf can never point the token at another server.
HOSTS = {
    "api": "https://api.home-connect.com",
    "simulator": "https://simulator.home-connect.com",
}
DEFAULT_HOST = "api"
DEVICE_AUTH_PATH = "/security/oauth/device_authorization"
TOKEN_PATH = "/security/oauth/token"
APPLIANCES_PATH = "/api/homeappliances"
EVENTS_PATH = "/api/homeappliances/events"
# READ-ONLY release (Operator decision Q-E): no Control, no Settings scope.
SCOPES = "IdentifyAppliance Monitor"
ACCEPT_JSON = "application/vnd.bsh.sdk.v1+json"
ACCEPT_SSE = "text/event-stream"
USER_AGENT = "sa02m-homeconnect"
# The only [control] mode this release accepts.
CONTROL_MODES = ("off",)
CLIENT_ID_RE = r"^[A-Za-z0-9_-]{8,128}$"

# ── Budget (1000 calls per day per client+user, failures included) ─────────
DAILY_LIMIT = 1000
# From here on only the event stream and token refresh may call the cloud.
LOCAL_BUDGET = 800
TOKEN_REFRESH_PER_MIN = 10
# Refresh the access token (24 h per the public docs) this long before expiry.
REFRESH_BEFORE_S = 3600
# A failed early refresh is retried after a doubling pause in this range; the
# still-valid token keeps working meanwhile.
REFRESH_RETRY_MIN_S = 60.0
REFRESH_RETRY_MAX_S = 1800.0
# Kinds a call is charged as; the budget policy is per kind.
KIND_API = "api"
KIND_SSE = "sse"
KIND_TOKEN = "token"
KINDS = (KIND_API, KIND_SSE, KIND_TOKEN)

# ── Device flow (RFC 8628; values from the response, these are fallbacks) ──
DEFAULT_POLL_INTERVAL_S = 5
SLOW_DOWN_STEP_S = 5
POLL_INTERVAL_MIN_S = 1
POLL_INTERVAL_MAX_S = 60
# The shorter of the two published device-code lifetimes (5 vs 10 min).
DEFAULT_DEVICE_CODE_TTL_S = 300
DEVICE_CODE_TTL_MIN_S = 30
DEVICE_CODE_TTL_MAX_S = 1800
# A card «Подключить» older than this is not honoured on (re)start.
LINK_REQUEST_TTL_S = 300

# ── HTTP ───────────────────────────────────────────────────────────────────
HTTP_TIMEOUT_S = 20
MAX_RESPONSE_BYTES = 1 << 20
API_RETRY_MAX = 2
API_BACKOFF_BASE_S = 2.0
API_BACKOFF_MAX_S = 60.0
RETRY_AFTER_DEFAULT_S = 60
RETRY_AFTER_MAX_S = 86400

# ── Event stream ───────────────────────────────────────────────────────────
# Server keep-alive comes about every 55 s; a silent socket past this is dead.
SSE_READ_TIMEOUT_S = 120
SSE_BACKOFF_MIN_S = 60.0
SSE_BACKOFF_MAX_S = 1800.0
BACKOFF_JITTER = 0.2
SSE_MAX_LINE = 65536
SSE_MAX_EVENT = 262144
SSE_EVENT_TYPES = (
    "KEEP-ALIVE", "STATUS", "EVENT", "NOTIFY", "CONNECTED", "DISCONNECTED",
    "PAIRED", "DEPAIRED",
)
# Stream down longer than this ⇒ every appliance carries meta/error = "r".
STREAM_STALE_S = 120
# At most one appliance detail read per this many seconds (initial load and
# re-reads), i.e. ≤ 4 appliances per minute.
INVENTORY_PACE_S = 15.0
MAX_APPLIANCES = 32

# ── Daemon timing ──────────────────────────────────────────────────────────
CONF_POLL_S = 2.0
TICK_S = 0.5
STATUS_HEARTBEAT_S = 30.0
# Status older than this while enabled is not trusted by the dispatch.
STATUS_STALE_S = 90

# ── States written to status.json ──────────────────────────────────────────
# `not_installed` is never written by the daemon: the dispatch answers it when
# the install footprint is absent (a board updated by OTA only).
STATE_NOT_INSTALLED = "not_installed"
STATE_DISABLED = "disabled"
STATE_MISSING_DEPS = "missing_deps"
STATE_MISSING_CLIENT_ID = "missing_client_id"
STATE_UNLINKED = "unlinked"
STATE_AWAITING_USER = "awaiting_user"
STATE_LINK_EXPIRED = "link_expired"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_RATE_LIMITED = "rate_limited"
STATE_OFFLINE = "offline"
STATE_TOKEN_REVOKED = "token_revoked"
STATE_ERROR = "error"
STATES = (
    STATE_NOT_INSTALLED,
    STATE_DISABLED,
    STATE_MISSING_DEPS,
    STATE_MISSING_CLIENT_ID,
    STATE_UNLINKED,
    STATE_AWAITING_USER,
    STATE_LINK_EXPIRED,
    STATE_CONNECTING,
    STATE_CONNECTED,
    STATE_RATE_LIMITED,
    STATE_OFFLINE,
    STATE_TOKEN_REVOKED,
    STATE_ERROR,
)
# Written once by a process that then exits — their age says nothing. Every
# other daemon state is re-written each STATUS_HEARTBEAT_S by a live process.
EXIT_ONCE_STATES = frozenset((STATE_DISABLED, STATE_MISSING_DEPS))

# `reason` — additive detail to `state`.
REASON_ACCESS_DENIED = "access_denied"
REASON_CLIENT_ID_REJECTED = "client_id_rejected"
REASON_TOKEN_STORE_INSECURE = "token_store_insecure"
REASON_TOKEN_STORE_CORRUPT = "token_store_corrupt"
REASON_BUDGET_LOCAL = "budget_local_reached"
REASON_RETRY_AFTER = "retry_after"
REASON_DAILY_LIMIT = "daily_limit"
REASON_STREAM_DOWN = "stream_down"
REASON_STATUS_STALE = "status_stale"
# With `missing_deps`: the conf exists but the daemon cannot read it (its read
# ACL is gone, docs/contracts/home-connect.md §11) — not «disabled».
REASON_CONF_UNREADABLE = "conf_unreadable"
REASONS = (
    REASON_ACCESS_DENIED,
    REASON_CLIENT_ID_REJECTED,
    REASON_TOKEN_STORE_INSECURE,
    REASON_TOKEN_STORE_CORRUPT,
    REASON_BUDGET_LOCAL,
    REASON_RETRY_AFTER,
    REASON_DAILY_LIMIT,
    REASON_STREAM_DOWN,
    REASON_STATUS_STALE,
    REASON_CONF_UNREADABLE,
)

# ── MQTT (docs/MQTT_TOPICS.md convention) ──────────────────────────────────
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
MQTT_QOS = 1
MQTT_KEEPALIVE_S = 60
DEVICE_PREFIX = "hc-"
DEVICE_ID_MAX = 64
DRIVER = "sa02m-homeconnect"
NAME_MAX = 64
TEXT_MAX = 64

# ── CGI dispatch ───────────────────────────────────────────────────────────
# The privileged verbs the CGI may nudge — exactly the sudoers pin.
TRIGGER_VERBS = ("enable", "disable", "restart", "unlink")
MAX_BODY_BYTES = 16384
