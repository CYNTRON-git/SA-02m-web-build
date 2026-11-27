"""Real source-cache and inherited-flock boundaries in private root fixtures."""

import fcntl
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import types
import tempfile
import time
import unittest
from unittest.mock import patch

from sa02m_devices import event_journal_delivery as files
from sa02m_devices import event_journal_source as source
from sa02m_devices.event_journal_state import JournalStateError

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "opt/sa02m-devices"


class CompletionFilesTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.geteuid(), 0, "real completion file fixtures require root")
        self.temp = tempfile.TemporaryDirectory(prefix="journal-completion-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.update = self.root / "update"
        self.update.mkdir(mode=0o755)
        self.install = self.root / "install space $literal\\name.lock"
        self.patch = patch.object(files, "INSTALL_LOCK", self.install)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_source_context_binds_pending_target_and_stable_observation(self):
        self.assertEqual(files.source_context(self.update), {"mode": "absent"})
        transaction = dict(schema_version=1, id="12345678-1234-1234-1234-123456789abc", operation="update",
                           source="github", target_version="1.0.8.2", target_commit="a" * 40,
                           stage="verifying", result="pending")
        path = self.update / "transaction.json"
        path.write_text(json.dumps(transaction))
        requested = files.source_context(self.update)
        self.assertEqual(requested["mode"], "pending")
        with self.assertRaises(JournalStateError):
            files.require_source_context(self.update, requested)
        for stage, result in (("rolled_back", "rolled_back"), ("error", "failed"), ("cancelled", "cancelled")):
            path.write_text(json.dumps({**transaction, "stage": stage, "result": result}))
            with self.assertRaises(JournalStateError):
                files.require_source_context(self.update, requested)
        done = {**transaction, "stage": "done", "result": "success"}
        path.write_text(json.dumps(done))
        files.require_source_context(self.update, requested)
        stable = files.source_context(self.update)
        files.require_source_context(self.update, stable)
        for key, value in (("target_commit", "b" * 40), ("id", "99999999-1234-1234-1234-123456789abc")):
            path.write_text(json.dumps({**done, key: value}))
            with self.assertRaises(JournalStateError):
                files.require_source_context(self.update, requested)
        path.unlink()
        with self.assertRaises(JournalStateError):
            files.require_source_context(self.update, stable)

    def test_unsafe_lock_paths_refuse_before_native_flock(self):
        records = files.completion_lock_records(self.update)
        self.assertEqual(records["install"]["mode"], 0o600)
        self.assertEqual(records["update"]["mode"], 0o644)
        for path in (self.install, self.update / "update.lock"):
            original = path.stat()
            for fault in ("writable", "owner", "symlink", "hardlink"):
                with self.subTest(path=path.name, fault=fault):
                    saved = path.with_name(path.name + ".saved")
                    if fault == "writable":
                        path.chmod(0o666)
                    elif fault == "owner":
                        os.chown(path, 65534, 65534)
                    elif fault == "symlink":
                        path.rename(saved)
                        path.symlink_to(saved)
                    else:
                        os.link(path, saved)
                    try:
                        with self.assertRaises((JournalStateError, OSError)):
                            files.completion_lock_records(self.update)
                    finally:
                        if fault == "symlink":
                            path.unlink()
                            saved.rename(path)
                        elif fault == "hardlink":
                            saved.unlink()
                        path.chmod(original.st_mode & 0o777)
                        os.chown(path, original.st_uid, original.st_gid)

    def child(self, records, *, wait_before_admission=False):
        script = self.root / "child.py"
        script.write_text('''import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from sa02m_devices import event_journal_delivery as files
root = Path(sys.argv[2])
files.INSTALL_LOCK = root / "install space $literal\\\\name.lock"
(root / 'imported').touch()
if sys.argv[3] == 'wait':
    sys.stdin.read(1)
records = json.loads((root / 'records.json').read_text())
with files.completion_admission(root / 'update', records):
    (root / 'admitted').touch()
    sys.stdin.read(1)
''')
        (self.root / "records.json").write_text(json.dumps(records))
        return [shutil.which("flock"), "--shared", "--nonblock", "--no-fork", "--", str(self.install),
                shutil.which("flock"), "--shared", "--wait", "2", "--no-fork", "--", str(self.update / "update.lock"),
                sys.executable, "-I", "-B", str(script), str(PACKAGE), str(self.root),
                "wait" if wait_before_admission else "now"]

    def wait_file(self, name, process):
        until = time.monotonic() + 5
        while not (self.root / name).exists() and time.monotonic() < until:
            if process.poll() is not None:
                self.fail(process.stderr.read().decode())
            time.sleep(0.01)
        self.assertTrue((self.root / name).exists(), name)

    def test_real_native_exclusions_precede_import_and_survive_adoption(self):
        records = files.completion_lock_records(self.update)
        updater = os.open(self.update / "update.lock", os.O_RDWR)
        self.addCleanup(os.close, updater)
        fcntl.flock(updater, fcntl.LOCK_EX)
        process = subprocess.Popen(self.child(records), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        time.sleep(0.15)
        self.assertIsNone(process.poll())
        self.assertFalse((self.root / "imported").exists())
        with self.install.open("rb") as contender, self.assertRaises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(updater, fcntl.LOCK_UN)
        self.wait_file("admitted", process)
        for path in (self.install, self.update / "update.lock"):
            with path.open("rb") as contender, self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, error = process.communicate(b"x", timeout=5)
        self.assertEqual(process.returncode, 0, error)
        for path in (self.install, self.update / "update.lock"):
            with path.open("rb") as contender:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_full_install_refuses_before_python_import(self):
        records = files.completion_lock_records(self.update)
        with self.install.open("rb") as install:
            fcntl.flock(install, fcntl.LOCK_EX)
            result = subprocess.run(self.child(records), input=b"x", capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "imported").exists())

    def test_lock_replacement_and_missing_inheritance_refuse(self):
        records = files.completion_lock_records(self.update)
        with self.assertRaisesRegex(JournalStateError, "inherited"):
            with files.completion_admission(self.update, records):
                self.fail("missing native locks admitted")
        process = subprocess.Popen(self.child(records, wait_before_admission=True), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.wait_file("imported", process)
        self.install.rename(self.root / "original.lock")
        self.install.touch(mode=0o600)
        _, error = process.communicate(b"xx", timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b"inode changed", error)
        self.assertFalse((self.root / "admitted").exists())

    def test_timestamp_valid_stale_pyc_cannot_supply_package_code(self):
        installed = self.root / "installed"
        package = installed / "sa02m_devices"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        module = package / "event_journal_startup.py"
        module.write_text("def main(): return 7\n")
        before = module.stat()
        py_compile.compile(str(module), doraise=True)
        module.write_text("def main(): return 0\n")
        os.utime(module, ns=(before.st_atime_ns, before.st_mtime_ns))
        legacy = installed / "legacy.py"
        legacy.write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).parent))\n"
                          "from sa02m_devices.event_journal_startup import main\nraise SystemExit(main())\n")
        old = subprocess.run([sys.executable, "-I", "-B", str(legacy)], capture_output=True, timeout=5)
        self.assertEqual(old.returncode, 7, old.stderr)
        entrypoint = installed / "sa02m_devices_journal_startup.py"
        shutil.copyfile(PACKAGE / entrypoint.name, entrypoint)
        current = subprocess.run([sys.executable, "-I", "-B", str(entrypoint)], capture_output=True, timeout=5)
        self.assertEqual(current.returncode, 0, current.stderr)
        print(json.dumps({"stale_timestamp_pyc_control": old.returncode,
                          "private_prefix_source_result": current.returncode}))

    def test_inventory_limits_identity_and_unsafe_sources(self):
        installed = self.root / "inventory"
        shutil.copytree(PACKAGE / "sa02m_devices", installed / "sa02m_devices", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(PACKAGE / "sa02m_devices_journal_startup.py", installed / "sa02m_devices_journal_startup.py")
        for path in installed.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        before, sources = source.inventory(installed, time.monotonic() + 5)
        target = installed / "sa02m_devices/__init__.py"
        target.write_bytes(target.read_bytes() + b"\n")
        self.assertNotEqual(before, source.inventory(installed, time.monotonic() + 5)[0])
        target.chmod(0o666)
        with self.assertRaises(JournalStateError):
            source.inventory(installed, time.monotonic() + 5)
        target.chmod(0o644)
        with self.assertRaisesRegex(JournalStateError, "deadline"):
            source.inventory(installed, time.monotonic() - 1)
        huge = installed / "sa02m_devices/huge.py"
        huge.write_bytes(b"#" * 4194305)
        with self.assertRaisesRegex(JournalStateError, "byte bound"):
            source.inventory(installed, time.monotonic() + 5)
        huge.unlink()
        for number in range(129):
            (installed / f"sa02m_devices/test-{number}.py").write_text("")
        with self.assertRaisesRegex(JournalStateError, "file bound"):
            source.inventory(installed, time.monotonic() + 5)

    def test_loaded_protocol_and_actual_import_origins_are_mandatory(self):
        from sa02m_devices import api
        self.assertRegex(source.verified_fingerprint(PACKAGE, time.monotonic() + 5), r"^[a-f0-9]{64}$")
        with patch.object(api, "EVENTS_JOURNAL_PROTOCOL", 0), self.assertRaisesRegex(JournalStateError, "incompatible"):
            source.verified_fingerprint(PACKAGE, time.monotonic() + 5)
        with patch.object(api, "__file__", str(self.root / "api.py")), self.assertRaisesRegex(JournalStateError, "outside"):
            source.verified_fingerprint(PACKAGE, time.monotonic() + 5)
        with patch.object(api.__spec__, "origin", str(self.root / "api.py")), self.assertRaisesRegex(JournalStateError, "outside"):
            source.verified_fingerprint(PACKAGE, time.monotonic() + 5)
        foreign = types.ModuleType("sa02m_devices.foreign")
        with patch.dict(sys.modules, {"sa02m_devices.foreign": foreign}), self.assertRaises(JournalStateError):
            source.verified_fingerprint(PACKAGE, time.monotonic() + 5)

    def test_failed_import_cleans_private_cache_without_touching_installed_pyc(self):
        installed = self.root / "failed-import"
        package = installed / "sa02m_devices"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "event_journal_startup.py").write_text("raise RuntimeError('intentional import refusal')\n")
        py_compile.compile(str(package / "event_journal_startup.py"), doraise=True)
        before = {str(path): path.read_bytes() for path in package.rglob("*.pyc")}
        entry = installed / "sa02m_devices_journal_startup.py"
        shutil.copyfile(PACKAGE / entry.name, entry)
        runtime_before = set(Path("/run").glob("sa02m-journal-import-*"))
        result = subprocess.run([sys.executable, "-I", "-B", str(entry)], capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"intentional import refusal", result.stderr)
        self.assertEqual(set(Path("/run").glob("sa02m-journal-import-*")), runtime_before)
        self.assertEqual({str(path): path.read_bytes() for path in package.rglob("*.pyc")}, before)


if __name__ == "__main__":
    unittest.main()
