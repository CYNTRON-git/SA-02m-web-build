"""Bounded installed-source identity for journal completion."""

import hashlib
import importlib
import os
from pathlib import Path
import stat
import sys
import time

from . import event_journal_delivery as files
from .event_journal_state import JournalStateError

ENTRYPOINTS = ("sa02m_devices_journal_startup.py",)


def installed_root():
    return Path(__file__).absolute().parents[1]


def inventory(root, deadline):
    root = files.canonical_directory(root)
    result, total = {}, 0

    def check_deadline():
        if time.monotonic() >= deadline:
            raise JournalStateError("journal source probe deadline expired")

    def read(dfd, name, relative):
        nonlocal total
        check_deadline()
        if len(result) >= 128:
            raise JournalStateError("journal source inventory exceeded file bound")
        fd = files.open_file(dfd, name, 0, private=False)
        try:
            before = os.fstat(fd)
            data = bytearray()
            while True:
                check_deadline()
                block = os.read(fd, min(65536, 4194304 - total + 1))
                if not block:
                    break
                total += len(block)
                if total > 4194304:
                    raise JournalStateError("journal source inventory exceeded byte bound")
                data.extend(block)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) \
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise JournalStateError("journal source changed while reading")
            result[relative] = bytes(data)
        finally:
            os.close(fd)

    def walk(path, relative):
        check_deadline()
        with files.directory(path, 0, private=False) as dfd:
            names = sorted(os.listdir(dfd))
            if len(names) > 256:
                raise JournalStateError("journal source directory exceeded entry bound")
            for name in names:
                if name == "__pycache__":
                    continue
                info = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                child = relative + "/" + name
                if stat.S_ISDIR(info.st_mode):
                    walk(path / name, child)
                elif name.endswith(".py"):
                    read(dfd, name, child)
                elif stat.S_ISLNK(info.st_mode):
                    raise JournalStateError("journal source contains a symlink")

    with files.directory(root, 0, private=False) as dfd:
        for name in ENTRYPOINTS:
            read(dfd, name, name)
    walk(root / "sa02m_devices", "sa02m_devices")
    if "sa02m_devices/__init__.py" not in result:
        raise JournalStateError("journal source package is incomplete")
    digest = hashlib.sha256()
    for name, data in sorted(result.items()):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest(), result


def verified_fingerprint(root, deadline):
    fingerprint, sources = inventory(root, deadline)
    api = importlib.import_module("sa02m_devices.api")
    if getattr(api, "EVENTS_JOURNAL_PROTOCOL", None) != 1 or any(
            not callable(getattr(api, name, None)) for name in ("handle_events", "handle_events_clear", "handle_history")):
        raise JournalStateError("journal installed API protocol is incompatible")
    for name, module in tuple(sys.modules.items()):
        if name != "sa02m_devices" and not name.startswith("sa02m_devices."):
            continue
        expected = root / (name.replace(".", "/") + ("/__init__.py" if hasattr(module, "__path__") else ".py"))
        try:
            relative = str(expected.relative_to(root))
            actual = Path(module.__file__)
            origin = Path(module.__spec__.origin)
        except (AttributeError, TypeError, ValueError) as exc:
            raise JournalStateError("journal imported source origin unavailable") from exc
        if relative not in sources or actual != expected or origin != expected \
                or actual.resolve() != expected or origin.resolve() != expected:
            raise JournalStateError("journal imported package is outside installed source")
    if inventory(root, deadline)[0] != fingerprint:
        raise JournalStateError("journal source changed during import verification")
    return fingerprint
