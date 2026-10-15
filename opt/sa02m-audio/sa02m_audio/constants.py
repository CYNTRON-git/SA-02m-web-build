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
PROBE_PREFIX = ".probe-"
TMP_PREFIX = ".au-"
TMP_SUFFIX = ".tmp"
DAEMON_USER = "sa02m-audio"
WEB_GROUP = "www-data"

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

# last_upload values; `off` is the only one this daemon writes until an
# upload URL exists (contract §Передача).
UP_OFF = "off"
UPLOAD_STATES = ("off", "pending", "ok", "failed", "refused", "unavailable")

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

# Event kinds in events.jsonl (contract §Журнал).
EVENT_KINDS = (
    "start", "end", "refused", "failed", "interrupted", "discarded", "xrun",
    "delete", "retention", "config", "medium", "mqtt",
)


def medium(name: str) -> Medium:
    for m in MEDIA:
        if m.name == name:
            return m
    raise KeyError(name)
