"""The three /run files the web card reads (docs/contracts/home-connect.md §6).

* `status.json` (0644) — state, counts, budget; NEVER a token, never the
  device-flow codes, never an appliance name (counts only).
* `link.json` (0640, group www-data) — the user code and verification URIs;
  exists ONLY while the state is `awaiting_user`. Never the device code.
* `inventory.json` (0640, group www-data) — appliance list with names, types
  and the controls published (card list; Phase 3 picker source).

Single writer: the daemon's main thread.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from . import __version__
from . import constants as C
from .fsutil import atomic_write, group_gid, remove_quietly

# The status.json key set — compared with the contract by the test suite.
STATUS_KEYS = (
    "state", "ts", "version", "reason", "message", "enabled", "linked", "host",
    "client_id_source", "appliances", "appliances_connected", "stream",
    "budget_day", "budget_used", "budget_limit", "budget_local", "budget_remaining",
    "rate_limited_until", "last_event_ts",
)
LINK_KEYS = ("user_code", "verification_uri", "verification_uri_complete", "expires_at", "ts")
INVENTORY_ITEM_KEYS = ("device_id", "name", "type", "brand", "connected", "controls")
STREAM_STATES = ("up", "down", "off")


class StatusWriter:
    def __init__(self, run_dir: Optional[str] = None) -> None:
        base = run_dir or C.RUN_DIR
        self.status_path = os.path.join(base, os.path.basename(C.STATUS_FILE))
        self.link_path = os.path.join(base, os.path.basename(C.LINK_FILE))
        self.inventory_path = os.path.join(base, os.path.basename(C.INVENTORY_FILE))
        self._gid = group_gid()
        self.last: Dict[str, Any] = {}

    def _ensure_dir(self) -> None:
        os.makedirs(os.path.dirname(self.status_path) or ".", exist_ok=True)

    def write_status(
        self,
        state: str,
        *,
        reason: str = "",
        message: str = "",
        enabled: bool = False,
        linked: bool = False,
        host: str = C.DEFAULT_HOST,
        client_id_source: str = "",
        appliances: int = 0,
        appliances_connected: int = 0,
        stream: str = "off",
        budget: Optional[Dict[str, Any]] = None,
        rate_limited_until: int = 0,
        last_event_ts: int = 0,
    ) -> Dict[str, Any]:
        if state not in C.STATES or state == C.STATE_NOT_INSTALLED:
            raise ValueError("daemon cannot write state %r" % state)
        if reason and reason not in C.REASONS:
            raise ValueError("unknown reason %r" % reason)
        if stream not in STREAM_STATES:
            raise ValueError("unknown stream state %r" % stream)
        b = budget or {}
        payload: Dict[str, Any] = {
            "state": state,
            "ts": int(time.time()),
            "version": __version__,
            "reason": reason,
            "message": message[:200],
            "enabled": bool(enabled),
            "linked": bool(linked),
            "host": host if host in C.HOSTS else C.DEFAULT_HOST,
            "client_id_source": client_id_source if client_id_source in ("own", "vendor") else "",
            "appliances": int(appliances),
            "appliances_connected": int(appliances_connected),
            "stream": stream,
            "budget_day": str(b.get("day", "")),
            "budget_used": int(b.get("used", 0)),
            "budget_limit": int(b.get("limit", C.DAILY_LIMIT)),
            "budget_local": int(b.get("local", C.LOCAL_BUDGET)),
            "budget_remaining": int(b.get("remaining", C.DAILY_LIMIT)),
            "rate_limited_until": int(rate_limited_until),
            "last_event_ts": int(last_event_ts),
        }
        self._ensure_dir()
        atomic_write(self.status_path, json.dumps(payload, ensure_ascii=False) + "\n",
                     mode=0o644, durable=False)
        self.last = payload
        if state != C.STATE_AWAITING_USER:
            self.clear_link()
        return payload

    def write_link(self, view: Dict[str, Any]) -> None:
        """Only the caller knows the state is awaiting_user; it calls this then."""
        payload = {k: view.get(k) for k in LINK_KEYS if k != "ts"}
        payload["ts"] = int(time.time())
        self._ensure_dir()
        atomic_write(self.link_path, json.dumps(payload) + "\n", mode=0o640,
                     gid=self._gid, durable=False)

    def clear_link(self) -> None:
        remove_quietly(self.link_path)

    def write_inventory(self, appliances: List[Dict[str, Any]]) -> None:
        items = [{k: a.get(k) for k in INVENTORY_ITEM_KEYS} for a in appliances]
        payload = {"ts": int(time.time()), "appliances": items}
        self._ensure_dir()
        atomic_write(self.inventory_path, json.dumps(payload, ensure_ascii=False) + "\n",
                     mode=0o640, gid=self._gid, durable=False)

    def clear_inventory(self) -> None:
        remove_quietly(self.inventory_path)
