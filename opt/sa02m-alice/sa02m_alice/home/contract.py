"""Transport-neutral validation; no hardware, credentials or framework imports."""
from __future__ import annotations

import hashlib
import json
import re
import uuid

API_VERSION = "1.0"
MAX_REQUEST = 65536
MAX_RESPONSE = 2 * 1024 * 1024
READ_PATHS = frozenset(("/meta", "/capabilities", "/snapshot", "/history", "/events",
                        "/rooms", "/groups", "/devices", "/scenarios"))
OPERATIONS = frozenset(("device.control", "device.rename", "device.upsert", "device.delete",
                        "room.upsert", "room.delete", "group.upsert", "group.delete",
                        "scenario.upsert", "scenario.delete", "scenario.run", "scenario.enable"))
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class APIError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def encoded(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise APIError(400, "invalid_json") from exc


def uuid_string(value):
    if not isinstance(value, str):
        raise APIError(400, "invalid_uuid")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError as exc:
        raise APIError(400, "invalid_uuid") from exc
    return value


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise APIError(400, "invalid_identifier")
    return value


def validate_request(method, path, query=None, body=None):
    if method not in ("GET", "POST") or not isinstance(path, str):
        raise APIError(404, "not_found")
    if method == "GET":
        if path not in READ_PATHS:
            if not path.startswith("/commands/"):
                raise APIError(404, "not_found")
            uuid_string(path[len("/commands/"):])
    elif path not in ("/lease", "/commands"):
        raise APIError(404, "not_found")
    if query is not None and (not isinstance(query, dict) or len(query) > 16 or any(
        not isinstance(k, str) or len(k) > 64 or not isinstance(v, list) or len(v) != 1
        or not isinstance(v[0], str) or len(v[0]) > 512 for k, v in query.items()
    )):
        raise APIError(400, "invalid_query")
    if body is not None and not isinstance(body, dict):
        raise APIError(400, "invalid_body")
    if len(encoded({"query": query, "body": body})) > MAX_REQUEST:
        raise APIError(413, "request_too_large")
    if method == "GET" and body:
        raise APIError(400, "unexpected_body")


def validate_command(body):
    if not isinstance(body, dict) or set(body) - {
        "command_id", "lease_id", "operation", "target", "capability", "value",
        "expected_revision", "data"
    }:
        raise APIError(400, "invalid_command")
    uuid_string(body.get("command_id"))
    uuid_string(body.get("lease_id"))
    op = body.get("operation")
    if not isinstance(op, str) or op not in OPERATIONS:
        raise APIError(422, "unsupported_operation")
    target = body.get("target", {})
    if not isinstance(target, dict) or len(target) > 1:
        raise APIError(400, "invalid_target")
    kind = op.split(".")[0] + "_id"
    if target and set(target) != {kind}:
        raise APIError(400, "invalid_target")
    if target:
        identifier(target[kind])
    if op == "device.control":
        if not target or set(body) & {"data", "expected_revision"}:
            raise APIError(400, "invalid_control")
        cap = body.get("capability")
        if not isinstance(cap, dict) or set(cap) != {"type", "instance"}:
            raise APIError(400, "invalid_capability")
        if cap["type"] not in ("devices.capabilities.on_off", "devices.capabilities.range",
                               "devices.capabilities.mode", "devices.capabilities.color_setting",
                               "devices.capabilities.toggle"):
            raise APIError(422, "unsupported_capability")
        identifier(cap["instance"])
        value = body.get("value")
        if not isinstance(value, (str, bool, int, float)) or isinstance(value, str) and len(value) > 128:
            raise APIError(400, "invalid_value")
    else:
        if set(body) & {"capability", "value"}:
            raise APIError(400, "invalid_config")
        if not re.fullmatch(r"[0-9a-f]{64}", str(body.get("expected_revision", ""))):
            raise APIError(400, "revision_required")
        if not isinstance(body.get("data", {}), dict):
            raise APIError(400, "invalid_data")
        if not op.endswith(".upsert") and not target:
            raise APIError(400, "target_required")
    encoded(body)
    return body


def command_hash(body):
    return hashlib.sha256(encoded({k: v for k, v in body.items()
                                  if k not in ("command_id", "lease_id")})).hexdigest()
