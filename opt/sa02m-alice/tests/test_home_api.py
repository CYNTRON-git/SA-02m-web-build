"""Behavioral tests using the real registry and dispatcher; no production I/O."""
import copy
from contextlib import redirect_stderr
import hashlib
import io
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
from sa02m_alice.home.contract import APIError, command_hash, validate_command, validate_request
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

    def test_rebound_capability_cannot_confirm_an_original_command(self):
        body = self.command()
        self.send(body)
        changed = copy.deepcopy(DOC)
        replacement = '/devices/replacement/controls/state'
        changed['devices'][0]['capabilities'][0]['mqtt'] = replacement
        self.registry.reload(changed)
        self.api.note_mqtt(replacement, '1')
        self.api.note_mqtt(TOPIC, '0')
        row = self.ledger.get(body['command_id'])
        self.assertEqual(row['status'], 'outcome_unknown')
        self.assertEqual(row['error'], 'binding_changed')
        self.assertFalse(row['physical_confirmation'])
        self.assertEqual(self.sent, [(TOPIC+'/on', '1')])
        self.assertEqual(self.send(body)[1]['receipt']['status'], 'outcome_unknown')
        self.assertEqual(len(self.sent), 1, 'unknown after rebind must never replay the command')

    def test_rename_keeps_original_binding_and_rebind_after_observation_keeps_receipt(self):
        body = self.command()
        self.send(body)
        changed = copy.deepcopy(DOC)
        changed['devices'][0]['name'] = 'Renamed light'
        self.registry.reload(changed)
        self.api.note_mqtt(TOPIC, '1')
        self.assertEqual(self.ledger.get(body['command_id'])['status'], 'observed')
        changed['devices'][0]['capabilities'][0]['mqtt'] = '/devices/replacement/controls/state'
        self.registry.reload(changed)
        self.api.note_mqtt(TOPIC, '0')
        self.assertEqual(self.ledger.get(body['command_id'])['status'], 'observed')

    def test_rebind_before_dispatch_pins_new_binding_and_removed_mapping_is_unknown(self):
        changed = copy.deepcopy(DOC)
        replacement = '/devices/replacement/controls/state'
        changed['devices'][0]['capabilities'][0]['mqtt'] = replacement
        self.registry.reload(changed)
        self.api.note_mqtt(replacement, '0')
        body = self.command()
        self.assertEqual(self.send(body)[1]['receipt']['status'], 'published')
        self.assertEqual(self.sent, [(replacement+'/on', '1')])
        self.api.note_mqtt(TOPIC, '1')
        self.assertEqual(self.ledger.get(body['command_id'])['status'], 'published')
        self.api.note_mqtt(replacement, '1', retained=True)
        self.assertEqual(self.ledger.get(body['command_id'])['status'], 'published')
        changed['devices'][0]['capabilities'] = []
        self.registry.reload(changed)
        self.api.note_mqtt(replacement, '1')
        self.assertEqual(self.ledger.get(body['command_id'])['status'], 'outcome_unknown')

    def test_same_topic_with_changed_normalization_cannot_confirm(self):
        body = self.command()
        self.send(body)
        changed = copy.deepcopy(DOC)
        changed['devices'][0]['capabilities'][0]['inverted'] = True
        self.registry.reload(changed)
        self.api.note_mqtt(TOPIC, '0')
        self.assertEqual(self.ledger.get(body['command_id'])['status'], 'outcome_unknown')
        self.assertEqual(self.ledger.get(body['command_id'])['error'], 'binding_changed')

    def test_compound_light_receipt_pins_only_its_target_capability(self):
        changed = copy.deepcopy(DOC)
        color = {'type':'devices.capabilities.color_setting', 'parameters':{'instance':'rgb','color_model':'rgb'},
                 'mqtt':'/devices/color-fixture/controls/color'}
        changed['devices'][0]['capabilities'].append(color)
        self.registry.reload(changed)
        self.api.note_mqtt(color['mqtt'], '#000000')
        body = self.command(capability={'type':color['type'],'instance':'rgb'}, value=0x112233)
        self.assertEqual(self.send(body)[1]['receipt']['status'], 'published')
        self.assertEqual(self.sent[-1][0], color['mqtt']+'/on')
        # Rebinding the other capability is not a change to this RGB command.
        changed['devices'][0]['capabilities'][0]['mqtt'] = '/devices/other-lamp/controls/state'
        self.registry.reload(changed)
        self.api.note_mqtt(color['mqtt'], '#112233')
        self.assertEqual(self.ledger.get(body['command_id'])['status'], 'observed')

    def test_snapshot_availability_scales_linearly_at_catalog_limit(self):
        class Counted(dict):
            visits = 0
            def items(self):
                for entry in super().items():
                    self.visits += 1
                    yield entry
        devices = []
        for index in range(512):
            caps = [{'type':'devices.capabilities.range',
                     'parameters':{'instance':instance, 'range':{'min':0,'max':100,'precision':1}},
                     'mqtt':f'/devices/generic-{index}/controls/{instance}'}
                    for instance in ('brightness','temperature','humidity','open')]
            devices.append({'id':f'light-{index}','name':f'Light {index}',
                            'type':'devices.types.light','capabilities':caps,'properties':[]})
        self.registry.reload({'devices':devices})
        self.api.observations.clear()
        for device in devices:
            for cap in device['capabilities']:
                self.api.note_mqtt(cap['mqtt'], '25')
        counted = self.api.observations = Counted(self.api.observations)
        status, result = self.api.request('GET','/snapshot')
        self.assertEqual(status, 200)
        caps = [cap for device in result['devices'] for cap in device['capabilities']]
        self.assertEqual(len(caps), 2048)
        self.assertTrue(all(cap['can_control'] and cap['value'] == 25 for cap in caps))
        self.assertEqual(counted.visits, 2048, 'availability must inspect each observation once, not once per capability')
        self.now += C.STATUS_STALE_S + 1
        later = self.api.request('GET','/snapshot')[1]
        self.assertTrue(all(not cap['can_control'] for d in later['devices'] for cap in d['capabilities']))
        self.assertEqual(counted.visits, 4096, 'the derived index must be rebuilt per request, never retained across requests')

    def test_snapshot_availability_keeps_latest_valid_same_producer_evidence(self):
        self.api.observations.clear()
        self.api.note_mqtt(TOPIC, '0', retained=True)
        self.now += 1
        self.api.note_mqtt('/devices/mr02m-COM3-10/controls/uptime_s','100')
        expected = self.now
        self.now += 1
        self.api.note_mqtt(TOPIC+'/on','1')
        self.api.note_mqtt('/devices/mr02m-COM3-10/controls/other/meta/error','r')
        self.api.note_mqtt('/devices/mr02m-COM3-100/controls/uptime_s','999')
        self.api.note_mqtt('/devices/mr02m-COM3-10/controls/retained','1',retained=True)
        cap = self.api.request('GET','/snapshot')[1]['devices'][0]['capabilities'][0]
        self.assertEqual(cap['availability'], {'can_control':True,'reason':'recent_producer_observation','evidence_at':expected})
        self.assertEqual(cap['quality'], 'cached')
        self.now += C.STATUS_STALE_S + 1
        self.assertEqual(self.api.availability(DOC['devices'][0]['capabilities'][0]),
                         {'can_control':False,'reason':'no_live_bus_evidence'})

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

    def test_consumed_dispatch_errors_log_safely_and_remain_deduplicated(self):
        secret = "PRIVATE_SENTINEL\npassword=secret\x1b /private/path"
        cases = (("device.control", "device_publish", "publish_unconfirmed"),
                 ("scenario.run", "scenario_dispatch", "engine_request_unconfirmed"),
                 ("room.upsert", "configuration", "configuration_interrupted"))
        for operation, logged_operation, wire_error in cases:
            with self.subTest(operation=operation):
                calls = []
                def fail(*args):
                    calls.append(args)
                    raise RuntimeError(secret)
                if operation == "device.control":
                    body = self.command()
                    self.api.publisher = fail
                else:
                    target = {"scenario_id": "scene1"} if operation == "scenario.run" else {}
                    body = self.command(operation=operation, target=target,
                                        expected_revision="a" * 64, data={"name": "PRIVATE_SENTINEL"})
                    body.pop("capability")
                    body.pop("value")
                    if operation == "scenario.run":
                        self.enable_rules_runtime()
                        self.api.publisher = fail
                    else:
                        self.adapter.configure = fail
                stderr = io.StringIO()
                with redirect_stderr(stderr), self.assertLogs("sa02m-home.dispatcher") as logs:
                    status, result = self.send(body)
                receipt = result["receipt"]
                self.assertEqual(status, 202)
                self.assertEqual(receipt["status"], "outcome_unknown")
                self.assertEqual(receipt["error"], wire_error)
                self.assertEqual(len(calls), 1)
                self.assertNotIn(body["command_id"], self.api.pending)
                self.assertNotIn(body["command_id"], self.api.rule_pending)
                self.assertEqual(self.ledger.get(body["command_id"]), receipt)
                self.assertEqual(len(logs.records), 1)
                record = logs.records[0]
                self.assertEqual(record.getMessage(), "home operation=" + logged_operation + " error_type=RuntimeError")
                self.assertIsNone(record.exc_info)
                self.assertIsNone(record.stack_info)
                output = "\n".join(logs.output) + stderr.getvalue()
                for forbidden in ("PRIVATE_SENTINEL", "password", "/private", "Traceback", "SA-test", TOPIC, body["command_id"]):
                    self.assertNotIn(forbidden, output)
                with self.assertNoLogs("sa02m-home.dispatcher"):
                    self.assertEqual(self.send(body), (200, self.api.envelope(receipt=receipt)))
                self.assertEqual(len(calls), 1)

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
