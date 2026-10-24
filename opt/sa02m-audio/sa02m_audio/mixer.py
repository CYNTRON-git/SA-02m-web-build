"""Capture gain of the USB microphone (contract §6 «Усиление захвата»).

`alsa-restore.service` runs before a USB card exists, so after a reboot or a
replug the kernel default comes back; the daemon re-applies `capture_gain`
itself. The control is found by TYPE, not by name: the first simple control
whose capabilities carry `cvolume`. Its range is read from the live control
at apply time — never hard-coded — and a value outside it is refused.

Every amixer call is an argv list (no shell) under `timeout -k 1 3` and a
subprocess timeout. The mixer write does not take the §11 lease: the lease
guards the PCM open, and a mixer control is independent of who records.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import namedtuple
from typing import Callable, List, Optional, Sequence, Tuple

from . import constants as C

# One simple control of `amixer scontents`. `values` = the capture raw value
# of each channel; `db` = amixer's own dB string of the first channel or "".
Control = namedtuple("Control", "name index caps min max values db")
# The outcome of an apply: `state` is one of C.GAIN_STATES.
Result = namedtuple("Result", "state control detail")

_HEADER = re.compile(r"^Simple mixer control '(.*)',(\d+)\s*$")
_LIMITS_CAPTURE = re.compile(r"\bCapture (-?\d+) - (-?\d+)\b")
_CHANNEL = re.compile(r"^([A-Za-z0-9 ]+): (.*)$")
_CHANNEL_CAPTURE = re.compile(r"\bCapture (-?\d+)(?: \[\d+%\])?(?: \[(-?[0-9]+\.[0-9]+)dB\])?")
_NAME = re.compile(C.MIXER_CONTROL_RE)
_CARD_ID = re.compile(C.CARD_ID_RE)
_DB = re.compile(C.MIXER_DB_RE)
_DEVICE_CARD_ID = re.compile(r"(?:^|[:,])CARD=([A-Za-z0-9_]{1,32})(?:,|\Z)")
_DEVICE_CARD_INDEX = re.compile(r"^(?:plug)?hw:([0-9]{1,2})(?:,|\Z)")

Runner = Callable[[Sequence[str]], Optional[str]]


def parse_scontents(text: str) -> List[Control]:
    """Every simple control in `amixer scontents` (or `sset`) output."""
    out: List[Control] = []
    cur: Optional[dict] = None

    def flush() -> None:
        if cur is not None:
            out.append(Control(cur["name"], cur["index"], tuple(cur["caps"]), cur["min"],
                               cur["max"], tuple(cur["values"]), cur["db"]))

    for raw in text.splitlines():
        m = _HEADER.match(raw)
        if m:
            flush()
            cur = {"name": m.group(1), "index": int(m.group(2)), "caps": [], "min": None,
                   "max": None, "values": [], "db": ""}
            continue
        if cur is None:
            continue
        line = raw.strip()
        if line.startswith("Capabilities:"):
            cur["caps"] = line[len("Capabilities:"):].split()
        elif line.startswith("Limits:"):
            lm = _LIMITS_CAPTURE.search(line)
            if lm:
                cur["min"], cur["max"] = int(lm.group(1)), int(lm.group(2))
        else:
            cm = _CHANNEL.match(line)
            if not cm or cm.group(1).endswith("channels"):
                continue
            vm = _CHANNEL_CAPTURE.search(cm.group(2))
            if vm:
                cur["values"].append(int(vm.group(1)))
                db = vm.group(2) or ""
                if not cur["db"] and _DB.match(db):
                    cur["db"] = db
    flush()
    return out


def pick_capture_volume(controls: Sequence[Control]) -> Optional[Control]:
    """The first control with a capture volume. Its name is passed to amixer
    as one argv word, so a name outside the grammar — or a control without a
    parsed range or value — counts as no control at all (never a guess at
    the next one)."""
    for ctl in controls:
        if "cvolume" in ctl.caps:
            if (_NAME.match(ctl.name) and ctl.min is not None and ctl.max is not None
                    and ctl.min <= ctl.max and ctl.values):
                return ctl
            return None
    return None


def card_arg(card) -> str:
    """The amixer `-c` value for a resolved alsa.Card: its id when that is a
    plain ALSA id, else its index."""
    return card.id if _CARD_ID.match(card.id) else str(card.index)


def card_from_device(device: str) -> Optional[str]:
    """The card an explicit ALSA device name points at (`CARD=<id>` or
    `hw:/plughw:<n>`), or None when it cannot be derived from the name."""
    m = _DEVICE_CARD_ID.search(device)
    if m:
        return m.group(1)
    m = _DEVICE_CARD_INDEX.match(device)
    if m:
        return str(int(m.group(1)))
    return None


def _prefix() -> List[str]:
    return [C.TIMEOUT, "-k", str(C.AMIXER_KILL_AFTER_S), str(C.AMIXER_TIMEOUT_S), C.AMIXER]


def argv_get(card: str) -> List[str]:
    return _prefix() + ["-c", card, "--", "scontents"]


def argv_set(card: str, ctl: Control, raw: int) -> List[str]:
    # `--` ends amixer's options: a negative raw value is not read as a flag.
    # `capture` before the value is amixer(1)'s stream-direction modifier: a
    # bare value would also set the playback volume of a control that has
    # both (a headset's sidetone). Not `cap` — that is the capture switch.
    return _prefix() + ["-c", card, "--", "sset", "%s,%d" % (ctl.name, ctl.index), "capture",
                        str(int(raw))]


def run(argv: Sequence[str]) -> Optional[str]:
    """stdout of a successful amixer call, None on any failure (not found,
    non-zero exit, the timeout)."""
    env = dict(os.environ, LC_ALL="C")
    try:
        proc = subprocess.run(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=C.AMIXER_RUN_TIMEOUT_S,
                              check=False, close_fds=True, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout[:C.AMIXER_OUTPUT_MAX].decode("utf-8", errors="replace")


def read(card: str, runner: Runner = run) -> Tuple[bool, Optional[Control]]:
    """(amixer answered, the capture volume control or None)."""
    text = runner(argv_get(card))
    if text is None:
        return False, None
    return True, pick_capture_volume(parse_scontents(text))


def landed(ctl: Optional[Control], raw: int) -> bool:
    return ctl is not None and bool(ctl.values) and all(v == raw for v in ctl.values)


def apply(card: str, raw: int, runner: Runner = run) -> Result:
    """Read the control, refuse a value outside its live range, set every
    channel to `raw` and confirm from sset's own output (no second fork)."""
    answered, ctl = read(card, runner)
    if not answered:
        return Result(C.GAIN_FAILED, None, "amixer -c %s scontents failed" % card)
    if ctl is None:
        return Result(C.GAIN_NO_CONTROL, None, "card %s has no capture volume control" % card)
    if raw < ctl.min or raw > ctl.max:
        return Result(C.GAIN_OUT_OF_RANGE, ctl, "capture_gain %d out of %d..%d, not applied"
                      % (raw, ctl.min, ctl.max))
    text = runner(argv_set(card, ctl, raw))
    if text is None:
        return Result(C.GAIN_FAILED, ctl, "amixer sset failed")
    after = [c for c in parse_scontents(text) if (c.name, c.index) == (ctl.name, ctl.index)]
    if not after or not landed(after[0], raw):
        return Result(C.GAIN_FAILED, ctl, "value %d did not land" % raw)
    return Result(C.GAIN_APPLIED, after[0]._replace(min=ctl.min, max=ctl.max), "")
