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
import os
import re
import stat as stat_module
import urllib.parse
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
    "keep_files", "keep_mb", "keep_hours", "delete_after_upload", "upload_url",
)
_TOKEN_RE = re.compile(C.TOKEN_RE)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def valid_upload_url(value: object) -> bool:
    """The STATIC grammar of `upload_url` ("" = no upload): http/https, a
    host, no `user:pass@`, printable ASCII without spaces, ≤ UPLOAD_URL_MAX.
    Where the target may be reached is the URL policy's to judge
    (sa02m_rules.http_guard, at request time)."""
    if not isinstance(value, str):
        return False
    if value == "":
        return True
    if len(value) > C.UPLOAD_URL_MAX or not all(33 <= ord(ch) <= 126 for ch in value):
        return False
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname
        parts.port
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and bool(host)
            and parts.username is None and parts.password is None)


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
    upload_url: str = ""
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
    url = sec.get("upload_url", "").strip()
    if valid_upload_url(url):
        out.upload_url = url
    else:
        # Never echo the refused value: it may carry a credential.
        out.warnings.append("upload_url refused (not an http/https URL without credentials)")
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
    if not valid_upload_url(conf.upload_url):
        raise ValueError("invalid upload_url")
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


# ── the upload token (a secret; contract §Передача) ───────────────────────
def valid_token(value: object) -> bool:
    return isinstance(value, str) and bool(_TOKEN_RE.match(value))


def read_token(path: Optional[str] = None) -> Optional[str]:
    """The bearer token, "" when none is set, None when the file exists but
    is not a readable regular single-link file holding a valid token (the
    upload then fails closed — it is never sent without the token the
    Operator set)."""
    target = path or C.TOKEN_FILE
    try:
        fd = os.open(target, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC)
    except FileNotFoundError:
        return ""
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 1024:
            return None
        raw = os.read(fd, 1025)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    # save_token writes one trailing newline. Anything else (empty, blank,
    # a second line, surrounding spaces) is not the token grammar: the file
    # exists, so the upload fails closed instead of going out with no token.
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n") or text.endswith("\r"):
        text = text[:-1]
    return text if valid_token(text) else None


def token_set(path: Optional[str] = None) -> bool:
    """Is a token stored? (the panel's write-only field answers only this)"""
    try:
        st = os.lstat(path or C.TOKEN_FILE)
    except OSError:
        return False
    return stat_module.S_ISREG(st.st_mode) and st.st_size > 0


def save_token(token: str, path: Optional[str] = None) -> None:
    """Replace (or, with "", remove) the token, 0640 — the setgid conf dir
    gives the file the daemon's group."""
    target = path or C.TOKEN_FILE
    if token == "":
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        return
    if not valid_token(token):
        raise ValueError("invalid token")
    atomic_write(target, token + "\n", mode=0o640)
