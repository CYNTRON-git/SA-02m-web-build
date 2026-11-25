"""Unprivileged contract checks for the updater clear guard."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sa02m_devices import event_journal_delivery as delivery
from sa02m_devices import event_journal_state as state


class DeliveryContractTests(unittest.TestCase):
    def test_real_transaction_schema_and_terminal_stages(self):
        root = Path(__file__).resolve().parents[3]
        def load(name, path, package=False):
            spec = importlib.util.spec_from_file_location(name, path,
                submodule_search_locations=[str(path.parent)] if package else None)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        load("_delivery_contract_update", root / "opt/sa02m-update/lib/__init__.py", True)
        producer = load("_delivery_contract_update.transaction", root / "opt/sa02m-update/lib/transaction.py")
        for operation, stages in (("update", producer.STAGES), ("factory_reset", producer.FACTORY_STAGES)):
            for stage in stages:
                with self.subTest(operation=operation, stage=stage):
                    transaction = producer.default_transaction(operation=operation, stage=stage)
                    stable = {"done", "rolled_back", "cancelled"}
                    if operation == "update":
                        stable |= {"idle", "uploaded", "error"}
                    if stage in stable:
                        delivery._transaction_ready(transaction)
                    else:
                        with self.assertRaises(state.JournalBusy):
                            delivery._transaction_ready(transaction)

    def test_absent_update_configuration_performs_no_filesystem_access(self):
        with patch.dict(os.environ, {"STAND_DEVICES_EVENTS_UPDATE_DIR": ""}), \
                patch.object(delivery, "directory", side_effect=AssertionError("unexpected filesystem access")):
            with delivery.clear_preflight():
                pass

    def test_unknown_schema_refuses(self):
        for value in ({}, {"schema_version": 2}, {"schema_version": True},
                      {"schema_version": 1, "operation": [], "stage": "done"}):
            with self.subTest(value=value), self.assertRaises(state.JournalStateError):
                delivery._transaction_ready(value)

    def test_missing_public_lock_has_canonical_mode_under_private_umask(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            before = os.umask(0o077)
            try:
                with delivery.directory(path, os.geteuid()) as dfd:
                    fd = delivery.open_file(dfd, "update.lock", os.geteuid(), create=True, private=False)
                    os.close(fd)
                    private = delivery.open_file(dfd, "bootstrap.lock", os.geteuid(), create=True)
                    os.close(private)
            finally:
                os.umask(before)
            self.assertEqual((path / "update.lock").stat().st_mode & 0o777, 0o644)
            self.assertEqual((path / "bootstrap.lock").stat().st_mode & 0o777, 0o600)

    def test_existing_lock_inode_and_pid_are_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            lock = path / "update.lock"
            lock.write_bytes(b"12345\n")
            lock.chmod(0o640)
            before = lock.stat()
            with delivery.directory(path, os.geteuid()) as dfd:
                fd = delivery.open_file(dfd, "update.lock", os.geteuid(), create=True, private=False)
                os.close(fd)
            after = lock.stat()
            self.assertEqual((after.st_dev, after.st_ino, after.st_mode),
                             (before.st_dev, before.st_ino, before.st_mode))
            self.assertEqual(lock.read_bytes(), b"12345\n")


class HarnessStatusTests(unittest.TestCase):
    def run_fixture_suite(self, source):
        wrapper = Path(__file__).resolve().parents[3] / "scripts/dev/test-event-journal-delivery.py"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scripts = root / "scripts/dev"
            scripts.mkdir(parents=True)
            (root / "opt/sa02m-devices").mkdir(parents=True)
            entry = scripts / wrapper.name
            entry.write_bytes(wrapper.read_bytes())
            (scripts / "journal_delivery_privileged_tests.py").write_text(source)
            # Only this fixture selects the root branch; the process keeps its real uid.
            probe = """
import runpy
import sys
from unittest.mock import patch
with patch('os.geteuid', return_value=0):
    runpy.run_path(sys.argv[1], run_name='__main__')
"""
            return subprocess.run([sys.executable, "-I", "-c", probe, str(entry)],
                                  capture_output=True, text=True, timeout=5)

    def test_empty_collected_suite_refuses_at_real_entry_point(self):
        for source in ('"""No collected tests."""\n',
                       'import unittest\ndef load_tests(loader, tests, pattern): return unittest.TestSuite()\n'):
            with self.subTest(source=source):
                result = self.run_fixture_suite(source)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("no tests collected", result.stderr)
                self.assertNotIn("Ran 0 tests", result.stderr)

    def test_nonempty_suites_keep_success_and_failure(self):
        for assertion, expected in (("self.assertTrue(True)", 0), ("self.fail('fixture failure')", 1)):
            with self.subTest(expected=expected):
                source = "import unittest\nclass Fixture(unittest.TestCase):\n    def test_case(self): " + assertion + "\n"
                result = self.run_fixture_suite(source)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertIn("Ran 1 test", result.stderr)
                if expected:
                    self.assertIn("fixture failure", result.stderr)

    def test_sudo_status_is_normalized_at_real_process_exit(self):
        script = Path(__file__).resolve().parents[3] / "scripts/dev/test-event-journal-delivery.py"
        probe = """
import runpy
import sys
from unittest.mock import patch
value = int(sys.argv[2])
with patch('os.geteuid', return_value=65534), patch('subprocess.call', return_value=value) as child:
    try:
        runpy.run_path(sys.argv[1], run_name='__main__')
    finally:
        child.assert_called_once_with(['sudo', '-n', sys.executable, '-I', sys.argv[1]])
"""
        for status in (0, 1, 77, 256, -9):
            with self.subTest(status=status):
                result = subprocess.run([sys.executable, "-c", probe, str(script), str(status)],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0 if status == 0 else 1, result.stderr)

    def test_missing_sudo_is_a_visible_failure(self):
        script = Path(__file__).resolve().parents[3] / "scripts/dev/test-event-journal-delivery.py"
        probe = """
import runpy
import sys
from unittest.mock import patch
with patch('os.geteuid', return_value=65534), patch('subprocess.call', side_effect=FileNotFoundError):
    runpy.run_path(sys.argv[1], run_name='__main__')
"""
        result = subprocess.run([sys.executable, "-c", probe, str(script)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("root or sudo -n is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
