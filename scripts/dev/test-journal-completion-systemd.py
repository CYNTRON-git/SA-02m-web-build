#!/usr/bin/env python3
# comment-mutation-proof-exempt: Real isolated PID1 acceptance. Replays the immutable old delivery map, executes actual hooks and native flock/worker, and asserts old-peer replacement, Type=simple update exclusion and source-context refusal. Requires explicit disposable-container admission; unavailable isolation is a failure, never a skip.
"""Journal completion acceptance in an explicitly disposable, no-host-mount PID1 container."""

import importlib.util
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "opt/sa02m-devices"))
from sa02m_devices import event_journal_queue as queue
from sa02m_devices import event_journal_request as request
from sa02m_devices import event_journal_startup as startup
from sa02m_devices import event_journal_state as state


def run(arguments, *, check=True, timeout=15, **kwargs):
    return subprocess.run(arguments, check=check, capture_output=True, text=True, timeout=timeout, **kwargs)


def ctl(*arguments, **kwargs):
    return run(["systemctl", *arguments], **kwargs)


def show(unit, name):
    return ctl("show", "--value", "--property=" + name, "--", unit).stdout.strip()


def wait_for(predicate, description, seconds=12):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("native journal fixture timed out: " + description)


class NativeCompletionTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.geteuid(), 0)
        self.assertEqual(Path("/proc/1/comm").read_text().strip(), "systemd")
        self.assertEqual(Path("/run/systemd/container").read_text().strip(), "podman")
        self.assertEqual(os.environ.get("SA02M_JOURNAL_PID1_TEST"), "disposable-no-host-mounts")
        self.consumers = tuple(queue.AFTER) + ("sa02m-stand-api.service",)
        for unit in self.consumers:
            self.assertEqual(show(unit, "LoadState"), "not-found", "fixture would replace an existing consumer")
        self.temp = tempfile.TemporaryDirectory(prefix="sa02m-journal-pid1-", dir="/run")
        self.root = Path(self.temp.name)
        self.units = set()
        self.owned_files = []
        self.workers = set()
        self.timeout_bytes = {}
        self.addCleanup(self.cleanup)
        self.before_workers = set(queue.TRANSIENT.glob(request.PREFIX + "*.service"))
        spec = importlib.util.spec_from_file_location("_native_journal_delivery", ROOT / "scripts/dev/event_journal_delivery_harness.py")
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        self.harness = harness
        payload = harness.candidate_payload(self.root)
        manifest = harness.github_manifest(payload, self.root)
        self.assertEqual(manifest.get("migrations", []), [])
        self.assertIn("sa02m-devices-logger", manifest["services"]["restart"])
        self.destination = self.root / "deployed"
        harness.deploy(manifest, payload, self.destination)
        self.package = self.destination / "opt/sa02m-devices"
        self.entry = self.package / "sa02m_devices_journal_startup.py"
        for relative in ("event_journal_queue.py", "event_journal_request.py", "event_journal_completion.py", "event_journal_source.py"):
            self.assertTrue((self.package / "sa02m_devices" / relative).is_file(), relative)
        self.config = startup.Configuration(state.Domain(self.root / "authority"), self.root / "receipt", self.root / "update")
        self.config.update.mkdir(mode=0o755)
        self.env_file = self.root / "empty.env"
        self.env_file.write_text("")
        self.env_file.chmod(0o644)
        self.settings = self.config.settings()
        self.updater_script = self.root / "updater.py"
        self.updater_script.write_text('''import fcntl, json, pathlib, sys, time
root = pathlib.Path(sys.argv[1])
with (root / 'update' / 'update.lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    (root / 'held').write_text('held')
    until = time.monotonic() + 30
    while not (root / 'release').exists():
        if time.monotonic() >= until: raise SystemExit(19)
        time.sleep(.02)
    transaction = json.loads((root / 'update' / 'transaction.json').read_text())
    transaction.update(stage='done', result='success')
    temporary = root / 'update' / 'transaction-new.json'
    temporary.write_text(json.dumps(transaction))
    temporary.replace(root / 'update' / 'transaction.json')
''')
        self.write_unit("sa02m-stand-api.service", self.stand(current=False))
        ctl("daemon-reload")
        ctl("start", "sa02m-stand-api.service")
        self.first_pid = show("sa02m-stand-api.service", "MainPID")
        self.assertNotEqual(self.first_pid, "0")
        self.write_unit("sa02m-stand-api.service", self.stand(current=True))
        for unit in queue.AFTER:
            raw = (self.destination / "etc/systemd/system" / unit).read_text()
            raw = raw.replace("/opt/sa02m-devices", str(self.package))
            for key, value in self.settings.items():
                raw = "\n".join("Environment=" + key + "=" + value if line.startswith("Environment=" + key + "=") else line
                                for line in raw.splitlines()) + "\n"
            raw = "\n".join("ExecStart=/usr/bin/sleep 300" if line.startswith("ExecStart=") else
                             "EnvironmentFile=-" + str(self.env_file) if line.startswith("EnvironmentFile=") else line
                             for line in raw.splitlines()) + "\n"
            if unit.endswith("-api.service"):
                raw = raw.replace("[Unit]\n", "[Unit]\nConditionPathExists=" + str(self.root / "never-created") + "\n", 1)
            self.write_unit(unit, raw)
        ctl("daemon-reload")

    def write_unit(self, unit, data):
        path = queue.RUNTIME / unit
        if path not in self.owned_files:
            self.assertFalse(os.path.lexists(path))
            self.owned_files.append(path)
        path.write_text(data)
        path.chmod(0o644)
        self.units.add(unit)

    def stand(self, *, current):
        return ("[Unit]\nDescription=Isolated journal peer fixture\n" +
                ("After=sa02m-devices-logger.service\n" if current else "") +
                "[Service]\nType=simple\nEnvironmentFile=-" + str(self.env_file) + "\n" +
                ("".join("Environment=" + key + "=" + value + "\n" for key, value in self.settings.items()) +
                 "ExecStartPre=/usr/bin/python3 -I -B " + str(self.entry) + " validate --consumer stand-api\n" if current else "") +
                "ExecStart=/usr/bin/sleep 300\n")

    def update(self):
        for name in ("held", "release"):
            (self.root / name).unlink(missing_ok=True)
        transaction = {"schema_version": 1, "id": str(uuid.uuid4()), "operation": "update", "source": "github",
                       "target_version": "1.0.8.2", "target_commit": "a" * 40, "stage": "verifying", "result": "pending"}
        path = self.config.update / "transaction.json"
        path.write_text(json.dumps(transaction))
        path.chmod(0o644)
        unit = "sa02m-journal-fixture-update-" + transaction["id"][:8] + ".service"
        self.units.add(unit)
        run(["systemd-run", "--unit=" + unit, "--property=Type=simple", "/usr/bin/python3", str(self.updater_script), str(self.root)])
        wait_for(lambda: (self.root / "held").exists(), "updater EX acquisition")
        self.assertEqual(show(unit, "ActiveState"), "active")
        return unit

    def worker(self):
        found = set(queue.TRANSIENT.glob(request.PREFIX + "*.service")) - self.before_workers
        self.assertEqual(len(found), 1)
        path = found.pop()
        self.before_workers.add(path)
        self.workers.add(path.name)
        line = next(line for line in path.read_text().splitlines() if line.startswith("ExecStart=:") )
        encoded = shlex.split(line[len("ExecStart=:"):])[-1]
        self.assertEqual(request.unit_name(encoded), path.name)
        return path.name, encoded

    def cleanup(self):
        self.workers.update(path.name for path in set(queue.TRANSIENT.glob(request.PREFIX + "*.service")) - self.before_workers)
        for unit in sorted(self.units | self.workers):
            ctl("stop", "--", unit, check=False)
        # Each test owns fresh consumer fixtures. Reset their counters before
        # removing those units; completion-unit failure evidence is not reset.
        for unit in self.consumers:
            result = ctl("reset-failed", "--", unit, check=False)
            if result.returncode:
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr.strip(), "Failed to reset failed state of unit " + unit + ": Unit " + unit + " not loaded.")
        for unit in self.workers:
            timeout = queue._timeout_path(unit)
            if timeout.exists():
                self.assertEqual(timeout.read_bytes(), self.timeout_bytes.get(unit, queue.TIMEOUT_BYTES))
                timeout.unlink()
                timeout.parent.rmdir()
        for path in self.owned_files:
            path.unlink()
        ctl("daemon-reload")
        self.temp.cleanup()

    def test_old_generator_logger_only_then_second_ota_use_actual_exclusion(self):
        for number in range(2):
            with self.subTest(ota=number + 1):
                updater = self.update()
                old_pid = show("sa02m-stand-api.service", "MainPID")
                started = time.monotonic()
                ctl("restart", *queue.AFTER)
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(show(queue.AFTER[0], "ActiveState"), "inactive")
                self.assertEqual(show(queue.AFTER[0], "ConditionResult"), "no")
                self.assertEqual(show(queue.AFTER[1], "ActiveState"), "active")
                self.assertEqual(show(updater, "ActiveState"), "active")
                worker, encoded = self.worker()
                pending = startup.validate(self.config)
                self.assertFalse(pending.ready)
                self.assertEqual(show("sa02m-stand-api.service", "MainPID"), old_pid)
                wait_for(lambda: show(worker, "MainPID") != "0", "worker native lock waiter")
                worker_pid = show(worker, "MainPID")
                self.assertEqual(Path("/proc") .joinpath(worker_pid, "exe").resolve(), Path("/usr/bin/flock"))
                contender = run(["/usr/bin/flock", "--exclusive", "--nonblock", "/run/sa02m-install.lock", "/usr/bin/true"], check=False)
                self.assertNotEqual(contender.returncode, 0)
                duplicate = run(["/usr/bin/python3", "-I", "-B", str(self.entry), "prepare", "--consumer", "devices-api"],
                                env={**os.environ, **self.settings})
                self.assertIn("pending", duplicate.stdout)
                self.assertEqual(show(worker, "MainPID"), worker_pid)
                self.assertEqual(show("sa02m-stand-api.service", "MainPID"), old_pid)
                self.assertEqual(show(worker, "JobTimeoutUSec"), "16min")
                self.assertEqual(show(worker, "TimeoutStartUSec"), "16min")
                self.assertNotIn("JobRunningTimeoutSec", (queue.RUNTIME / (worker + ".d") / queue.TIMEOUT_FILE).read_text())
                (self.root / "release").touch()
                wait_for(lambda: startup.validate(self.config).ready, "worker ready publication")
                result = startup.validate(self.config)
                self.assertEqual((result.identity, result.generation), (pending.identity, pending.generation))
                self.assertNotEqual(show("sa02m-stand-api.service", "MainPID"), old_pid)
                new_pid = show("sa02m-stand-api.service", "MainPID")
                raw = Path("/proc").joinpath(new_pid, "environ").read_bytes().split(b"\0")
                for key, value in self.settings.items():
                    self.assertIn((key + "=" + value).encode(), raw)
                wait_for(lambda: show(worker, "ActiveState") not in {"activating", "active"}, "worker exit")
                for lock in ("/run/sa02m-install.lock", str(self.config.update / "update.lock")):
                    self.assertEqual(run(["/usr/bin/flock", "--exclusive", "--nonblock", lock, "/usr/bin/true"], check=False).returncode, 0)
                state_path = self.config.domain.directory / "state.json"
                before = state_path.read_bytes(), state_path.stat().st_mtime_ns
                ctl("restart", queue.AFTER[1])
                self.assertEqual(show("sa02m-stand-api.service", "MainPID"), new_pid)
                self.assertEqual((state_path.read_bytes(), state_path.stat().st_mtime_ns), before)
                print(json.dumps({"ota": number + 1, "worker": worker, "old_pid": old_pid, "new_pid": new_pid,
                                  "request_bytes": len(encoded), "ready": result.ready, "identity_preserved": True}), flush=True)

    def test_failed_same_request_reuse_and_foreign_collision(self):
        updater = self.update()
        ctl("start", queue.AFTER[1])
        worker, encoded = self.worker()
        transaction_path = self.config.update / "transaction.json"
        transaction = json.loads(transaction_path.read_text())
        transaction_path.write_text(json.dumps({**transaction, "stage": "rolled_back", "result": "rolled_back"}))
        ctl("stop", updater)
        wait_for(lambda: show(worker, "ActiveState") == "failed", "rollback worker refusal")
        self.assertFalse(startup.validate(self.config).ready)
        self.assertEqual(show("sa02m-stand-api.service", "MainPID"), self.first_pid)
        transaction_path.write_text(json.dumps({**transaction, "stage": "done", "result": "success"}))
        with startup.receipt_guard(self.config, deadline=time.monotonic() + 5), \
                patch.object(queue.source, "installed_root", return_value=self.package):
            self.assertEqual(queue.enqueue(encoded, deadline=time.monotonic() + 5), worker)
        wait_for(lambda: startup.validate(self.config).ready, "same-request failed-unit reuse")
        wait_for(lambda: show(worker, "LoadState") == "not-found", "successful transient collection")
        before = startup.validate(self.config)
        run(["systemd-run", "--no-block", "--unit=" + worker, "--property=Type=oneshot", "/usr/bin/sleep", "300"])
        with patch.object(queue.source, "installed_root", return_value=self.package), self.assertRaises(state.JournalStateError):
            queue.enqueue(encoded, deadline=time.monotonic() + 5)
        self.assertEqual(startup.validate(self.config), before)

    def test_source_replaced_while_waiting_cannot_publish(self):
        self.update()
        ctl("start", queue.AFTER[1])
        worker, encoded = self.worker()
        path = self.package / "sa02m_devices" / "event_journal_completion.py"
        path.write_bytes(path.read_bytes() + b"\n# Isolated replacement after request admission.\n")
        (self.root / "release").touch()
        wait_for(lambda: show(worker, "ActiveState") == "failed", "changed-source refusal")
        self.assertFalse(startup.validate(self.config).ready)
        self.assertEqual(show("sa02m-stand-api.service", "MainPID"), self.first_pid)
        journal = run(["journalctl", "--no-pager", "-u", worker]).stdout
        self.assertIn("installed source changed", journal)
        self.assertEqual(request.decode(encoded)["context"]["mode"], "pending")

    def test_full_installer_exclusion_refuses_before_python(self):
        with open("/run/sa02m-install.lock", "a+") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ctl("start", queue.AFTER[1])
            worker, _ = self.worker()
            wait_for(lambda: show(worker, "ActiveState") == "failed", "native full-install refusal")
            self.assertEqual(show(worker, "ExecMainStatus"), "1")
            self.assertFalse(startup.validate(self.config).ready)
            self.assertEqual(show("sa02m-stand-api.service", "MainPID"), self.first_pid)
            self.assertNotIn("journal completion unavailable", run(["journalctl", "--no-pager", "-u", worker]).stdout)

    def test_native_queued_timeout_cancels_before_worker_start(self):
        blocker = "sa02m-journal-fixture-blocker-" + uuid.uuid4().hex[:8] + ".service"
        self.units.add(blocker)
        run(["systemd-run", "--no-block", "--unit=" + blocker, "--property=Type=oneshot", "/usr/bin/sleep", "30"])
        wait_for(lambda: show(blocker, "ActiveState") == "activating", "unfinished predecessor start")
        unit = request.PREFIX + uuid.uuid4().hex * 2 + ".service"
        self.workers.add(unit)
        short = b"[Unit]\nJobTimeoutSec=1s\n"
        self.timeout_bytes[unit] = short
        with patch.object(queue, "TIMEOUT_BYTES", short):
            queue._publish_timeout(unit, queue.Budget(time.monotonic() + 5))
        ctl("daemon-reload")
        marker = self.root / "must-not-start"
        started = time.monotonic()
        run(["systemd-run", "--no-block", "--unit=" + unit, "--property=Type=oneshot", "--property=After=" + blocker,
             "/usr/bin/touch", str(marker)])
        result = ctl("start", unit, check=False, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stderr)
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(marker.exists())

    def test_runtime_dropin_and_fragment_unsafe_shapes_refuse_without_state_change(self):
        self.update()
        ctl("start", queue.AFTER[1])
        worker, encoded = self.worker()
        before = startup.validate(self.config)
        for path in (queue._timeout_path(worker), queue.TRANSIENT / worker):
            raw = path.read_bytes()
            for kind in ("symlink", "hardlink", "writable", "foreign-owner"):
                with self.subTest(path=path.name, kind=kind):
                    saved = self.root / (path.name + ".saved")
                    if kind == "symlink":
                        path.rename(saved)
                        path.symlink_to(saved)
                    elif kind == "hardlink":
                        os.link(path, saved)
                    elif kind == "writable":
                        path.chmod(0o666)
                    else:
                        os.chown(path, 65534, 65534)
                    try:
                        with patch.object(queue.source, "installed_root", return_value=self.package), \
                                self.assertRaises((OSError, state.JournalStateError)):
                            queue.enqueue(encoded, deadline=time.monotonic() + 5)
                        self.assertEqual(startup.validate(self.config), before)
                        self.assertEqual(show("sa02m-stand-api.service", "MainPID"), self.first_pid)
                    finally:
                        if kind == "symlink":
                            path.unlink()
                            saved.rename(path)
                        elif kind == "hardlink":
                            saved.unlink()
                        elif kind == "writable":
                            path.chmod(0o644)
                        else:
                            os.chown(path, 0, 0)
                    self.assertEqual(path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
