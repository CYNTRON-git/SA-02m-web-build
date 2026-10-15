"""The recorder conf: `/etc/sa02m-audio/sa02m-audio.conf` (INI, non-secret).

The one home of the conf grammar: keys, defaults and bounds come from
constants and are applied here on READ (a hand-edited value is clamped or
replaced, with a warning) and on WRITE (`save` refuses a value out of bounds).
The daemon only reads; the panel CGI writes through `save`.

Fail-closed rules (contract §Конфиг):
* a file that exists but cannot be opened ⇒ `unreadable` — the daemon records
  nothing and says `conf_unreadable`, never «disabled»;
* an unrecognised `storage` ⇒ `emmc`, the only default, and the warning names
  the refused value (the daemon turns it into a `config` event).
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field
from typing import List, Optional

from . import constants as C
from .fsutil import atomic_write

SECTION = "recorder"
_DEVICE_RE = re.compile(C.DEVICE_RE)
_INT_RE = re.compile(r"^[0-9]{1,6}$")
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")

# Every key `render` writes, in file order — compared with the contract.
KEYS = (
    "enabled", "storage", "device", "default_duration_s", "max_duration_s",
    "keep_files", "keep_mb", "keep_hours", "delete_after_upload",
)


@dataclass
class RecorderConfig:
    enabled: bool = False
    storage: str = C.DEFAULT_MEDIUM
    device: str = C.DEVICE_AUTO
    default_duration_s: int = C.DEFAULT_DURATION_S
    max_duration_s: int = C.MAX_DURATION_HARD_S
    keep_files: int = C.KEEP_FILES_DEFAULT
    keep_mb: int = C.KEEP_MB_DEFAULT
    keep_hours: int = C.KEEP_HOURS_DEFAULT
    delete_after_upload: bool = True
    warnings: List[str] = field(default_factory=list)
    # The file EXISTS but could not be read — never the same as disabled.
    unreadable: bool = False

    def comparable(self) -> tuple:
        """The values a running daemon reacts to (warnings excluded)."""
        return tuple(getattr(self, k) for k in KEYS) + (self.unreadable,)


def keep_mb_max(storage: str) -> int:
    return C.medium(storage).keep_mb_max


def bounds(conf: RecorderConfig) -> dict:
    """key -> (min, max) for the integer keys, given this conf's medium."""
    return {
        "max_duration_s": (C.MIN_DURATION_S, C.MAX_DURATION_HARD_S),
        "default_duration_s": (C.MIN_DURATION_S, conf.max_duration_s),
        "keep_files": (C.KEEP_FILES_MIN, C.KEEP_FILES_MAX),
        "keep_mb": (C.KEEP_MB_MIN, keep_mb_max(conf.storage)),
        "keep_hours": (C.KEEP_HOURS_MIN, C.KEEP_HOURS_MAX),
    }


def _read_bool(raw: str, default: bool, key: str, warnings: List[str]) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    warnings.append("%s %r refused, using %s" % (key, raw[:20], "true" if default else "false"))
    return default


def load(path: Optional[str] = None) -> RecorderConfig:
    """Read the conf. Absent file / section ⇒ defaults (disabled); a file that
    exists but cannot be opened ⇒ `unreadable`."""
    target = path or C.CONF_FILE
    try:
        with open(target, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return RecorderConfig()
    except UnicodeDecodeError as exc:
        return RecorderConfig(warnings=["unparsable conf: %s" % exc])
    except OSError as exc:
        return RecorderConfig(unreadable=True,
                              warnings=["unreadable conf: %s" % (exc.strerror or exc)])
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read_string(text, source=target)
    except configparser.Error as exc:
        return RecorderConfig(warnings=["unparsable conf: %s" % str(exc)[:200]])
    out = RecorderConfig()
    if not cfg.has_section(SECTION):
        return out
    sec = cfg[SECTION]
    out.enabled = _read_bool(sec.get("enabled", "false"), False, "enabled", out.warnings)
    storage = sec.get("storage", C.DEFAULT_MEDIUM).strip()
    if storage in C.MEDIUM_NAMES:
        out.storage = storage
    else:
        out.warnings.append("storage %r refused, using %s" % (storage[:20], C.DEFAULT_MEDIUM))
    device = sec.get("device", C.DEVICE_AUTO).strip()
    if device == C.DEVICE_AUTO or _DEVICE_RE.match(device):
        out.device = device
    else:
        out.warnings.append("device %r refused, using %s" % (device[:20], C.DEVICE_AUTO))
    out.delete_after_upload = _read_bool(sec.get("delete_after_upload", "true"), True,
                                         "delete_after_upload", out.warnings)
    # max before default: the default's upper bound is the max.
    for key in ("max_duration_s", "default_duration_s", "keep_files", "keep_mb", "keep_hours"):
        raw = sec.get(key)
        if raw is None:
            continue
        lo, hi = bounds(out)[key]
        raw = raw.strip()
        if not _INT_RE.match(raw):
            out.warnings.append("%s %r refused, using %d" % (key, raw[:20], getattr(out, key)))
            continue
        value = int(raw)
        if value < lo or value > hi:
            clamped = min(max(value, lo), hi)
            out.warnings.append("%s %d out of %d..%d, using %d" % (key, value, lo, hi, clamped))
            value = clamped
        setattr(out, key, value)
    # A lowered max drags a default that was read before it was known.
    if out.default_duration_s > out.max_duration_s:
        out.default_duration_s = out.max_duration_s
    return out


def validate(conf: RecorderConfig) -> None:
    """Raise ValueError when a value is outside the grammar `load` accepts."""
    if conf.storage not in C.MEDIUM_NAMES:
        raise ValueError("invalid storage")
    if not (conf.device == C.DEVICE_AUTO or _DEVICE_RE.match(conf.device)):
        raise ValueError("invalid device")
    for key in ("enabled", "delete_after_upload"):
        if not isinstance(getattr(conf, key), bool):
            raise ValueError("invalid %s" % key)
    for key, (lo, hi) in bounds(conf).items():
        value = getattr(conf, key)
        if isinstance(value, bool) or not isinstance(value, int) or value < lo or value > hi:
            raise ValueError("invalid %s" % key)


def render(conf: RecorderConfig) -> str:
    validate(conf)
    lines = ["# SA-02m audio recorder — written by the panel (docs/contracts/audio-recorder.md)",
             "[%s]" % SECTION]
    for key in KEYS:
        value = getattr(conf, key)
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append("%s = %s" % (key, value))
    return "\n".join(lines) + "\n"


def save(conf: RecorderConfig, path: Optional[str] = None) -> None:
    """Validate, then replace the conf atomically (0640; the setgid conf dir
    gives the file the daemon's group)."""
    atomic_write(path or C.CONF_FILE, render(conf), mode=0o640)
