"""Bounded PID1 admission with lossless evidence for an exact completion request."""

import os
from pathlib import Path
import secrets
import shlex

from . import event_journal_delivery as files
from . import event_journal_peer as peer
from . import event_journal_request as request
from . import event_journal_source as source
from .event_journal_state import JournalStateError

RUNTIME = Path("/run/systemd/system")
TRANSIENT = Path("/run/systemd/transient")
TIMEOUT_FILE = "50-sa02m-journal-timeout.conf"
TIMEOUT_BYTES = b"[Unit]\nJobTimeoutSec=960s\n"
DESCRIPTION = "SA-02m journal completion"
AFTER = ("sa02m-devices-api.service", "sa02m-devices-logger.service")
DEFAULT_AFTER = {"basic.target", "sysinit.target", "system.slice", "systemd-journald.socket"}
PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "Job", "Transient",
              "FragmentPath", "DropInPaths", "NeedDaemonReload", "Type", "User",
              "Group", "DynamicUser", "Restart", "KillMode", "Environment",
              "TimeoutStartUSec", "JobTimeoutUSec", "After")


Budget = peer.Budget


def argv(encoded):
    value = request.decode(encoded)
    update = Path(value["settings"]["STAND_DEVICES_EVENTS_UPDATE_DIR"])
    return ["/usr/bin/flock", "--shared", "--nonblock", "--no-fork", str(files.INSTALL_LOCK),
            "/usr/bin/flock", "--shared", "--timeout", "900", "--no-fork", str(update / "update.lock"),
            "/usr/bin/python3", "-I", "-B", str(source.installed_root() / "sa02m_devices_journal_startup.py"),
            "complete", "--request", encoded]


def _quote(value):
    # systemd's EXEC_SYNTAX without environment expansion (unit_concat_strv).
    controls = {"\a": "\\a", "\b": "\\b", "\f": "\\f", "\n": "\\n",
                "\r": "\\r", "\t": "\\t", "\v": "\\v"}
    result = []
    for character in value:
        if character == "%":
            result.append("%%")
        elif character in {"\\", '"'}:
            result.append("\\" + character)
        elif character in controls:
            result.append(controls[character])
        elif ord(character) < 32 or ord(character) == 127:
            result.append("\\%03o" % ord(character))
        else:
            result.append(character)
    return '"' + "".join(result) + '"'


def fragment(arguments):
    return ("# This is a transient unit file, created programmatically via the systemd API. Do not edit.\n"
            "[Unit]\nDescription=" + DESCRIPTION + "\n\n[Service]\nType=oneshot\nUser=root\nGroup=root\n"
            "KillMode=control-group\nRestart=no\nTimeoutStartSec=16min\n\n[Unit]\n"
            + "".join("After=" + unit + "\n" for unit in AFTER)
            + "\n[Service]\nExecStart=\nExecStart=:" + " ".join(map(_quote, arguments)) + "\n").encode("utf-8")


def _snapshot(path, budget):
    with files.directory(path.parent, 0, private=False) as dfd:
        fd = files.open_file(dfd, path.name, 0, private=False)
        try:
            before = os.fstat(fd)
            raw = os.pread(fd, budget.remaining + 1, 0)
            after = os.fstat(fd)
        finally:
            os.close(fd)
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if signature(before) != signature(after):
        raise JournalStateError("journal runtime evidence changed while reading")
    budget.consume(raw)
    return signature(before), raw


def _timeout_path(unit):
    return RUNTIME / (unit + ".d") / TIMEOUT_FILE


def _publish_timeout(unit, budget):
    path = _timeout_path(unit)
    with files.directory(RUNTIME, 0, private=False):
        with files.directory(path.parent, 0, create=True, private=False) as dfd:
            if set(os.listdir(dfd)) - {TIMEOUT_FILE}:
                raise JournalStateError("foreign journal timeout directory content")
            try:
                previous = _snapshot(path, budget)
            except FileNotFoundError:
                temporary = ".timeout-" + secrets.token_hex(16)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o644, dir_fd=dfd)
                try:
                    os.fchmod(fd, 0o644)
                    with os.fdopen(os.dup(fd), "wb") as stream:
                        stream.write(TIMEOUT_BYTES)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(temporary, TIMEOUT_FILE, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
                finally:
                    os.close(fd)
                    os.unlink(temporary, dir_fd=dfd)
                os.fsync(dfd)
                previous = _snapshot(path, budget)
            if previous[1] != TIMEOUT_BYTES:
                raise JournalStateError("foreign journal timeout configuration")
    return previous


def _show(unit, budget, properties):
    raw = budget.command(["show", "--all", "--no-pager", "--property=" + ",".join(properties), "--", unit])
    values = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in properties or key in values:
            raise JournalStateError("ambiguous journal completion metadata")
        values[key] = value
    if set(values) != set(properties) or values["Id"] != unit:
        raise JournalStateError("incomplete journal completion metadata")
    return values


def _existing(unit, arguments, budget):
    common = _show(unit, budget, peer.COMMON_PROPERTIES)
    if common["LoadState"] == "not-found":
        if (common["ActiveState"], common["SubState"], common["Job"]) != ("inactive", "dead", ""):
            raise JournalStateError("contradictory absent completion service")
        return None
    if common["LoadState"] != "loaded":
        raise JournalStateError("foreign journal completion load state")
    path = TRANSIENT / unit
    timeout = _timeout_path(unit)
    before = _snapshot(path, budget), _snapshot(timeout, budget)
    values = _show(unit, budget, PROPERTIES)
    required = {"LoadState": "loaded", "Transient": "yes", "FragmentPath": str(path),
                "NeedDaemonReload": "no", "Type": "oneshot", "User": "root", "Group": "root",
                "DynamicUser": "no", "Restart": "no", "KillMode": "control-group", "Environment": "",
                "TimeoutStartUSec": "16min", "JobTimeoutUSec": "16min"}
    if any(values[key] != expected for key, expected in required.items()):
        raise JournalStateError("foreign journal completion configuration")
    try:
        dropins = shlex.split(values["DropInPaths"])
        after = shlex.split(values["After"])
    except ValueError as exc:
        raise JournalStateError("invalid completion relation encoding") from exc
    if dropins != [str(timeout)] or len(after) != len(set(after)) \
            or not set(AFTER) <= set(after) or set(after) - set(AFTER) - DEFAULT_AFTER:
        raise JournalStateError("foreign journal completion drop-ins or ordering")
    if values["ActiveState"] not in {"active", "activating", "inactive", "failed"} \
            or values["SubState"] not in {"running", "start", "exited", "dead", "failed"} \
            or (values["Job"] and not values["Job"].isdecimal()):
        raise JournalStateError("unsupported completion service state")
    if before[0][1] != fragment(arguments) or before[1][1] != TIMEOUT_BYTES:
        raise JournalStateError("foreign journal completion command or timeout")
    if before != (_snapshot(path, budget), _snapshot(timeout, budget)):
        raise JournalStateError("journal completion configuration changed")
    return values


def enqueue(encoded, *, deadline, budget=None):
    unit = request.unit_name(encoded)
    arguments = argv(encoded)
    if budget is None:
        budget = Budget(deadline)
    existing = _existing(unit, arguments, budget)
    if existing is not None:
        if existing["Job"] or existing["ActiveState"] in {"active", "activating"}:
            return unit
        budget.command(["--no-block", "start", "--", unit])
        _existing(unit, arguments, budget)
        return unit
    _publish_timeout(unit, budget)
    budget.command(["daemon-reload"])
    budget.command(["--system", "--no-block", "--unit=" + unit, "--description=" + DESCRIPTION,
                    "--property=Type=oneshot", "--property=User=root", "--property=Group=root",
                    "--property=KillMode=control-group", "--property=Restart=no",
                    "--property=TimeoutStartSec=960s", "--property=After=" + " ".join(AFTER),
                    "--expand-environment=no", "--", *arguments], executable="systemd-run")
    if _existing(unit, arguments, budget) is None:
        raise JournalStateError("accepted completion service disappeared")
    return unit
