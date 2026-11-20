"""Claim a short-lived home command before starting the existing engine once."""
import http.client
import json
import socket
import time
import uuid


def _verified_run(claim, engine, reload, monotonic):
    # Imported only for a valid Home API request: legacy rules installations
    # without the optional home package retain their original behavior.
    from sa02m_alice.home.adapter import BoardAdapter
    from sa02m_alice.common.config_store import devices_lock
    from sa02m_alice.config import api as config
    store = config._rules_store()
    if store is None:
        return None
    with devices_lock(), store.command_lock():
        if (claim.get("deadline", 0) <= monotonic()
                or BoardAdapter(None).revision() != claim.get("expected_revision")):
            return None
        reload()
        if claim.get("deadline", 0) <= monotonic():
            return None
        return engine.run_now(claim["scenario_id"])


class _Connection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=2)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/run/sa02m-home/api.sock")


def _call(action, body):
    conn = _Connection()
    try:
        conn.request("POST", "/internal/home/v1/rules/" + action, json.dumps(body), {"Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read(8193)
        if response.status != 200 or len(raw) > 8192:
            return None
        return json.loads(raw)
    finally:
        conn.close()


def execute(raw, retained, engine, reload, *, call=_call, monotonic=time.monotonic,
            verified_run=_verified_run):
    if retained or len(raw) > 256:
        return
    try:
        body = json.loads(raw)
        cid = body.get("command_id")
        if set(body) != {"command_id"} or str(uuid.UUID(cid)) != cid:
            return
        claim = call("claim", {"command_id": cid})
        if not claim or claim.get("deadline", 0) <= monotonic():
            return
        # Nothing is written to a .run file; a dead/offline/restarted engine
        # cannot later replay this request. The durable claim refuses duplicates.
        result = verified_run(claim, engine, reload, monotonic)
        call("finish", {"command_id": cid, "ok": bool(result and result.get("ok"))})
    except (OSError, ValueError, TypeError, KeyError, ImportError, http.client.HTTPException):
        return  # The dispatcher records uncertainty, never retries the action.
