"""Recorder constants — the one home of paths, media, caps, grammar and enums
(docs/contracts/audio-recorder.md). Every other module and the contract tables
test read them from here; nothing re-derives a value."""

from __future__ import annotations

import os
from collections import namedtuple

# ── Paths (contract §Файлы и права) ────────────────────────────────────────
# Env overrides exist for the test suite and a dev checkout; production uses
# the defaults, which the unit and the tmpfiles entry name.
ETC_DIR = os.environ.get("SA02M_AUDIO_ETC", "/etc/sa02m-audio")
CONF_FILE = os.path.join(ETC_DIR, "sa02m-audio.conf")
# The upload bearer token: a secret. www-data:sa02m-audio 0640 — the panel
# writes it (write-only field), the daemon reads it; never logged, never in a
# backup, never in a response.
TOKEN_FILE = os.path.join(ETC_DIR, "upload.token")
# Operator allow list for commands returned in an upload response. Absent or
# invalid ⇒ the daemon applies nothing (voice_commands stays fail-closed).
VOICE_ALLOW_FILE = os.path.join(ETC_DIR, "voice-allow.json")
VAR_DIR = os.environ.get("SA02M_AUDIO_VAR", "/var/lib/sa02m-audio")
# Audit ring; always on eMMC, whatever medium the clips go to.
EVENTS_FILE = os.path.join(VAR_DIR, "events.jsonl")
RUN_DIR = os.environ.get("SA02M_AUDIO_RUN", "/run/sa02m-audio")
STATUS_FILE = os.path.join(RUN_DIR, "status.json")
DAEMON_LOCK = os.path.join(RUN_DIR, "daemon.lock")
# The mic lease every opener of the USB microphone takes (contract §Аренда
# микрофона). /run/lock is 1777 on the board, so any consumer can create it.
MIC_LOCK = os.environ.get("SA02M_AUDIO_MIC_LOCK", "/run/lock/sa02m-mic.lock")
MOUNTS_FILE = "/proc/mounts"
ASOUND_DIR = "/proc/asound"
REC_SUBDIR = "rec"
TMP_SUBDIR = "tmp"
# Name of the store dir on a removable medium (inside its mount point).
REMOVABLE_STORE_NAME = "sa02m-audio"
PART_SUFFIX = ".wav.part"
CLIP_SUFFIX = ".wav"
# Per-clip sidecar `<id>.json` next to the clip (duration, peak, upload state),
# written atomically by the daemon; its temp name lives in tmp/.
META_SUFFIX = ".json"
META_TMP_SUFFIX = ".meta.tmp"
META_MAX_BYTES = 1024
PROBE_PREFIX = ".probe-"
TMP_PREFIX = ".au-"
TMP_SUFFIX = ".tmp"
DAEMON_USER = "sa02m-audio"
WEB_GROUP = "www-data"
# Install footprint the panel dispatch probes for `not_installed`: the unit,
# the package and the daemon's system user. OTA delivers the first two only.
UNIT_FILE = "/etc/systemd/system/sa02m-audio.service"
PACKAGE_DIR = "/opt/sa02m-audio/sa02m_audio"

# ── Media (contract §Носители) ─────────────────────────────────────────────
# One home for where a clip may be written. `mount_point` None = the rootfs
# (eMMC). The two removable mount points are storage-mount.sh's
# set_device_context literals — a test pins them to that file.
Medium = namedtuple("Medium", "name mount_point store_dir floor_bytes keep_mb_max")
MB = 1024 * 1024
MEDIUM_EMMC = "emmc"
MEDIUM_USB = "usb"
MEDIUM_SDCARD = "sdcard"
MEDIA = (
    Medium(MEDIUM_EMMC, None, VAR_DIR, 200 * MB, 256),
    Medium(MEDIUM_USB, "/media/usb", "/media/usb/" + REMOVABLE_STORE_NAME, 16 * MB, 4096),
    Medium(MEDIUM_SDCARD, "/media/sdcard", "/media/sdcard/" + REMOVABLE_STORE_NAME, 16 * MB, 4096),
)
MEDIUM_NAMES = tuple(m.name for m in MEDIA)
DEFAULT_MEDIUM = MEDIUM_EMMC

# Verdict on a medium (controls/medium_state, status.json media.<m>.state).
MEDIUM_OK = "ok"
MEDIUM_ABSENT = "absent"
MEDIUM_READONLY = "readonly"
MEDIUM_FULL = "full"
MEDIUM_STATES = (MEDIUM_OK, MEDIUM_ABSENT, MEDIUM_READONLY, MEDIUM_FULL)

# ── Audio format (contract §Формат) ────────────────────────────────────────
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
BYTES_PER_S = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
ARECORD_FORMAT = "S16_LE"
# A stopped clip shorter than this is discarded, never stored.
MIN_KEEP_MS = 1000
# dBFS reported for digital silence (log of zero).
PEAK_FLOOR_DBFS = -90
ARECORD = "arecord"
TIMEOUT = "timeout"
# `timeout` bound past the requested duration, and its kill-after grace.
ARECORD_GRACE_S = 5
ARECORD_KILL_AFTER_S = 2
READ_CHUNK = BYTES_PER_S // 10
STDERR_KEEP = 4096
CHILD_STOP_WAIT_S = 3.0

# ── Device (contract §Устройство захвата) ──────────────────────────────────
DEVICE_AUTO = "auto"
DEVICE_RE = r"^[A-Za-z0-9:=,_.-]{1,64}$"
CARD_ID_RE = r"^[A-Za-z0-9_]{1,32}$"
USB_AUDIO_DRIVER = "USB-Audio"

# ── Conf bounds (contract §Конфиг) ─────────────────────────────────────────
DEFAULT_DURATION_S = 10
MIN_DURATION_S = 2
MAX_DURATION_HARD_S = 30
KEEP_FILES_DEFAULT = 50
KEEP_FILES_MIN = 1
KEEP_FILES_MAX = 50
KEEP_MB_DEFAULT = 32
KEEP_MB_MIN = 1
KEEP_HOURS_DEFAULT = 24
KEEP_HOURS_MIN = 1
KEEP_HOURS_MAX = 168

# ── Command intake (contract §MQTT) ────────────────────────────────────────
RATE_MAX_STARTS = 60
RATE_WINDOW_S = 3600.0
INBOX_MAX = 64
ID_RE = r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$"
DELETE_ALL = "all"
START_WORDS = ("1", "on", "true")
STOP_WORDS = ("0", "off", "false", "stop")

# last_status values (controls/last_status).
ST_OK = "ok"
ST_BUSY = "busy"
ST_DISABLED = "disabled"
ST_NO_MIC = "no_mic"
ST_MIC_BUSY = "mic_busy"
ST_STORAGE_FULL = "storage_full"
ST_MEDIUM_ABSENT = "medium_absent"
ST_MEDIUM_READONLY = "medium_readonly"
ST_MEDIUM_LOST = "medium_lost"
ST_RATE_LIMITED = "rate_limited"
ST_BAD_COMMAND = "bad_command"
ST_INTERRUPTED = "interrupted"
ST_ERROR = "error"
LAST_STATUSES = (
    ST_OK, ST_BUSY, ST_DISABLED, ST_NO_MIC, ST_MIC_BUSY, ST_STORAGE_FULL,
    ST_MEDIUM_ABSENT, ST_MEDIUM_READONLY, ST_MEDIUM_LOST, ST_RATE_LIMITED,
    ST_BAD_COMMAND, ST_INTERRUPTED, ST_ERROR,
)
# The medium verdict that refuses a start, mapped to the start's last_status.
MEDIUM_REFUSAL = {
    MEDIUM_ABSENT: ST_MEDIUM_ABSENT,
    MEDIUM_READONLY: ST_MEDIUM_READONLY,
    MEDIUM_FULL: ST_STORAGE_FULL,
}

MIC_PRESENT = "present"
MIC_ABSENT = "absent"
MIC_BUSY = "busy"
MIC_STATES = (MIC_PRESENT, MIC_ABSENT, MIC_BUSY)

# last_upload values (contract §Передача): `off` no URL configured; `pending`
# queued / attempts running; `ok` a 2xx; `failed` attempts exhausted, queue
# full, or the token unreadable; `refused` the URL policy refused the target;
# `unavailable` the URL policy (sa02m_rules.http_guard) cannot be imported.
UP_OFF = "off"
UP_PENDING = "pending"
UP_OK = "ok"
UP_FAILED = "failed"
UP_REFUSED = "refused"
UP_UNAVAILABLE = "unavailable"
UPLOAD_STATES = (UP_OFF, UP_PENDING, UP_OK, UP_FAILED, UP_REFUSED, UP_UNAVAILABLE)

# ── Upload (contract §Передача) ────────────────────────────────────────────
# The URL policy's home is the rules engine (decision M1); it lives here.
RULES_DIR = os.environ.get("SA02M_AUDIO_RULES_DIR", "/opt/sa02m-rules")
UPLOAD_URL_MAX = 500
UPLOAD_TIMEOUT_S = 10
# One attempt at each offset after the clip is stored: 3 attempts.
UPLOAD_DELAYS_S = (0, 10, 60)
UPLOAD_QUEUE_MAX = 5
UPLOAD_MAX_REDIRECTS = 3
UPLOAD_RESPONSE_MAX = 64 * 1024
TOKEN_RE = r"^[A-Za-z0-9._~+/=-]{1,512}$"
# How many command objects one JSON reply may carry, and how many clip ids
# are remembered so a retry of the same upload does not publish twice.
VOICE_COMMANDS_MAX = 8
VOICE_SEEN_MAX = 64
VOICE_ALLOW_MAX_BYTES = 8192

# ── MQTT (docs/MQTT_TOPICS.md convention) ──────────────────────────────────
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
MQTT_QOS = 1
MQTT_KEEPALIVE_S = 60
DEVICE_ID = "sa02m-audio"
DRIVER = "sa02m-audio"
DEVICE_NAME = "SA-02m audio recorder"
DEVICE_TOPIC = "/devices/" + DEVICE_ID
# Command controls: the only topics this daemon subscribes to.
CMD_RECORD = "record"
CMD_DELETE = "delete"
COMMAND_CONTROLS = (CMD_RECORD, CMD_DELETE)
# State controls, in WB order: (name, WB type, units). `record` is the only
# writable one (its `/on` is the command); every other is readonly.
Control = namedtuple("Control", "name wb_type units")
CONTROLS = (
    Control("record", "switch", ""),
    Control("mic", "text", ""),
    Control("medium", "text", ""),
    Control("medium_state", "text", ""),
    Control("last_file", "text", ""),
    Control("last_status", "text", ""),
    Control("last_duration_ms", "value", "ms"),
    Control("last_size", "value", ""),
    Control("last_peak_dbfs", "value", ""),
    Control("last_upload", "text", ""),
    Control("count", "value", ""),
)
WRITABLE_CONTROLS = ("record",)

# ── Daemon timing ──────────────────────────────────────────────────────────
TICK_S = 0.5
CONF_POLL_S = 2.0
MEDIA_CHECK_S = 10.0
RETENTION_S = 3600.0
EVENTS_MAX = 200
# Exit code of a second daemon copy (the lock is held).
EXIT_LOCKED = 75

# status.json `state`.
STATE_DISABLED = "disabled"
STATE_CONF_UNREADABLE = "conf_unreadable"
STATE_MISSING_DEPS = "missing_deps"
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"
STATES = (
    STATE_DISABLED, STATE_CONF_UNREADABLE, STATE_MISSING_DEPS, STATE_IDLE,
    STATE_RECORDING, STATE_STOPPED, STATE_ERROR,
)

# ── Panel dispatch (contract §CGI) ─────────────────────────────────────────
# A live daemon rewrites status.json at least every MEDIA_CHECK_S; older than
# this, the panel says it is not running.
STATUS_STALE_S = 30
EVENTS_TAIL = 20
# A valid clip is < 1 MB (30 s); anything bigger in a store is never streamed.
DOWNLOAD_MAX_BYTES = 2 * MB
MAX_BODY_BYTES = 16384
MAX_QUERY_BYTES = 512
# The privileged verbs the CGI may nudge — exactly the sudoers pin.
TRIGGER_VERBS = ("enable", "disable", "restart")
# Panel-only states: never written by the daemon.
STATE_NOT_INSTALLED = "not_installed"
STATE_NOT_RUNNING = "not_running"
PANEL_STATES = (STATE_NOT_INSTALLED, STATE_NOT_RUNNING)
# The removable media whose selection needs the explicit acknowledgement.
REMOVABLE_MEDIA = (MEDIUM_USB, MEDIUM_SDCARD)

# Event kinds in events.jsonl (contract §Журнал).
EVENT_KINDS = (
    "start", "end", "refused", "failed", "interrupted", "discarded", "xrun",
    "delete", "retention", "config", "medium", "mqtt", "upload", "voice",
)


def medium(name: str) -> Medium:
    for m in MEDIA:
        if m.name == name:
            return m
    raise KeyError(name)
