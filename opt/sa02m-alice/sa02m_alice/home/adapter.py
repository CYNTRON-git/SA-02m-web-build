"""Reuse the board's validated registry, document stores and archive readers."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

from ..common.config_store import devices_lock, load_devices
from ..config import api as config
from .contract import APIError, encoded


class BoardAdapter:
    def __init__(self, registry):
        self.registry = registry

    def scenarios(self):
        return config.listed_scenarios()

    def catalog(self, resource):
        return load_devices().get(resource, [])

    @contextmanager
    def consistent_read(self):
        store = config._rules_store()
        with devices_lock(), (store.command_lock() if store is not None else nullcontext()):
            self.registry.reload()
            yield

    def scenario(self, sid):
        if self.scenarios() is None:
            raise APIError(422, "scenarios_unavailable")
        data = config.apply_scenarios({"get": True, "id": sid})
        if not data.get("ok"):
            raise APIError(404, "scenario_not_found")
        data.pop("ok", None)
        return data

    def cloud_linked(self):
        try:
            with Path("/run/sa02m-alice/status-cloud.json").open() as stream:
                raw = stream.read(65537)
            return len(raw) <= 65536 and json.loads(raw).get("state") == "connected"
        except (OSError, ValueError, AttributeError):
            return False

    def revision(self):
        doc = load_devices()
        store = config._rules_store()
        rules = store.load() if store is not None else {}
        # Runtime journals do not invalidate an editor's catalogue revision.
        stable_rules = {k: v for k, v in rules.items() if k not in ("runs", "notify_queue")}
        for row in stable_rules.get("scenarios", []):
            for key in ("last_run", "last_error"):
                row.pop(key, None)
        return hashlib.sha256(encoded({"catalog": doc, "rules": stable_rules})).hexdigest()

    def features(self):
        try:
            from sa02m_devices import api as archive
            history = archive is not None
        except ImportError:
            history = False
        return {"snapshot": 1, "commands": 1, "receipts": 1, "catalog": 1,
                "scenarios": 1 if self.scenarios() is not None else 0,
                "history": 1 if history else 0, "events": 1 if history else 0,
                "scenario_code_edit": 0, "account": 0, "claim": 0,
                "push_notifications": 0, "event_stream": 0}

    def configure(self, command):
        op = command["operation"]
        target, data = command.get("target", {}), dict(command.get("data", {}))
        kind = op.split(".")[0]
        tid = target.get(kind + "_id")
        if tid:
            if data.get("id", tid) != tid:
                raise APIError(400, "target_mismatch")
            data["id"] = tid
        store = config._rules_store()
        with devices_lock(), (store.command_lock() if store is not None else nullcontext()):
            if command["expected_revision"] != self.revision():
                raise APIError(409, "revision_conflict")
            if op == "device.rename":
                out = config.rename_device(tid, data.get("name"))
            elif op == "device.upsert":
                if tid:
                    previous = next((d for d in load_devices().get("devices", []) if d.get("id") == tid), None)
                    if previous is None:
                        raise APIError(404, "device_not_found")
                    data = {**previous, **data}
                out = config.upsert_device(data)
            elif op == "device.delete":
                out = config.delete_device(tid)
            elif kind in ("room", "group"):
                if op.endswith(".delete"):
                    data = {"id": tid, "delete": True}
                elif set(data) & {"delete", "reset", "action"}:
                    raise APIError(400, "invalid_data")
                out = (config.apply_rooms if kind == "room" else config.apply_groups)(data)
            elif kind == "scenario":
                if self.scenarios() is None:
                    raise APIError(422, "scenarios_unavailable")
                if op == "scenario.delete":
                    data = {"id": tid, "delete": True}
                elif op == "scenario.run":
                    raise APIError(422, "use_runtime_claim_protocol")
                elif op == "scenario.enable":
                    if type(data.get("enabled")) is not bool:
                        raise APIError(400, "invalid_enabled")
                    data = {"id": tid, "enabled": data["enabled"]}
                elif set(data) & {"run_now", "delete", "replace", "upsert", "ack_notify", "library", "get"}:
                    raise APIError(400, "invalid_data")
                elif data.get("type") == "code" or data.get("code") not in (None, ""):
                    raise APIError(422, "code_edit_unsupported")
                out = store.apply_command(data, preserve_unknown=True)
            else:
                raise APIError(422, "unsupported_operation")
            if not out.get("ok"):
                raise APIError(422, str(out.get("error") or "configuration_refused")[:128])
            self.registry.reload()
            return {"catalog_revision": self.revision(), "result": out,
                    "confirmation": "engine_request" if op == "scenario.run" else "document_saved"}

    def prepare_scenario(self, command):
        store = config._rules_store()
        if store is None:
            raise APIError(422, "scenarios_unavailable")
        with devices_lock(), store.command_lock():
            if command["expected_revision"] != self.revision():
                raise APIError(409, "revision_conflict")
            sid = command["target"]["scenario_id"]
            row = next((s for s in store.load().get("scenarios", []) if s.get("id") == sid), None)
            if row is None or not row.get("enabled", True):
                raise APIError(422, "scenario_unavailable")
            if row.get("type") == "logic":
                raise APIError(422, "logic_runs_automatically")
            return sid

    def archive(self, resource, query):
        try:
            from sa02m_devices import api as archive
        except ImportError as exc:
            raise APIError(422, "history_unavailable") from exc
        allowed = ({"kind", "metric", "group", "range", "window_s", "device_id", "channel"}
                   if resource == "history" else {"device_id", "limit", "t0", "t1", "kinds"})
        if set(query) - allowed:
            raise APIError(400, "unsupported_query")
        query = dict(query)
        if resource == "events":
            try:
                limit = int(query.get("limit", ["100"])[0])
                if not 1 <= limit <= 500:
                    raise ValueError()
                t0 = float(query.get("t0", [str(time.time() - 86400)])[0])
                t1 = float(query.get("t1", [str(time.time())])[0])
                if not 0 <= t1 - t0 <= 30 * 86400:
                    raise ValueError()
            except ValueError as exc:
                raise APIError(400, "invalid_range") from exc
            query.update(limit=[str(limit)], t0=[str(t0)], t1=[str(t1)])
            body, status = archive.handle_events(query)
        else:
            if query.get("range", ["1h"])[0] not in ("1h", "6h", "24h", "7d", "30d"):
                raise APIError(400, "invalid_range")
            if "window_s" in query:
                try:
                    window = int(query["window_s"][0])
                    if not 60 <= window <= 30 * 86400:
                        raise ValueError()
                except ValueError as exc:
                    raise APIError(400, "invalid_range") from exc
            body, status = archive.handle_history(query)
        if not isinstance(body, dict):
            raise APIError(503, "archive_unavailable")
        body.update(source="board_archive", coverage="available_records_only")
        return status, body
