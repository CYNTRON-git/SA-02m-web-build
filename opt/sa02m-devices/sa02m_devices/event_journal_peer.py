"""Bounded, active-only stand transition used by service provisioning."""

import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import time

from .event_journal_delivery import verify
from .event_journal_state import JournalStateError

PEER = "sa02m-stand-api.service"
UNITS = {"devices-api": "sa02m-devices-api.service",
         "devices-logger": "sa02m-devices-logger.service", "stand-api": PEER}
COMMON_PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "Job")
PROPERTIES = COMMON_PROPERTIES + ("UnitFileState", "MainPID", "Environment", "EnvironmentFiles")
UNIT_NAME = re.compile(r"[A-Za-z0-9:_.@\\-]+\Z")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise JournalStateError("journal peer probe deadline expired")
    return remaining


def command(arguments, deadline, limit=65536, *, executable="systemctl"):
    """Cap output while reading, not after an unbounded communicate buffer."""
    with subprocess.Popen([executable, *arguments], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        chunks, size = [], 0
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if not selector.select(_remaining(deadline)):
                    raise JournalStateError("journal systemctl probe timed out")
                chunk = os.read(process.stdout.fileno(), min(4096, limit + 1 - size))
                if not chunk:
                    selector.unregister(process.stdout)
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    raise JournalStateError("journal systemctl output exceeded bound")
            if process.wait(timeout=_remaining(deadline)) != 0:
                raise JournalStateError("journal systemctl command failed")
            return b"".join(chunks).decode("utf-8", "strict")
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            raise JournalStateError("journal systemctl evidence unavailable") from exc
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1)


class Budget:
    """One deadline/output allowance shared by observation and enqueue."""
    def __init__(self, deadline):
        self.deadline = deadline
        self.remaining = 65536

    def consume(self, raw):
        _remaining(self.deadline)
        self.remaining -= len(raw)
        if self.remaining < 0:
            raise JournalStateError("journal scheduling evidence exceeded bound")

    def command(self, arguments, *, executable="systemctl"):
        text = command(arguments, self.deadline, self.remaining, executable=executable)
        self.consume(text.encode("utf-8"))
        return text


def show(unit, deadline, *, _service=True, budget=None):
    if not UNIT_NAME.fullmatch(unit):
        raise JournalStateError("invalid peer relation unit")
    required = PROPERTIES if _service else COMMON_PROPERTIES
    requested = required + (("MainPID",) if not _service and unit == PEER else ())
    arguments = ["show", "--all", "--no-pager", "--property=" + ",".join(requested), "--", unit]
    text = command(arguments, deadline) if budget is None else budget.command(arguments)
    properties = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in properties or key not in requested:
            raise JournalStateError("incomplete peer properties")
        properties[key] = value
    if not set(required).issubset(properties):
        missing = ",".join(key for key in required if key not in properties)
        raise JournalStateError("incomplete peer properties for " + unit + ": " + missing)
    return properties, len(text.encode())


def environment(properties, *, budget=None):
    values = {}
    def assignments(words):
        for word in words:
            key, separator, value = word.partition("=")
            if not separator:
                raise JournalStateError("unrecognized unit environment")
            if key.startswith("STAND_DEVICES_EVENTS_"):
                values[key] = value
    assignments(shlex.split(properties["Environment"]))
    files = properties["EnvironmentFiles"]
    matches = list(re.finditer(r"(?:^| )(/[^ ]+) \(ignore_errors=(yes|no)\)", files))
    if " ".join(match.group(0).strip() for match in matches) != files.strip():
        raise JournalStateError("unrecognized unit EnvironmentFiles")
    for match in matches:
        try:
            fd = os.open(match[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            if match[2] == "yes":
                continue
            raise JournalStateError("required unit EnvironmentFile missing") from None
        try:
            verify(os.fstat(fd), 0, private=False)
            raw = os.read(fd, 65537)
        finally:
            os.close(fd)
        if len(raw) > 65536:
            raise JournalStateError("unit EnvironmentFile exceeds bound")
        if budget is not None:
            budget.consume(raw)
        for line in raw.decode("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            key, separator, value = line.partition("=")
            if key.strip().startswith("STAND_DEVICES_EVENTS_"):
                words = shlex.split(value)
                if not separator or len(words) > 1 or line.endswith("\\"):
                    raise JournalStateError("unsupported journal EnvironmentFile setting")
                values[key.strip()] = words[0] if words else ""
    return values


def check_settings(values, expected):
    for key, value in expected.items():
        if values.get(key) != value:
            raise JournalStateError("journal shared configuration mismatch: " + key)


def process_identity(pid):
    if not pid.isdecimal() or int(pid) <= 0:
        raise JournalStateError("peer has no main process")
    root = Path("/proc") / pid
    def birth():
        raw = (root / "stat").read_bytes()
        if len(raw) > 8192:
            raise JournalStateError("invalid peer process identity")
        return raw.rsplit(b")", 1)[1].split()[19]
    try:
        before = birth()
        with (root / "environ").open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536 or before != birth():
            raise JournalStateError("peer process environment unavailable")
        selected = {}
        for item in raw.split(b"\0"):
            key, _, value = item.partition(b"=")
            if key.startswith(b"STAND_DEVICES_EVENTS_"):
                selected[key.decode("ascii")] = value.decode("utf-8")
        return (pid, before), selected
    except (OSError, UnicodeError, IndexError) as exc:
        raise JournalStateError("peer process evidence unavailable") from exc


def skipped(properties):
    return properties["LoadState"] in {"not-found", "masked"} \
        or properties["UnitFileState"] == "masked" \
        or properties["ActiveState"] in {"inactive", "failed"}


def _consumer_info(unit, expected, deadline, *, budget=None):
    common, _ = show(unit, deadline, _service=False, budget=budget)
    if common["Id"] != unit:
        raise JournalStateError("journal consumer unit alias is ambiguous")
    if common["LoadState"] in {"not-found", "masked"}:
        if common["LoadState"] == "not-found" and unit != PEER:
            raise JournalStateError("required journal consumer is absent: " + unit)
        if (common["ActiveState"] != "inactive" or common["SubState"] != "dead" or common["Job"]
                or common.get("MainPID", "0") != "0"):
            raise JournalStateError("contradictory unavailable journal consumer")
        return common
    if common["LoadState"] != "loaded":
        raise JournalStateError("unknown journal consumer load state")
    info, _ = show(unit, deadline, budget=budget)
    if info["Id"] != unit or info["LoadState"] != "loaded":
        raise JournalStateError("journal consumer changed during observation")
    if info["UnitFileState"] == "masked" and (info["ActiveState"] != "inactive" or info["Job"]):
        raise JournalStateError("contradictory masked journal consumer")
    check_settings(environment(info, budget=budget), expected)
    return info


def observe(expected, *, deadline=None, budget=None):
    """Fresh evidence only; this operation never dispatches or publishes state."""
    if deadline is None:
        deadline = time.monotonic() + 5
    if budget is None:
        budget = Budget(deadline)
    consumers = {unit: _consumer_info(unit, expected, deadline, budget=budget) for unit in UNITS.values()}
    info = consumers[PEER]
    while info["ActiveState"] in {"activating", "deactivating", "reloading"}:
        time.sleep(min(0.05, _remaining(deadline)))
        info = _consumer_info(PEER, expected, deadline, budget=budget)
    if info["LoadState"] in {"not-found", "masked"} or skipped(info):
        return info, None, True
    if info["ActiveState"] != "active":
        raise JournalStateError("unknown stand service state")
    identity, values = process_identity(info["MainPID"])
    return info, identity, all(values.get(key) == value for key, value in expected.items())


def transition(expected, mark_not_ready, *, force_refresh=False, observed=None, deadline=None):
    """External completion/installer context; never called by a starting hook."""
    if deadline is None:
        deadline = time.monotonic() + 5
    info, original, current = observe(expected, deadline=deadline) if observed is None else observed
    if original is None or (current and not force_refresh):
        return
    mark_not_ready()
    command(["--no-block", "try-restart", PEER], deadline)
    deadline = time.monotonic() + 35
    while True:
        info = _consumer_info(PEER, expected, deadline)
        if info["LoadState"] in {"not-found", "masked"} or skipped(info):
            raise JournalStateError("stand became unavailable during transition")
        if info["ActiveState"] == "active":
            identity, values = process_identity(info["MainPID"])
            if identity != original:
                check_settings(values, expected)
                return
        time.sleep(min(0.05, _remaining(deadline)))
