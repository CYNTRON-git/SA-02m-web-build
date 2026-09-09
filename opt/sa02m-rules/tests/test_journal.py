"""WP-A16 (1.0.6.41): run records leave the scenarios document.

A scenario run costs no synchronous whole-store write. `runs`, `notify_queue`
and per-scenario `last_run`/`last_error` live in the sibling journal
(`runs.json`, contract §Store), buffered in the engine and flushed on a
bounded cadence (`RUNS_FLUSH_S` / `RUNS_FLUSH_MAX`), on `run_now`, and at
shutdown; the document is written only on a scenario/library/vars change, so
the service's mtime watch never sees a run. The cloud-facing shapes
(`listed()` / `_ok()`) stay identical — the read side merges the journal.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import engine, store  # noqa: E402
from sa02m_rules import service as rules_service  # noqa: E402


def _block(sid="s1", action=None, trigger=None):
    return {"id": sid, "name": sid, "enabled": True, "type": "block",
            "trigger": trigger if trigger is not None else [
                {"kind": "state", "device": "lamp", "cap": "on_off",
                 "op": "changed"}],
            "condition": {},
            "action": action if action is not None else [
                {"kind": "set", "device": "led", "cap": "on_off", "value": 1}]}


def _engine(td, scenarios, now=1000.0, pubs=None):
    path = os.path.join(td, "scenarios.json")
    store.save({"scenarios": scenarios, "library": "", "vars": {}}, path)
    clock = [now]
    e = engine.Engine(lambda d, c, v: (pubs if pubs is not None else []).append((d, c, v)),
                      path, now=lambda: clock[0], pub_state=lambda *_a: None)
    return e, clock, path


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


class _Writes:
    """Records every atomic write by target path (the document AND the
    journal share `_atomic_write`, so counting there sees both)."""

    def __init__(self):
        self.paths = []
        self._orig = store._atomic_write

    def __enter__(self):
        self._patch = mock.patch.object(store, "_atomic_write", self._spy)
        self._patch.start()
        return self

    def __exit__(self, *_a):
        self._patch.stop()

    def _spy(self, path, data):
        self.paths.append(path)
        return self._orig(path, data)

    def to(self, path):
        return sum(1 for p in self.paths if p == path)


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


class DocumentWriteTests(unittest.TestCase):
    """Step 1: a run never rewrites the document."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

    def test_runs_never_write_the_document(self):
        e, _clock, path = _engine(self.td.name, [_block(action=[
            {"kind": "set", "device": "led", "cap": "on_off", "value": 1},
            {"kind": "notify", "text": "hit"}])])
        before = _bytes(path)
        with _Writes() as w:
            for i in range(5):
                e.on_state("lamp", "on_off", i % 2)
        self.assertEqual(len(e.doc["runs"]), 5)
        self.assertEqual(e.doc["notify_queue"][-1]["text"], "hit")
        self.assertEqual(w.to(path), 0, w.paths)
        self.assertEqual(_bytes(path), before)

    def test_content_change_writes_the_document_without_run_state(self):
        e, _clock, path = _engine(self.td.name, [_block()])
        e.on_state("lamp", "on_off", 1)
        with _Writes() as w:
            e.set_home_mode("away")  # vars are document content — one write
        self.assertEqual(w.to(path), 1, w.paths)
        raw = _read(path)
        self.assertEqual(raw["vars"]["home_mode"], "away")
        self.assertNotIn("runs", raw)
        self.assertNotIn("notify_queue", raw)
        self.assertNotIn("last_run", raw["scenarios"][0])
        self.assertNotIn("last_error", raw["scenarios"][0])

    def test_code_and_logic_notify_are_buffered(self):
        e, clock, path = _engine(self.td.name, [{
            "id": "c1", "name": "c", "enabled": True, "type": "code",
            "trigger": [{"kind": "state", "device": "lamp", "cap": "on_off",
                         "op": "changed"}],
            "condition": {}, "action": [], "code": "Notify.text('from code')"}])
        journal = store.journal_path(path)
        with _Writes() as w:
            e.on_state("lamp", "on_off", 1)
            engine.LogicRuntime(e, "c1").notify("from logic")
        self.assertEqual([n["text"] for n in e.doc["notify_queue"]],
                         ["from code", "from logic"])
        self.assertEqual(w.paths, [])  # neither the document nor the journal
        clock[0] += store.RUNS_FLUSH_S
        e.tick()
        self.assertEqual([n["text"] for n in _read(journal)["notify_queue"]],
                         ["from code", "from logic"])


class ServiceWatchTests(unittest.TestCase):
    """Step 3: the mtime watch never sees a run."""

    def test_runs_leave_document_mtime_and_reload_alone(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "scenarios.json")
        store.save({"scenarios": [_block()], "library": "", "vars": {}}, path)
        app = rules_service.RulesApp(FakeClient(), path)
        mtime = os.path.getmtime(path)
        with mock.patch.object(app.engine, "adopt", wraps=app.engine.adopt) as adopt:
            for i in range(5):
                app._apply_message("/devices/lamp/controls/on_off", str(i % 2))
                app.tick()
        self.assertEqual(len(app.engine.doc["runs"]), 5)
        self.assertEqual(os.path.getmtime(path), mtime)
        self.assertEqual(app._last_mtime, mtime)
        self.assertEqual(adopt.call_count, 0)


class CadenceTests(unittest.TestCase):
    """Step 2: buffered, flushed on cadence / count / run_now."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

    def test_flush_after_runs_flush_s(self):
        e, clock, path = _engine(self.td.name, [_block()])
        journal = store.journal_path(path)
        e.on_state("lamp", "on_off", 1)
        e.tick()
        self.assertFalse(os.path.exists(journal))
        clock[0] += store.RUNS_FLUSH_S - 1
        e.tick()
        self.assertFalse(os.path.exists(journal))
        clock[0] += 1
        e.tick()
        j = _read(journal)
        self.assertEqual([r["id"] for r in j["runs"]], ["s1"])
        self.assertEqual(j["last"]["s1"], {"last_run": 1000.0, "last_error": ""})
        # Nothing pending → no rewrite on the next ticks.
        with _Writes() as w:
            clock[0] += store.RUNS_FLUSH_S
            e.tick()
        self.assertEqual(w.paths, [])

    def test_flush_at_runs_flush_max_records(self):
        e, _clock, path = _engine(self.td.name, [_block()])
        journal = store.journal_path(path)
        for i in range(store.RUNS_FLUSH_MAX - 1):
            e.on_state("lamp", "on_off", i % 2)
        self.assertFalse(os.path.exists(journal))
        e.on_state("lamp", "on_off", 1)
        self.assertEqual(len(_read(journal)["runs"]), store.RUNS_FLUSH_MAX)

    def test_run_now_flushes_immediately(self):
        e, _clock, path = _engine(self.td.name, [_block()])
        journal = store.journal_path(path)
        e.run_now("s1")
        self.assertEqual([r["source"] for r in _read(journal)["runs"]], ["external"])

    def test_journal_write_failure_keeps_the_engine_alive(self):
        e, _clock, path = _engine(self.td.name, [_block()])
        journal = store.journal_path(path)
        orig = store._atomic_write

        def refuse(p, data):
            if p == journal:
                raise OSError(28, "No space left on device")
            return orig(p, data)

        with mock.patch.object(store, "_atomic_write", refuse):
            rec = e.run_now("s1")
        self.assertTrue(rec["ok"])
        self.assertFalse(os.path.exists(journal))
        self.assertEqual(len(e.journal.pending_runs), 1)
        e.flush_runs()
        self.assertEqual(len(_read(journal)["runs"]), 1)
        self.assertEqual(e.journal.pending_runs, [])


class RetentionTests(unittest.TestCase):
    """Step 4: the journal keeps RUNS_MAX records, oldest dropped."""

    def test_journal_retention_cap(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        e, clock, path = _engine(td.name, [_block()])
        for i in range(store.RUNS_MAX + 10):
            clock[0] = 1000.0 + i
            e.run_now("s1")
        on_disk = _read(store.journal_path(path))["runs"]
        self.assertEqual(len(on_disk), store.RUNS_MAX)
        self.assertEqual(on_disk[0]["ts"], 1010.0)
        self.assertEqual(len(e.doc["runs"]), store.RUNS_MAX)
        self.assertEqual(e.doc["runs"][0]["ts"], 1010.0)


class ReadSideTests(unittest.TestCase):
    """The cloud channel (apply_command in sa02m-alice-client's process)
    sees runs / last_run / last_error in the same shapes as before."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")

    def test_listed_and_ok_shapes_unchanged_and_merge_the_journal(self):
        r = store.apply_command({"name": "t", "trigger": [], "action": [
            {"kind": "set", "device": "led", "cap": "on_off", "value": 1}]},
            self.path)
        self.assertEqual(set(r), {"ok", "scenarios", "runs", "notify_queue",
                                  "library", "scenario"})
        self.assertEqual(set(r["scenarios"][0]),
                         {"id", "name", "enabled", "type", "order", "last_run",
                          "last_error", "summary"})
        self.assertIsNone(r["scenarios"][0]["last_run"])
        # The daemon runs it and flushes; a fresh reader merges the journal.
        e = engine.Engine(lambda *_a: None, self.path, now=lambda: 4242.0,
                          pub_state=lambda *_a: None)
        e.run_now("s1")
        r = store.apply_command({"id": "s1", "get": True}, self.path)
        self.assertEqual(r["scenarios"][0]["last_run"], 4242.0)
        self.assertEqual(r["scenarios"][0]["last_error"], "")
        self.assertEqual(r["scenario"]["last_run"], 4242.0)
        self.assertEqual([x["id"] for x in r["runs"]], ["s1"])
        # An upsert keeps last_run through the response (prev row merge).
        r = store.apply_command({"id": "s1", "name": "renamed", "trigger": [],
                                 "action": []}, self.path)
        self.assertEqual(r["scenario"]["last_run"], 4242.0)
        self.assertEqual(store.listed(store.load(self.path))[0]["last_run"], 4242.0)

    def test_corrupt_or_missing_journal_reads_as_empty(self):
        store.apply_command({"name": "t", "trigger": [], "action": []}, self.path)
        journal = store.journal_path(self.path)
        self.assertEqual(store.load(self.path)["runs"], [])  # missing
        with open(journal, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        doc = store.load(self.path)
        self.assertEqual(doc["runs"], [])
        self.assertEqual(doc["notify_queue"], [])
        self.assertEqual(doc["scenarios"][0]["id"], "s1")
        with open(journal, "w", encoding="utf-8") as fh:
            fh.write('["a list, not an object"]')
        self.assertEqual(store.load(self.path)["runs"], [])
        # The daemon still boots and runs; its flush repairs the file.
        e = engine.Engine(lambda *_a: None, self.path, pub_state=lambda *_a: None)
        self.assertTrue(e.run_now("s1")["ok"])
        self.assertEqual(len(_read(journal)["runs"]), 1)

    def test_ack_notify_drain_is_not_resurrected_by_the_daemon(self):
        e, _clock, path = _engine(self.td.name, [_block(action=[
            {"kind": "notify", "text": "one"}])])
        journal = store.journal_path(path)
        e.run_now("s1")
        self.assertEqual(len(_read(journal)["notify_queue"]), 1)
        r = store.apply_command({"ack_notify": True}, path)
        self.assertEqual(r["notify_queue"], [])
        self.assertEqual(_read(journal)["notify_queue"], [])
        e.doc["scenarios"][0]["action"][0]["text"] = "two"
        e.run_now("s1")
        self.assertEqual([n["text"] for n in _read(journal)["notify_queue"]],
                         ["two"])


class MigrationTests(unittest.TestCase):
    """Step 6: a pre-1.0.6.41 document carries runs inside — moved once."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        self.journal = store.journal_path(self.path)
        self.legacy = {
            "scenarios": [dict(_block(), last_run=5.0, last_error="x")],
            "library": "", "vars": {},
            "runs": [{"ts": 4.0, "id": "s1", "name": "s1", "error": "", "ok": True,
                      "source": "scenario"},
                     {"ts": 5.0, "id": "s1", "name": "s1", "error": "x", "ok": False,
                      "source": "scenario"}],
            "notify_queue": [{"ts": 5.0, "text": "old"}],
        }

    def _write_legacy(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.legacy, fh)

    def test_legacy_runs_are_visible_to_a_reader_before_migration(self):
        self._write_legacy()
        doc = store.load(self.path)
        self.assertEqual([r["ts"] for r in doc["runs"]], [4.0, 5.0])
        self.assertEqual(doc["notify_queue"][0]["text"], "old")
        self.assertEqual(store.listed(doc)[0]["last_run"], 5.0)
        self.assertEqual(store.listed(doc)[0]["last_error"], "x")
        self.assertFalse(os.path.exists(self.journal))  # a reader never writes

    def test_engine_moves_legacy_runs_to_the_journal_once(self):
        self._write_legacy()
        e = engine.Engine(lambda *_a: None, self.path, now=lambda: 1000.0,
                          pub_state=lambda *_a: None)
        j = _read(self.journal)
        self.assertEqual([r["ts"] for r in j["runs"]], [4.0, 5.0])
        self.assertEqual(j["notify_queue"][0]["text"], "old")
        self.assertEqual(j["last"]["s1"], {"last_run": 5.0, "last_error": "x"})
        raw = _read(self.path)
        self.assertNotIn("runs", raw)
        self.assertNotIn("notify_queue", raw)
        self.assertNotIn("last_run", raw["scenarios"][0])
        self.assertNotIn("last_error", raw["scenarios"][0])
        self.assertEqual(raw["scenarios"][0]["trigger"], _block()["trigger"])
        # The engine's own view and a fresh reader both see the moved state.
        self.assertEqual([r["ts"] for r in e.doc["runs"]], [4.0, 5.0])
        self.assertEqual(e.doc["scenarios"][0]["last_run"], 5.0)
        self.assertEqual(store.listed(store.load(self.path))[0]["last_error"], "x")
        # Once: a second boot rewrites nothing.
        with _Writes() as w:
            engine.Engine(lambda *_a: None, self.path, now=lambda: 1000.0,
                          pub_state=lambda *_a: None)
        self.assertEqual(w.paths, [])

    def test_journal_wins_over_leftover_document_fields(self):
        """Crash between the two migration writes: the journal was written,
        the document still carries the legacy fields — the journal is truth."""
        self._write_legacy()
        with open(self.journal, "w", encoding="utf-8") as fh:
            json.dump({"runs": [{"ts": 9.0, "id": "s1", "name": "s1", "error": "",
                                 "ok": True, "source": "scenario"}],
                       "notify_queue": [], "last": {"s1": {"last_run": 9.0,
                                                           "last_error": ""}}}, fh)
        self.assertEqual([r["ts"] for r in store.load(self.path)["runs"]], [9.0])
        self.assertEqual(store.listed(store.load(self.path))[0]["last_run"], 9.0)
        e = engine.Engine(lambda *_a: None, self.path, now=lambda: 1000.0,
                          pub_state=lambda *_a: None)
        self.assertEqual([r["ts"] for r in e.doc["runs"]], [9.0])
        self.assertEqual([r["ts"] for r in _read(self.journal)["runs"]], [9.0])
        self.assertNotIn("runs", _read(self.path))


class ShutdownTests(unittest.TestCase):
    """Pending records survive a stop: RulesApp.stop() and main()'s SIGTERM."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        self.journal = store.journal_path(self.path)

    def test_stop_flushes_pending(self):
        store.save({"scenarios": [_block()], "library": "", "vars": {}}, self.path)
        app = rules_service.RulesApp(FakeClient(), self.path)
        app._apply_message("/devices/lamp/controls/on_off", "1")
        self.assertFalse(os.path.exists(self.journal))
        app.stop()
        self.assertEqual([r["id"] for r in _read(self.journal)["runs"]], ["s1"])

    def test_main_flushes_on_sigterm(self):
        store.save({"scenarios": [_block(trigger=[{"kind": "boot"}])],
                    "library": "", "vars": {}}, self.path)
        client = types.SimpleNamespace(
            connect=lambda *_a, **_k: None, loop_start=lambda: None,
            loop_stop=lambda: None, subscribe=lambda *_a, **_k: None,
            publish=lambda *_a, **_k: None)
        fake_mqtt = types.ModuleType("paho.mqtt.client")
        fake_mqtt.Client = lambda *_a, **_k: client
        fake_paho = types.ModuleType("paho")
        fake_paho.mqtt = types.ModuleType("paho.mqtt")
        fake_paho.mqtt.client = fake_mqtt
        saved = {k: sys.modules.get(k) for k in ("paho", "paho.mqtt", "paho.mqtt.client")}
        sys.modules.update({"paho": fake_paho, "paho.mqtt": fake_paho.mqtt,
                            "paho.mqtt.client": fake_mqtt})
        prev_term = signal.getsignal(signal.SIGTERM)

        def sleep_then_term(_s):
            handler = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(handler), handler)  # main() installed one
            handler(signal.SIGTERM, None)

        try:
            with mock.patch.object(rules_service, "PATH", self.path), \
                    mock.patch.object(rules_service.time, "sleep", sleep_then_term):
                self.assertEqual(rules_service.main(), 0)
        finally:
            signal.signal(signal.SIGTERM, prev_term)
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.assertEqual([r["source"] for r in _read(self.journal)["runs"]],
                         ["scenario"])


if __name__ == "__main__":
    unittest.main()
