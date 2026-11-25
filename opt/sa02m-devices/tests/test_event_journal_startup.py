"""Service provisioning uses independent durable receipts, never runtime guesses."""

import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from sa02m_devices import event_journal_state as state
from sa02m_devices import event_journal_startup as startup
from sa02m_devices import event_journal_peer as peer


class OwnedDomain(state.Domain):
    @property
    def owner(self):
        return os.geteuid()

def prepare_worker(directory, receipts, queue):
    try:
        config = startup.Configuration(OwnedDomain(Path(directory)), Path(receipts), None)
        queue.put(startup.prepare(config, "installer", rootfs=True).identity)
    except Exception as exc:
        queue.put(type(exc).__name__ + ": " + str(exc))


class StartupFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = startup.Configuration(OwnedDomain(self.root / "authority"),
                                           self.root / "receipt", None)

    def prepare(self):
        return startup.prepare(self.config, "installer", rootfs=True)

class StartupTests(StartupFixture, unittest.TestCase):
    def test_unknown_partial_receipt_and_authority_refuse(self):
        for kind in ("receipt", "authority"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                config = startup.Configuration(OwnedDomain(Path(folder) / "state"), Path(folder) / "receipt", None)
                target = config.receipts if kind == "receipt" else config.domain.directory
                target.mkdir(mode=0o700)
                (target / "foreign").write_text("unexplained")
                with self.assertRaises(state.JournalStateError):
                    startup.prepare(config, "installer", rootfs=True)
                self.assertFalse((config.domain.directory / "state.json").exists())

    def test_bootstrap_lock_uses_shared_deadline(self):
        before = time.monotonic()
        with startup.receipt_guard(self.config, deadline=before + 1):
            with self.assertRaises(state.JournalBusy), startup.receipt_guard(self.config, deadline=before + 0.1):
                self.fail("bootstrap lock admitted concurrent owner")
        self.assertLess(time.monotonic() - before, 1)

    def test_first_and_repeated_provision_do_not_rewrite(self):
        first = self.prepare()
        self.assertEqual(first.generation, "legacy")
        self.assertTrue(first.ready)
        files = [self.config.domain.directory / "state.json", self.config.receipts / "receipt.json"]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
        self.assertEqual(self.prepare(), first)
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in files])

    def test_concurrent_first_prepare_has_one_identity(self):
        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        processes = [ctx.Process(target=prepare_worker, args=(str(self.config.domain.directory),
                     str(self.config.receipts), queue)) for _ in range(2)]
        for process in processes:
            process.start()
        identities = [queue.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(identities, [self.prepare().identity] * 2)

    def test_receipt_blocks_state_loss_and_corruption(self):
        self.prepare()
        state.clear(self.config.domain, lambda: [], timeout=1)
        original = state.read_atomic(self.config.domain)
        path = self.config.domain.directory / "state.json"
        raw = path.read_bytes()
        for damage in ("missing", "corrupt"):
            if damage == "missing":
                path.unlink()
            else:
                path.write_text("broken", encoding="utf-8")
            with self.assertRaises(state.JournalStateError):
                self.prepare()
            path.write_bytes(raw)
            path.chmod(0o600)
        self.assertEqual(state.read_atomic(self.config.domain), original)
        shutil.rmtree(self.config.domain.directory)
        with self.assertRaises(state.JournalStateError):
            self.prepare()
        self.assertFalse(self.config.domain.directory.exists())

    def test_existing_random_generation_is_adopted(self):
        state.bootstrap(self.config.domain, recovery=True, ready=True)
        before = state.read_atomic(self.config.domain)
        self.assertEqual(self.prepare(), before)
        receipt = json.loads((self.config.receipts / "receipt.json").read_text())
        self.assertEqual(receipt["identity"], before.identity)

    def test_interruptions_reuse_recorded_identity(self):
        for point in ("receipt_initializing", "authority", "receipt_initialized", "peer", "ready"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as folder:
                config = startup.Configuration(OwnedDomain(Path(folder) / "state"),
                                               Path(folder) / "receipt", None)
                def interrupt(stage):
                    if stage == point:
                        raise RuntimeError("injected power interruption")
                with patch.object(startup, "_checkpoint", interrupt):
                    with self.assertRaises(RuntimeError):
                        startup.prepare(config, "installer", rootfs=True)
                receipt = json.loads((config.receipts / "receipt.json").read_text())
                result = startup.prepare(config, "installer", rootfs=True)
                self.assertEqual(result.identity, receipt["identity"])
                self.assertEqual(result.generation, "legacy")
                self.assertTrue(result.ready)

    def test_validate_never_waits_for_bootstrap_lock(self):
        self.prepare()
        state.set_ready(self.config.domain, False)
        with startup.receipt_guard(self.config, deadline=time.monotonic() + 1):
            self.assertFalse(startup.validate(self.config).ready)

    def test_unsafe_receipt_refused_without_reset(self):
        first = self.prepare()
        receipt = self.config.receipts / "receipt.json"
        receipt.chmod(0o666)
        with self.assertRaises(state.JournalStateError):
            self.prepare()
        self.assertEqual(first, state.read_atomic(self.config.domain))

    def test_recovery_replaces_receipt_and_never_accepts_legacy(self):
        before = self.prepare()
        after = startup.recover(self.config)
        self.assertNotEqual(before.identity, after.identity)
        self.assertNotEqual(after.generation, "legacy")
        self.assertFalse(after.ready)
        self.assertEqual(startup.validate(self.config), after)
        self.assertEqual(self.prepare().generation, after.generation)

    def test_real_process_exit_at_durable_boundaries(self):
        for point in ("receipt_initializing", "authority", "receipt_initialized", "peer", "ready"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as folder:
                config = startup.Configuration(OwnedDomain(Path(folder) / "state"), Path(folder) / "receipt", None)
                def child():
                    startup._checkpoint = lambda stage: os._exit(23) if stage == point else None
                    startup.prepare(config, "installer", rootfs=True)
                process = multiprocessing.get_context("fork").Process(target=child)
                process.start()
                process.join(5)
                self.assertEqual(process.exitcode, 23)
                identity = json.loads((config.receipts / "receipt.json").read_text())["identity"]
                self.assertEqual(startup.prepare(config, "installer", rootfs=True).identity, identity)

    def test_recovery_interruption_refuses_until_explicit_retry(self):
        self.prepare()
        def crash(stage):
            if stage == "recovered_authority":
                raise RuntimeError("power loss")
        with patch.object(startup, "_checkpoint", crash), self.assertRaises(RuntimeError):
            startup.recover(self.config)
        with self.assertRaises(state.JournalStateError):
            self.prepare()
        self.assertFalse(state.read_atomic(self.config.domain).ready)
        startup.recover(self.config)
        self.assertTrue(self.prepare().ready)


def properties(unit, **values):
    result = dict.fromkeys(peer.PROPERTIES, "")
    result.update(Id=unit, LoadState="loaded", ActiveState="active", UnitFileState="enabled", MainPID="10")
    result.update(values)
    return result


class PeerTests(StartupFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.prepare()
        self.calls = []
        self.shows = []
        self.process = ("10", b"100")
        self.env = {}
        self.units = {unit: properties(unit, Environment=" ".join(k + "=" + v for k, v in self.config.settings().items()))
                      for unit in peer.UNITS.values()}
        def show(unit, deadline):
            self.shows.append(unit)
            peer._remaining(deadline)
            return self.units[unit], 100
        self.addCleanup(patch.stopall)
        patch.object(peer, "show", show).start()
        patch.object(peer, "process_identity", lambda pid: (self.process, self.env)).start()
        patch.object(peer, "command", self.dispatch).start()

    def dispatch(self, argv, deadline):
        self.calls.append(argv)
        self.assertFalse(state.read_atomic(self.config.domain).ready)
        # A separate process must acquire the actual journal guard while parent
        # owns bootstrap.lock; validate neither restarts nor takes that mutex.
        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        def validate_child():
            try:
                queue.put(startup.validate(self.config, deadline=time.monotonic() + 1).ready)
            except Exception as exc:
                queue.put(str(exc))
        child = ctx.Process(target=validate_child)
        child.start()
        self.assertIs(queue.get(timeout=2), False)
        child.join(2)
        self.assertEqual(child.exitcode, 0)
        self.process = ("20", b"200")
        self.env = self.config.settings()
        return ""

    def run_peer(self):
        return startup.prepare(self.config, "devices-api")

    def test_old_peer_restarts_once_and_current_peer_is_untouched(self):
        self.assertTrue(self.run_peer().ready)
        self.assertTrue(self.run_peer().ready)
        self.assertEqual(self.calls, [["--no-block", "try-restart", peer.PEER]])

    def test_concurrent_prepare_bounces_active_old_peer_once(self):
        ctx = multiprocessing.get_context("fork")
        count, queue = ctx.Value("i", 0), ctx.Queue()
        def identity(pid):
            return (("20", b"200"), self.config.settings()) if count.value else (("10", b"100"), {})
        def dispatch(argv, deadline):
            self.assertEqual(argv, ["--no-block", "try-restart", peer.PEER])
            with count.get_lock():
                count.value += 1
            return ""
        def prepare_child():
            try:
                queue.put(startup.prepare(self.config, "devices-api").identity)
            except Exception as exc:
                queue.put(type(exc).__name__)
        with patch.object(peer, "process_identity", identity), patch.object(peer, "command", dispatch):
            children = [ctx.Process(target=prepare_child) for _ in range(2)]
            for child in children:
                child.start()
            actual = [queue.get(timeout=5) for _ in children]
            for child in children:
                child.join(5)
                self.assertEqual(child.exitcode, 0)
        self.assertEqual(actual, [state.read_atomic(self.config.domain).identity] * 2)
        self.assertEqual(count.value, 1)

    def test_inactive_masked_absent_are_never_started(self):
        for override in ({"ActiveState": "inactive"}, {"LoadState": "masked"},
                         {"LoadState": "not-found"}, {"ActiveState": "failed"}):
            with self.subTest(override=override):
                self.units[peer.PEER].update(override)
                self.assertTrue(self.run_peer().ready)
                self.assertEqual(self.calls, [])

    def test_direct_and_transitive_relations_refuse_before_dispatch(self):
        for relation in peer.RELATIONS:
            for transitive in (False, True):
                with self.subTest(relation=relation, transitive=transitive):
                    self.units[peer.PEER][relation] = "middle.service" if transitive else peer.UNITS["devices-api"]
                    self.units["middle.service"] = properties("middle.service", **{relation: peer.UNITS["devices-api"]})
                    with self.assertRaises(state.JournalStateError):
                        self.run_peer()
                    self.assertFalse(state.read_atomic(self.config.domain).ready)
                    self.assertEqual(self.calls, [])
                    self.units[peer.PEER][relation] = ""

    def test_unrelated_manager_units_are_not_scanned(self):
        self.units.update({f"unrelated-{n}.service": properties(f"unrelated-{n}.service") for n in range(1000)})
        self.run_peer()
        self.assertEqual(set(self.shows), set(peer.UNITS.values()))

    def test_common_ordering_ancestor_is_safe(self):
        self.units[peer.PEER]["After"] = "network.target"
        self.units[peer.UNITS["devices-api"]]["After"] = "network.target"
        self.units["network.target"] = properties("network.target", Before=peer.PEER + " " + peer.UNITS["devices-api"])
        self.assertTrue(self.run_peer().ready)
        self.assertEqual(len(self.calls), 1)

    def test_restart_propagation_then_dependency_refuses(self):
        self.units[peer.PEER]["ConsistsOf"] = "middle.service"
        self.units["middle.service"] = properties("middle.service", Requires=peer.UNITS["devices-api"])
        with self.assertRaises(state.JournalStateError):
            self.run_peer()
        self.assertEqual(self.calls, [])

    def test_incomplete_or_oversized_graph_refuses(self):
        self.units[peer.PEER]["After"] = "missing.service"
        self.units["missing.service"] = properties("missing.service", LoadState="not-found")
        with self.assertRaises(state.JournalStateError):
            self.run_peer()
        self.assertEqual(self.calls, [])
        self.units[peer.PEER]["After"] = "node-0.service"
        for number in range(257):
            self.units[f"node-{number}.service"] = properties(f"node-{number}.service", After=f"node-{number + 1}.service")
        with self.assertRaises(state.JournalStateError):
            self.run_peer()
        self.assertEqual(self.calls, [])

    def test_wrong_replacement_environment_fails_closed(self):
        original = self.dispatch
        def wrong(argv, deadline):
            original(argv, deadline)
            self.env = {}
        with patch.object(peer, "command", wrong), self.assertRaises(state.JournalStateError):
            self.run_peer()
        self.assertFalse(state.read_atomic(self.config.domain).ready)

    def test_effective_path_mismatch_names_only_setting(self):
        self.units[peer.PEER]["Environment"] = "STAND_DEVICES_EVENTS_STATE_DIR=/other"
        with self.assertRaisesRegex(state.JournalStateError, "STAND_DEVICES_EVENTS_STATE_DIR"):
            self.run_peer()
        self.assertFalse(state.read_atomic(self.config.domain).ready)
        self.assertEqual(self.calls, [])

    def test_failed_dispatch_and_expired_probe_leave_unavailable(self):
        for name in ("command", "show"):
            with patch.object(peer, name, side_effect=state.JournalStateError("deadline expired")):
                with self.assertRaises(state.JournalStateError):
                    self.run_peer()
            self.assertFalse(state.read_atomic(self.config.domain).ready)


class BoundedCommandTests(unittest.TestCase):
    def test_real_child_timeout_failure_and_output_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            command = Path(folder) / "systemctl"
            for body in ("import time; time.sleep(10)", "raise SystemExit(7)", "print('x' * 70000)"):
                with self.subTest(body=body):
                    command.write_text("#!/usr/bin/python3\n" + body + "\n")
                    command.chmod(0o755)
                    before = time.monotonic()
                    with patch.dict(os.environ, {"PATH": folder}), self.assertRaises(state.JournalStateError):
                        peer.command(["show"], before + 0.2)
                    self.assertLess(time.monotonic() - before, 2)


if __name__ == "__main__":
    unittest.main()
