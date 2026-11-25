"""Trusted local files and the updater's outer journal-clear guard."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat

from .event_journal_state import JournalBusy, JournalStateError

NOFOLLOW = os.O_NOFOLLOW


def canonical_directory(value):
    path = Path(value)
    if not path.is_absolute() or str(path) != str(path.resolve()):
        raise JournalStateError("journal configuration requires a canonical absolute path")
    return path


def verify(info, owner, *, directory=False, private=True):
    correct = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct or info.st_uid != owner or info.st_mode & (0o077 if private else 0o022):
        raise JournalStateError("unsafe delivery file type, owner or permissions")
    if not directory and info.st_nlink != 1:
        raise JournalStateError("delivery file must have exactly one link")


@contextmanager
def directory(path, owner, *, create=False, private=True):
    path = canonical_directory(path)
    if create:
        try:
            path.mkdir(mode=0o700, parents=True)
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except FileExistsError:
            pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
    try:
        verify(os.fstat(fd), owner, directory=True, private=private)
        yield fd
    finally:
        os.close(fd)


def open_file(dfd, name, owner, *, create=False, private=True):
    flags = (os.O_RDWR if create else os.O_RDONLY) | NOFOLLOW | os.O_NONBLOCK
    created = False
    if create:
        try:
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL,
                         0o600 if private else 0o644, dir_fd=dfd)
            created = True
        except FileExistsError:
            fd = os.open(name, flags, dir_fd=dfd)
    else:
        fd = os.open(name, flags, dir_fd=dfd)
    try:
        verify(os.fstat(fd), owner, private=private)
        if created and not private:
            # The updater's non-owner PID reader needs 0644 even under umask 077.
            os.fchmod(fd, 0o644)
        return fd
    except BaseException:
        os.close(fd)
        raise


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JournalStateError("duplicate delivery JSON key")
        result[key] = value
    return result


def read_json(dfd, name, owner, *, limit=2048, private=True):
    try:
        fd = open_file(dfd, name, owner, private=private)
    except FileNotFoundError:
        return None
    try:
        raw = os.pread(fd, limit + 1, 0)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise JournalStateError("oversized delivery JSON")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError) as exc:
        raise JournalStateError("malformed delivery JSON") from exc
    if not isinstance(value, dict):
        raise JournalStateError("delivery JSON must be an object")
    return value


def publish_json(dfd, name, value):
    temporary = ".receipt-" + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
                 0o600, dir_fd=dfd)
    try:
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        os.fsync(dfd)
    finally:
        os.close(fd)
        try:
            os.unlink(temporary, dir_fd=dfd)
        except FileNotFoundError:
            pass


def _transaction_ready(value):
    # Consumer of transaction.py's v1 journal, without importing its global lib package.
    stages = {"idle", "uploaded", "validating", "backing_up", "applying", "verifying",
              "committing", "done", "rolling_back", "rolled_back", "error", "cancelled"}
    operation = value.get("operation")
    stage = value.get("stage")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1 \
            or not all(isinstance(value.get(key), str) for key in ("operation", "stage", "result", "source")) \
            or operation not in {"update", "factory_reset"} \
            or value.get("result") not in {"pending", "success", "failed", "rolled_back", "cancelled"} \
            or value.get("source") not in ({"file", "github", "web"} if operation == "factory_reset"
                                           else {"file", "github"}) \
            or stage not in (stages | {"confirmed", "wipe", "apply", "verify"}
                             if operation == "factory_reset" else stages):
        raise JournalStateError("invalid updater transaction")
    stable = {"idle", "uploaded", "done", "rolled_back", "cancelled", "error"}
    if stage not in stable or (operation == "factory_reset" and stage not in {"done", "rolled_back", "cancelled"}):
        raise JournalBusy("journal clear waits for update recovery")


@contextmanager
def clear_preflight():
    """Hold updater SH through durable clear publication; standalone calls do no I/O."""
    configured = os.environ.get("STAND_DEVICES_EVENTS_UPDATE_DIR", "").strip()
    if not configured:
        yield
        return
    try:
        with directory(configured, 0, private=False) as dfd:
            fd = open_file(dfd, "update.lock", 0, create=True, private=False)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise JournalBusy("journal clear waits for update completion") from None
                marker = Path(os.environ.get("SA02M_IMAGING_LOCK", "/run/sa02m-imaging.lock"))
                if not marker.is_absolute():
                    raise JournalStateError("SA02M_IMAGING_LOCK must be absolute")
                if os.path.lexists(marker):
                    raise JournalBusy("journal clear waits for image maintenance")
                transaction = read_json(dfd, "transaction.json", 0, limit=65536, private=False)
                if transaction is not None:
                    _transaction_ready(transaction)
                yield
            finally:
                os.close(fd)
    except JournalBusy:
        raise
    except OSError as exc:
        raise JournalStateError("updater journal unavailable") from exc
