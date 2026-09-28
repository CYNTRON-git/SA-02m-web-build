"""BSH keys → WB controls: the one closed table (docs/contracts/home-connect.md §4).

Pure, stdlib. Every value from the cloud passes through a typed rule here
before it can become an MQTT payload: booleans must be JSON booleans, numbers
must be finite and in range, enum values must match the BSH enum grammar and
reduce to their last segment ([A-Za-z0-9_]). A key the table does not name, or
a value the rule refuses, publishes NOTHING — a control is never fabricated
(P2). Appliance ids and names are sanitised before they touch a topic or a
payload (`device_id`, `clean_name`) — no BSH string becomes a topic segment
unsanitised (threat model: forged cloud payloads).
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from . import constants as C


class Control(NamedTuple):
    name: str
    wb_type: str  # switch | value | text
    units: str
    source: str   # the BSH key(s), as in the contract table


# The contract table (§4) is compared with this tuple as a set.
MAPPING: Tuple[Control, ...] = (
    Control("connected", "switch", "", "appliance.connected, CONNECTED/DISCONNECTED"),
    Control("power_on", "switch", "", "BSH.Common.Setting.PowerState"),
    Control("door_open", "switch", "", "BSH.Common.Status.DoorState"),
    Control("running", "switch", "", "BSH.Common.Status.OperationState"),
    Control("finished", "switch", "", "BSH.Common.Status.OperationState"),
    Control("remote_start_allowed", "switch", "", "BSH.Common.Status.RemoteControlStartAllowed"),
    Control("remote_control_active", "switch", "", "BSH.Common.Status.RemoteControlActive"),
    Control("local_control_active", "switch", "", "BSH.Common.Status.LocalControlActive"),
    Control("operation_state", "text", "", "BSH.Common.Status.OperationState"),
    Control("active_program", "text", "", "BSH.Common.Root.ActiveProgram, programs/active"),
    Control("remaining_s", "value", "s", "BSH.Common.Option.RemainingProgramTime"),
    Control("progress_pct", "value", "%", "BSH.Common.Option.ProgramProgress"),
    Control("last_event", "text", "",
            "BSH.Common.Event.ProgramFinished, BSH.Common.Event.ProgramAborted, "
            "BSH.Common.Event.AlarmClockElapsed"),
    Control("last_event_ts", "value", "", "timestamp of last_event"),
)
CONTROLS: Dict[str, Control] = {c.name: c for c in MAPPING}
CONTROL_ORDER: Dict[str, int] = {c.name: i + 1 for i, c in enumerate(MAPPING)}

K_POWER = "BSH.Common.Setting.PowerState"
K_DOOR = "BSH.Common.Status.DoorState"
K_OPSTATE = "BSH.Common.Status.OperationState"
K_REMOTE_START = "BSH.Common.Status.RemoteControlStartAllowed"
K_REMOTE_ACTIVE = "BSH.Common.Status.RemoteControlActive"
K_LOCAL_ACTIVE = "BSH.Common.Status.LocalControlActive"
K_ACTIVE_PROGRAM = "BSH.Common.Root.ActiveProgram"
K_REMAINING = "BSH.Common.Option.RemainingProgramTime"
K_PROGRESS = "BSH.Common.Option.ProgramProgress"
EVENT_KEYS = (
    "BSH.Common.Event.ProgramFinished",
    "BSH.Common.Event.ProgramAborted",
    "BSH.Common.Event.AlarmClockElapsed",
)
EVENT_PRESENT = "BSH.Common.EnumType.EventPresentState.Present"

# Enum values with a fixed meaning; any other value of these keys publishes
# nothing for the derived switch.
POWER_VALUES = {"On": "1", "Off": "0", "Standby": "0", "MainsOff": "0"}
DOOR_VALUES = {"Open": "1", "Closed": "0", "Locked": "0"}
# Operation states that carry an active program worth reading.
PROGRAM_STATES = frozenset(("Run", "DelayedStart", "Pause", "ActionRequired"))

REMAINING_MAX_S = 7 * 86400
_ENUM_RE = re.compile(r"^BSH\.Common\.EnumType\.[A-Za-z]+\.([A-Za-z0-9_]{1,64})$")
_KEY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.]{1,200}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_ID_BAD = re.compile(r"[^a-z0-9-]")
_TYPE_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")

Update = Tuple[str, str]  # (control name, WB payload)


def device_id(ha_id: str) -> str:
    """`hc-<haId>`: lower-cased, anything outside [a-z0-9-] → '-', ≤ 64 chars."""
    body = _ID_BAD.sub("-", (ha_id or "").lower()).strip("-")
    if not body:
        raise ValueError("empty appliance id")
    return (C.DEVICE_PREFIX + body)[: C.DEVICE_ID_MAX].rstrip("-")


def clean_name(value: Any, fallback: str) -> str:
    """An appliance name fit for a payload: control/format characters removed,
    whitespace collapsed, ≤ NAME_MAX; empty ⇒ `fallback`."""
    if not isinstance(value, str):
        return fallback
    kept = "".join(ch for ch in value if unicodedata.category(ch)[0] not in ("C",))
    kept = " ".join(kept.split())[: C.NAME_MAX].strip()
    return kept or fallback


def clean_type(value: Any) -> str:
    return value if isinstance(value, str) and _TYPE_RE.match(value) else ""


def enum_segment(value: Any) -> Optional[str]:
    """`BSH.Common.EnumType.X.Y` → `Y`; anything else → None."""
    if not isinstance(value, str):
        return None
    m = _ENUM_RE.match(value)
    return m.group(1) if m else None


def key_segment(value: Any) -> Optional[str]:
    """Last segment of a BSH key (`Dishcare.Dishwasher.Program.Eco50` →
    `Eco50`); None when the key does not match the key grammar."""
    if not isinstance(value, str) or not _KEY_SEGMENT_RE.match(value):
        return None
    last = value.rsplit(".", 1)[-1]
    return last if _SEGMENT_RE.match(last) else None


def _bool(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return "1" if value else "0"
    return None


def _number(value: Any, low: float, high: float) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if not low <= value <= high:
        return None
    return str(int(round(value)))


def connected_update(connected: Any) -> List[Update]:
    payload = _bool(connected)
    return [("connected", payload)] if payload is not None else []


def active_program_update(value: Any) -> List[Update]:
    """ActiveProgram root key: a program key, or null/"" = no program."""
    if value is None or value == "":
        return [("active_program", "")]
    seg = key_segment(value)
    return [("active_program", seg)] if seg is not None else []


def apply_item(key: Any, value: Any, *, ts: Optional[int] = None) -> List[Update]:
    """One BSH (key, value) pair → the control updates it justifies."""
    if not isinstance(key, str):
        return []
    if key == K_POWER:
        seg = enum_segment(value)
        return [("power_on", POWER_VALUES[seg])] if seg in POWER_VALUES else []
    if key == K_DOOR:
        seg = enum_segment(value)
        return [("door_open", DOOR_VALUES[seg])] if seg in DOOR_VALUES else []
    if key == K_OPSTATE:
        seg = enum_segment(value)
        if seg is None:
            return []
        return [
            ("operation_state", seg),
            ("running", "1" if seg == "Run" else "0"),
            ("finished", "1" if seg == "Finished" else "0"),
        ]
    if key == K_REMOTE_START:
        payload = _bool(value)
        return [("remote_start_allowed", payload)] if payload is not None else []
    if key == K_REMOTE_ACTIVE:
        payload = _bool(value)
        return [("remote_control_active", payload)] if payload is not None else []
    if key == K_LOCAL_ACTIVE:
        payload = _bool(value)
        return [("local_control_active", payload)] if payload is not None else []
    if key == K_ACTIVE_PROGRAM:
        return active_program_update(value)
    if key == K_REMAINING:
        payload = _number(value, 0, REMAINING_MAX_S)
        return [("remaining_s", payload)] if payload is not None else []
    if key == K_PROGRESS:
        payload = _number(value, 0, 100)
        return [("progress_pct", payload)] if payload is not None else []
    if key in EVENT_KEYS:
        # An event item without a value, or with Present, is the event itself;
        # Off / Confirmed are its acknowledgement — nothing new happened.
        if value is not None and value != EVENT_PRESENT:
            return []
        seg = key_segment(key)
        if seg is None or ts is None:
            return []
        return [("last_event", seg), ("last_event_ts", str(int(ts)))]
    return []


def program_updates(program: Dict[str, Any]) -> List[Update]:
    """A `programs/active` answer → active_program + its mapped options."""
    out = active_program_update(program.get("key"))
    options = program.get("options")
    if isinstance(options, list):
        for opt in options:
            if isinstance(opt, dict):
                out.extend(apply_item(opt.get("key"), opt.get("value")))
    return out
