"""Privileged, fixed-path update operations. CGI authentication precedes this boundary."""
from __future__ import annotations

import fcntl
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

from . import PackageError
from . import transaction as txnmod
from .upload_receive import receive_multipart_file
from .state_security import require_secure_state

TERMINAL = {"idle", "done", "error", "rolled_back", "cancelled"}


@contextmanager
def control_lock(statedir):
    statedir.mkdir(parents=True, exist_ok=True)
    with (statedir / "control.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PackageError("E_LOCK", "update control busy") from exc
        yield


class Controller:
    def __init__(self, statedir=Path("/var/lib/sa02m-update"),
                 inspect="/usr/local/libexec/sa02m-update-inspect",
                 systemctl="/usr/bin/systemctl", require_secure=True):
        self.state = Path(statedir)
        self.require_secure = require_secure
        self.package = self.state / "incoming/package.sa02m"
        self.inspect_cmd = inspect
        self.systemctl = systemctl

    def inspect(self):
        proc = subprocess.run([self.inspect_cmd, str(self.package)], capture_output=True,
                              timeout=120, check=False)
        try:
            obj = json.loads(proc.stdout)
        except (ValueError, UnicodeError) as exc:
            raise PackageError("E_CMD", "package inspector failed") from exc
        if not obj.get("ok"):
            raise PackageError(obj.get("error_code") or "E_TAR",
                               obj.get("error_message") or "package rejected")
        return obj

    def busy(self, txn):
        return txn and txn.get("stage") not in TERMINAL | {"uploaded"}

    def accepted(self, txn):
        return {"ok": True, "http_status": 202, "transaction_id": txn["id"],
                "stage": txn["stage"], "status": "running", "legacy": {"status": "running"}}

    def start(self, txn, unit):
        txnmod.save_transaction(txn, self.state)
        try:
            proc = subprocess.run([self.systemctl, "start", "--no-block", unit],
                                  capture_output=True, timeout=15, check=False)
        except subprocess.TimeoutExpired as exc:
            # The system manager may have queued the job; retain its reservation.
            raise PackageError("E_RECOVERY_REQUIRED", "start outcome unknown; inspect the reserved transaction") from exc
        except OSError as exc:
            txnmod.update_stage(self.state, "error", result="failed", error_code="E_CMD",
                                error_message="systemd launcher unavailable")
            raise PackageError("E_CMD", "systemd launcher unavailable") from exc
        if proc.returncode:
            txnmod.update_stage(self.state, "error", result="failed", error_code="E_CMD",
                                error_message="systemd rejected update start")
            raise PackageError("E_CMD", "systemd rejected update start")
        return self.accepted(txn)

    def cancel(self, txn):
        if not txn or txn.get("operation") == "factory_reset" or txn["stage"] not in txnmod.CANCEL_ALLOWED_STAGES:
            raise PackageError("E_CANCEL", "cancellation is no longer allowed")
        if txn["stage"] in {"idle", "uploaded"}:
            txnmod.update_stage(self.state, "cancelled", result="cancelled",
                                error_code="E_CANCEL", error_message="cancelled by operator")
            self.package.unlink(missing_ok=True)
            return {"ok": True, "stage": "cancelled", "transaction_id": txn["id"]}
        # The runner consumes this under control.lock immediately before applying.
        # Do not stop its process or rewrite its journal concurrently.
        txnmod.atomic_write_json(self.state / "cancel", {"transaction_id": txn["id"]}, mode=0o600)
        return {"ok": True, "cancel_requested": True, "stage": txn["stage"],
                "transaction_id": txn["id"]}

    def execute(self, args, stream):
        if not args or args[0] not in {"upload", "inspect", "apply", "online", "cancel", "factory-reset"}:
            raise PackageError("E_CMD", "invalid operation")
        command = args[0]
        counts = {"upload": 3, "apply": 2, "inspect": 1, "online": 1, "cancel": 1, "factory-reset": 3}
        if len(args) != counts[command]:
            raise PackageError("E_CMD", "invalid arguments")
        if command == "factory-reset" and args[1:] != ["SA02M-RESET", "true"]:
            raise PackageError("E_CANCEL", "exact confirmation and mandatory backup required")
        if self.require_secure:
            require_secure_state(self.state)
            if command in {"apply", "online"}:
                try:
                    p = subprocess.run(["/usr/local/libexec/sa02m-update-runner", "capabilities"],
                                       capture_output=True, timeout=15)
                    caps = json.loads(p.stdout)
                except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError):
                    caps = {}
                if not caps.get("control_v1") or not caps.get("port_maintenance_v1"):
                    raise PackageError("E_BOOTSTRAP_REQUIRED", "run install.sh --refresh before updating")
        with control_lock(self.state):
            txn = txnmod.load_transaction(self.state)
            if txn and txn.get("operation") == "factory_reset" and txn.get("stage") == "error":
                raise PackageError("E_RECOVERY_REQUIRED", "factory reset requires explicit root recovery")
            if not self.busy(txn) and ((self.state / "maintenance").exists() or (self.state / "maintenance").is_symlink()):
                raise PackageError("E_RECOVERY_REQUIRED", "unfinished maintenance requires recovery")
            if command == "cancel":
                return self.cancel(txn)
            if self.busy(txn):
                same = ((command == "apply" and txn.get("source") == "file"
                         and txn.get("target_version") == args[1])
                        or (command == "online" and txn.get("source") == "github")
                        or (command == "factory-reset" and txn.get("operation") == "factory_reset"))
                if same:
                    try:
                        age = (datetime.now(timezone.utc) - datetime.strptime(txn["updated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()
                    except (KeyError, ValueError):
                        age = 121
                    if age > 120:
                        alive = False
                        for unit in ("sa02m-update", "sa02m-update-download", "sa02m-update-verify", "sa02m-factory-reset"):
                            proc = subprocess.run([self.systemctl, "show", "-p", "ActiveState", "--value", unit], capture_output=True, text=True, timeout=10)
                            alive |= proc.stdout.strip() in {"active", "activating", "reloading"}
                        if not alive:
                            raise PackageError("E_RECOVERY_REQUIRED", "unfinished update requires recovery before retry")
                    return self.accepted(txn)
                raise PackageError("E_LOCK", "update transaction in progress")
            with txnmod.held_lock(self.state):
                if command == "factory-reset":
                    new = txnmod.default_transaction(operation="factory_reset", source="web", stage="confirmed", package_path="")
                    new.update(confirm_phrase_ok=True, backup_ok=True, signature_ok=True)
                    return self.start(new, "sa02m-factory-reset.service")
                if command == "inspect":
                    return self.inspect()
                if command == "upload":
                    if len(args[1]) > 512:
                        raise PackageError("E_UPLOAD", "Content-Type too long")
                    # Root owns incoming; an unprivileged CGI cannot swap a validated file.
                    incoming = self.package.parent
                    if incoming.is_symlink():
                        raise PackageError("E_UPLOAD", "incoming directory is a symlink")
                    incoming.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.chmod(incoming, 0o700)
                    for name in ("package.partial", "package.sa02m"):
                        if (incoming / name).is_symlink():
                            (incoming / name).unlink()
                    try:
                        receive_multipart_file({"REQUEST_METHOD": "POST", "CONTENT_TYPE": args[1],
                                                "CONTENT_LENGTH": args[2], "wsgi.input": stream},
                                               incoming_dir=incoming)
                        info = self.inspect()
                    except Exception:
                        self.package.unlink(missing_ok=True)
                        (incoming / "package.partial").unlink(missing_ok=True)
                        if txn and txn.get("stage") == "uploaded":
                            txnmod.update_stage(self.state, "error", result="failed",
                                                error_code="E_UPLOAD", error_message="replacement upload failed")
                        raise
                    new = txnmod.default_transaction(source="file", stage="uploaded",
                                                     package_path=str(self.package))
                    txnmod.save_transaction(new, self.state)
                    return info
                if command == "apply":
                    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2,3}", args[1]):
                        raise PackageError("E_COMPAT", "invalid confirm_version")
                    info = self.inspect()
                    details = info.get("inspect") or info
                    if details.get("version") != args[1]:
                        raise PackageError("E_COMPAT", "confirmed version differs from package")
                    new = txnmod.default_transaction(source="file", stage="validating",
                                                     package_path=str(self.package), target_version=args[1])
                    new["signature_ok"] = bool(details.get("signature_ok"))
                    (self.state / "cancel").unlink(missing_ok=True)
                    return self.start(new, "sa02m-update.service")
                # Download and apply share the same reservation; the launcher preserves its ID.
                new = txnmod.default_transaction(source="github", stage="validating", package_path="")
                new["download_pending"] = True
                (self.state / "cancel").unlink(missing_ok=True)
                return self.start(new, "sa02m-update-download.service")


def main(controller=None, args=None):
    try:
        out = (controller or Controller()).execute(sys.argv[1:] if args is None else args, sys.stdin.buffer)
    except PackageError as exc:
        out = {"ok": False, "error_code": exc.code, "error_message": exc.message}
    except Exception:
        # The public body stays generic; the trace goes to stderr, which fcgiwrap
        # relays to the nginx error log (the CGI lib no longer discards it).
        traceback.print_exc()
        out = {"ok": False, "error_code": "E_INTERNAL", "error_message": "update controller failed"}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1
