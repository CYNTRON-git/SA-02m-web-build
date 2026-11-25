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
RELATIONS = ("After", "Before", "Requires", "Wants", "Requisite", "BindsTo",
             "ConsistsOf", "PropagatesStopTo", "Conflicts", "OnFailure", "RequiredBy", "BoundBy")
PROPERTIES = ("Id", "LoadState", "ActiveState", "UnitFileState", "MainPID",
              "Environment", "EnvironmentFiles") + RELATIONS
UNIT_NAME = re.compile(r"[A-Za-z0-9:_.@\\-]+\Z")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise JournalStateError("journal peer probe deadline expired")
    return remaining


def command(arguments, deadline, limit=65536):
    """Cap output while reading, not after an unbounded communicate buffer."""
    with subprocess.Popen(["systemctl", *arguments], stdout=subprocess.PIPE,
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


def show(unit, deadline):
    if not UNIT_NAME.fullmatch(unit):
        raise JournalStateError("invalid peer relation unit")
    text = command(["show", unit, "--all", "--no-pager", "--property=" + ",".join(PROPERTIES)], deadline)
    properties = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in properties or key not in PROPERTIES:
            raise JournalStateError("incomplete peer properties")
        properties[key] = value
    if set(properties) != set(PROPERTIES):
        raise JournalStateError("incomplete peer properties")
    return properties, len(text.encode())


def environment(properties):
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


def graph_safe(current, cache, deadline):
    """Follow relevant directed closures; never enumerate the whole manager."""
    size = sum(item[1] for item in cache.values())
    def get(unit):
        nonlocal size
        if unit not in cache:
            if len(cache) >= 256:
                raise JournalStateError("peer relation closure exceeds unit bound")
            cache[unit] = show(unit, deadline)
            size += cache[unit][1]
        if size > 65536 or cache[unit][0]["LoadState"] not in {"loaded", "masked"}:
            raise JournalStateError("peer relation closure incomplete")
        if cache[unit][0]["Id"] == current:
            raise JournalStateError("peer relation alias reaches the starting unit")
        return cache[unit][0]
    if current == PEER:
        raise JournalStateError("peer cannot restart its current unit")
    for relations in (("After",), ("Before",)):
        pending, visited = [PEER], set()
        while pending:
            unit = pending.pop()
            if unit in visited:
                continue
            visited.add(unit)
            if unit == current:
                raise JournalStateError("peer restart reaches the starting unit")
            info = get(unit)
            for relation in relations:
                pending.extend(info[relation].split())
    pending, visited = [(PEER, "restart")], set()
    while pending:
        unit, action = pending.pop()
        if (unit, action) in visited:
            continue
        visited.add((unit, action))
        if unit == current:
            raise JournalStateError("peer restart reaches the starting unit")
        info = get(unit)
        # Starting an already-active dependency does not restart its dependants.
        # Keep job directions separate to avoid expanding all network consumers.
        if action in {"restart", "stop"}:
            for relation in ("ConsistsOf", "PropagatesStopTo", "RequiredBy", "BoundBy"):
                pending.extend((other, action) for other in info[relation].split())
        if action in {"restart", "start"}:
            for relation in ("Requires", "Wants", "Requisite", "BindsTo", "OnFailure"):
                pending.extend((other, "start") for other in info[relation].split())
        pending.extend((other, "stop") for other in info["Conflicts"].split())


def skipped(properties):
    return properties["LoadState"] in {"not-found", "masked"} \
        or properties["UnitFileState"] == "masked" \
        or properties["ActiveState"] in {"inactive", "failed"}


def prepare_peer(expected, consumer, mark_not_ready, *, force_refresh=False):
    deadline = time.monotonic() + 5
    cache = {unit: show(unit, deadline) for unit in UNITS.values()}
    if any(info["LoadState"] == "loaded" and info["Id"] != unit for unit, (info, _) in cache.items()):
        raise JournalStateError("journal consumer unit alias is ambiguous")
    for properties, _ in cache.values():
        if properties["LoadState"] not in {"not-found", "masked"}:
            check_settings(environment(properties), expected)
    peer = cache[PEER][0]
    while peer["ActiveState"] in {"activating", "deactivating", "reloading"}:
        time.sleep(min(0.05, _remaining(deadline)))
        peer, count = show(PEER, deadline)
        cache[PEER] = peer, count
    if skipped(peer):
        return
    if peer["LoadState"] != "loaded" or peer["ActiveState"] != "active":
        raise JournalStateError("unknown stand service state")
    original, values = process_identity(peer["MainPID"])
    if all(values.get(key) == value for key, value in expected.items()) and not force_refresh:
        return
    mark_not_ready()
    graph_safe(UNITS.get(consumer), cache, deadline)
    command(["--no-block", "try-restart", PEER], deadline)
    deadline = time.monotonic() + 35
    while True:
        info, _ = show(PEER, deadline)
        if skipped(info):
            # try-restart must not turn an operator stop into a start.
            raise JournalStateError("stand became unavailable during transition")
        if info["ActiveState"] == "active":
            identity, values = process_identity(info["MainPID"])
            if identity != original:
                check_settings(values, expected)
                return
        time.sleep(min(0.05, _remaining(deadline)))
