"""The «Запись звука» card's dispatch — run by `sa02m_audio_api.cgi` as www-data.

Standard library only (no paho: a board without it still gets an honest card),
on the system python3 with `PYTHONPATH=/opt/sa02m-audio`. It reads the conf,
the daemon's status.json and events.jsonl and the clip listing, writes the
conf, and NAMES what the CGI may do next — it never publishes to MQTT and never
runs a privileged command itself. Contract: docs/contracts/audio-recorder.md
§15.

CLI (the CGI's only ways in):
* `python3 -m sa02m_audio.api GET|POST` — request body on stdin; stdout is
  exactly two lines: the response JSON, then the follow-up the CGI runs
  (`pub record <seconds>|stop`, `pub delete <id>|all`, `pub light_music 0|1`,
  `sudo enable|disable|restart`) or an empty line. The CGI re-validates the
  follow-up against its own fixed `case` before anything runs.
* `python3 -m sa02m_audio.api DOWNLOAD` with QUERY_STRING in the environment
  — writes the whole HTTP response (headers + WAV bytes, or a JSON error) to
  stdout itself, so the bytes never pass through a shell variable.

Which media are present is judged by the daemon's own code
(media.MediaProbe.presence, read-only — the panel never creates or probes a
store); a removable medium that is not present is never listed or read, so
the rootfs phantom under /media/usb is never served.
"""

from __future__ import annotations

import json
import os
import re
import stat as stat_module
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

from . import __version__
from . import config as conf_mod
from . import constants as C
from .fsutil import is_regular
from .media import MediaProbe, close_all, store_dirs
from .store import Store, valid_id

Response = Dict[str, Any]

# status.json states a LIVE daemon keeps rewriting.
_LIVE = frozenset((C.STATE_IDLE, C.STATE_RECORDING))
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# Panel-editable conf keys (the device name is not a panel setting).
SAVE_KEYS = ("storage", "default_duration_s", "max_duration_s", "keep_files", "keep_mb",
             "keep_hours", "delete_after_upload", "upload_url", "voice_commands",
             "light_music_target", "light_music_style", "light_music_rate_hz", "capture_gain")
MUTATIONS = ("record", "stop", "delete", "save", "set_token", "enable", "disable", "restart",
             "light_music")
# Every `"error"` code the dispatch can answer — compared with the contract;
# `_err` refuses any other.
ERROR_CODES = (
    "not_found", "not_installed", "conf_unreadable", "disabled", "not_running",
    "bad_seconds", "bad_id", "not_enabled", "unknown_field", "invalid_value",
    "ack_required", "conf_write_failed", "method_not_allowed", "payload_too_large",
    "invalid_json", "bad_request", "url_refused", "bad_token", "no_target",
)
MEDIA = C.MEDIA


def footprint_installed() -> bool:
    """unit + package + daemon user. A board that got the package by OTA
    alone lacks the user ⇒ `not_installed`."""
    if not (os.path.isfile(C.UNIT_FILE) and os.path.isdir(C.PACKAGE_DIR)):
        return False
    try:
        import pwd
        pwd.getpwnam(C.DAEMON_USER)
    except (ImportError, KeyError):
        return False
    return True


def _read_json(path: str, max_bytes: int = 1 << 20) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.loads(fh.read(max_bytes))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _str(value: Any, allowed: Optional[Tuple[str, ...]] = None, max_len: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    if allowed is not None:
        return value if value in allowed else ""
    return value[:max_len]


def _media_view(raw: Any) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for m in MEDIA:
        v = raw.get(m.name) if isinstance(raw.get(m.name), dict) else {}
        out[m.name] = {"state": _str(v.get("state"), C.MEDIUM_STATES) or C.MEDIUM_ABSENT,
                       "free_bytes": _int(v.get("free_bytes")), "files": _int(v.get("files")),
                       "bytes": _int(v.get("bytes"))}
    return out


def _recording_view(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict) or not valid_id(raw.get("id")):
        return None
    return {"id": raw["id"], "medium": _str(raw.get("medium"), C.MEDIUM_NAMES),
            "seconds": _int(raw.get("seconds")), "started_ts": _int(raw.get("started_ts"))}


_TARGET_RE = re.compile(C.LIGHT_MUSIC_TARGET_RE)
_YAML_ITEM = re.compile(r"^( *)- *(.*)$")
_YAML_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*) *: *(.*)$")


def _yaml_scalar(raw: str) -> str:
    """A plain or quoted scalar, its trailing comment dropped."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else ""
    cut = raw.find(" #")
    return (raw[:cut] if cut >= 0 else raw).strip()


def led_devices(path: Optional[str] = None) -> List[str]:
    """The `type: led` device ids of the bridge's `devices:` list, in file
    order. Not a YAML parser: it reads the block-style list both the template
    and the panel's own save write (`- id: …` with `type:` on the item's level)
    and anything it cannot read is left out — the list is an allow-list, so
    a miss only hides a strip, it never admits a string."""
    try:
        with open(path or C.BRIDGE_CONF, encoding="utf-8", errors="replace") as fh:
            text = fh.read(C.BRIDGE_CONF_MAX_BYTES)
    except OSError:
        return []
    out: List[str] = []
    item: Optional[Dict[str, str]] = None
    level = -1
    in_devices = False

    def flush() -> None:
        dev = item or {}
        ident = dev.get("id", "")
        if dev.get("type") == "led" and _TARGET_RE.match(ident) and ident not in out:
            out.append(ident)

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0 and not stripped.startswith("-"):
            flush()
            item = None
            in_devices = stripped == "devices:"
            continue
        if not in_devices:
            continue
        m = _YAML_ITEM.match(line)
        if m and (item is None or len(m.group(1)) <= level):
            flush()
            item, level = {}, len(m.group(1))
            rest = m.group(2)
        elif item is not None and indent == level + 2:
            rest = stripped
        else:
            continue
        k = _YAML_KEY.match(rest)
        if k and k.group(1) not in item:
            item[k.group(1)] = _yaml_scalar(k.group(2))
    flush()
    return out


def _probe() -> MediaProbe:
    return MediaProbe(MEDIA)


def present_media() -> List[str]:
    probe = _probe()
    mounts = probe.read_mounts()
    return [m.name for m in MEDIA if probe.presence(m, mounts)[0]]


def clips_view(present: List[str]) -> List[Dict[str, Any]]:
    """Clips of the present media, newest first, each with what its sidecar
    says (duration, peak, upload) when the sidecar passes its checks."""
    store = Store(MEDIA)
    clips = []
    for name in present:
        clips.extend(store.list_clips(name))
    clips.sort(key=lambda c: (c.mtime, c.id), reverse=True)
    out = []
    for c in clips:
        row: Dict[str, Any] = {"id": c.id, "medium": c.medium, "size": c.size, "ts": int(c.mtime)}
        row.update(store.read_meta(c.medium, c.id))
        out.append(row)
    return out


def url_refusal(url: str) -> str:
    """The URL policy's STATIC verdict ("" = acceptable), when the policy is
    installed. The daemon judges again at request time, after resolving;
    without the policy the daemon never sends (`unavailable`)."""
    if not url:
        return ""
    from .upload import load_guard

    guard = load_guard()
    if guard is None:
        return ""
    try:
        return str(guard.check_url(url, resolve=False))[:120]
    except Exception:
        return "url check failed"


def events_tail() -> List[Dict[str, Any]]:
    try:
        with open(C.EVENTS_FILE, encoding="utf-8", errors="replace") as fh:
            lines = fh.read(1 << 20).splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-C.EVENTS_TAIL:]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict) or _str(item.get("event"), C.EVENT_KINDS) == "":
            continue
        ev: Dict[str, Any] = {"ts": _int(item.get("ts")), "event": item["event"]}
        for key in ("status", "id", "medium", "detail", "source"):
            if isinstance(item.get(key), str):
                ev[key] = item[key][:200]
        if _int(item.get("seconds")):
            ev["seconds"] = _int(item.get("seconds"))
        out.append(ev)
    return out


def conf_view(conf: conf_mod.RecorderConfig) -> Dict[str, Any]:
    return {key: getattr(conf, key) for key in conf_mod.KEYS}


def bounds_view(conf: conf_mod.RecorderConfig) -> Dict[str, Any]:
    out = {k: list(v) for k, v in conf_mod.bounds(conf).items() if k != "keep_mb"}
    out["keep_mb"] = {m.name: [C.KEEP_MB_MIN, m.keep_mb_max] for m in MEDIA}
    return out


def status_view(now: Optional[float] = None) -> Response:
    now = time.time() if now is None else now
    conf = conf_mod.load()
    base: Response = {"ok": True, "version": __version__, "removable": list(C.REMOVABLE_MEDIA)}
    if not footprint_installed():
        base.update(state=C.STATE_NOT_INSTALLED, installed=False, enabled=False, live=False)
        return base
    st = _read_json(C.STATUS_FILE) or {}
    daemon_state = _str(st.get("state"), C.STATES)
    fresh = 0 <= now - _int(st.get("ts")) <= C.STATUS_STALE_S
    live = bool(conf.enabled) and daemon_state in _LIVE and fresh
    if conf.unreadable:
        state = C.STATE_CONF_UNREADABLE
    elif not conf.enabled:
        state = C.STATE_DISABLED
    elif live:
        state = daemon_state
    elif daemon_state in (C.STATE_CONF_UNREADABLE, C.STATE_MISSING_DEPS, C.STATE_ERROR):
        state = daemon_state
    else:
        state = C.STATE_NOT_RUNNING
    present = present_media()
    base.update(
        state=state,
        installed=True,
        enabled=bool(conf.enabled),
        live=live,
        message=_str(st.get("message")) if state != C.STATE_IDLE else "",
        mic=_str(st.get("mic"), C.MIC_STATES) if live else "",
        mic_device=_str(st.get("mic_device"), max_len=64) if live else "",
        medium=conf.storage,
        medium_state=_str(st.get("medium_state"), C.MEDIUM_STATES) if live else "",
        media=_media_view(st.get("media")),
        recording=_recording_view(st.get("recording")) if live else None,
        last_file=st.get("last_file") if valid_id(st.get("last_file")) else "",
        last_status=_str(st.get("last_status"), C.LAST_STATUSES),
        last_duration_ms=_int(st.get("last_duration_ms")),
        last_size=_int(st.get("last_size")),
        last_peak_dbfs=_int(st.get("last_peak_dbfs"), C.PEAK_FLOOR_DBFS),
        last_upload=_str(st.get("last_upload"), C.UPLOAD_STATES) or C.UP_OFF,
        rate_used=_int(st.get("rate_used")),
        rate_max=_int(st.get("rate_max"), C.RATE_MAX_STARTS),
        present=present,
        clips=clips_view(present),
        events=events_tail(),
        conf=conf_view(conf),
        bounds=bounds_view(conf),
        token_set=conf_mod.token_set(),
        light_music=st.get("light_music") is True if live else False,
        light_music_state=_str(st.get("light_music_state"), C.LIGHT_MUSIC_STATES) if live else "",
        light_music_update_hz=_rate(st.get("light_music_update_hz")) if live else 0.0,
        mic_gain=_mic_gain_view(st.get("mic_gain")) if live else None,
        led_devices=led_devices(),
        light_music_styles=list(C.LIGHT_MUSIC_STYLES),
    )
    return base


_MIXER_DB = re.compile(C.MIXER_DB_RE)
_MIXER_CONTROL = re.compile(C.MIXER_CONTROL_RE)


def _gain_int(value: Any) -> Optional[int]:
    return value if value is not None and conf_mod.valid_capture_gain(value) else None


def _mic_gain_view(raw: Any) -> Optional[Dict[str, Any]]:
    """status.json `mic_gain` (contract §9), every field re-checked: an
    unknown state drops the whole view; any other field outside its grammar
    reads as absent (null / ""), never echoed."""
    if not isinstance(raw, dict) or _str(raw.get("state"), C.GAIN_STATES) == "":
        return None
    lo, hi = _gain_int(raw.get("min")), _gain_int(raw.get("max"))
    if lo is None or hi is None or lo > hi:
        lo = hi = None
    db, control = raw.get("db"), raw.get("control")
    return {"state": raw["state"], "want": _gain_int(raw.get("want")),
            "applied": _gain_int(raw.get("applied")), "min": lo, "max": hi,
            "db": db if isinstance(db, str) and _MIXER_DB.match(db) else "",
            "control": control if isinstance(control, str) and _MIXER_CONTROL.match(control) else ""}


def _rate(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return round(max(0.0, min(float(value), 100.0)), 1)


def _err(code: str, **extra: Any) -> Response:
    if code not in ERROR_CODES:
        raise ValueError("undeclared error code %r" % code)
    out: Response = {"ok": False, "error": code}
    out.update(extra)
    return out


def _needs_live(conf: conf_mod.RecorderConfig) -> Optional[Response]:
    view = status_view()
    if not conf.enabled:
        return _err(C.ST_DISABLED, status=view)
    if not view.get("live"):
        return _err(C.STATE_NOT_RUNNING, status=view)
    return None


def _apply_save(conf: conf_mod.RecorderConfig, body: Dict[str, Any]) -> Optional[Response]:
    """Mutate `conf` from the request; an error response, or None."""
    for key in body:
        if key not in SAVE_KEYS and key not in ("action", "ack_removable"):
            return _err("unknown_field", field=str(key)[:32])
    for key in SAVE_KEYS:
        if key not in body:
            continue
        value = body[key]
        current = getattr(conf, key)
        if key == "capture_gain":
            # int or null (= not managed); the current value may be None, so
            # its type cannot pick the check. validate() gives the grammar.
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                return _err("invalid_value", field=key)
        elif isinstance(current, bool):
            if not isinstance(value, bool):
                return _err("invalid_value", field=key)
        elif isinstance(current, int):
            if isinstance(value, bool) or not isinstance(value, int):
                return _err("invalid_value", field=key)
        elif not isinstance(value, str):
            return _err("invalid_value", field=key)
        setattr(conf, key, value)
    if conf.storage not in C.MEDIUM_NAMES:
        return _err("invalid_value", field="storage")
    # The removable-media risk is acknowledged on EVERY save that keeps a
    # removable medium selected (decision F2, contract §5).
    if conf.storage in C.REMOVABLE_MEDIA and body.get("ack_removable") is not True:
        return _err("ack_required")
    try:
        conf_mod.validate(conf)
    except ValueError as exc:
        field = str(exc).replace("invalid ", "")[:32]
        return _err("invalid_value", field=field)
    if "upload_url" in body:
        reason = url_refusal(conf.upload_url)
        if reason:
            return _err("url_refused", reason=reason)
    # The strip must be one the bridge has as `type: led`: an allow-list read
    # from its own conf, never a free string (contract §15).
    if "light_music_target" in body and conf.light_music_target \
            and conf.light_music_target not in led_devices():
        return _err("invalid_value", field="light_music_target")
    return None


def mutate(body: Dict[str, Any]) -> Tuple[Response, str]:
    action = body.get("action")
    if action not in MUTATIONS:
        return _err("not_found"), ""
    if not footprint_installed():
        return _err(C.STATE_NOT_INSTALLED, state=C.STATE_NOT_INSTALLED), ""
    conf = conf_mod.load()
    if conf.unreadable:
        return _err(C.STATE_CONF_UNREADABLE), ""
    if action == "record":
        refused = _needs_live(conf)
        if refused:
            return refused, ""
        seconds = body.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) \
                or not C.MIN_DURATION_S <= seconds <= conf.max_duration_s:
            return _err("bad_seconds", max=conf.max_duration_s), ""
        return {"ok": True, "action": action}, "pub record %d" % seconds
    if action == "stop":
        refused = _needs_live(conf)
        if refused:
            return refused, ""
        return {"ok": True, "action": action}, "pub record stop"
    if action == "delete":
        refused = _needs_live(conf)
        if refused:
            return refused, ""
        target = body.get("id")
        if not (target == C.DELETE_ALL or valid_id(target)):
            return _err("bad_id"), ""
        return {"ok": True, "action": action}, "pub delete %s" % target
    if action == "light_music":
        refused = _needs_live(conf)
        if refused:
            return refused, ""
        on = body.get("on")
        if not isinstance(on, bool):
            return _err("invalid_value", field="on"), ""
        if not conf.light_music_target:
            return _err("no_target"), ""
        return {"ok": True, "action": action}, "pub light_music %d" % (1 if on else 0)
    if action == "set_token":
        token = body.get("token")
        if not (token == "" or conf_mod.valid_token(token)):
            return _err("bad_token"), ""
        try:
            conf_mod.save_token(token)
        except (OSError, ValueError):
            return _err("conf_write_failed"), ""
        # Write-only: the answer says whether a token is set, never what it is.
        return {"ok": True, "action": action, "token_set": conf_mod.token_set()}, ""
    if action == "restart":
        if not conf.enabled:
            return _err("not_enabled"), ""
        return {"ok": True, "action": action}, "sudo restart"
    if action in ("enable", "disable"):
        conf.enabled = action == "enable"
    else:
        refused = _apply_save(conf, body)
        if refused:
            return refused, ""
    try:
        conf_mod.save(conf)
    except (OSError, ValueError):
        return _err("conf_write_failed"), ""
    if action == "save":
        return {"ok": True, "action": action, "status": status_view()}, ""
    return {"ok": True, "action": action}, "sudo %s" % action


def dispatch(method: str, body_text: str) -> Tuple[Response, str]:
    if method == "GET":
        return status_view(), ""
    if method != "POST":
        return _err("method_not_allowed"), ""
    if len(body_text.encode("utf-8")) > C.MAX_BODY_BYTES:
        return _err("payload_too_large"), ""
    try:
        body = json.loads(body_text) if body_text.strip() else {}
    except ValueError:
        return _err("invalid_json"), ""
    if not isinstance(body, dict):
        return _err("invalid_json"), ""
    if body.get("action", "status") == "status":
        return status_view(), ""
    return mutate(body)


# ── download ───────────────────────────────────────────────────────────────
def _open_clip(rec_id: str, present: List[str]) -> Optional[Tuple[int, int]]:
    """(fd, size) of `<id>.wav` on a present medium: opened O_NOFOLLOW
    through the store's own dir descriptors, a regular single-link file no
    larger than DOWNLOAD_MAX_BYTES. None otherwise (never a path)."""
    media = {m.name: m for m in MEDIA}
    for name in present:
        try:
            fds = store_dirs(media[name], create=False, subdirs=(C.REC_SUBDIR,))
        except OSError:
            continue
        try:
            name_wav = rec_id + C.CLIP_SUFFIX
            if is_regular(fds[1], name_wav) is None:
                continue
            try:
                fd = os.open(name_wav, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC,
                             dir_fd=fds[1])
            except OSError:
                continue
            st = os.fstat(fd)
            if not stat_module.S_ISREG(st.st_mode) or st.st_nlink != 1 \
                    or st.st_size > C.DOWNLOAD_MAX_BYTES:
                os.close(fd)
                continue
            return fd, int(st.st_size)
        finally:
            close_all(*fds)
    return None


def _json_reply(out: Any, payload: Response) -> None:
    out.write(b"Content-Type: application/json; charset=UTF-8\r\nCache-Control: no-store\r\n"
              b"X-Content-Type-Options: nosniff\r\n\r\n")
    out.write((json.dumps(payload) + "\n").encode("utf-8"))


def download(query: str, out: Any) -> int:
    if len(query) > C.MAX_QUERY_BYTES:
        _json_reply(out, _err("bad_request"))
        return 0
    params = parse_qs(query, keep_blank_values=True, max_num_fields=8)
    ids = params.get("id") or [""]
    inline = (params.get("inline") or ["0"])[0] == "1"
    rec_id = ids[0]
    if len(ids) != 1 or not valid_id(rec_id):
        _json_reply(out, _err("bad_id"))
        return 0
    if not footprint_installed():
        _json_reply(out, _err(C.STATE_NOT_INSTALLED))
        return 0
    found = _open_clip(rec_id, present_media())
    if found is None:
        _json_reply(out, _err("not_found"))
        return 0
    fd, size = found
    try:
        disposition = "inline" if inline else 'attachment; filename="%s.wav"' % rec_id
        out.write(("Content-Type: audio/wav\r\nContent-Length: %d\r\nContent-Disposition: %s\r\n"
                   "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n"
                   % (size, disposition)).encode("ascii"))
        left = size
        while left > 0:
            chunk = os.read(fd, min(65536, left))
            if not chunk:
                break
            out.write(chunk)
            left -= len(chunk)
    finally:
        os.close(fd)
    return 0


def main(argv: List[str]) -> int:
    method = argv[0] if argv else "GET"
    if method == "DOWNLOAD":
        out = sys.stdout.buffer
        rc = download(os.environ.get("QUERY_STRING", ""), out)
        out.flush()
        return rc
    body = sys.stdin.read(C.MAX_BODY_BYTES + 1) if method == "POST" else ""
    response, follow = dispatch(method, body)
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.write(follow + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
