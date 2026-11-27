"""Pending hooks, bound completion and lossless PID1 identity proofs."""

from contextlib import nullcontext
import copy
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from sa02m_devices import event_journal_completion as completion
from sa02m_devices import event_journal_delivery as files
from sa02m_devices import event_journal_peer as peer
from sa02m_devices import event_journal_queue as queue
from sa02m_devices import event_journal_request as request
from sa02m_devices import event_journal_source as source
from sa02m_devices import event_journal_startup as startup
from sa02m_devices import event_journal_state as state


class OwnedDomain(state.Domain):
    @property
    def owner(self):
        return os.geteuid()


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = startup.Configuration(OwnedDomain(self.root / "state"), self.root / "receipt", self.root / "update")
        self.initial = startup.prepare(self.config, "installer", rootfs=True)
        self.context = {"mode": "absent"}
        self.old = False
        self.trace = []
        self.addCleanup(patch.stopall)
        patch.object(files, "source_context", lambda update: copy.deepcopy(self.context)).start()
        patch.object(source, "verified_fingerprint", return_value="b" * 64).start()
        patch.object(files, "completion_lock_records", return_value={
            "install": {"device": 1, "inode": 2, "mode": 0o600},
            "update": {"device": 1, "inode": 3, "mode": 0o644}}).start()
        patch.object(queue, "enqueue", self.enqueue).start()
        patch.object(peer, "observe", self.observe).start()
        patch.object(peer, "transition", self.transition).start()
        patch.object(files, "completion_admission", lambda *args: nullcontext()).start()
        patch.object(state, "Domain", OwnedDomain).start()

    def observe(self, *args, **kwargs):
        return {}, ("20", b"2") if not self.old else ("10", b"1"), not self.old

    def enqueue(self, encoded, *, deadline, budget=None):
        self.assertFalse(startup.validate(self.config).ready)
        self.trace.append(("enqueue", encoded))
        return request.unit_name(encoded)

    def transition(self, expected, mark_not_ready, *, force_refresh=False, **kwargs):
        self.trace.append(("transition", force_refresh))
        mark_not_ready()
        self.old = False

    def pending_context(self):
        return {"mode": "pending", "id": "12345678-1234-1234-1234-123456789abc", "operation": "update",
                "source": "github", "target_version": "1.0.8.2", "target_commit": "a" * 40}

    def finish_update(self):
        self.context.update(mode="stable", stage="done", result="success")

    def test_hook_never_dispatches_and_matching_hooks_coalesce(self):
        self.old = True
        first = startup.prepare(self.config, "devices-logger")
        second = startup.prepare(self.config, "devices-api")
        self.assertFalse(first.ready)
        self.assertEqual(first, second)
        self.assertEqual([kind for kind, _ in self.trace], ["enqueue", "enqueue"])
        self.assertEqual(self.trace[0][1], self.trace[1][1])
        self.assertEqual(first.identity, self.initial.identity)

    def test_normal_current_restart_has_no_write_or_worker(self):
        paths = (self.config.domain.directory / "state.json", self.config.receipts / "receipt.json")
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
        self.assertEqual(startup.prepare(self.config, "devices-logger"), self.initial)
        self.assertEqual(self.trace, [])
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths])

    def test_second_ota_current_protocol_still_refreshes_once_after_success(self):
        self.context = self.pending_context()
        first = startup.prepare(self.config, "devices-logger")
        self.assertFalse(first.ready)
        encoded = self.trace[0][1]
        self.assertEqual(startup.prepare(self.config, "devices-api"), first)
        self.assertEqual(self.trace[1][1], encoded)
        with self.assertRaises(state.JournalStateError):
            completion.complete(encoded)
        self.assertFalse(startup.validate(self.config).ready)
        self.finish_update()
        self.assertTrue(completion.complete(encoded).ready)
        self.assertTrue(completion.complete(encoded).ready)
        self.assertEqual([entry for entry in self.trace if entry[0] == "transition"], [("transition", True)])

    def test_worker_preserves_current_generation_and_stale_identity_never_clobbers(self):
        self.old = True
        startup.prepare(self.config, "devices-api")
        encoded = self.trace[0][1]
        newer = startup.recover(self.config)
        startup.prepare(self.config, "installer", rootfs=True)
        with self.assertRaisesRegex(state.JournalStateError, "authority changed"):
            completion.complete(encoded)
        after = startup.validate(self.config)
        self.assertEqual((after.identity, after.generation), (newer.identity, newer.generation))
        self.assertTrue(after.ready)
        self.assertFalse(any(entry[0] == "transition" for entry in self.trace))

    def test_rollback_changed_context_source_and_missing_state_do_not_provision(self):
        self.context = self.pending_context()
        startup.prepare(self.config, "devices-logger")
        encoded = self.trace[0][1]
        for context in ({**self.context, "mode": "stable", "stage": "rolled_back", "result": "rolled_back"},
                        {"mode": "absent"}, {**self.context, "target_commit": "c" * 40}):
            with self.subTest(context=context), patch.object(files, "source_context", return_value=context), \
                    self.assertRaises(state.JournalStateError):
                completion.complete(encoded)
        self.finish_update()
        with patch.object(source, "verified_fingerprint", return_value="d" * 64), self.assertRaises(state.JournalStateError):
            completion.complete(encoded)
        receipt = self.config.receipts / "receipt.json"
        receipt.unlink()
        with self.assertRaises(state.JournalStateError):
            completion.complete(encoded)
        self.assertFalse(receipt.exists())
        self.assertFalse(state.read_atomic(self.config.domain).ready)
        self.assertFalse(any(entry[0] == "transition" for entry in self.trace))

    def test_worker_missing_receipt_directory_does_not_recreate_it(self):
        self.old = True
        startup.prepare(self.config, "devices-api")
        encoded = self.trace[0][1]
        for path in self.config.receipts.iterdir():
            path.unlink()
        self.config.receipts.rmdir()
        with self.assertRaises(FileNotFoundError):
            completion.complete(encoded)
        self.assertFalse(self.config.receipts.exists())

    def test_installer_force_refresh_never_acquires_shared_completion_locks(self):
        with patch.object(files, "completion_admission", side_effect=AssertionError("installer SH")):
            self.assertTrue(startup.prepare(self.config, "installer").ready)
        self.assertEqual(self.trace, [("transition", True)])

    def test_unknown_consumer_and_failed_observation_never_dispatch(self):
        with self.assertRaises(ValueError):
            startup.prepare(self.config, "typo")
        with patch.object(peer, "observe", side_effect=state.JournalStateError("bad service evidence")), \
                self.assertRaises(state.JournalStateError):
            startup.prepare(self.config, "devices-logger")
        self.assertEqual(self.trace, [])
        self.assertFalse(startup.validate(self.config).ready)

    def test_enqueue_reload_or_publication_failure_is_not_ready(self):
        self.old = True
        for error in (state.JournalStateError("daemon reload failed"), OSError("runtime publication failed")):
            with self.subTest(error=str(error)), patch.object(queue, "enqueue", side_effect=error), self.assertRaises(type(error)):
                startup.prepare(self.config, "devices-logger")
            current = startup.validate(self.config)
            self.assertFalse(current.ready)
            self.assertEqual((current.identity, current.generation), (self.initial.identity, self.initial.generation))
        self.assertFalse(any(kind == "transition" for kind, _ in self.trace))


class QueueEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.unit = "sa02m-journal-complete-" + "a" * 64 + ".service"
        self.arguments = ["/usr/bin/true", "space $literal\\path", "Привет\n\t\v%\"'", "split", "one two"]
        self.values = dict.fromkeys(queue.PROPERTIES, "")
        self.values.update(Id=self.unit, LoadState="loaded", ActiveState="activating", SubState="start", Job="42",
            Transient="yes", FragmentPath=str(queue.TRANSIENT / self.unit),
            DropInPaths=str(queue._timeout_path(self.unit)), NeedDaemonReload="no", Type="oneshot", User="root",
            Group="root", DynamicUser="no", Restart="no", KillMode="control-group", TimeoutStartUSec="16min",
            JobTimeoutUSec="16min", After=" ".join((*queue.AFTER, *sorted(queue.DEFAULT_AFTER))))
        self.fragment = queue.fragment(self.arguments)
        self.timeout = queue.TIMEOUT_BYTES
        self.addCleanup(patch.stopall)
        patch.object(queue, "_snapshot", self.snapshot).start()
        patch.object(peer, "command", self.command).start()

    def snapshot(self, path, budget):
        data = self.timeout if path.name == queue.TIMEOUT_FILE else self.fragment
        budget.consume(data)
        return (1, 2, len(data), 4, 5), data

    def command(self, arguments, deadline, limit=65536, **kwargs):
        keys = next(arg.split("=", 1)[1].split(",") for arg in arguments if arg.startswith("--property="))
        return "".join(key + "=" + self.values[key] + "\n" for key in keys if key in self.values)

    def existing(self):
        return queue._existing(self.unit, self.arguments, queue.Budget(time.monotonic() + 5))

    def test_exact_metadata_and_lossless_command(self):
        self.assertEqual(self.existing(), self.values)
        self.assertIn(b'"space $literal\\\\path"', self.fragment)
        self.assertIn(b'\\n\\t\\v%%\\"', self.fragment)
        changed = self.arguments[:-2] + ["split one", "two"]
        self.assertEqual(" ".join(changed), " ".join(self.arguments))
        self.assertNotEqual(queue.fragment(changed), self.fragment)
        self.fragment = queue.fragment(changed)
        with self.assertRaises(state.JournalStateError):
            self.existing()

    def test_each_required_property_and_foreign_policy_refuse(self):
        base = dict(self.values)
        for key in queue.PROPERTIES:
            self.values = {name: value for name, value in base.items() if name != key}
            with self.subTest(missing=key), self.assertRaises(state.JournalStateError):
                self.existing()
        for key, value in (("Type", "simple"), ("User", "nobody"), ("DynamicUser", "yes"),
                           ("JobTimeoutUSec", "infinity"), ("TimeoutStartUSec", "1min"),
                           ("NeedDaemonReload", "yes"), ("Environment", "SECRET=hidden"),
                           ("DropInPaths", str(queue._timeout_path(self.unit)) + " /foreign.conf"),
                           ("After", " ".join(queue.AFTER) + " foreign.service"), ("After", "basic.target"),
                           ("FragmentPath", "/foreign.service"), ("Id", "alias.service"), ("Job", "bad")):
            self.values = {**base, key: value}
            with self.subTest(key=key), self.assertRaises(state.JournalStateError):
                self.existing()

    def test_fragment_ambiguities_timeout_and_budgets_refuse(self):
        original = self.fragment
        for raw in (original + b'ExecStart="/bin/true"\n', original.replace(b"ExecStart=\n", b""),
                    original.replace(b"ExecStart=:", b"ExecStart="), original + b"EnvironmentFile=/secret\n"):
            self.fragment = raw
            with self.subTest(raw=raw[-50:]), self.assertRaises(state.JournalStateError):
                self.existing()
        self.fragment = original
        self.timeout = b"[Unit]\nJobRunningTimeoutSec=960s\n"
        with self.assertRaises(state.JournalStateError):
            self.existing()
        with self.assertRaises(state.JournalStateError):
            queue.Budget(time.monotonic() + 5).consume(b"x" * 65537)
        with self.assertRaises(state.JournalStateError):
            queue.Budget(time.monotonic() - 1).consume(b"")


if __name__ == "__main__":
    unittest.main()
