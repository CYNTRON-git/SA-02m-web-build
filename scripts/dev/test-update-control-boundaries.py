# comment-mutation-proof-exempt: behavioral controller, migration and inherited-lock probes in temporary roots with injected process failures; no source-text acceptance pins.
"""Exercise cancellation, maintenance inheritance and bootstrap/state boundaries."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import fcntl  # noqa: F401 - the controller's lock idiom; Windows CPython has none
except ImportError:
    # run.mjs SKIP_EXIT: a skip is reported as a skip, never as a pass.
    print("SKIP  update-control-boundaries: fcntl unavailable (Windows CPython) - the controller is Linux-only; run under WSL / Linux CI")
    sys.exit(77)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "opt/sa02m-update"))
from lib import state_security as security
from lib import transaction as tx

# The update state must belong to security.OWNER_UID and the migration must run
# as it (root on the board). This suite creates the state as whoever runs it, so
# it rebinds the owner to itself: as root that is the product value; as an
# unprivileged CI user the same ownership checks and repairs run instead of
# skipping the migration cases (a skip is PARTIAL, FAIL under QUALITY_STRICT).
# StateOwnership pins that the product value is still root and that a foreign
# owner is still refused.
security.OWNER_UID, security.OWNER_GID = os.geteuid(), os.getegid()

SOURCE = Path(os.environ.get("SA02M_TEST_RUNNER", str(ROOT / "etc/sa02m-update-runner.sh"))).read_text()


def function(name):
    match = re.search(r"^"+re.escape(name)+r"\(\) \{.*?^\}", SOURCE, re.M | re.S)
    if not match:
        raise AssertionError("missing runner function "+name)
    return match.group()


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def bash(self, script):
        return subprocess.run(["bash", "-c", script], cwd=self.root, capture_output=True, text=True, timeout=20)

    def test_cancel_during_backup_prevents_stop_and_apply(self):
        prefix = """set -euo pipefail
STATEDIR=$PWD; PACKAGE_DEFAULT=$PWD/package; IMAGING_LOCK=$PWD/imaging
mkdir -p overlay
printf validating > stage
ensure_dirs() { :; }; migrate_legacy_state() { :; }; acquire_lock() { :; }
acquire_port_maintenance() { :; }; txn_exists() { return 0; }
escape_foreign_cgroup() { ESCAPED=0; }; preflight_commands() { :; }
prepare_github_overlay() { :; }; prepare_profile_excludes() { :; }
read_installed_version() { echo 1.0.7.5; }
txn_get() { case "$1" in stage) cat stage ;; id) echo txn ;; source) echo github ;; overlay_path) echo "$PWD/overlay" ;; esac; }
txn_patch() { for arg in "$@"; do case "$arg" in stage=*) printf '%s' "${arg#stage=}" > stage ;; esac; done; }
build_rollback_archive() { printf cancel > "$STATEDIR/cancel"; }
wipe_incoming_staging() { :; }; cleanup_imaging_lock() { :; }; log() { :; }; utc_now() { echo now; }
stop_port_consumers() { touch STOPPED; exit 75; }; stop_before_apply() { touch STOPPED; exit 75; }
install_imaging_lock() { touch APPLIED; exit 76; }
"""
        r = self.bash(prefix+function("cancel_requested")+"\n"+function("honour_cancel_if_early")+"\n"+function("cmd_apply")+"\ncmd_apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.root / "stage").read_text(), "cancelled")
        self.assertFalse((self.root / "STOPPED").exists())
        self.assertFalse((self.root / "APPLIED").exists())
        self.assertFalse((self.root / "maintenance").exists())

    def test_maintenance_flock_survives_exec_and_reentry(self):
        gate = self.root / "gate/maintenance.lock"
        fn = function("acquire_port_maintenance").replace("/run/sa02m-port-coordinator/maintenance.lock", str(gate))
        script = """set -euo pipefail
txn_has_key() { return 1; }
python3() { echo called >> "$PWD/probes"; }
"""+fn+"""
export -f acquire_port_maintenance txn_has_key python3
acquire_port_maintenance
exec bash -c 'acquire_port_maintenance; if flock -n """+str(gate)+""" true; then exit 91; fi'
"""
        r = self.bash(script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.root / "probes").read_text().splitlines(), ["called"])
        self.assertEqual(subprocess.run(["flock", "-n", str(gate), "true"]).returncode, 0)

    def test_port_service_restore_order_and_inactive_preservation(self):
        snapshot = {"sa02m-port-coordinator": True, "sa02m-flasher": True,
                    "sa02m-modbus-mqtt": True, "sa02m-serial-gateway": False}
        (self.root / "snapshot").write_text(json.dumps(snapshot))
        script = """set -euo pipefail
txn_has_key() { return 0; }; txn_get() { cat snapshot; }
_systemctl_bounded() { printf '%s\\n' "$*" >> calls; }
systemctl() { return 0; }
"""+function("restore_port_consumers").replace("/run/systemd/system", str(self.root / "units"))+"\nrestore_port_consumers"
        r = self.bash(script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.root / "calls").read_text().splitlines(),
                         ["45 start sa02m-port-coordinator", "45 start sa02m-flasher", "45 start sa02m-modbus-mqtt"])
        r = self.bash(script.replace("systemctl() { return 0; }", "systemctl() { return 1; }"))
        self.assertNotEqual(r.returncode, 0)

    def test_missing_runner_refuses_bootstrap_before_clone_or_deploy(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        for name, body in {"git": "touch "+str(self.root / "CLONED")+"; exit 71", "systemctl": "exit 0"}.items():
            p = bindir / name
            p.write_text("#!/bin/sh\n"+body+"\n")
            p.chmod(0o755)
        # The controller reserved the journal (validating, download_pending) before
        # starting the download unit; a refusal here must close that reservation,
        # or it only ages into E_RECOVERY_REQUIRED (review A8).
        state = self.root / "state"
        state.mkdir()
        pending = tx.default_transaction(source="github", stage="validating")
        pending["download_pending"] = True
        tx.save_transaction(pending, state)
        env = dict(os.environ, PATH=str(bindir)+":"+os.environ["PATH"],
                   SA02M_WEB_BUILD_STATEDIR=str(self.root / "legacy"),
                   SA02M_UPDATE_STATEDIR=str(state),
                   SA02M_UPDATE_RUNNER=str(self.root / "missing"))
        r = subprocess.run(["bash", str(ROOT / "etc/sa02m-web-update-apply.sh")], env=env,
                           capture_output=True, text=True, timeout=15)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("E_BOOTSTRAP_REQUIRED", r.stdout)
        self.assertFalse((self.root / "CLONED").exists())
        self.assertFalse((self.root / "legacy/deployed_commit").exists())
        journal = tx.load_transaction(state)
        self.assertEqual(journal["id"], pending["id"])
        self.assertEqual((journal["stage"], journal["error_code"]), ("error", "E_BOOTSTRAP_REQUIRED"), journal)

    def test_controller_internal_failure_keeps_public_code_and_logs_trace(self):
        # A crash inside the controller answered E_INTERNAL and left no trace
        # anywhere on the board (review A4): the public body stays generic, the
        # traceback goes to stderr for fcgiwrap/journald to relay.
        import contextlib
        import io
        from lib import control
        class Broken:
            def execute(self, args, stream):
                raise ZeroDivisionError("boom-marker")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = control.main(Broken(), ["inspect"])
        self.assertEqual(rc, 1)
        body = json.loads(out.getvalue())
        self.assertEqual((body["ok"], body["error_code"]), (False, "E_INTERNAL"))
        self.assertNotIn("boom-marker", out.getvalue())
        self.assertIn("ZeroDivisionError: boom-marker", err.getvalue())

    def test_online_download_failure_records_error_without_commit_marker(self):
        state = self.root / "state"
        state.mkdir()
        pending = tx.default_transaction(source="github", stage="validating")
        pending["download_pending"] = True
        tx.save_transaction(pending, state)
        bindir = self.root / "bin"
        bindir.mkdir()
        for name, body in {"git": "exit 71", "systemctl": "exit 0",
                           "runner": "printf '%s\\n' '{\"control_v1\":true,\"port_maintenance_v1\":true}'"}.items():
            path = bindir / name
            path.write_text("#!/bin/sh\n"+body+"\n")
            path.chmod(0o755)
        env = dict(os.environ, PATH=str(bindir)+":"+os.environ["PATH"],
                   SA02M_WEB_BUILD_STATEDIR=str(self.root / "legacy"),
                   SA02M_UPDATE_STATEDIR=str(state), SA02M_UPDATE_RUNNER=str(bindir / "runner"))
        result = subprocess.run(["bash", str(ROOT / "etc/sa02m-web-update-apply.sh")], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        failed = tx.load_transaction(state)
        self.assertEqual(failed["id"], pending["id"])
        self.assertEqual(failed["stage"], "error", result.stderr)
        self.assertEqual(failed["error_code"], "E_DOWNLOAD")
        self.assertFalse((state / "state/deployed_commit").exists())
        self.assertFalse((self.root / "legacy/deployed_commit").exists())


    def test_boot_inactive_guards_ordering_and_terminal_marker_cleanup(self):
        units = self.root / "units"
        permit = self.root / "permit"
        snapshot = {"sa02m-port-coordinator": True, "sa02m-flasher": True,
                    "sa02m-modbus-mqtt": False, "sa02m-serial-gateway": False}
        (self.root / "snapshot").write_text(json.dumps(snapshot))
        helper = function("guard_boot_port_consumers").replace("/run/systemd/system", str(units)).replace("/run/sa02m-update-inactive-port-permit", str(permit))
        prefix = """set -euo pipefail
RUNNER_CONTEXT=boot
txn_has_key() { return 0; }; txn_get() { cat snapshot; }
_systemctl_bounded() { echo "$*" >> calls; }
"""
        result = self.bash(prefix+helper+"\nguard_boot_port_consumers")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.parent.name for p in units.glob("*/*.conf")),
                         ["sa02m-modbus-mqtt.service.d", "sa02m-serial-gateway.service.d"])
        foreign = units / "sa02m-modbus-mqtt.service.d/80-bootstrap.conf"
        foreign.write_text("keep")
        restored = function("restore_port_consumers").replace("/run/systemd/system", str(units))
        result = self.bash(prefix+"\nsystemctl() { return 0; }\n"+restored+"\nrestore_port_consumers")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(foreign.read_text(), "keep")
        self.assertFalse(list(units.glob("*/90-sa02m-update-inactive.conf")))
        self.assertNotIn("start sa02m-modbus-mqtt", (self.root / "calls").read_text())
        permit.touch()
        result = self.bash(prefix+helper+"\nguard_boot_port_consumers")
        self.assertNotEqual(result.returncode, 0)
        before = (ROOT / "etc/systemd/sa02m-update-recover.service").read_text().split("Before=", 1)[1].splitlines()[0]
        after = (ROOT / "etc/systemd/sa02m-update-verify.service").read_text().split("After=", 1)[1].splitlines()[0]
        for name in snapshot:
            self.assertIn(name+".service", before)
            self.assertIn(name+".service", after)
        for stage in ("done", "rolled_back"):
            (self.root / "maintenance").write_text("txn")
            script = """set -euo pipefail
STATEDIR=$PWD
log() { :; }; load_runtime_wdt_prev() { :; }; cleanup_imaging_lock() { :; }
txn_get() { case "$1" in stage) echo """+stage+""";; id) echo txn;; operation) echo update;; esac; }
"""+function("recover_transaction")+"\nrecover_transaction"
            result = self.bash(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.root / "maintenance").exists())


    def test_factory_journal_never_recovered_as_update(self):
        script = """set -euo pipefail
txn_get() { case "$1" in operation) echo factory_reset;; stage) echo "$TEST_STAGE";; id) echo reset;; esac; }
log() { echo "$*" >&2; }
"""+function("recover_transaction")+"\nrecover_transaction"
        for stage in ("confirmed", "validating", "backing_up", "wipe", "apply", "verify", "rolling_back", "error", "done"):
            r = self.bash("TEST_STAGE="+stage+"\n"+script)
            if stage == "done":
                self.assertEqual(r.returncode, 0, r.stderr)
            else:
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("E_RECOVERY_REQUIRED", r.stderr)
                self.assertNotIn("command not found", r.stderr)


    def test_secure_migration_quarantines_untrusted_terminal_state(self):
        state = self.root / "state"
        state.mkdir(mode=0o775)
        os.chmod(state, 0o775)
        (state / "transaction.json").write_text('{"stage":"uploaded"}')
        with patch.object(security.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="inactive\n")):
            security.migrate(state)
        security.require_secure_state(state)
        self.assertFalse((state / "transaction.json").exists())
        archives = list(self.root.glob("state.untrusted-*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o700)
        self.assertEqual((archives[0] / "transaction.json").read_text(), '{"stage":"uploaded"}')
        with patch.object(security.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="inactive\n")):
            security.migrate(state)
        self.assertEqual(len(list(self.root.glob("state.untrusted-*"))), 1)
        # The second run (03-webserver.sh / update-www-only.sh after the wrapper)
        # meets the secure state and creates update.lock: www-data must be able to
        # READ the runner pid in it (lib_web_update.sh liveness detector), so the
        # lock is world-readable and still root-only writable (review B2).
        lock_mode = (state / "update.lock").stat().st_mode & 0o777
        self.assertEqual(lock_mode & 0o044, 0o044, "update.lock mode %o: pid unreadable by www-data" % lock_mode)
        self.assertEqual(lock_mode & 0o022, 0, "update.lock mode %o: writable beyond root" % lock_mode)
        security.require_secure_state(state)

    def test_completed_recover_oneshot_allowed_but_running_or_failed_rejected(self):
        def systemctl(argv, **kw):
            value = "inactive"
            if argv[-1] == "sa02m-update-recover":
                value = {"ActiveState": "active", "SubState": substate,
                         "ExecMainStatus": exit_status}[argv[3]]
            return subprocess.CompletedProcess(argv, 0, stdout=value+"\n")
        for substate, exit_status in (("running", "0"), ("exited", "1"), ("exited", "0")):
            state = self.root / (substate+exit_status)
            state.mkdir()
            with patch.object(security.subprocess, "run", side_effect=systemctl):
                if substate == "exited" and exit_status == "0":
                    security.migrate(state)
                    security.require_secure_state(state)
                else:
                    with self.assertRaises(RuntimeError):
                        security.migrate(state)


    def test_migration_refuses_pending_symlink_hardlink_and_held_lock(self):
        state = self.root / "state"
        state.mkdir()
        journal = state / "transaction.json"
        with patch.object(security.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="inactive\n")):
            journal.write_text('{"stage":"applying"}')
            with self.assertRaises(RuntimeError):
                security.migrate(state)
            self.assertEqual(journal.read_text(), '{"stage":"applying"}')
            journal.unlink()
            target = self.root / "target"
            target.write_text('{"stage":"done"}')
            journal.symlink_to(target)
            with self.assertRaises(RuntimeError):
                security.migrate(state)
            journal.unlink()
            os.link(target, journal)
            with self.assertRaises(RuntimeError):
                security.migrate(state)
            journal.unlink()
            journal.write_text('{"stage":"done"}')
            with tx.held_lock(state):
                with self.assertRaises(BlockingIOError):
                    security.migrate(state)
            self.assertFalse((state / security.STAMP).exists())
            (state / "maintenance").touch()
            with self.assertRaises(RuntimeError):
                security.migrate(state)
            self.assertEqual(journal.read_text(), '{"stage":"done"}')


class StateOwnership(unittest.TestCase):
    """The owner check itself: the rebinding above changes WHICH uid is the
    owner, never whether a foreign owner is refused."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        with patch.object(security.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="inactive\n")):
            security.migrate(self.state)
        self.addCleanup(setattr, security, "OWNER_UID", security.OWNER_UID)

    def test_the_product_owner_is_root(self):
        # The rebinding is this suite's; a fresh interpreter sees root.
        out = subprocess.run(
            [sys.executable, "-c", "from lib import state_security as s; print(s.OWNER_UID, s.OWNER_GID)"],
            cwd=str(ROOT / "opt/sa02m-update"), capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.split(), ["0", "0"], out.stderr)

    def test_the_migration_creates_the_state_under_the_owner(self):
        for path in (self.state, self.state / security.STAMP, self.state / "incoming"):
            self.assertEqual(path.lstat().st_uid, security.OWNER_UID, path)

    def test_the_migration_refuses_any_uid_but_the_owner(self):
        security.OWNER_UID = os.geteuid() + 1
        with self.assertRaisesRegex(PermissionError, "root required"):
            security.migrate(self.state)

    def test_every_owner_check_refuses_a_foreign_uid(self):
        # A file of another uid cannot be created without root, so lstat
        # reports one for exactly the path under test; every other path keeps
        # its real owner. The control run proves the state is otherwise secure.
        (self.state / "transaction.json").write_text('{"stage":"done"}')
        os.chmod(self.state / "transaction.json", 0o644)
        security.require_secure_state(self.state)
        real = Path.lstat
        cases = ((self.state, "unsafe update state"),
                 (self.state / security.STAMP, "unsafe update state"),
                 (self.state / "incoming", "unsafe incoming directory"),
                 (self.state / "transaction.json", "unsafe update journal"))
        for target, message in cases:
            def lstat(path, *args, **kwargs):
                st = real(path, *args, **kwargs)
                if path != target:
                    return st
                fields = list(st[:10])
                fields[4] = security.OWNER_UID + 1  # st_uid
                return os.stat_result(fields)
            with self.subTest(path=target.name), patch.object(Path, "lstat", lstat):
                with self.assertRaises(security.PackageError) as caught:
                    security.require_secure_state(self.state)
                self.assertEqual(caught.exception.code, "E_BOOTSTRAP_REQUIRED")
                self.assertIn(message, caught.exception.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
