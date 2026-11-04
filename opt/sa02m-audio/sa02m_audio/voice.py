"""Voice-command reply on an upload (contract §Передача).

The recogniser answers HTTP 200 with a JSON document. This module turns that
document into absolute MQTT writes on the loopback broker, and only for
controls named in the allow file. It does not import paho: the daemon's link
publishes. A missing or broken allow file applies nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from . import constants as C

log = logging.getLogger("sa02m_audio.voice")

# `\Z`, never `$`: with re.match, `$` also matches before a trailing newline,
# which then rides into the topic or the payload.
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}\Z")
_CONTROL_RE = re.compile(r"^[A-Za-z0-9_]{1,32}\Z")
_VALUE_RE = re.compile(r"^(0|[1-9][0-9]{0,5})\Z")
# `R;G;B`, the bridge's WB `rgb` form (bridge_led._parse_color).
_RGB_RE = re.compile(r"^(0|[1-9][0-9]{0,2});(0|[1-9][0-9]{0,2});(0|[1-9][0-9]{0,2})\Z")

# An allow entry is either (min, max) for a decimal control or RGB.
RGB = "rgb"

Command = Dict[str, str]
Bound = Union[Tuple[int, int], str]
Allow = Dict[str, Dict[str, Bound]]


def allow_path_for(conf_path: Optional[str]) -> str:
    """The allow file sits next to the conf the daemon was given."""
    if conf_path:
        return os.path.join(os.path.dirname(os.path.abspath(conf_path)), "voice-allow.json")
    return C.VOICE_ALLOW_FILE


def commands_from_body(body: bytes, rec_id: str) -> Tuple[Command, ...]:
    """Commands in a JSON body, or () when the body is not a command document.

    A `recording_id` that is present and does not match this clip drops the
    whole document. Items whose value is neither a decimal nor an `R;G;B`
    string are skipped; which grammar a control takes is split()'s to judge.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        return ()
    if not isinstance(data, dict):
        return ()
    rid = data.get("recording_id")
    if "recording_id" in data and (not isinstance(rid, str) or rid != rec_id):
        log.warning("voice response recording id does not match clip %s", rec_id)
        return ()
    raw = data.get("commands")
    if not isinstance(raw, list):
        return ()
    out: List[Command] = []
    for item in raw[:C.VOICE_COMMANDS_MAX]:
        cmd = _one(item)
        if cmd is not None:
            out.append(cmd)
    return tuple(out)


def _one(item: Any) -> Optional[Command]:
    if not isinstance(item, dict):
        return None
    device = item.get("device")
    control = item.get("control")
    value = item.get("value")
    if isinstance(value, bool) or not isinstance(device, str) or not isinstance(control, str):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    if not (_DEVICE_RE.match(device) and _CONTROL_RE.match(control)):
        return None
    if not (_VALUE_RE.match(value) or _RGB_RE.match(value)):
        return None
    return {"device": device, "control": control, "value": value}


def load_allow(path: str) -> Optional[Allow]:
    """device -> control -> (min, max) or RGB. None when the file is missing
    or not a closed allow list (fail closed: apply nothing).

    A control is `{"min": n, "max": m}` or exactly `{"type": "rgb"}`; any
    other `type`, or `type` next to another key, invalidates the file."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read(C.VOICE_ALLOW_MAX_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        log.warning("voice allow list unreadable: %s", exc)
        return None
    if len(text) > C.VOICE_ALLOW_MAX_BYTES:
        log.warning("voice allow list too large")
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, dict) or not devices:
        return None
    allow: Allow = {}
    for device, controls in devices.items():
        if not isinstance(device, str) or not _DEVICE_RE.match(device) or not isinstance(controls, dict):
            return None
        parsed: Dict[str, Bound] = {}
        for control, bounds in controls.items():
            if not isinstance(control, str) or not _CONTROL_RE.match(control):
                return None
            if not isinstance(bounds, dict):
                return None
            if "type" in bounds:
                if set(bounds) != {"type"} or bounds["type"] != RGB:
                    return None
                parsed[control] = RGB
                continue
            lo, hi = bounds.get("min"), bounds.get("max")
            if isinstance(lo, bool) or isinstance(hi, bool):
                return None
            if not isinstance(lo, int) or not isinstance(hi, int) or lo < 0 or hi < lo or hi > 999999:
                return None
            parsed[control] = (lo, hi)
        if not parsed:
            return None
        allow[device] = parsed
    return allow


def split(commands: Sequence[Command], allow: Allow) -> Tuple[Tuple[Command, ...], int]:
    """(accepted, rejected_count). A value outside the control's range, or of
    the other grammar than the control's entry, is rejected — never raised."""
    accepted: List[Command] = []
    rejected = 0
    for cmd in commands:
        bounds = allow.get(cmd["device"], {}).get(cmd["control"])
        if bounds is None:
            rejected += 1
            continue
        if not _value_fits(cmd["value"], bounds):
            rejected += 1
            continue
        accepted.append(cmd)
    return tuple(accepted), rejected


def _value_fits(value: str, bounds: Bound) -> bool:
    if bounds == RGB:
        m = _RGB_RE.match(value)
        return m is not None and all(int(part) <= 255 for part in m.groups())
    if isinstance(bounds, str) or not _VALUE_RE.match(value):
        return False
    return bounds[0] <= int(value) <= bounds[1]


class SeenIds:
    """Recording ids whose commands were already published. Bounded."""

    def __init__(self, limit: int = C.VOICE_SEEN_MAX) -> None:
        self._limit = limit
        self._order: List[str] = []
        self._set = set()

    def seen(self, rec_id: str) -> bool:
        return rec_id in self._set

    def add(self, rec_id: str) -> None:
        if rec_id in self._set:
            return
        self._set.add(rec_id)
        self._order.append(rec_id)
        while len(self._order) > self._limit:
            self._set.discard(self._order.pop(0))


def topic_of(cmd: Command) -> str:
    return "/devices/%s/controls/%s/on" % (cmd["device"], cmd["control"])


def apply_commands(link: Any, rec_id: str, commands: Sequence[Command],
                   allow: Optional[Allow], seen: SeenIds) -> Tuple[str, str]:
    """Publish accepted commands. Returns (status, detail) for the voice event.

    status is "" when there is nothing to say (no commands). A repeat of
    `rec_id` publishes nothing. The id is remembered only after every
    accepted publish succeeded.
    """
    if not commands:
        return "", ""
    if seen.seen(rec_id):
        return "ok", "duplicate recording id"
    if allow is None:
        return "refused", "allow list missing or invalid"
    accepted, rejected = split(commands, allow)
    if not accepted:
        return "refused", "rejected %d" % rejected
    if link is None or not getattr(link, "connected", False):
        return "failed", "mqtt not connected"
    for cmd in accepted:
        ok = link.publish_command(topic_of(cmd), cmd["value"])
        if not ok:
            return "failed", "publish %s/%s refused" % (cmd["device"], cmd["control"])
    seen.add(rec_id)
    if rejected:
        return "ok", "applied %d, rejected %d" % (len(accepted), rejected)
    return "ok", "applied %d" % len(accepted)
