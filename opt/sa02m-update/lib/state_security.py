"""Replace pre-controller writable journals without trusting their contents."""
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from . import PackageError

STAMP = "controller-state-v1"
# The owner the update state must carry, and the uid the migration must run as:
# root on the board; nothing here assigns them. A test run by an unprivileged
# user rebinds both to its own ids (scripts/dev/test-update-control-boundaries.py)
# so the same ownership checks and repairs run against a state it can create.
OWNER_UID = 0
OWNER_GID = 0


def require_secure_state(state):
    state = Path(state)
    for path, directory in ((state, True), (state / STAMP, False)):
        try:
            st = path.lstat()
        except OSError as exc:
            raise PackageError("E_BOOTSTRAP_REQUIRED", "run install.sh --refresh to secure update state") from exc
        kind = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
        if not kind or st.st_uid != OWNER_UID or st.st_mode & 0o022 or (not directory and st.st_nlink != 1):
            raise PackageError("E_BOOTSTRAP_REQUIRED", "unsafe update state; run install.sh --refresh")
    incoming = state / "incoming"
    if incoming.exists() or incoming.is_symlink():
        st = incoming.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != OWNER_UID or st.st_mode & 0o077:
            raise PackageError("E_BOOTSTRAP_REQUIRED", "unsafe incoming directory; run install.sh --refresh")
    for name in ("transaction.json", "update.lock", "control.lock", "cancel"):
        path = state / name
        if not path.exists() and not path.is_symlink():
            continue
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != OWNER_UID or st.st_mode & 0o022 or st.st_nlink != 1:
            raise PackageError("E_BOOTSTRAP_REQUIRED", "unsafe update journal; run install.sh --refresh")


def migrate(state=Path("/var/lib/sa02m-update")):
    with ExitStack() as stack:
        return _migrate(state, stack)


def _migrate(state, stack):
    if os.geteuid() != OWNER_UID:
        raise PermissionError("root required")
    state = Path(state)
    secure = False
    try:
        require_secure_state(state)
        secure = True
    except PackageError:
        pass
    # Never inspect or repair a transaction editable by the old CGI user.
    for unit in ("sa02m-update", "sa02m-update-download", "sa02m-update-verify", "sa02m-update-recover", "sa02m-factory-reset"):
        result = subprocess.run(["/usr/bin/systemctl", "show", "-p", "ActiveState", "--value", unit],
                                capture_output=True, text=True, timeout=10)
        if result.stdout.strip() == "active" and unit == "sa02m-update-recover":
            sub = subprocess.run(["/usr/bin/systemctl", "show", "-p", "SubState", "--value", unit],
                                 capture_output=True, text=True, timeout=10)
            status = subprocess.run(["/usr/bin/systemctl", "show", "-p", "ExecMainStatus", "--value", unit],
                                    capture_output=True, text=True, timeout=10)
            if sub.returncode == status.returncode == 0 and sub.stdout.strip() == "exited" and status.stdout.strip() == "0":
                continue
        if result.stdout.strip() in {"active", "activating", "reloading", "deactivating"}:
            raise RuntimeError("cannot migrate state while update unit is active")
    transient = subprocess.run(["/usr/bin/systemctl", "list-units", "--plain", "--no-legend", "sa02m-update-apply-*.service"],
                               capture_output=True, text=True, timeout=10)
    if any(word in transient.stdout for word in ("running", "activating")):
        raise RuntimeError("transient update runner active")
    if state.is_symlink():
        raise RuntimeError("unsafe state symlink requires explicit recovery")
    held = None
    if state.exists():
        if not secure:
            os.chown(state, OWNER_UID, OWNER_GID)
            os.chmod(state, 0o755)
        if (state / "maintenance").exists() or (state / "maintenance").is_symlink():
            raise RuntimeError("unfinished maintenance requires recovery before refresh")
        journal = state / "transaction.json"
        if journal.exists() or journal.is_symlink():
            st = journal.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 65536:
                raise RuntimeError("unsafe journal requires explicit recovery")
            try:
                data = json.loads(journal.read_text())
                stage = data["stage"]
                if data.get("operation") == "factory_reset" and stage not in {"done", "rolled_back", "cancelled", "idle"}:
                    raise RuntimeError("factory reset requires explicit recovery")
                if data.get("port_restore_pending"):
                    raise RuntimeError("unfinished service restoration requires recovery")
            except (ValueError, KeyError, UnicodeError, TypeError):
                raise RuntimeError("ambiguous journal requires explicit recovery")
            if stage not in {"idle", "uploaded", "done", "error", "rolled_back", "cancelled"}:
                raise RuntimeError("unfinished transaction requires recovery before refresh")
        lock_path = state / "update.lock"
        # 0644, not 0600: the CGI liveness detector (lib_web_update.sh, www-data)
        # reads the runner pid from this file; a 0600 lock blinded it on every
        # bootstrapped board. Root alone writes it (require_secure_state pins g/o-w).
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        os.fchmod(fd, 0o644)
        held = stack.enter_context(os.fdopen(fd, "a+b"))
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if secure:
        if held is not None:
            held.close()
        return
    state.parent.mkdir(parents=True, exist_ok=True)
    with (state.parent / ".sa02m-update-state-migration.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if state.exists() or state.is_symlink():
            # Rename the directory entry, never follow a hostile symlink.
            quarantine = state.with_name(state.name+".untrusted-"+str(time.time_ns()))
            state.rename(quarantine)
            if not quarantine.is_symlink():
                os.chown(quarantine, OWNER_UID, OWNER_GID)
                os.chmod(quarantine, 0o700)
        state.mkdir(mode=0o755)
        os.chown(state, OWNER_UID, OWNER_GID)
        (state / "incoming").mkdir(mode=0o700)
        stamp = state / STAMP
        stamp.write_text("1\n")
        os.chmod(stamp, 0o644)
        with stamp.open("rb") as f:
            os.fsync(f.fileno())
        fd = os.open(state, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if held is not None:
            held.close()
