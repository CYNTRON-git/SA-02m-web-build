"""Behavioral tests using the real registry and dispatcher; no production I/O."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "sa02m-devices"))
sys.path.insert(0, str(ROOT.parent / "sa02m-rules"))
from sa02m_alice.client.device_registry import DeviceRegistry
from sa02m_alice.common import constants as C
from sa02m_alice.home import bridge
from sa02m_alice.home.contract import APIError, MAX_RESPONSE, command_hash, encoded, validate_command, validate_request
from sa02m_alice.home.dispatcher import Dispatcher
from sa02m_alice.home.ledger import Ledger
from sa02m_alice.home.publisher import publish_once, _read_exact

TOPIC = "/devices/mr02m-COM3-10/controls/do_1"
DOC = {"rooms": [], "devices": [{"id": "lamp", "name": "Light", "type": "devices.types.light",
       "capabilities": [{"type": "devices.capabilities.on_off", "parameters": {"instance": "on"}, "mqtt": TOPIC}],
       "properties": []}]}


class Adapter:
    def __init__(self):
        self.applied = []

    def features(self):
        return {"snapshot": 1, "commands": 1, "receipts": 1, "scenarios": 0}

    def revision(self):
        return "a" * 64

    def scenarios(self):
        return None

    def configure(self, body):
        if body["expected_revision"] != self.revision():
            raise APIError(409, "revision_conflict")
        self.applied.append(body)
        return {"confirmation": "document_saved", "catalog_revision": "b" * 64}

    def prepare_scenario(self, body):
        if body["expected_revision"] != self.revision():
            raise APIError(409, "revision_conflict")
        return body["target"]["scenario_id"]


class HomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.registry = DeviceRegistry(copy.deepcopy(DOC), profile=C.PROFILE_CLOUD, clock=lambda: self.now)
        self.ledger = Ledger(Path(self.tmp.name) / "receipts.db", clock=lambda: self.now)
        self.sent = []
        self.adapter = Adapter()
        self.api = Dispatcher("SA-test", self.registry, lambda *args: self.sent.append(args), self.ledger,
                              self.adapter, clock=lambda: self.now, monotonic=lambda: self.now)
        self.api.note_mqtt(TOPIC, "0")

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def command(self, **changes):
        lease = self.api.request("POST", "/lease", body={})[1]["lease"]["lease_id"]
        body = {"command_id": str(uuid.uuid4()), "lease_id": lease, "operation": "device.control",
                "target": {"device_id": "lamp"}, "capability": {"type": "devices.capabilities.on_off", "instance": "on"}, "value": True}
        body.update(changes)
        return body

    def send(self, body):
        return self.api.request("POST", "/commands", body=body)

    def test_send_not_observation_or_optimistic_cache(self):
        body = self.command()
        status, result = self.send(body)
        self.assertEqual(status, 202)
        self.assertEqual(result["receipt"]["status"], "published")
        self.assertEqual(self.registry.get_cached(TOPIC), "0")
        self.assertFalse(result["receipt"]["physical_confirmation"])
        self.api.note_mqtt(TOPIC, "1")
        self.assertEqual(self.api.request("GET", "/commands/" + body["command_id"])[1]["receipt"]["status"], "observed")

    def test_retained_or_unrelated_topic_never_confirms(self):
        body = self.command()
        self.send(body)
        self.api.note_mqtt(TOPIC, "1", retained=True)
        self.api.note_mqtt("/devices/other/controls/do_1", "1")
        self.assertEqual(self.ledger.get(body["command_id"])["status"], "published")

    def test_identical_duplicate_even_after_lease_expiry_never_republishes(self):
        body = self.command()
        self.send(body)
        self.now += 90
        status, _ = self.send(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.sent), 1)

    def test_changed_duplicate_is_conflict(self):
        body = self.command()
        self.send(body)
        self.assertEqual(self.send({**body, "value": False})[0], 409)
        self.assertEqual(len(self.sent), 1)

    def test_expired_lease_never_creates_receipt(self):
        body = self.command()
        self.now += 61
        self.assertEqual(self.send(body)[0], 409)
        self.assertIsNone(self.ledger.get(body["command_id"]))
        self.assertFalse(self.sent)

    def test_concurrent_duplicate_only_publishes_once(self):
        body = self.command()
        threads = [threading.Thread(target=self.send, args=(body,)) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.sent), 1)

    def test_publish_exception_unknown_and_no_retry(self):
        body = self.command()
        self.api.publisher = lambda *_: (_ for _ in ()).throw(TimeoutError())
        self.assertEqual(self.send(body)[1]["receipt"]["status"], "outcome_unknown")
        self.api.publisher = lambda *args: self.sent.append(args)
        self.send(body)
        self.assertFalse(self.sent)

    def test_observation_timeout_unknown_not_failed_physics(self):
        body = self.command()
        self.send(body)
        self.now += 16
        status, result = self.api.request("GET", "/commands/" + body["command_id"])
        self.assertEqual(result["receipt"]["status"], "outcome_unknown")
        self.assertEqual(len(self.sent), 1)

    def test_restart_preserves_dedupe_and_invalidates_old_leases(self):
        body = self.command()
        self.send(body)
        self.ledger.close()
        self.ledger = Ledger(Path(self.tmp.name) / "receipts.db", clock=lambda: self.now)
        self.api.ledger = self.ledger
        self.api.leases.clear()
        self.assertEqual(self.send(body)[1]["receipt"]["status"], "outcome_unknown")
        self.assertEqual(len(self.sent), 1)

    def test_capacity_refuses_without_evicting_existing(self):
        self.ledger.max_rows = 1
        first = self.command()
        self.send(first)
        self.assertEqual(self.send(self.command())[0], 429)
        self.assertEqual(self.send(first)[0], 200)
        self.assertEqual(len(self.sent), 1)

    def test_unknown_and_readonly_device_rejected_before_publish(self):
        self.assertEqual(self.send(self.command(target={"device_id": "missing"}))[1]["receipt"]["status"], "rejected")
        doc = copy.deepcopy(DOC)
        doc["devices"][0]["capabilities"][0]["writable"] = False
        self.registry.reload(doc)
        self.assertEqual(self.send(self.command())[1]["receipt"]["status"], "rejected")
        self.assertFalse(self.sent)

    def test_invalid_values_and_routes_do_not_publish(self):
        self.assertEqual(self.send(self.command(value=float("nan")))[0], 400)
        self.assertEqual(self.send(self.command(operation="exec"))[0], 422)
        self.assertEqual(self.send(self.command(operation={}))[0], 422)
        self.assertEqual(self.send(self.command(operation=[]))[0], 422)
        self.assertEqual(self.api.request("POST", "/../exec", body={})[0], 404)
        self.assertEqual(self.api.request("GET", "/snapshot", {"x": ["1", "2"]})[0], 400)
        self.assertFalse(self.sent)

    def test_configuration_deduped_and_revision_conflict_has_no_write(self):
        body = self.command(operation="room.upsert", target={}, expected_revision="a" * 64, data={"name": "Lab"})
        body.pop("capability")
        body.pop("value")
        self.assertEqual(self.send(body)[1]["receipt"]["status"], "applied")
        self.send(body)
        self.assertEqual(len(self.adapter.applied), 1)
        body = {**body, "command_id": str(uuid.uuid4()), "expected_revision": "c" * 64}
        self.assertEqual(self.send(body)[1]["receipt"]["error"], "revision_conflict")
        self.assertEqual(len(self.adapter.applied), 1)

    def test_legacy_receipts_fit_cloud_without_changing_the_stored_outcome(self):
        for status in ("applied", "outcome_unknown"):
            with self.subTest(status=status):
                cid = str(uuid.uuid4())
                command = {"command_id": cid, "lease_id": str(uuid.uuid4()), "operation": "room.upsert",
                           "expected_revision": "a" * 64, "data": {"name": "Lab"}}
                old = {"command_id": cid, "operation": command["operation"], "target": {}, "status": status,
                       "created_at": 20, "updated_at": 21, "physical_confirmation": False,
                       "confirmation": "document_saved" if status == "applied" else "none",
                       "catalog_revision": "b" * 64, "result": {"ok": status == "applied",
                       "room": {"id": "room7", "name": "Lab"}, "devices": ["Ж" * 180000]}}
                raw = encoded(old).decode()
                self.assertLess(len(raw.encode()), MAX_RESPONSE)
                self.assertGreater(len(json.dumps(old, ensure_ascii=True)), bridge.CLOUD_PACKET_BYTES)
                with self.ledger.db:
                    self.ledger.db.execute("INSERT INTO receipts VALUES (?,?,?,?)", (cid, command_hash(command), 20, raw))
                self.ledger.close()
                self.ledger = Ledger(Path(self.tmp.name) / "receipts.db", clock=lambda: self.now)
                self.api.ledger = self.ledger
                expected = {**old, "result": {"ok": status == "applied", "room": {"id": "room7"}, "details_omitted": True}}
                code, body = self.api.request("GET", "/commands/" + cid)
                self.assertEqual(code, 200)
                self.assertLess(len(encoded(body)), 1024)
                self.assertEqual(body["receipt"], expected)
                self.assertEqual(self.send(command), (200, body))
                self.assertFalse(self.adapter.applied)
                stored = self.ledger.db.execute("SELECT receipt FROM receipts WHERE id=?", (cid,)).fetchone()[0]
                self.assertEqual(stored, raw)

    def test_receipt_detail_budget_includes_exact_utf8_boundary(self):
        cid = str(uuid.uuid4())
        self.ledger.create(cid, "test-digest", "group.delete", {"group_id": "used"})
        result = {"ok": True, "groups": [{"id": "other", "name": "Ж"}]}
        result["groups"][0]["name"] += "x" * (65536 - len(encoded(result)))
        full = self.ledger.update(cid, "applied", result=result)
        self.assertEqual(full["result"], result)
        result["groups"][0]["name"] += "x"
        short = self.ledger.update(cid, "applied", result=result)
        self.assertEqual(short["result"], {"ok": True, "details_omitted": True})

    def test_command_response_limit_after_dispatch_marks_outcome_unknown(self):
        command = self.command()
        with patch("sa02m_alice.home.dispatcher.MAX_RESPONSE", 4):
            status, failure = self.send(command)
            self.assertEqual(status, 413)
            self.assertIs(failure.get("outcome_unknown"), True)
            self.assertNotIn("outcome_unknown", self.api.request("GET", "/meta")[1])
        code, recovered = self.api.request("GET", "/commands/" + command["command_id"])
        self.assertEqual(code, 200)
        self.assertEqual(recovered["receipt"]["status"], "published")
        self.assertEqual(len(self.sent), 1)

    def test_snapshot_honest_quality_and_unsupported_scenarios(self):
        device = self.api.request("GET", "/snapshot")[1]["devices"][0]
        self.assertEqual(device["capabilities"][0]["value"], False)
        self.assertEqual(device["capabilities"][0]["quality"], "live")
        self.now += 121
        self.assertEqual(self.api.request("GET", "/snapshot")[1]["devices"][0]["capabilities"][0]["quality"], "unknown")
        self.assertEqual(self.api.request("GET", "/scenarios")[0], 422)

    def test_cached_value_is_not_permission_but_live_bus_heartbeat_is(self):
        self.api.observations.clear()
        self.api.note_mqtt(TOPIC, "0", retained=True)
        cap = self.api.snapshot()["devices"][0]["capabilities"][0]
        self.assertEqual(cap["quality"], "cached")
        self.assertFalse(cap["can_control"])
        self.assertEqual(self.send(self.command())[1]["receipt"]["error"], "no_live_bus_evidence")
        self.api.note_mqtt("/devices/mr02m-COM3-10/controls/uptime_s", "200")
        cap = self.api.snapshot()["devices"][0]["capabilities"][0]
        self.assertEqual(cap["quality"], "cached")
        self.assertTrue(cap["can_control"])
        self.assertEqual(self.send(self.command())[1]["receipt"]["status"], "published")

    def test_broker_down_disallows_control_with_live_cached_values(self):
        self.api.broker_available = lambda: False
        self.assertEqual(self.send(self.command())[1]["receipt"]["error"], "broker_offline")
        self.assertFalse(self.sent)

    def test_failure_barrier_prevents_publish_when_ledger_write_fails(self):
        body = self.command()
        with patch.object(self.ledger, "update", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.send(body)
        self.assertFalse(self.sent)

    def test_optimistic_cache_mutation_is_detected(self):
        original = self.registry.apply_actions_tracked
        def optimistic(devices, **_kwargs):
            return original(devices, update_cache=True)
        with patch.object(self.registry, "apply_actions_tracked", optimistic):
            self.send(self.command())
            with self.assertRaises(AssertionError):
                self.assertEqual(self.registry.get_cached(TOPIC), "0")

    def test_scenario_durable_claim_once_without_late_run_flag(self):
        self.enable_rules_runtime()
        from sa02m_rules.home_command import execute
        body = self.command(operation="scenario.run", target={"scenario_id": "scene1"}, expected_revision="a" * 64)
        body.pop("capability")
        body.pop("value")
        self.send(body)
        commands = []
        class Engine:
            def run_now(self, sid):
                commands.append(sid)
                return {"ok": True}
        def call(action, data):
            try:
                return self.api.rule_request(action, data)[1]
            except APIError:
                return None
        raw = json.dumps({"command_id": body["command_id"]})
        def verified(claim, engine, reload, monotonic):
            self.assertEqual(claim["expected_revision"], body["expected_revision"])
            return engine.run_now(claim["scenario_id"])
        execute(raw, True, Engine(), lambda: None, call=call, monotonic=lambda: self.now, verified_run=verified)
        self.assertFalse(commands)
        execute(raw, False, Engine(), lambda: None, call=call, monotonic=lambda: self.now, verified_run=verified)
        execute(raw, False, Engine(), lambda: None, call=call, monotonic=lambda: self.now, verified_run=verified)
        self.assertEqual(commands, ["scene1"])
        self.assertEqual(self.ledger.get(body["command_id"])["confirmation"], "engine_started")
        self.assertFalse(self.ledger.get(body["command_id"])["physical_confirmation"])
        self.assertFalse(list(Path(self.tmp.name).glob("*.run")))

    def test_scenario_expired_claim_never_executes(self):
        self.enable_rules_runtime()
        body = self.command(operation="scenario.run", target={"scenario_id": "scene1"}, expected_revision="a" * 64)
        body.pop("capability")
        body.pop("value")
        self.send(body)
        self.now += 6
        with self.assertRaises(APIError):
            self.api.rule_request("claim", {"command_id": body["command_id"]})
        result = self.api.request("GET", "/commands/" + body["command_id"])[1]["receipt"]
        self.assertEqual(result["status"], "outcome_unknown")

    def enable_rules_runtime(self):
        self.adapter.features = lambda: {"scenarios": 1}
        self.api.note_mqtt("/devices/sa02m-rules/controls/home_api_v1", "1")

    def test_rules_runtime_requires_fresh_nonretained_proof(self):
        marker = "/devices/sa02m-rules/controls/home_api_v1"
        self.adapter.features = lambda: {"scenarios": 1}
        self.assertEqual(self.api.meta()["features"]["scenario_run"], 0)
        self.api.note_mqtt(marker, "1", retained=True)
        self.assertEqual(self.api.meta()["features"]["scenario_run"], 0)
        self.api.note_mqtt(marker, "1")
        self.assertEqual(self.api.meta()["features"]["scenario_run"], 1)
        self.now += 31
        self.assertEqual(self.api.meta()["features"]["scenario_run"], 0)

    def test_old_rules_runtime_never_receives_scenario_command(self):
        body = self.command(operation="scenario.run", target={"scenario_id": "scene1"}, expected_revision="a" * 64)
        body.pop("capability")
        body.pop("value")
        self.assertEqual(self.send(body)[1]["receipt"]["error"], "rules_runtime_upgrade_required")
        self.assertFalse(self.sent)

    def test_wall_clock_jumps_do_not_evict_live_lease_receipts(self):
        mono = [1000.0]
        self.ledger.monotonic = self.api.monotonic = lambda: mono[0]
        first = self.command(operation="room.upsert", target={}, expected_revision="a" * 64, data={"name": "A"})
        first.pop("capability")
        first.pop("value")
        self.assertEqual(self.send(first)[0], 202)
        for jump in (90000, -180000, 900000):
            self.now += jump
            second = {**first, "command_id": str(uuid.uuid4()), "data": {"name": "B"}}
            self.assertEqual(self.send(second)[0], 202)
            self.assertEqual(self.send(first)[0], 200)
        self.assertEqual(sum(row["command_id"] == first["command_id"] for row in self.adapter.applied), 1)

    def test_monotonic_cleanup_and_restart_retain_receipts_conservatively(self):
        mono = [1000.0]
        self.ledger.monotonic = lambda: mono[0]
        first = self.command()
        self.send(first)
        self.now += 90000
        self.ledger.close()
        self.ledger = Ledger(Path(self.tmp.name) / "receipts.db", clock=lambda: self.now,
                             monotonic=lambda: mono[0], max_rows=1)
        self.assertIsNotNone(self.ledger.get(first["command_id"]))
        with self.assertRaises(APIError):
            self.ledger.create(str(uuid.uuid4()), "hash", "room.upsert", {})
        mono[0] += 86401
        self.ledger.create(str(uuid.uuid4()), "hash", "room.upsert", {})
        self.assertIsNone(self.ledger.get(first["command_id"]))

    def test_wire_examples_pass_canonical_validation(self):
        wire = json.loads((ROOT.parents[1] / "docs/contracts/home-api-v1-wire.json").read_text(encoding="utf-8"))
        for name in ("command_example", "config_example"):
            validate_command(wire[name])


class PublisherTest(unittest.TestCase):
    def broker(self, acknowledge):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        seen = []
        def read_packet(conn):
            head = _read_exact(conn, 1)[0]
            size, mul = 0, 1
            while True:
                byte = _read_exact(conn, 1)[0]
                size += (byte & 127) * mul
                mul *= 128
                if byte < 128:
                    break
            return head, _read_exact(conn, size)
        def serve():
            with listener:
                conn, _ = listener.accept()
                with conn:
                    read_packet(conn)
                    conn.sendall(b"\x20\x02\x00\x00")
                    seen.append(read_packet(conn))
                    if acknowledge:
                        conn.sendall(b"\x40\x02\x00\x01")
                    else:
                        time.sleep(.2)
        thread = threading.Thread(target=serve)
        thread.start()
        return port, seen, thread

    def test_real_socket_qos1_nonretained_puback(self):
        port, seen, thread = self.broker(True)
        publish_once(TOPIC + "/on", "1", port=port, timeout=.2)
        thread.join()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], 0x32)

    def test_missing_puback_no_second_connection_or_replay(self):
        port, seen, thread = self.broker(False)
        with self.assertRaises((TimeoutError, OSError)):
            publish_once(TOPIC + "/on", "1", port=port, timeout=.05)
        thread.join()
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
