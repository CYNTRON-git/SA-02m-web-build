"""Single dispatcher; side effects are recorded durably before attempting them."""
from __future__ import annotations

import copy
from contextlib import nullcontext
import threading
import time
import uuid

from ..client.device_registry import action_instance
from ..common import constants as C
from .contract import (APIError, API_VERSION, MAX_REQUEST, MAX_RESPONSE, command_hash,
                       encoded, identifier, validate_command, validate_request)


class Dispatcher:
    def __init__(self, controller_id, registry, publisher, ledger, adapter, *,
                 clock=time.time, monotonic=time.monotonic):
        self.controller_id, self.registry = controller_id, registry
        self.publisher, self.ledger, self.adapter = publisher, ledger, adapter
        self.clock, self.monotonic = clock, monotonic
        self.boot_id = str(uuid.uuid4())
        self.leases, self.observations, self.pending, self.rule_pending = {}, {}, {}, {}
        self.lock = threading.RLock()
        self.broker_available = lambda: True

    def topics(self):
        topics = set(self.registry.subscribe_topics())
        topics.add("/devices/sa02m-rules/controls/home_api_v1")
        for _did, _dev, caps, props in self.registry.catalogue_items():
            for item in caps + props:
                parts = str(item.get("mqtt", "")).split("/")
                if len(parts) >= 5 and parts[1] == "devices":
                    topics.add("/devices/" + parts[2] + "/controls/uptime_s")
        return topics

    def features(self):
        features = self.adapter.features()
        marker = "/devices/sa02m-rules/controls/home_api_v1"
        seen = self.observations.get(marker, {})
        features["scenario_run"] = int(bool(features.get("scenarios") and self.broker_available()
            and seen and not seen["retained"] and self.monotonic() - seen["mono"] <= 30
            and self.registry.get_cached(marker) == "1"))
        return features

    def availability(self, item):
        if item.get("writable") is False:
            return {"can_control": False, "reason": "readonly"}
        if not self.broker_available():
            return {"can_control": False, "reason": "broker_offline"}
        topic = str(item.get("mqtt", ""))
        parts = topic.split("/")
        if len(parts) < 5 or parts[1] != "devices":
            return {"can_control": False, "reason": "unsupported_topic"}
        prefix = "/devices/" + parts[2] + "/"
        # Retained values can show a last-known position. Only a recent
        # non-retained poll/heartbeat from the same producer permits a write.
        recent = [o for t, o in self.observations.items() if t.startswith(prefix)
                  and not t.endswith("/meta/error") and not t.endswith("/on")
                  and not o["retained"] and self.monotonic() - o["mono"] <= C.STATUS_STALE_S]
        if not recent:
            return {"can_control": False, "reason": "no_live_bus_evidence"}
        latest = max(recent, key=lambda o: o["mono"])
        return {"can_control": True, "reason": "recent_producer_observation", "evidence_at": latest["at"]}

    def envelope(self, **fields):
        return {"ok": True, "api_version": API_VERSION, "controller_id": self.controller_id, **fields}

    def meta(self):
        return self.envelope(features=self.features(), account_scope="cloud",
                             limits={"request_bytes": MAX_REQUEST, "response_bytes": MAX_RESPONSE,
                                     "lease_seconds": 60, "receipt_seconds": self.ledger.retention,
                                     "receipt_count": self.ledger.max_rows},
                             boot_id=self.boot_id, authentication="panel_admin_or_cloud_acl")

    def request(self, method, path, query=None, body=None):
        try:
            validate_request(method, path, query, body)
            with self.lock:
                self._expire_pending()
                status, result = self._dispatch(method, path, query or {}, body or {})
            if len(encoded(result)) <= MAX_RESPONSE:
                return status, result
            error = {"ok": False, "api_version": API_VERSION, "error": "response_too_large"}
            if method == "POST" and path == "/commands":
                error["outcome_unknown"] = True
            return 413, error
        except APIError as exc:
            return exc.status, {"ok": False, "api_version": API_VERSION, "error": exc.code}

    def _dispatch(self, method, path, query, body):
        if method == "POST" and path == "/lease":
            if body:
                raise APIError(400, "unexpected_body")
            now = self.monotonic()
            self.leases = {k: v for k, v in self.leases.items() if v > now}
            if len(self.leases) >= 256:
                raise APIError(429, "lease_limit")
            lid = str(uuid.uuid4())
            self.leases[lid] = now + 60
            return 200, self.envelope(lease={"lease_id": lid, "boot_id": self.boot_id, "expires_in_s": 60})
        if method == "POST":
            return self.command(validate_command(body))
        if path == "/meta":
            return 200, self.meta()
        if path.startswith("/commands/"):
            row = self.ledger.get(path[len("/commands/"):])
            if row is None:
                raise APIError(404, "receipt_not_found")
            return 200, self.envelope(receipt=row)
        if path in ("/history", "/events"):
            status, data = self.adapter.archive(path[1:], query)
            return status, {**self.envelope(), **data}
        if path in ("/devices", "/rooms", "/groups"):
            with self.adapter.consistent_read():
                return 200, self.envelope(**{path[1:]: self.adapter.catalog(path[1:])},
                                          catalog_revision=self.adapter.revision())
        if path == "/scenarios":
            with self.adapter.consistent_read() if hasattr(self.adapter, "consistent_read") else nullcontext():
                if query:
                    if set(query) != {"id"}:
                        raise APIError(400, "unsupported_query")
                    data = self.adapter.scenario(identifier(query["id"][0]))
                    return 200, self.envelope(**data, catalog_revision=self.adapter.revision())
                data = self.adapter.scenarios()
                if data is None:
                    raise APIError(422, "scenarios_unavailable")
                return 200, self.envelope(**data, catalog_revision=self.adapter.revision())
        snapshot = self.snapshot()
        if path == "/snapshot":
            return 200, snapshot
        if path == "/capabilities":
            return 200, self.envelope(features=self.features(),
                devices=[{"id": d["id"], "capabilities": d["capabilities"]} for d in snapshot["devices"]])
        return 200, self.envelope(**{path[1:]: snapshot[path[1:]], "catalog_revision": snapshot["catalog_revision"]})

    def snapshot(self):
        with self.adapter.consistent_read() if hasattr(self.adapter, "consistent_read") else nullcontext():
            return self._snapshot_locked()

    def _snapshot_locked(self):
        queried = {d["id"]: d for d in self.registry.query_devices()}
        raw = {did: (dev, caps, props) for did, dev, caps, props in self.registry.catalogue_items()}
        devices = self.registry.discovery_devices(profile=C.PROFILE_CLOUD)
        if len(devices) > 512 or sum(len(d["capabilities"]) + len(d["properties"]) for d in devices) > 2048:
            raise APIError(413, "catalog_too_large")
        for device in devices:
            live = queried.get(device["id"], {})
            dev, caps, props = raw[device["id"]]
            device["online"] = "error_code" not in live
            if "error_code" in live:
                device["error_code"] = live["error_code"]
            for key, items in (("capabilities", caps), ("properties", props)):
                states = {(v.get("type"), action_instance(v.get("type", ""), v.get("state", {}))): v
                          for v in live.get(key, [])}
                for item in device[key]:
                    instance = action_instance(item.get("type", ""),
                                               item.get("parameters", {}))
                    item["instance"] = instance
                    state = states.get((item["type"], instance), {}).get("state")
                    source = next((v for v in items if v.get("type") == item["type"]
                                   and action_instance(v.get("type", ""), v.get("parameters", {})) == instance), {})
                    observation = self.observations.get(source.get("mqtt"), {})
                    fresh = observation and self.monotonic() - observation["mono"] <= 120
                    item.update(value=state.get("value") if state else None,
                                quality=("live" if not observation.get("retained") else "cached") if fresh and state else "unknown",
                                observed_at=observation.get("at"), source="mqtt")
                    if state:
                        item["state"] = state
                    if key == "capabilities":
                        item["writable"] = source.get("writable", True) is not False
                        item["availability"] = self.availability(source)
                        if "error_code" in live:
                            item["availability"] = {"can_control": False, "reason": "device_unreachable"}
                        item["can_control"] = item["availability"]["can_control"]
            device["room_id"] = dev.get("room_id")
        scenes = self.adapter.scenarios()
        return self.envelope(home={"id": self.controller_id, "name": "SA-02m"}, devices=devices,
            rooms=self.registry.listed_rooms(), groups=self.registry.listed_groups(),
            scenarios=(scenes or {}).get("scenarios", []), online=bool(self.broker_available()), linked=True,
            cloud_linked=self.adapter.cloud_linked() if hasattr(self.adapter, "cloud_linked") else False,
            catalog_revision=self.adapter.revision(), state_source="board_mqtt_cache",
            generated_at=self.clock(), features=self.features())

    def command(self, body):
        cid, digest = body["command_id"], command_hash(body)
        previous = self.ledger.existing(cid, digest)
        if previous is not None:
            return 200, self.envelope(receipt=previous)
        if self.leases.get(body["lease_id"], 0) <= self.monotonic():
            raise APIError(409, "lease_expired")
        row = self.ledger.create(cid, digest, body["operation"], body.get("target", {}))
        if body["operation"] == "scenario.run":
            try:
                if not self.features().get("scenario_run"):
                    raise APIError(422, "rules_runtime_upgrade_required")
                sid = self.adapter.prepare_scenario(body)
                if not self.broker_available():
                    raise APIError(503, "broker_offline")
                self.ledger.update(cid, "dispatch_started")
                self.rule_pending[cid] = {"scenario_id": sid, "expected_revision": body["expected_revision"],
                                          "deadline": self.monotonic() + 5}
                self.publisher("/devices/sa02m-rules/controls/home_command/on", encoded({"command_id": cid}).decode())
                row = self.ledger.update(cid, "published", confirmation="broker_publish")
            except APIError as exc:
                row = self.ledger.update(cid, "rejected", error=exc.code)
            except Exception:
                self.rule_pending.pop(cid, None)
                row = self.ledger.update(cid, "outcome_unknown", error="engine_request_unconfirmed")
            return 202, self.envelope(receipt=row)
        if body["operation"] != "device.control":
            self.ledger.update(cid, "dispatch_started")
            try:
                outcome = self.adapter.configure(body)
                state = "published" if body["operation"] == "scenario.run" else "applied"
                row = self.ledger.update(cid, state, **outcome)
            except APIError as exc:
                row = self.ledger.update(cid, "rejected", error=exc.code)
            except Exception:
                row = self.ledger.update(cid, "outcome_unknown", error="configuration_interrupted")
            return 202, self.envelope(receipt=row)
        cap = body["capability"]
        source = next((item for did, _dev, caps, _props in self.registry.catalogue_items()
                       if did == body["target"]["device_id"] for item in caps
                       if item.get("type") == cap["type"]
                       and action_instance(item.get("type", ""), item.get("parameters", {})) == cap["instance"]), None)
        if source is not None and not self.availability(source)["can_control"]:
            row = self.ledger.update(cid, "rejected", error=self.availability(source)["reason"])
            return 202, self.envelope(receipt=row)
        requested = [{"id": body["target"]["device_id"], "capabilities": [{
            "type": cap["type"], "state": {"instance": cap["instance"], "value": body["value"]}}]}]
        results, publications = self.registry.apply_actions_tracked(requested, update_cache=False)
        refused = [c for d in results for c in d.get("capabilities", []) if c.get("status") != C.STATUS_DONE]
        if refused or not publications:
            row = self.ledger.update(cid, "rejected", error=(refused[0].get("error_code") if refused else "invalid_action"))
            return 202, self.envelope(receipt=row)
        self.ledger.update(cid, "dispatch_started")
        # Record after the durable barrier, before publish; callback runs under
        # this same lock, so a quick observation cannot race publication state.
        self.pending[cid] = {"command": copy.deepcopy(body), "started": self.monotonic(),
                             "topics": {p[0].removesuffix("/on") for p in publications}, "seen": set()}
        try:
            for topic, payload, _key in publications:
                self.publisher(topic, payload)
            row = self.ledger.update(cid, "published", confirmation="broker_publish")
        except Exception:
            self.pending.pop(cid, None)
            row = self.ledger.update(cid, "outcome_unknown", error="publish_unconfirmed", confirmation="none")
        return 202, self.envelope(receipt=row)

    def note_mqtt(self, topic, payload, *, retained=False):
        received = self.monotonic()
        with self.lock:
            self.registry.note_mqtt(topic, payload, retained=retained)
            self.observations[topic] = {"mono": received, "at": self.clock(), "retained": retained}
            if retained:
                return
            for cid, pending in list(self.pending.items()):
                if topic not in pending["topics"] or received < pending["started"]:
                    continue
                pending["seen"].add(topic)
                body = pending["command"]
                states = self.registry.query_devices([body["target"]["device_id"]])
                matches = any(c.get("type") == body["capability"]["type"]
                              and (c.get("state") or {}).get("instance") == body["capability"]["instance"]
                              and (c.get("state") or {}).get("value") == body["value"]
                              for d in states for c in d.get("capabilities", []))
                if matches and pending["topics"] <= pending["seen"]:
                    self.ledger.update(cid, "observed", confirmation="mqtt_state", observed_at=self.clock())
                    del self.pending[cid]

    def _expire_pending(self):
        for cid, row in list(self.rule_pending.items()):
            if self.monotonic() > row["deadline"]:
                self.ledger.update(cid, "outcome_unknown", error="engine_claim_timeout")
                del self.rule_pending[cid]
        for cid, row in list(self.pending.items()):
            if self.monotonic() - row["started"] > 15:
                self.ledger.update(cid, "outcome_unknown", error="observation_timeout", confirmation="broker_publish")
                del self.pending[cid]

    def rule_request(self, action, body):
        from .contract import uuid_string
        cid = uuid_string(body.get("command_id"))
        with self.lock:
            if action == "claim":
                pending = self.rule_pending.get(cid)
                row = self.ledger.get(cid)
                if not pending or self.monotonic() > pending["deadline"] or row.get("engine_claimed"):
                    raise APIError(409, "claim_unavailable")
                self.ledger.update(cid, "dispatch_started", engine_claimed=True, confirmation="engine_claimed")
                return 200, self.envelope(scenario_id=pending["scenario_id"], deadline=pending["deadline"],
                                          expected_revision=pending["expected_revision"])
            if action == "finish":
                row = self.ledger.get(cid)
                if not row or not row.get("engine_claimed") or cid not in self.rule_pending:
                    raise APIError(409, "claim_unavailable")
                result = body.get("ok")
                if type(result) is not bool:
                    raise APIError(400, "invalid_result")
                self.ledger.update(cid, "published" if result else "partial", confirmation="engine_started",
                                   execution_tracking="scenario_journal", error=None if result else "engine_refused_or_partial")
                self.rule_pending.pop(cid, None)
                return 200, self.envelope()
            raise APIError(404, "not_found")
