"""Executable first-update delivery and updater/clear serialization."""

import fcntl
from contextlib import contextmanager
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from sa02m_devices import event_journal_delivery as delivery
from sa02m_devices import event_journal_startup as startup
from sa02m_devices import event_journal_state as state
from sa02m_devices import device_events
from sa02m_devices import event_journal_peer as peer

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("_delivery_harness", ROOT / "scripts/dev/event_journal_delivery_harness.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
harness.module("_delivery_update", ROOT / "opt/sa02m-update/lib/__init__.py", package=True)
transaction = harness.module("_delivery_update.transaction", ROOT / "opt/sa02m-update/lib/transaction.py")
state_security = harness.module("_delivery_update.state_security", ROOT / "opt/sa02m-update/lib/state_security.py")


@contextmanager
def traversable_fixture(root):
    assigned = Path(os.environ["SA02M_JOURNAL_TEST_SCRATCH"])
    if not root.is_relative_to(assigned) or assigned == Path("/tmp"):
        raise AssertionError("permission fixture must have its own assigned scratch")
    modes = []
    current = root
    while current != assigned.parent:
        modes.append((current, current.stat().st_mode & 0o7777))
        current.chmod(0o755)
        current = current.parent
    try:
        yield assigned
    finally:
        for path, mode in reversed(modes):
            path.chmod(mode)


class UpdateGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.update = self.root / "update"
        before = os.umask(0o022)
        try:
            # Exercise the real filesystem producer without consulting the host manager.
            with patch.object(state_security.subprocess, "run", return_value=
                              subprocess.CompletedProcess([], 0, stdout="inactive\n")) as manager:
                state_security.migrate(self.update)
            self.assertEqual(manager.call_count, 6)
        finally:
            os.umask(before)
        state_security.require_secure_state(self.update)
        self.assertEqual(self.update.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.update / "incoming").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.update / state_security.STAMP).stat().st_mode & 0o777, 0o644)
        self.config = startup.Configuration(state.Domain(self.root / "authority"), self.root / "receipts", self.update)
        startup.prepare(self.config, "installer", rootfs=True)
        self.environment = patch.dict(os.environ, {**self.config.settings(), "SA02M_IMAGING_LOCK": str(self.root / "imaging")})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_first_clear_lock_is_readable_by_the_actual_unprivileged_pid_reader(self):
        lock = self.update / "update.lock"
        self.assertFalse(lock.exists())
        before = os.umask(0o077)
        try:
            with delivery.clear_preflight():
                pass
        finally:
            os.umask(before)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o644)
        inode = lock.stat().st_ino
        with transaction.UpdateLock(self.update), traversable_fixture(self.root) as assigned:
            current = self.update
            while current != assigned.parent:
                self.assertEqual(current.stat().st_mode & 0o777, 0o755)
                current = current.parent
            result = subprocess.run([sys.executable, "-c",
                "import json,os,pathlib,sys; print(json.dumps({'uid':os.geteuid(), 'pid':pathlib.Path(sys.argv[1]).read_text()}))",
                str(lock)], user=65534, group=65534, extra_groups=[], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"uid": 65534, "pid": str(os.getpid()) + "\n"})
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o644)
        for folder in (self.config.domain.directory, self.config.receipts):
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            for path in folder.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_preflight_preserves_existing_producer_pid_and_inode(self):
        lock = self.update / "update.lock"
        with transaction.UpdateLock(self.update):
            pass
        original = lock.stat()
        content = lock.read_bytes()
        with delivery.clear_preflight():
            self.assertEqual(lock.read_bytes(), content)
        current = lock.stat()
        self.assertEqual((current.st_dev, current.st_ino, current.st_mode),
                         (original.st_dev, original.st_ino, original.st_mode))
        self.assertEqual(lock.read_bytes(), content)

    def test_public_updater_still_rejects_unsafe_owner_writes_links_and_types(self):
        with transaction.UpdateLock(self.update):
            pass
        transaction.save_transaction(transaction.default_transaction(stage="done"), self.update)
        for path in (self.update / "update.lock", self.update / "transaction.json", self.update):
            original = path.stat()
            with self.subTest(path=path.name, fault="owner"):
                os.chown(path, 65534, 65534)
                try:
                    with self.assertRaises(state.JournalStateError), delivery.clear_preflight():
                        self.fail("non-owner updater admitted")
                finally:
                    os.chown(path, original.st_uid, original.st_gid)
            for write_bit in (0o020, 0o002):
                with self.subTest(path=path.name, fault=write_bit):
                    path.chmod(original.st_mode | write_bit)
                    try:
                        with self.assertRaises(state.JournalStateError), delivery.clear_preflight():
                            self.fail("writable updater admitted")
                    finally:
                        path.chmod(original.st_mode)
            saved = path.with_name(path.name + ".saved")
            path.rename(saved)
            try:
                for fault in ("symlink", "wrong-type", "fifo"):
                    with self.subTest(path=path.name, fault=fault):
                        if fault == "symlink":
                            path.symlink_to(saved)
                        elif fault == "fifo":
                            os.mkfifo(path, 0o600)
                        elif saved.is_dir():
                            path.touch(mode=0o644)
                        else:
                            path.mkdir(mode=0o755)
                        try:
                            with self.assertRaises(state.JournalStateError), delivery.clear_preflight():
                                self.fail("unsafe updater type admitted")
                        finally:
                            if path.is_dir() and not path.is_symlink():
                                path.rmdir()
                            else:
                                path.unlink()
            finally:
                saved.rename(path)

    def test_real_producer_transactions_terminal_and_recoverable_states(self):
        stable = {"idle", "uploaded", "done", "rolled_back", "cancelled", "error"}
        for operation, stages in (("update", transaction.STAGES), ("factory_reset", transaction.FACTORY_STAGES)):
            for stage in stages:
                with self.subTest(operation=operation, stage=stage):
                    value = transaction.default_transaction(operation=operation, stage=stage)
                    transaction.save_transaction(value, self.update)
                    allowed = stage in stable and (operation != "factory_reset" or stage in {"done", "rolled_back", "cancelled"})
                    if allowed:
                        with delivery.clear_preflight():
                            pass
                    else:
                        with self.assertRaises(state.JournalBusy), delivery.clear_preflight():
                            self.fail("recoverable transaction admitted clear")

    def test_lock_content_and_generation_preserved_before_commit(self):
        db = self.root / "archive.db"
        with sqlite3.connect(db) as conn:
            device_events.ensure_events_schema_on(conn)
            conn.execute("INSERT INTO device_events(ts,device_id,kind,phase,message) VALUES(1,'c','alarm','on','old')")
        before = state.read_atomic(self.config.domain)
        with transaction.UpdateLock(self.update):
            content = (self.update / "update.lock").read_bytes()
            with self.assertRaises(device_events.EventsBusy):
                device_events.clear_events(path=db)
            self.assertEqual(content, (self.update / "update.lock").read_bytes())
        transaction.save_transaction(transaction.default_transaction(stage="verifying"), self.update)
        with self.assertRaises(device_events.EventsBusy):
            device_events.clear_events(path=db)
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM device_events").fetchone(), (1,))
        self.assertEqual(before, state.read_atomic(self.config.domain))
        transaction.save_transaction(transaction.default_transaction(stage="done"), self.update)
        self.assertEqual(device_events.clear_events(path=db), 1)
        self.assertNotEqual(before.generation, state.read_atomic(self.config.domain).generation)

    def test_shared_clear_lock_serializes_next_apply(self):
        ctx = multiprocessing.get_context("fork")
        ready, attempt = ctx.Event(), ctx.Event()
        queue = ctx.Queue()
        def apply():
            ready.set()
            attempt.wait(3)
            try:
                with transaction.UpdateLock(self.update):
                    queue.put("acquired")
            except Exception:
                queue.put("busy")
        child = ctx.Process(target=apply)
        child.start()
        self.assertTrue(ready.wait(3))
        with delivery.clear_preflight():
            attempt.set()
            self.assertEqual(queue.get(timeout=3), "busy")
        child.join(3)
        self.assertEqual(child.exitcode, 0)
        with transaction.UpdateLock(self.update):
            pass

    def test_malformed_unsafe_and_imaging_state_refuse(self):
        path = self.update / "transaction.json"
        for raw in ('{}', '{"schema_version":1,"schema_version":1}', '{broken', 'x' * 65537):
            path.write_text(raw)
            with self.assertRaises(state.JournalStateError), delivery.clear_preflight():
                self.fail("bad transaction admitted")
        path.unlink()
        (self.root / "imaging").touch()
        with self.assertRaises(state.JournalBusy), delivery.clear_preflight():
            self.fail("imaging admitted")
        (self.root / "imaging").unlink()
        (self.update / "update.lock").unlink()
        (self.update / "update.lock").symlink_to(self.root / "target")
        with self.assertRaises(state.JournalStateError), delivery.clear_preflight():
            self.fail("unsafe lock admitted")
        self.assertFalse((self.root / "target").exists())

    def test_standalone_context_has_no_host_access(self):
        with patch.dict(os.environ, {"STAND_DEVICES_EVENTS_UPDATE_DIR": ""}), \
                patch.object(delivery, "directory", side_effect=AssertionError("host access")):
            with delivery.clear_preflight():
                pass

    def test_rollback_then_repeat_keeps_identity(self):
        initial = state.read_atomic(self.config.domain)
        for stage in ("applying", "verifying", "rolling_back"):
            transaction.save_transaction(transaction.default_transaction(stage=stage), self.update)
            with self.assertRaises(device_events.EventsBusy):
                device_events.clear_events(path=self.root / "absent.db")
        transaction.save_transaction(transaction.default_transaction(stage="rolled_back"), self.update)
        self.assertEqual(startup.prepare(self.config, "installer", rootfs=True), initial)
        transaction.save_transaction(transaction.default_transaction(stage="done"), self.update)
        device_events.clear_events(path=self.root / "absent.db")
        self.assertEqual(state.read_atomic(self.config.domain).identity, initial.identity)
        self.assertNotEqual(state.read_atomic(self.config.domain).generation, initial.generation)

    def test_real_http_refusal_and_degraded_sample_recording(self):
        import hashlib
        import http.client
        from http.server import ThreadingHTTPServer
        import threading
        from sa02m_devices import api
        from sa02m_devices.history_writer import ArchiveWriter
        session, csrf = "a" * 64, "c" * 64
        sessions = self.root / "sessions"
        sessions.mkdir()
        hashed = hashlib.sha256(session.encode()).hexdigest()
        (sessions / hashed).write_text("2000000000 admin\n")
        (sessions / (hashed + ".csrf")).write_text(csrf + "\n")
        server = ThreadingHTTPServer(("127.0.0.1", 0), api.DevicesAPIHandler)
        server.session_dir = str(sessions)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            try:
                connection.request(method, path, headers={"Cookie": "session_token=" + session, "X-SA02M-CSRF": csrf})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        db = self.root / "samples.db"
        writer = ArchiveWriter(db, flush_every_s=1)
        self.addCleanup(writer.close)
        before = state.read_atomic(self.config.domain)
        with patch.dict(os.environ, {"STAND_DEVICES_HISTORY_DB": str(db)}):
            transaction.save_transaction(transaction.default_transaction(stage="done"), self.update)
            self.assertEqual(request("POST", "/api/devices/events/clear"), (200, {"ok": True, "deleted": 0}))
            self.assertNotEqual(state.read_atomic(self.config.domain).generation, before.generation)
            for number, fault in enumerate(("verifying", "not-ready", "corrupt")):
                transaction.save_transaction(transaction.default_transaction(stage="verifying" if fault == "verifying" else "done"), self.update)
                if fault == "not-ready":
                    state.set_ready(self.config.domain, False)
                if fault == "corrupt":
                    (self.config.domain.directory / "state.json").write_text("broken")
                writer.add({"ts": 1700000000.0 + number, "mtd": [{"ok": True, "id": "mtdx62-COM3-20",
                            "illuminance_lux": 80.0 + number, "target_distance_m": 2.0, "presence": 1.0}]}, mr_due=True)
                writer.flush()
                status, body = request("POST", "/api/devices/events/clear")
                self.assertEqual(status, 503 if fault == "verifying" else 500)
                self.assertFalse(body["ok"])
                self.assertEqual(request("GET", "/api/health")[0], 200)
                self.assertEqual(request("GET", "/api/devices/events")[0], 503 if fault == "corrupt" else 200)
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT ts,device_id,illuminance_lux FROM mtd_samples ORDER BY ts").fetchall(),
                                 [(1700000000.0 + n, "mtdx62-COM3-20", 80.0 + n) for n in range(3)])
        self.assertEqual(json.loads((self.config.receipts / "receipt.json").read_text())["identity"], before.identity)


class FirstDeliveryTests(unittest.TestCase):
    def test_omitted_path_uses_root_owned_production_authority_without_sidecar(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "automatic.db"
            domain = state.Domain(root / "production-authority")
            with patch.dict(os.environ, {"STAND_DEVICES_EVENTS_STATE_DIR": "", "STAND_DEVICES_HISTORY_DB": "",
                                         "STAND_DEVICES_EVENTS_UPDATE_DIR": ""}), \
                    patch.object(state, "DEFAULT_DIRECTORY", domain.directory), \
                    patch.object(device_events, "db_path", lambda path=None: Path(path) if path is not None else archive):
                with self.assertRaises(state.JournalStateError):
                    device_events.ensure_events_schema()
                self.assertFalse(archive.exists())
                self.assertFalse(domain.directory.exists())
                before = state.bootstrap(domain, ready=True)
                self.assertEqual(domain.directory.stat().st_uid, 0)
                self.assertEqual(device_events.ensure_events_schema(), archive)
                self.assertEqual(device_events.list_events()["events"], [])
                self.assertEqual(state.read_atomic(domain), before)
                self.assertFalse(Path(str(archive) + ".events-state").exists())

    def test_installer_active_peer_finishes_before_readiness(self):
        with tempfile.TemporaryDirectory() as folder:
            destination, output = harness.installer_rootfs(Path(folder), active_peer=True)
            authority = state.Domain(destination / "var/lib/sa02m-devices/event-journal")
            self.assertTrue(state.read_atomic(authority).ready)
            calls = (Path(folder) / "peer-fixture/restarts").read_text().splitlines()
            self.assertEqual(calls, ["try-restart sa02m-stand-api.service"])
            self.assertTrue((destination / "etc/systemd/system/sa02m-stand-api.service.d/20-devices-socket-group.conf").is_file())

    def test_socket_hook_order_gate_rejects_missing_or_late_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            harness.order_gate_mutations(Path(folder))

    def test_installer_rootfs_uses_provisioner_without_manager(self):
        with tempfile.TemporaryDirectory() as folder:
            destination, output = harness.installer_rootfs(Path(folder))
            authority = state.Domain(destination / "var/lib/sa02m-devices/event-journal")
            self.assertTrue(state.read_atomic(authority).ready)
            self.assertIn("capture sa02m-devices-api.service sa02m-devices-logger.service", output)
            self.assertTrue((destination / "etc/systemd/system/sa02m-stand-api.service.d/30-event-journal.conf").is_file())

    def test_shipped_unit_graph_verifies_in_isolated_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            overlay = harness.candidate_payload(root)
            manifest = harness.github_manifest(overlay, root)
            destination = root / "installed"
            harness.deploy(manifest, overlay, destination)
            harness.verify_units(destination)

    def test_released_github_and_signed_offline_payload_execute_delivered_hooks(self):
        for source in ("github", "offline"):
            for skipped_api in (False, True):
                with self.subTest(source=source, skipped_api=skipped_api), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    overlay = harness.candidate_payload(root)
                    if source == "github":
                        manifest = harness.github_manifest(overlay, root)
                    else:
                        manifest, overlay = harness.offline_manifest(overlay, root)
                    destinations = {item["dst"] for item in manifest["deploy"]}
                    required = {"/opt/sa02m-devices/sa02m_devices_journal_startup.py",
                                "/opt/sa02m-devices/sa02m_devices/event_journal_startup.py",
                                "/opt/sa02m-devices/sa02m_devices/event_journal_peer.py",
                                "/opt/sa02m-devices/sa02m_devices/event_journal_delivery.py",
                                "/etc/systemd/system/sa02m-stand-api.service.d/30-event-journal.conf"}
                    self.assertTrue(required <= destinations, required - destinations)
                    self.assertNotIn("sa02m-stand-api", manifest["services"]["restart"])
                    destination = root / "installed"
                    harness.deploy(manifest, overlay, destination)
                    self.assertFalse((destination / "scripts/11-devices.sh").exists())
                    self.assertFalse((destination / "usr/local/libexec/sa02m-update-runner").exists())
                    ran = harness.service_hooks(manifest, destination, skip_api=skipped_api)
                    self.assertIn("sa02m-devices-logger", ran)
                    authority = state.Domain(destination / "var/lib/sa02m-devices/event-journal")
                    self.assertTrue(state.read_atomic(authority).ready)
                    receipts = destination / "var/lib/sa02m-devices-journal-init/receipt.json"
                    self.assertEqual(json.loads(receipts.read_text())["identity"], state.read_atomic(authority).identity)


unit_tests = harness.module("_delivery_startup_unit_tests", ROOT / "opt/sa02m-devices/tests/test_event_journal_startup.py")


class PrivilegedStartupTests(unit_tests.StartupTests):
    def test_core_nonowner_probe(self):
        probe = harness.module("_delivery_core_permissions", ROOT / "opt/sa02m-devices/tests/test_events_clear_permissions.py")
        with traversable_fixture(self.root) as assigned, patch.dict(os.environ, {"TMPDIR": str(assigned)}):
            probe.permission_probe(self.root / "core-permissions")

    def test_nonowner_cannot_modify_receipt(self):
        self.prepare()
        def demote():
            os.setgid(65534)
            os.setuid(65534)
        with traversable_fixture(self.root):
            result = subprocess.run([sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write('bad')",
                                     str(self.config.receipts / "receipt.json")], preexec_fn=demote,
                                    capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"PermissionError", result.stderr)
        self.assertTrue(startup.validate(self.config).ready)


class PrivilegedPeerTests(unit_tests.PeerTests):
    def test_environment_file_override_is_compared(self):
        envfile = self.root / "defaults"
        envfile.write_text('STAND_DEVICES_EVENTS_STATE_DIR="/different"\n')
        self.units[peer.PEER]["EnvironmentFiles"] = str(envfile) + " (ignore_errors=no)"
        with self.assertRaisesRegex(state.JournalStateError, "STAND_DEVICES_EVENTS_STATE_DIR"):
            self.run_peer()
        self.assertEqual(self.calls, [])


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(UpdateGuardTests))
    suite.addTests(loader.loadTestsFromTestCase(FirstDeliveryTests))
    suite.addTest(PrivilegedStartupTests("test_nonowner_cannot_modify_receipt"))
    suite.addTest(PrivilegedStartupTests("test_core_nonowner_probe"))
    suite.addTest(PrivilegedPeerTests("test_environment_file_override_is_compared"))
    return suite

if __name__ == "__main__":
    unittest.main()
