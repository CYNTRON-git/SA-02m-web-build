"""Durable journal generations, observation provenance and ordered POSIX guards."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
import threading
import time
from typing import Callable, Iterator


DEFAULT_DIRECTORY = Path("/var/lib/sa02m-devices/event-journal")
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
log = logging.getLogger("sa02m-devices-journal")


class JournalStateError(RuntimeError):
    """The journal authority cannot be trusted; recovery is administrative."""


class JournalBusy(TimeoutError):
    """The shared acquisition deadline expired."""


@dataclass(frozen=True)
class Domain:
    directory: Path
    standalone: bool = False
    archive: Path | None = None

    @property
    def owner(self) -> int:
        return os.geteuid() if self.standalone else 0


@dataclass(frozen=True)
class State:
    identity: str
    generation: str
    ready: bool


@dataclass(frozen=True)
class Capture:
    domain: Domain | None
    identity: str | None
    generation: str | None


_registry_lock = threading.Lock()
_mutexes: dict[Path, threading.RLock] = {}
_local = threading.local()
_diagnostics: dict[Path | None, float] = {}
_guard_fds: set[int] = set()


def _after_fork() -> None:
    global _registry_lock, _mutexes, _local, _diagnostics, _guard_fds
    # Closing the child's references preserves the parent's open-description
    # lock. LOCK_UN here would incorrectly unlock the parent's guard too.
    for fd in _guard_fds:
        os.close(fd)
    _guard_fds = set()
    _registry_lock = threading.Lock()
    _mutexes, _diagnostics = {}, {}
    _local = threading.local()


os.register_at_fork(after_in_child=_after_fork)


def domain_for(path: Path | None = None) -> Domain:
    configured = os.environ.get("STAND_DEVICES_EVENTS_STATE_DIR", "").strip()
    if configured:
        directory = Path(configured)
        if not directory.is_absolute():
            raise JournalStateError("journal state directory must be absolute")
        if directory.is_symlink():
            raise JournalStateError("journal state directory cannot be a symlink")
        return Domain(directory.resolve())
    forced = os.environ.get("STAND_DEVICES_HISTORY_DB", "").strip()
    if path is not None or forced:
        archive = Path(path if path is not None else forced).resolve()
        return Domain(Path(str(archive) + ".events-state"), True, archive)
    return Domain(DEFAULT_DIRECTORY)


def valid_generation(value: object) -> bool:
    return isinstance(value, str) and (value == "legacy" or bool(_TOKEN.fullmatch(value)))


def accepts(state: State, generation: str) -> bool:
    if not valid_generation(generation):
        raise JournalStateError("invalid event generation")
    return state.generation == "legacy" or generation == state.generation


def eligible(capture: Capture, destination: State | None,
             states: dict[Domain, State | None]) -> bool:
    origin = states.get(capture.domain)
    return bool(origin and destination and capture.identity == origin.identity
                and capture.generation == origin.generation
                and accepts(destination, capture.generation))


def _verify(info: os.stat_result, owner: int, *, directory: bool = False) -> None:
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or info.st_uid != owner or info.st_mode & 0o077:
        raise JournalStateError("unsafe journal authority type, owner or permissions")


@contextmanager
def _directory(domain: Domain) -> Iterator[int]:
    try:
        fd = os.open(domain.directory, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    except OSError as exc:
        raise JournalStateError(f"journal directory unavailable: {exc}") from exc
    try:
        _verify(os.fstat(fd), domain.owner, directory=True)
        yield fd
    finally:
        os.close(fd)


def _pairs(items: list[tuple]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise JournalStateError("duplicate journal state key")
        result[key] = value
    return result


def _read(fd: int, owner: int) -> dict:
    _verify(os.fstat(fd), owner)
    raw = os.pread(fd, 1025, 0)
    if len(raw) > 1024:
        raise JournalStateError("oversized journal state")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeError, ValueError) as exc:
        raise JournalStateError("malformed journal state") from exc
    if not isinstance(value, dict):
        raise JournalStateError("journal state must be an object")
    return value


def _identity(value: dict) -> str:
    if set(value) != {"version", "identity"} or type(value["version"]) is not int \
            or value["version"] != 1 or not isinstance(value["identity"], str) \
            or not _TOKEN.fullmatch(value["identity"]):
        raise JournalStateError("invalid journal guard identity")
    return value["identity"]


def _state(value: dict, identity: str) -> State:
    if set(value) != {"version", "identity", "generation", "ready"} \
            or type(value["version"]) is not int or value["version"] != 1 \
            or value["identity"] != identity or not valid_generation(value["generation"]) \
            or type(value["ready"]) is not bool:
        raise JournalStateError("invalid journal authority record")
    return State(identity, value["generation"], value["ready"])


def _read_child(dfd: int, name: str, owner: int) -> dict:
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        try:
            return _read(fd, owner)
        finally:
            os.close(fd)
    except OSError as exc:
        raise JournalStateError(f"journal {name} unavailable: {exc}") from exc


def read_atomic(domain: Domain) -> State:
    """Read only: legacy conn= callers must never wait behind a SQLite holder."""
    with _directory(domain) as dfd:
        identity = _identity(_read_child(dfd, "guard.lock", domain.owner))
        return _state(_read_child(dfd, "state.json", domain.owner), identity)


def _write_bytes(fd: int, value: dict) -> None:
    raw = (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")
    with os.fdopen(os.dup(fd), "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _publish(dfd: int, state: State) -> None:
    name = ".state-" + secrets.token_hex(16)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=dfd)
    try:
        _write_bytes(fd, {"version": 1, "identity": state.identity,
                          "generation": state.generation, "ready": state.ready})
        os.replace(name, "state.json", src_dir_fd=dfd, dst_dir_fd=dfd)
        os.fsync(dfd)
    finally:
        os.close(fd)
        try:
            os.unlink(name, dir_fd=dfd)
        except FileNotFoundError:
            pass


def _has_tagged_rows(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(device_events)")}
        if "journal_generation" not in columns:
            return False
        return bool(conn.execute(
            "SELECT 1 FROM device_events WHERE journal_generation < 'legacy' "
            "OR journal_generation > 'legacy' LIMIT 1"
        ).fetchone())
    finally:
        conn.close()


def bootstrap(domain: Domain, *, ready: bool = False, recovery: bool = False,
              deadline: float | None = None) -> State:
    """Explicit first install; standalone callers may invoke this on first use.

    Publish a complete directory so concurrent first users never see partial
    creation. The parent flock serializes creators without a replaceable lock.
    An already-existing partial directory is always an administrative fault.
    """
    if os.geteuid() != domain.owner:
        raise JournalStateError("journal initialization requires its owner")
    deadline = time.monotonic() + 5.0 if deadline is None else deadline
    domain.directory.parent.mkdir(parents=True, exist_ok=True)
    parent = os.open(domain.directory.parent, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    temporary = None
    try:
        _flock(parent, True, deadline)
        if os.path.lexists(domain.directory):
            return read_atomic(domain)
        if not recovery and domain.standalone and _has_tagged_rows(domain.archive):
            raise JournalStateError("tagged archive lost its journal authority")
        temporary = Path(tempfile.mkdtemp(prefix=".journal-init-", dir=domain.directory.parent))
        identity = secrets.token_hex(16)
        state = State(identity, secrets.token_hex(16) if recovery else "legacy", ready)
        dfd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
        try:
            fd = os.open("guard.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dfd)
            try:
                _write_bytes(fd, {"version": 1, "identity": identity})
            finally:
                os.close(fd)
            _publish(dfd, state)
        finally:
            os.close(dfd)
        os.rename(temporary, domain.directory)
        temporary = None
        os.fsync(parent)
        return state
    finally:
        if temporary is not None:
            for child in temporary.iterdir():
                child.unlink()
            temporary.rmdir()
        os.close(parent)


def remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _flock(fd: int, exclusive: bool, deadline: float) -> None:
    while True:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            delay = remaining(deadline)
            if delay <= 0:
                raise JournalBusy("journal guard busy") from None
            time.sleep(min(0.01, delay))


@contextmanager
def guard(domain: Domain, *, exclusive: bool = False, deadline: float | None = None,
          allow_invalid: bool = False) -> Iterator[State | None]:
    deadline = time.monotonic() + 5.0 if deadline is None else deadline
    held = getattr(_local, "held", None)
    if held is None:
        _local.held = held = {}
    if domain.directory in held:
        state, outer_exclusive = held[domain.directory]
        if exclusive and not outer_exclusive:
            raise JournalStateError("journal guard upgrade is forbidden")
        yield state
        return
    with _registry_lock:
        mutex = _mutexes.setdefault(domain.directory, threading.RLock())
    if not mutex.acquire(timeout=remaining(deadline)):
        raise JournalBusy("journal thread guard busy")
    try:
        if domain.standalone and not os.path.lexists(domain.directory):
            bootstrap(domain, ready=True, deadline=deadline)
        with _directory(domain) as dfd:
            try:
                fd = os.open("guard.lock", os.O_RDWR | _NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            except OSError as exc:
                raise JournalStateError(f"journal guard unavailable: {exc}") from exc
            try:
                _guard_fds.add(fd)
                identity = _identity(_read(fd, domain.owner))
                _flock(fd, exclusive, deadline)
                try:
                    state = _state(_read_child(dfd, "state.json", domain.owner), identity)
                except JournalStateError:
                    if not allow_invalid:
                        raise
                    state = None
                held[domain.directory] = (state, exclusive)
                try:
                    yield state
                finally:
                    held.pop(domain.directory, None)
            finally:
                _guard_fds.discard(fd)
                os.close(fd)
    finally:
        mutex.release()


@contextmanager
def guards(domains, *, exclusive: bool = False, tolerate_invalid: bool = False):
    states = {None: None}
    deadline = time.monotonic() + 5.0
    with ExitStack() as stack:
        for domain in sorted({d for d in domains if d is not None}, key=lambda d: str(d.directory)):
            try:
                states[domain] = stack.enter_context(guard(
                    domain, exclusive=exclusive, deadline=deadline,
                    allow_invalid=tolerate_invalid,
                ))
                if states[domain] is None:
                    diagnostic(domain, JournalStateError("untrusted authority; samples only"))
            except JournalStateError as exc:
                if not tolerate_invalid:
                    raise
                diagnostic(domain, exc)
                states[domain] = None
        yield states


def diagnostic(domain: Domain | None, error: Exception | None) -> None:
    now = time.monotonic()
    directory = domain.directory if domain else None
    previous = _diagnostics.get(directory)
    if error is None:
        if previous is not None:
            log.warning("journal authority recovered: %s", directory)
            _diagnostics.pop(directory, None)
    elif previous is None or now - previous >= 60.0:
        log.warning("journal events unavailable at %s: %s", directory, error)
        _diagnostics[directory] = now


@contextmanager
def observation(path: Path | None = None) -> Iterator[Capture]:
    try:
        domain = domain_for(path)
    except JournalStateError as exc:
        diagnostic(None, exc)
        yield Capture(None, None, None)
        return
    with guards([domain], tolerate_invalid=True) as states:
        state = states[domain]
        if state is None:
            diagnostic(domain, JournalStateError("untrusted authority; samples only"))
        else:
            diagnostic(domain, None)
        yield Capture(domain, state.identity if state else None, state.generation if state else None)


def capture(path: Path | None = None) -> Capture:
    with observation(path) as result:
        return result


@contextmanager
def event_operation(path: Path | None, *, external_connection: bool = False):
    domain = domain_for(path)
    held = getattr(_local, "held", {})
    if domain.directory in held:
        state = held[domain.directory][0]
        if state is None:
            raise JournalStateError("untrusted journal authority")
        yield state
    elif external_connection:
        yield read_atomic(domain)
    else:
        with guard(domain) as state:
            yield state


def clear(domain: Domain, targets: Callable[[], list[Path]], *, timeout: float) -> int:
    if domain.standalone and not os.path.lexists(domain.directory) \
            and domain.archive is not None and not domain.archive.exists():
        return 0
    deadline = time.monotonic() + max(0.0, timeout)
    with guard(domain, exclusive=True, deadline=deadline) as state:
        if not state.ready:
            raise JournalStateError("journal installation is not ready")
        connections = []
        seen = set()
        total = 0
        try:
            for path in sorted({p.resolve() for p in targets()}, key=str):
                try:
                    info = path.stat()
                except FileNotFoundError:
                    continue
                identity = (info.st_dev, info.st_ino)
                if identity in seen:
                    continue
                seen.add(identity)
                conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                       timeout=remaining(deadline), isolation_level=None)
                connections.append(conn)
                conn.execute("BEGIN IMMEDIATE")
            for conn in connections:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='device_events'").fetchone():
                    total += conn.execute("DELETE FROM device_events").rowcount
            for conn in connections:
                conn.execute("COMMIT")
            with _directory(domain) as dfd:
                updated = replace(state, generation=secrets.token_hex(16))
                _publish(dfd, updated)
                _local.held[domain.directory] = (updated, True)
            return total
        finally:
            for conn in connections:
                conn.close()


def set_ready(domain: Domain, ready: bool, *, deadline: float | None = None) -> State:
    with guard(domain, exclusive=True, deadline=deadline) as state:
        if state.ready == ready:
            return state
        state = replace(state, ready=ready)
        with _directory(domain) as dfd:
            _publish(dfd, state)
            _local.held[domain.directory] = (state, True)
        return state


def recover(domain: Domain, *, deadline: float | None = None) -> State:
    """Consumers must be stopped. Never restore a legacy generation."""
    deadline = time.monotonic() + 5.0 if deadline is None else deadline
    if not os.path.lexists(domain.directory):
        return bootstrap(domain, ready=False, recovery=True, deadline=deadline)
    with _directory(domain) as dfd:
        fd = os.open("guard.lock", os.O_RDWR | os.O_CREAT | _NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=dfd)
        try:
            _verify(os.fstat(fd), domain.owner)
            _flock(fd, True, deadline)
            state = State(secrets.token_hex(16), secrets.token_hex(16), False)
            os.ftruncate(fd, 0)
            _write_bytes(fd, {"version": 1, "identity": state.identity})
            _publish(dfd, state)
            return state
        finally:
            os.close(fd)


def initialize_recorded(domain: Domain, identity: str, *, deadline: float | None = None) -> State:
    """Finish only the first initialization authorized by a durable receipt.

    The caller owns that receipt and its mutex. Never use this from an event
    operation: a published random generation or readiness cannot be reset.
    """
    _identity({"version": 1, "identity": identity})
    if os.geteuid() != domain.owner:
        raise JournalStateError("journal initialization requires its owner")
    deadline = time.monotonic() + 5.0 if deadline is None else deadline
    domain.directory.parent.mkdir(parents=True, exist_ok=True)
    parent = os.open(domain.directory.parent, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    try:
        _flock(parent, True, deadline)
        if not os.path.lexists(domain.directory):
            domain.directory.mkdir(mode=0o700)
            os.fsync(parent)
        with _directory(domain) as dfd:
            for name in os.listdir(dfd):
                if name not in ("guard.lock", "state.json") and not re.fullmatch(r"\.state-[0-9a-f]{32}", name):
                    raise JournalStateError("unexpected journal initialization artifact")
            has_state = os.path.lexists(domain.directory / "state.json")
            try:
                fd = os.open("guard.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=dfd)
            except FileExistsError:
                fd = os.open("guard.lock", os.O_RDWR | _NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            try:
                _verify(os.fstat(fd), domain.owner)
                _flock(fd, True, deadline)
                if os.fstat(fd).st_size == 0 and not has_state:
                    _write_bytes(fd, {"version": 1, "identity": identity})
                if _identity(_read(fd, domain.owner)) != identity:
                    raise JournalStateError("journal initialization identity mismatch")
                expected = State(identity, "legacy", False)
                if has_state:
                    actual = _state(_read_child(dfd, "state.json", domain.owner), identity)
                    if actual != expected:
                        raise JournalStateError("journal initialization already completed")
                    return actual
                _publish(dfd, expected)
                return expected
            finally:
                os.close(fd)
    finally:
        os.close(parent)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("bootstrap", "validate", "ready", "not-ready"))
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if not args.directory.is_absolute():
        parser.error("--directory must be absolute")
    domain = Domain(args.directory)
    try:
        if args.action == "bootstrap":
            bootstrap(domain)
        elif args.action == "validate":
            read_atomic(domain)
        else:
            set_ready(domain, args.action == "ready")
    except (JournalStateError, JournalBusy, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"journal state: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
